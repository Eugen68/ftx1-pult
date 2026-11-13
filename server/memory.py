"""
Редактор каналов памяти FTX-1: чтение всех каналов, правка, запись обратно, файлы и группы.

Как это работает по CAT (проверено на FTX-1 с прошивкой 1.13, 13.11.2026):
  * MR00001;  -> MR 00001 FFFFFFFFF ±OOOO R T M 1 C 00 S ;  частота, клар, вид, режим тона, сдвиг; «?» — пусто.
                 Режим тона в MR: 0 выкл, 1 TSQ, 2 ENC (DCS в MR показывается как 0).
  * MW — тот же формат, P7 = 1: создаёт/переписывает канал (частота, вид, клар, сдвиг, ENC/TSQ).
    Номер тона и DCS командой MW не задаются (и MW с режимом 3 отвечает «?»).
  * Номер CTCSS/код DCS/режим DCS сохраняет команда AM («VFO-A в память»): курсор MAIN ставится на канал
    (MC0nnnnn), MAIN — в VFO (VM000), VFO-A настраивается (FA, MD0, CT0, CN00/CN01, OS0), затем AM.
    AM стирает имя — поэтому имя (MT) пишется после. VFO-A и положение MAIN после записи возвращаются.
  * MT00001ИМЯ; — имя;  MZ00001 F TTTTTTTTT; — отдельная частота передачи (F=1 вкл, T — частота TX).
  * Чтение тонов: канал включается (MC0nnnnn), спрашиваются CT0, CN00, CN01, трансивер возвращается как был.

Разнос (величина сдвига ретранслятора) в канале FTX-1 не хранится — только направление (симплекс/+/−),
а величина берётся из меню FM RPT SHIFT по диапазону. В файле редактора разнос хранится для удобства
(расчёт частоты передачи, экспорт в CSV).

Группы — свои, в файле редактора (в трансивере группы каналов по CAT не задаются).
"""

from i18n import tr
import asyncio
import csv
import io
import json
import logging
import os
import re
import time

from cat import MODES, MODE_CODES, CTCSS

log = logging.getLogger("mem")

DCS = [23, 25, 26, 31, 32, 36, 43, 47, 51, 53, 54, 65, 71, 72, 73, 74, 114, 115, 116, 122, 125, 131, 132,
       134, 143, 145, 152, 155, 156, 162, 165, 172, 174, 205, 212, 223, 225, 226, 243, 244, 245, 246, 251,
       252, 255, 261, 263, 265, 266, 271, 274, 306, 311, 315, 325, 331, 332, 343, 346, 351, 356, 364, 365,
       371, 411, 412, 413, 423, 431, 432, 445, 446, 452, 454, 455, 462, 464, 465, 466, 503, 506, 516, 523,
       526, 532, 546, 565, 606, 612, 624, 627, 631, 632, 654, 662, 664, 703, 712, 723, 731, 732, 734, 743, 754]

# Режим тона: в нашей модели как у команды CT — 0 выкл, 1 ENC (тон на передачу), 2 TSQ (тон-шумодав), 3 DCS.
# В ответе MR и в памяти — 1 TSQ, 2 ENC (наоборот).
MR_TO_CT = {0: 0, 1: 2, 2: 1, 3: 3}
FM_MODES = ('FM', 'FM-N', 'DATA-FM', 'DATA-FM-N')
CH_MIN, CH_MAX = 1, 999            # обычные каналы FTX-1: 001–999
PMS = {}


def default_offset_khz(freq):
    """Привычный разнос по диапазону (для расчёта частоты передачи в редакторе)."""
    mhz = freq / 1e6
    if 28 <= mhz < 30:
        return 100
    if 50 <= mhz < 54:
        return 600
    if 144 <= mhz < 148:
        return 600
    if 430 <= mhz < 440:
        return 7600
    return 0


def parse_mr(frame):
    """'MR00001014074000+00000002...' -> dict канала или None."""
    m = re.match(r'^MR(\d{5})(\d{9})([+-])(\d{4})(\d)(\d)(\w)(\d)(\d)(\d\d)(\d)', frame or '')
    if not m:
        return None
    ch, f, sg, off, rx, tx, md, _vm, ct, _r, sh = m.groups()
    clar = int(off) * (-1 if sg == '-' else 1)
    return {'ch': int(ch), 'freq': int(f), 'clar': clar, 'rx_clar': rx == '1', 'tx_clar': tx == '1',
            'mode': MODES.get(md, 'USB'), 'tone_mode': MR_TO_CT.get(int(ct), 0), 'shift': int(sh) if sh in '012' else 0,
            'empty': False}


