"""
Редактор каналов памяти FTX-1: чтение всех каналов, правка, запись обратно, файлы и группы.

Как это работает по CAT (проверено разработчиками Hamlib на прошивке 1.12, см. rigs/yaesu/ftx1/ftx1_mem.c):
  * MR00001;  -> MR 00001 FFFFFFFFF ±OOOO R T M V C 00 S ;  (частота, клар, вид, тон-режим, сдвиг)
                 «?» — канал пустой.  Режим тона в MR: 0 выкл, 1 TSQ, 2 ENC, 3 DCS.
  * MT00001;  -> имя (12 знаков);  MT00001ИМЯ........;  — записать имя.
  * Номер тона/кода DCS в MR нет: его читаем, переключив трансивер на канал (MC0nnnnn) и спросив CN00/CN01,
    потом возвращаем трансивер как было.
  * Команда MW в прошивке не работает (всегда «?»), поэтому канал пишется так же, как в Hamlib:
    курсор памяти SUB на нужный канал (MC1nnnnn), VFO-B настраивается на частоту/вид/тон/сдвиг/клар,
    BM — «VFO-B в память», затем имя (MT). Частота и вид VFO-B после записи возвращаются.
    Ограничения прошивки: код DCS и флаг TX-клара командой BM не сохраняются.

Разнос (величина сдвига ретранслятора) в канале FTX-1 не хранится — только направление (симплекс/+/−),
а величина берётся из меню FM RPT SHIFT по диапазону. В файле редактора разнос хранится для удобства
(расчёт частоты передачи, экспорт в CSV).

Группы — свои, в файле редактора (в трансивере группы каналов по CAT не задаются).
"""
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
            'group': '', 'comment': ''}


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
            return 'Связь с трансивером не запущена'
        if st.rig.state.get('tx') or st.ptt_owner is not None or st.ft.busy or st.digi.tx_busy or st.cw.sender.busy:
            return 'Идёт передача — дождитесь окончания'
        return ''

    # ------------------------------------------------------------------ чтение
    async def read_channel(self, ch):
        mr = (await self.rig.exec_seq(['MR%05d' % ch]))[0]
        c = parse_mr(mr)
        if not c:
            return blank(ch)
        mt = (await self.rig.exec_seq(['MT%05d' % ch]))[0]
        c = dict(blank(ch), **c)
        if mt and mt.startswith('MT%05d' % ch):
            c['name'] = mt[7:19].rstrip()
        c['offset_khz'] = default_offset_khz(c['freq']) if c['shift'] else 0
        return c

    async def read_tones(self, chans):
        """Номер CTCSS / код DCS: трансивер ненадолго переключается на канал, потом возвращается."""
        need = [c for c in chans if not c['empty'] and c['tone_mode']]
        if not need:
            return
        r = await self.rig.exec_seq(['VM0', 'MC0'])
        saved_vm, saved_mc = r[0] or '', r[1] or ''
        try:
            for c in need:
                if self.stop_flag:
                    break
                q = 'CN01' if c['tone_mode'] == 3 else 'CN00'
                a = await self.rig.exec_seq(['MC0%05d' % c['ch'], '~60', q])
                m = re.match(r'^CN0[01](\d{3})', a[2] or '')
                if m:
                    i = int(m.group(1))
                    if c['tone_mode'] == 3 and i < len(DCS):
                        c['dcs'] = DCS[i]
                    elif c['tone_mode'] != 3 and i < len(CTCSS):
                        c['ctcss'] = CTCSS[i]
        finally:
            back = saved_mc if saved_vm.startswith('VM011') and saved_mc.startswith('MC') else 'VM000'
            await self.rig.exec_seq([back])
            self.rig.refresh(0.2)

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
                            'msg': 'Прочитано каналов: %d, занято: %d' % (len(chans), sum(not x['empty'] for x in chans))})
        except Exception as e:
            log.exception("Память: чтение")
            c.ws.send_json({'t': 'mem_err', 'msg': 'Ошибка чтения: %s' % e})
        finally:
            self.task = None

    # ------------------------------------------------------------------ запись
    def _write_cmds(self, c):
        ch = c['ch']
        code = MODE_CODES.get(c['mode'], '4')
        cmds = ['MC1%05d' % ch, '~80', 'VM000', 'FB%09d' % c['freq'], 'MD1%s' % code]
        if c['mode'] in FM_MODES:
            if c['tone_mode'] == 3:
                cmds += ['CT13', 'CN11%03d' % DCS.index(c['dcs'])]
            elif c['tone_mode'] in (1, 2):
                cmds += ['CT1%d' % c['tone_mode'], 'CN10%03d' % CTCSS.index(c['ctcss'])]
            else:
                cmds += ['CT10']
            cmds += ['OS1%d' % c['shift']]
        if c['clar'] or c['rx_clar'] or c['tx_clar']:
            cmds += ['CF100%d0000' % (1 if c['rx_clar'] else 0), 'CF100%d0000' % (3 if c['tx_clar'] else 2),
                     'CF101%s%04d' % ('-' if c['clar'] < 0 else '+', abs(c['clar']))]
        else:
            cmds += ['CF10000000', 'CF10020000', 'CF101+0000']
        cmds += ['BM', '~150', 'MT%05d%-12s' % (ch, c['name'][:12])]
        return cmds

    async def write_channel(self, c):
        # Сначала убеждаемся, что курсор памяти SUB встал именно на нужный канал: BM пишет VFO-B
        # «в последний выбранный канал», и при неверном курсоре запись ушла бы в чужую ячейку.
        r = await self.rig.exec_seq(['MC1%05d' % c['ch'], '~120', 'MC1'])
        m = re.match(r'^MC1?(\d{5,6})$', r[2] or '')
        if not m or int(m.group(1)) != c['ch']:
            raise RuntimeError('трансивер не встал на канал %d (ответ «%s») — запись остановлена, ничего не записано'
                               % (c['ch'], r[2]))
        snap = await self.rig.exec_seq(['FB', 'MD1'])
        try:
            await self.rig.exec_seq(self._write_cmds(c))
        finally:
            back = ['CF10000000', 'CF10020000', 'CF101+0000']
            if c['mode'] in FM_MODES:
                back += ['CT10', 'OS10']
            back += [x for x in snap if x and x != '?']
            await self.rig.exec_seq(back)
        got = await self.read_channel(c['ch'])
        # проверка: частота, вид, имя
        problems = []
        if got['empty']:
            problems.append('трансивер не принял канал')
        else:
            if got['freq'] != c['freq']:
                problems.append('частота %s' % got['freq'])
            if got['mode'] != c['mode']:
                problems.append('вид %s' % got['mode'])
            if got['name'].rstrip() != c['name'].rstrip():
                problems.append('имя «%s»' % got['name'])
            if c['mode'] in FM_MODES and got['shift'] != c['shift']:
                problems.append('сдвиг')
            if c['mode'] in FM_MODES and c['tone_mode'] == 3 and got['tone_mode'] != 3:
                problems.append('DCS не сохраняется прошивкой — задайте с трансивера')
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
                    results.append({'ch': ch['ch'], 'ok': False, 'msg': 'очистить канал по CAT нельзя — только с трансивера'})
                    continue
                if not 30_000 <= ch['freq'] <= 470_000_000:
                    results.append({'ch': ch['ch'], 'ok': False, 'msg': 'частота вне диапазона трансивера'})
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
                            'msg': 'Записано каналов: %d из %d' % (good, len(chans))})
        except Exception as e:
            log.exception("Память: запись")
            c.ws.send_json({'t': 'mem_err', 'msg': 'Ошибка записи: %s' % e})
        finally:
            self.task = None
            self.rig.refresh(0.3)

    # ------------------------------------------------------------------ файлы
    def _path(self, name):
        name = re.sub(r'[\\/:*?"<>|]', '_', str(name or '').strip())[:60] or 'Без названия'
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
            tx = c['freq'] + (c['offset_khz'] * 1000 if c['shift'] == 1 else -c['offset_khz'] * 1000 if c['shift'] == 2 else 0)
            w.writerow([c['ch'], c['group'], c['name'], '%.6f' % (c['freq'] / 1e6), c['mode'],
                        ['Simplex', '+', '-'][c['shift']], c['offset_khz'], '%.6f' % (tx / 1e6),
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
                return c.ws.send_json({'t': 'mem_err', 'msg': 'Уже идёт чтение или запись'})
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
                return c.ws.send_json({'t': 'mem_err', 'msg': 'Уже идёт чтение или запись'})
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
                                'groups': data.get('groups', []), 'file': name, 'msg': 'Открыт файл «%s»' % name})
            except (OSError, ValueError) as e:
                c.ws.send_json({'t': 'mem_err', 'msg': 'Не открылся файл: %s' % e})
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
                            'msg': 'Из CSV взято каналов: %d' % len(chans)})