def blank(ch):
    return {'ch': ch, 'empty': True, 'freq': 0, 'mode': 'FM', 'name': '', 'clar': 0, 'rx_clar': False,
            'tx_clar': False, 'tone_mode': 0, 'ctcss': 88.5, 'dcs': 23, 'shift': 0, 'offset_khz': 0,
            'split': False, 'tx_freq': 0, 'group': '', 'comment': ''}


def clean(c):
    """Привести канал из браузера к допустимым значениям."""
    out = blank(int(c.get('ch', 0)))
    out['empty'] = bool(c.get('empty'))
    try:
        out['freq'] = int(round(float(c.get('freq') or 0)))
    except (TypeError, ValueError):
        out['freq'] = 0
    out['mode'] = c.get('mode') if c.get('mode') in MODE_CODES else 'FM'
    out['name'] = re.sub(r'[^\x20-\x7e]|;', '', str(c.get('name') or ''))[:12]
    out['clar'] = max(-9999, min(9999, int(c.get('clar') or 0)))
    out['rx_clar'] = bool(c.get('rx_clar'))
    out['tx_clar'] = bool(c.get('tx_clar'))
    out['tone_mode'] = int(c.get('tone_mode') or 0) if int(c.get('tone_mode') or 0) in (0, 1, 2, 3) else 0
    try:
        t = float(c.get('ctcss') or 88.5)
        out['ctcss'] = min(CTCSS, key=lambda x: abs(x - t))
    except (TypeError, ValueError):
        pass
    d = int(c.get('dcs') or 23)
    out['dcs'] = d if d in DCS else 23
    out['shift'] = int(c.get('shift') or 0) if int(c.get('shift') or 0) in (0, 1, 2) else 0
    out['offset_khz'] = max(0, min(100000, int(float(c.get('offset_khz') or 0))))
    out['split'] = bool(c.get('split'))
    try:
        out['tx_freq'] = int(round(float(c.get('tx_freq') or 0)))
    except (TypeError, ValueError):
        out['tx_freq'] = 0
    if out['split'] and not 30_000 <= out['tx_freq'] <= 470_000_000:
        out['split'] = False
    out['group'] = str(c.get('group') or '')[:40]
    out['comment'] = str(c.get('comment') or '')[:200]
    return out


class MemoryDesk:
    def __init__(self, station, base_dir):
        self.st = station
        self.dir = os.path.join(base_dir, 'memory')
        self.task = None
        self.stop_flag = False

    @property
    def rig(self):
        return self.st.rig

    def _bc(self, obj):
        for c in self.st.clients:
            if c.authed and not c.admin:
                c.ws.send_json(obj)

    def busy_reason(self):
        st = self.st
        if not st.rig or not st.running:
            return tr('Связь с трансивером не запущена')
        if st.rig.state.get('tx') or st.ptt_owner is not None or st.ft.busy or st.digi.tx_busy or st.cw.sender.busy:
            return tr('Идёт передача — дождитесь окончания')
        return ''

    # ------------------------------------------------------------------ чтение
    async def read_channel(self, ch):
        mr = (await self.rig.exec_seq(['MR%05d' % ch]))[0]
        c = parse_mr(mr)
        if not c:
            return blank(ch)
        mt, mz = await self.rig.exec_seq(['MT%05d' % ch, 'MZ%05d' % ch])
        mm = re.match(r'^MZ\d{5}([01])(\d{9})', mz or '')
        if mm:
            c_split, c_tx = mm.group(1) == '1', int(mm.group(2))
        else:
            c_split, c_tx = False, 0
        c = dict(blank(ch), **c)
        if mt and mt.startswith('MT%05d' % ch):
            c['name'] = mt[7:19].rstrip()
        c['offset_khz'] = default_offset_khz(c['freq']) if c['shift'] else 0
        c['split'], c['tx_freq'] = c_split, c_tx if c_split else 0
        return c

    async def _q(self, cmd, prefix, tries=2):
        """Запрос с проверкой, что ответ именно на него (трансивер с AI присылает и чужие CN/CT)."""
        for _ in range(tries):
            r = (await self.rig.exec_seq(['~120', cmd]))[1] or ''
            if r.startswith(prefix):
                return r
        return ''

    async def read_tones(self, chans):
        """Режим тона, номер CTCSS и код DCS: канал на мгновение включается, потом трансивер возвращается."""
        need = [c for c in chans if not c['empty'] and c['mode'] in FM_MODES]
        if not need:
            return
        r = await self.rig.exec_seq(['VM0', 'MC0'])
        saved_vm, saved_mc = r[0] or '', r[1] or ''
        try:
            for c in need:
                if self.stop_flag:
                    break
                await self.rig.exec_seq(['MC0%05d' % c['ch'], '~200'])
                ct = await self._q('CT0', 'CT0')
                cn = await self._q('CN00', 'CN00')
                dc = await self._q('CN01', 'CN01')
                m = re.match(r'^CT0(\d)', ct)
                if m:
                    c['tone_mode'] = int(m.group(1)) if int(m.group(1)) in (0, 1, 2, 3) else 0
                m = re.match(r'^CN00(\d{3})', cn)
                if m and int(m.group(1)) < len(CTCSS):
                    c['ctcss'] = CTCSS[int(m.group(1))]
                m = re.match(r'^CN01(\d{3})', dc)
                if m and int(m.group(1)) < len(DCS):
                    c['dcs'] = DCS[int(m.group(1))]
        finally:
            await self._restore_pos(saved_vm, saved_mc)
            self.rig.refresh(0.2)

    async def _restore_pos(self, saved_vm, saved_mc):
        if saved_vm.startswith('VM011') and re.match(r'^MC0\d{5}$', saved_mc):
            await self.rig.exec_seq([saved_mc])
        else:
            await self.rig.exec_seq(['VM000'])

    async def _read_all(self, c, rng, tones):
        chans = []
        n = len(rng)
        try:
            for i, ch in enumerate(rng):
                if self.stop_flag:
                    break
                ch_data = await self.read_channel(ch)
                chans.append(ch_data)
                if i % 3 == 0 or i == n - 1:
                    c.ws.send_json({'t': 'mem_progress', 'what': 'read', 'i': i + 1, 'n': n})
            if tones and not self.stop_flag:
                c.ws.send_json({'t': 'mem_progress', 'what': 'tones', 'i': 0, 'n': 1})
                await self.read_tones(chans)
            for ch in chans:
                if ch['ch'] in PMS:
                    ch['pms'] = PMS[ch['ch']]
            c.ws.send_json({'t': 'mem_data', 'chans': chans, 'partial': self.stop_flag,
                            'msg': tr('Прочитано каналов: %d, занято: %d') % (len(chans), sum(not x['empty'] for x in chans))})
        except Exception as e:
            log.exception(tr("Память: чтение"))
            c.ws.send_json({'t': 'mem_err', 'msg': tr('Ошибка чтения: %s') % e})
        finally:
            self.task = None

    # ------------------------------------------------------------------ запись
    @staticmethod
    def _mw(c):
        """Команда MW (P7 = 1). Режим тона в кодировке памяти: 1 TSQ, 2 ENC; DCS задаётся через AM."""
        ct = {0: 0, 1: 2, 2: 1, 3: 0}[c['tone_mode']] if c['mode'] in FM_MODES else 0
        sh = c['shift'] if c['mode'] in FM_MODES else 0
        return 'MW%05d%09d%s%04d%d%d%s1%d00%d' % (c['ch'], c['freq'], '-' if c['clar'] < 0 else '+', abs(c['clar']),
                                                 int(c['rx_clar']), int(c['tx_clar']),
                                                 MODE_CODES.get(c['mode'], '4'), ct, sh)

    async def write_channel(self, c):
        ch = c['ch']
        was_empty = not parse_mr((await self.rig.exec_seq(['MR%05d' % ch]))[0])
        fm = c['mode'] in FM_MODES
        code = MODE_CODES.get(c['mode'], '4')
        # 1) канал должен существовать, чтобы на него можно было встать (пустой создаём командой MW)
        if was_empty:
            await self.rig.exec_seq([self._mw(c), '~150'])
            if not parse_mr((await self.rig.exec_seq(['MR%05d' % ch]))[0]):
                raise RuntimeError(tr('трансивер не создал канал %d — запись остановлена') % ch)
        need_am = fm and (c['tone_mode'] in (1, 2, 3))
        if need_am:
            # 2) номер тона / DCS: через VFO-A и AM (MW их не задаёт)
            snap = await self.rig.exec_seq(['VM0', 'MC0'])
            saved_vm, saved_mc = snap[0] or '', snap[1] or ''
            r = await self.rig.exec_seq(['MC0%05d' % ch, '~200', 'MC0'])
            m = re.match(r'^MC0(\d{5})$', r[2] or '')
            if not m or int(m.group(1)) != ch:
                await self._restore_pos(saved_vm, saved_mc)
                raise RuntimeError(tr('трансивер не встал на канал %d (ответ «%s») — запись остановлена') % (ch, r[2]))
            await self.rig.exec_seq(['VM000', '~150'])
            va = await self.rig.exec_seq(['FA', 'MD0', 'CT0', 'CN00', 'CN01', 'OS0'])
            try:
                cmds = ['FA%09d' % c['freq'], 'MD0%s' % code, '~100', 'OS0%d' % c['shift']]
                if c['tone_mode'] == 3:
                    cmds += ['CN01%03d' % DCS.index(c['dcs']), 'CT03']
                else:
                    cmds += ['CN00%03d' % CTCSS.index(c['ctcss']), 'CT0%d' % c['tone_mode']]
                await self.rig.exec_seq(cmds + ['~100', 'AM', '~250'])
            finally:
                back = [x for x in va if x and x != '?' and re.match(r'^(FA|MD0|CT0|CN0[01]|OS0)', x)]
                await self.rig.exec_seq(back + ['~100'])
                await self._restore_pos(saved_vm, saved_mc)
        # 3) частота/вид/клар/сдвиг — MW (номер тона не трогает); для DCS MW не принимает режим 3 — пропускаем
        if c['tone_mode'] != 3 or not fm:
            await self.rig.exec_seq([self._mw(c), '~150'])
        # 4) имя (AM его стирает) и отдельная частота передачи
        tx = c['tx_freq'] if c['split'] else c['freq']
        await self.rig.exec_seq(['MT%05d%-12s' % (ch, c['name'][:12]), 'MZ%05d%d%09d' % (ch, int(c['split']), tx), '~150'])
        got = await self.read_channel(ch)
        problems = []
        if got['empty']:
            problems.append(tr('трансивер не принял канал'))
        else:
            if got['freq'] != c['freq']:
                problems.append(tr('частота %s') % got['freq'])
            if got['mode'] != c['mode']:
                problems.append(tr('вид %s') % got['mode'])
            if got['name'].rstrip() != c['name'].rstrip():
                problems.append(tr('имя «%s»') % got['name'])
            if fm and got['shift'] != c['shift']:
                problems.append(tr('сдвиг'))
            if got['split'] != c['split'] or (c['split'] and got['tx_freq'] != c['tx_freq']):
                problems.append(tr('частота передачи'))
        return not problems, problems, got

    async def _write_all(self, c, chans):
        n = len(chans)
        results = []
        try:
            for i, ch in enumerate(chans):
                if self.stop_flag:
                    break
                why = self.busy_reason()
                if why:
                    c.ws.send_json({'t': 'mem_err', 'msg': why})
                    break
                if ch['empty']:
                    results.append({'ch': ch['ch'], 'ok': False, 'msg': tr('очистить канал по CAT нельзя — только с трансивера')})
                    continue
                if not 30_000 <= ch['freq'] <= 470_000_000:
                    results.append({'ch': ch['ch'], 'ok': False, 'msg': tr('частота вне диапазона трансивера')})
                    continue
                try:
                    ok, problems, got = await self.write_channel(ch)
                except RuntimeError as e:
                    results.append({'ch': ch['ch'], 'ok': False, 'msg': str(e)})
                    c.ws.send_json({'t': 'mem_err', 'msg': str(e)})
                    break
                results.append({'ch': ch['ch'], 'ok': ok, 'msg': ', '.join(problems)})
                c.ws.send_json({'t': 'mem_progress', 'what': 'write', 'i': i + 1, 'n': n, 'ch': ch['ch']})
            good = sum(r['ok'] for r in results)
            c.ws.send_json({'t': 'mem_written', 'results': results,
                            'msg': tr('Записано каналов: %d из %d') % (good, len(chans))})
        except Exception as e:
            log.exception(tr("Память: запись"))
            c.ws.send_json({'t': 'mem_err', 'msg': tr('Ошибка записи: %s') % e})
        finally:
            self.task = None
            self.rig.refresh(0.3)

    # ------------------------------------------------------------------ файлы
    def _path(self, name):
        name = re.sub(r'[\\/:*?"<>|]', '_', str(name or '').strip())[:60] or tr('Без названия')
        return os.path.join(self.dir, name + '.json'), name

    def list_files(self):
        try:
            out = []
            for f in sorted(os.listdir(self.dir)):
                if f.endswith('.json'):
                    p = os.path.join(self.dir, f)
                    out.append({'name': f[:-5], 'mtime': int(os.path.getmtime(p))})
            return out
        except OSError:
            return []

    # ------------------------------------------------------------------ CSV (свой формат и CHIRP)
    @staticmethod
    def to_csv(chans):
        f = io.StringIO()
        w = csv.writer(f)
        w.writerow(['Channel', 'Group', 'Name', 'RX MHz', 'Mode', 'Shift', 'Offset kHz', 'TX MHz', 'Tone mode',
                    'CTCSS', 'DCS', 'Clarifier Hz', 'RX clar', 'TX clar', 'Comment'])
        for c in chans:
            if c.get('empty'):
                continue
            tx = c['tx_freq'] if c.get('split') else \
                c['freq'] + (c['offset_khz'] * 1000 if c['shift'] == 1 else -c['offset_khz'] * 1000 if c['shift'] == 2 else 0)
            w.writerow([c['ch'], c['group'], c['name'], '%.6f' % (c['freq'] / 1e6), c['mode'],
                        'Split' if c.get('split') else ['Simplex', '+', '-'][c['shift']], c['offset_khz'], '%.6f' % (tx / 1e6),
                        ['Off', 'Tone', 'TSQL', 'DCS'][c['tone_mode']], c['ctcss'], '%03d' % c['dcs'], c['clar'],
                        int(c['rx_clar']), int(c['tx_clar']), c['comment']])
        return f.getvalue()

    @staticmethod
    def from_csv(text):
        """Свой CSV или CSV из CHIRP -> список каналов."""
        rows = list(csv.DictReader(io.StringIO(text.lstrip('﻿'))))
        out = []
        for r in rows:
            r = {k.strip().lower(): (v or '').strip() for k, v in r.items() if k}
            try:
                if 'rx mhz' in r:                                         # свой формат
                    c = {'ch': int(r.get('channel') or 0), 'group': r.get('group', ''), 'name': r.get('name', ''),
                         'freq': float(r['rx mhz']) * 1e6, 'mode': r.get('mode', 'FM'),
                         'shift': {'+': 1, '-': 2}.get(r.get('shift', ''), 0), 'offset_khz': float(r.get('offset khz') or 0),
                         'split': r.get('shift', '').lower() == 'split', 'tx_freq': float(r.get('tx mhz') or 0) * 1e6,
                         'tone_mode': {'tone': 1, 'tsql': 2, 'dcs': 3}.get(r.get('tone mode', '').lower(), 0),
                         'ctcss': r.get('ctcss') or 88.5, 'dcs': int(r.get('dcs') or 23),
                         'clar': int(float(r.get('clarifier hz') or 0)), 'rx_clar': r.get('rx clar') == '1',
                         'tx_clar': r.get('tx clar') == '1', 'comment': r.get('comment', '')}
                elif 'frequency' in r:                                    # CHIRP
                    mode = {'NFM': 'FM-N', 'FM': 'FM', 'AM': 'AM', 'USB': 'USB', 'LSB': 'LSB', 'CW': 'CW-U',
                            'DIG': 'DATA-U', 'NAM': 'AM-N'}.get(r.get('mode', 'FM').upper(), 'FM')
                    tone = r.get('tone', '')
                    c = {'ch': int(r.get('location') or 0), 'name': r.get('name', ''),
                         'freq': float(r['frequency']) * 1e6, 'mode': mode,
                         'shift': {'+': 1, '-': 2}.get(r.get('duplex', ''), 0),
                         'split': r.get('duplex', '') == 'split',
                         'tx_freq': float(r.get('offset') or 0) * 1e6 if r.get('duplex', '') == 'split' else 0,
                         'offset_khz': float(r.get('offset') or 0) * 1000,
                         'tone_mode': {'Tone': 1, 'TSQL': 2, 'DTCS': 3}.get(tone, 0),
                         'ctcss': r.get('ctonefreq') if tone == 'TSQL' else r.get('rtonefreq') or 88.5,
                         'dcs': int(r.get('dtcscode') or 23), 'comment': r.get('comment', '')}
                else:
                    continue
                out.append(clean(c))
            except (ValueError, KeyError):
                continue
        return out

    # ------------------------------------------------------------------ сообщения пульта
    async def on_msg(self, c, m):
        t = m['t']
        if t == 'mem_hello':
            c.ws.send_json({'t': 'mem_info', 'files': self.list_files(), 'ctcss': CTCSS, 'dcs': DCS,
                            'modes': [v for k, v in sorted(MODES.items()) if v not in ('C4FM-DN', 'C4FM-VW')],
                            'pms': PMS, 'busy': self.task is not None})
        elif t == 'mem_read':
            if self.task:
                return c.ws.send_json({'t': 'mem_err', 'msg': tr('Уже идёт чтение или запись')})
            why = self.busy_reason()
            if why:
                return c.ws.send_json({'t': 'mem_err', 'msg': why})
            a = max(CH_MIN, min(CH_MAX, int(m.get('from', CH_MIN))))
            b = max(a, min(CH_MAX, int(m.get('to', CH_MAX))))
            rng = list(range(a, b + 1)) + (list(PMS) if m.get('pms') else [])
            self.stop_flag = False
            self.task = asyncio.create_task(self._read_all(c, rng, bool(m.get('tones', True))))
        elif t == 'mem_write':
            if self.task:
                return c.ws.send_json({'t': 'mem_err', 'msg': tr('Уже идёт чтение или запись')})
            why = self.busy_reason()
            if why:
                return c.ws.send_json({'t': 'mem_err', 'msg': why})
            chans = [clean(x) for x in (m.get('chans') or [])]
            chans = [x for x in chans if CH_MIN <= x['ch'] <= CH_MAX or x['ch'] in PMS]
            self.stop_flag = False
            self.task = asyncio.create_task(self._write_all(c, chans))
        elif t == 'mem_stop':
            self.stop_flag = True
        elif t == 'mem_recall':                   # перейти на канал на трансивере
            ch = int(m.get('ch', 0))
            if CH_MIN <= ch <= CH_MAX or ch in PMS:
                await self.rig.exec_seq(['MC0%05d' % ch])
                self.rig.refresh(0.2)
        elif t == 'mem_from_vfo':                 # взять частоту и вид с рабочего VFO
            s = self.rig.state
            k = 'freq_b' if s.get('vfo') else 'freq_a'
            mk = 'mode_b' if s.get('vfo') else 'mode'
            c.ws.send_json({'t': 'mem_vfo', 'ch': m.get('ch'), 'freq': s.get(k), 'mode': s.get(mk)})
        elif t == 'mem_save':
            os.makedirs(self.dir, exist_ok=True)
            path, name = self._path(m.get('name'))
            data = {'app': 'FTX1-Pult', 'saved': int(time.time()), 'groups': m.get('groups') or [],
                    'chans': [clean(x) for x in (m.get('chans') or [])]}
            with open(path + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(path + '.tmp', path)
            c.ws.send_json({'t': 'mem_info', 'files': self.list_files(), 'saved': name})
        elif t == 'mem_load':
            path, name = self._path(m.get('name'))
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
                c.ws.send_json({'t': 'mem_data', 'chans': [clean(x) for x in data.get('chans', [])],
                                'groups': data.get('groups', []), 'file': name, 'msg': tr('Открыт файл «%s»') % name})
            except (OSError, ValueError) as e:
                c.ws.send_json({'t': 'mem_err', 'msg': tr('Не открылся файл: %s') % e})
        elif t == 'mem_delete':
            path, name = self._path(m.get('name'))
            try:
                os.remove(path)
            except OSError:
                pass
            c.ws.send_json({'t': 'mem_info', 'files': self.list_files()})
        elif t == 'mem_csv_out':
            c.ws.send_json({'t': 'mem_csv', 'text': self.to_csv([clean(x) for x in (m.get('chans') or [])])})
        elif t == 'mem_csv_in':
            chans = self.from_csv(str(m.get('text') or ''))
            c.ws.send_json({'t': 'mem_data', 'chans': chans, 'imported': True,
                            'msg': tr('Из CSV взято каналов: %d') % len(chans)})
