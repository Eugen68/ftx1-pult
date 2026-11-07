"""
FT8 / FT4 в пульте: приём по слотам времени, автоматическое проведение связи, лог.

Декодирование и упаковка сообщений — библиотека ft8_lib (модуль _ftx), всё остальное здесь:
  * слоты по UTC (FT8 — 15 с, FT4 — 7,5 с), декодирование в конце слота;
  * автопоследовательность как в WSJT-X/MSHV:
      мы зовём CQ:   CQ R6AAF KN94 -> (DX R6AAF grid) -> DX R6AAF -10 -> (R6AAF DX R-12) -> DX R6AAF RR73 -> лог
      мы отвечаем:   DX R6AAF KN94 -> (R6AAF DX -12) -> DX R6AAF R-10 -> (RR73/RRR/73) -> DX R6AAF 73 -> лог
  * передача: GFSK, начало в 0,5 с от начала слота (как в WSJT-X), PTT командой CAT;
  * лог: файл ADIF и рассылка по UDP в формате WSJT-X (QSO Logged + Logged ADIF) — его понимают
    HAMLOG-агент, Log4OM, JTAlert, GridTracker и т. п.
"""
import asyncio
import datetime as dt
import logging
import math
import os
import re
import socket
import struct
import time

import numpy as np

from audio import Decimator, SR

log = logging.getLogger("ft8")

try:
    import _ftx
except Exception as e:                       # модуль собирается при сборке программы
    _ftx = None
    _ftx_err = str(e)

MODES = {
    'FT8': {'slot': 15.0, 'decode_at': 14.4, 'nsym': 79, 'sym': 0.160, 'bt': 2.0, 'ft4': False, 'late': 2.5, 'dt0': 0.64},
    'FT4': {'slot': 7.5, 'decode_at': 6.9, 'nsym': 105, 'sym': 0.048, 'bt': 1.0, 'ft4': True, 'late': 1.0, 'dt0': 0.55},
}
BANDS = {   # стандартные частоты (кГц) для FT8 и FT4
    'FT8': [('160м', 1840), ('80м', 3573), ('60м', 5357), ('40м', 7074), ('30м', 10136), ('20м', 14074),
            ('17м', 18100), ('15м', 21074), ('12м', 24915), ('10м', 28074), ('6м', 50313), ('2м', 144174)],
    'FT4': [('80м', 3575.5), ('40м', 7047.5), ('30м', 10140), ('20м', 14080), ('17м', 18104), ('15м', 21140),
            ('12м', 24919), ('10м', 28180), ('6м', 50318)],
}
CALL_RE = r'[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}(?:/[A-Z0-9]+)?|[A-Z0-9]+/[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}'
GRID_RE = r'[A-R]{2}[0-9]{2}'
REPORT_RE = r'R?[+-]\d\d'


def band_of(hz):
    mhz = hz / 1e6
    for lo, hi, b in ((1.8, 2.0, '160m'), (3.5, 4.0, '80m'), (5.3, 5.5, '60m'), (7.0, 7.3, '40m'),
                      (10.1, 10.15, '30m'), (14.0, 14.35, '20m'), (18.068, 18.168, '17m'), (21.0, 21.45, '15m'),
                      (24.89, 24.99, '12m'), (28.0, 29.7, '10m'), (50, 54, '6m'), (144, 148, '2m'),
                      (430, 440, '70cm')):
        if lo <= mhz <= hi:
            return b
    return ''


def gfsk(tones, f0, mode, fs=SR, amp=0.5):
    """Сигнал FT8/FT4 из тонов: гауссово сглаживание частоты, плавные края."""
    m = MODES[mode]
    nsps = int(round(fs * m['sym']))
    n = len(tones) * nsps
    t = np.arange(3 * nsps) / nsps - 1.5
    k = 5.336446 * m['bt']
    from math import erf
    pulse = np.array([(erf(k * (x + 0.5)) - erf(k * (x - 0.5))) / 2 for x in t])
    dphi = np.full(n + 2 * nsps, 2 * np.pi * f0 / fs)
    peak = 2 * np.pi / nsps
    for i, s in enumerate(tones):
        dphi[i * nsps:i * nsps + 3 * nsps] += peak * s * pulse
    dphi[:2 * nsps] += peak * tones[0] * pulse[nsps:]
    dphi[n:n + 2 * nsps] += peak * tones[-1] * pulse[:2 * nsps]
    phi = np.concatenate([[0.0], np.cumsum(dphi[nsps:nsps + n - 1], dtype=np.float64)])
    x = np.sin(np.mod(phi, 2 * np.pi))
    r = nsps // 8
    env = (1 - np.cos(np.pi * np.arange(r) / r)) / 2
    x[:r] *= env
    x[-r:] *= env[::-1]
    return (x * amp).astype(np.float32)


# ---------------------------------------------------------------------- UDP в формате WSJT-X
class WsjtxUdp:
    MAGIC, SCHEMA = 0xADBCCBDA, 2

    def __init__(self, ident='FTX1-PULT'):
        self.id = ident
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @staticmethod
    def _s(v):
        if v is None:
            return struct.pack('>I', 0xFFFFFFFF)
        b = str(v).encode('utf-8')
        return struct.pack('>I', len(b)) + b

    @staticmethod
    def _dt(d):
        """QDateTime: юлианский день, мс с полуночи, UTC."""
        jd = d.toordinal() + 1721425
        ms = ((d.hour * 60 + d.minute) * 60 + d.second) * 1000 + d.microsecond // 1000
        return struct.pack('>qIB', jd, ms, 1)

    def _head(self, mtype):
        return struct.pack('>III', self.MAGIC, self.SCHEMA, mtype) + self._s(self.id)

    def send(self, host, port, data):
        try:
            self.sock.sendto(data, (host, int(port)))
        except OSError as e:
            log.debug("UDP %s:%s: %s", host, port, e)

    def heartbeat(self, host, port, version):
        self.send(host, port, self._head(0) + struct.pack('>I', 3) + self._s(version) + self._s(''))

    def qso_logged(self, host, port, q):
        d = self._head(5) + self._dt(q['off']) + self._s(q['call']) + self._s(q.get('grid', '')) + \
            struct.pack('>Q', int(q['freq'])) + self._s(q['mode']) + self._s(q.get('rst_sent', '')) + \
            self._s(q.get('rst_rcvd', '')) + self._s(q.get('pwr', '')) + self._s(q.get('comment', '')) + \
            self._s(q.get('name', '')) + self._dt(q['on']) + self._s(q.get('operator', '')) + \
            self._s(q['my_call']) + self._s(q.get('my_grid', '')) + self._s('') + self._s('') + self._s('')
        self.send(host, port, d)

    def logged_adif(self, host, port, adif):
        text = '<adif_ver:5>3.1.0\n<programid:9>FTX1-Pult\n<EOH>\n' + adif
        self.send(host, port, self._head(12) + self._s(text))


def adif_record(q):
    def f(tag, v):
        v = str(v)
        return '<%s:%d>%s ' % (tag, len(v.encode('utf-8')), v) if v != '' else ''
    mode, sub = ('MFSK', 'FT4') if q['mode'] == 'FT4' else ('FT8', '')
    rec = (f('call', q['call']) + f('gridsquare', q.get('grid', '')) + f('mode', mode) + f('submode', sub) +
           f('rst_sent', q.get('rst_sent', '')) + f('rst_rcvd', q.get('rst_rcvd', '')) +
           f('qso_date', q['on'].strftime('%Y%m%d')) + f('time_on', q['on'].strftime('%H%M%S')) +
           f('qso_date_off', q['off'].strftime('%Y%m%d')) + f('time_off', q['off'].strftime('%H%M%S')) +
           f('band', band_of(q['freq'])) + f('freq', '%.6f' % (q['freq'] / 1e6)) +
           f('station_callsign', q['my_call']) + f('my_gridsquare', q.get('my_grid', '')) +
           f('tx_pwr', q.get('pwr', '')) + f('comment', q.get('comment', '')))
    return rec + '<eor>\n'


# ---------------------------------------------------------------------- стол FT8
class Ft8Desk:
    def __init__(self, station, base_dir, version='1.0'):
        self.st = station
        self.version = version
        self.adif_path = os.path.join(base_dir, 'ft8_log.adi')
        self.dec = Decimator(4)                 # 48 -> 12 кГц
        self.buf = np.zeros(0, np.float32)
        self.buf_t1 = 0.0                       # время (UTC, с) последнего отсчёта в буфере
        self.enabled = False
        self.mode = 'FT8'
        self.tx_enable = False
        self.tx_hz = 1500.0
        self.rx_hz = 1500.0
        self.qso = None                         # текущая связь
        self.tx_msg = ''                        # что передаём в ближайший свой слот
        self.tx_par = 0                         # чётность своих слотов (0 — 1-й/чётный, 1 — 2-й/нечётный)
        self.tries = 0
        self.busy = False                       # идёт передача
        self.task = None
        self.decodes = []                       # последние декоды (для нового подключения)
        self.qsos = []                          # лог текущего сеанса
        self.worked = set()
        self.last_slot = None
        self.tx_slot = None
        self.dts = []
        self.udp = WsjtxUdp()
        self.hb_t = 0
        self.auto_cq = False
        self._load_worked()

    # ------------------------------------------------------------------ настройки
    @property
    def cfg(self):
        return self.st.cfg

    def my_call(self):
        return (self.cfg.get('my_call') or '').upper().strip()

    def my_grid(self):
        return (self.cfg.get('my_grid') or '').upper().strip()[:4]

    def _clients(self):
        return [c for c in self.st.clients if c.authed and not c.admin and getattr(c, 'ft_on', False)]

    def bc(self, obj):
        for c in self._clients():
            c.ws.send_json(obj)

    def state(self):
        q = self.qso
        return {'t': 'ft_state', 'on': self.enabled, 'mode': self.mode, 'tx_enable': self.tx_enable,
                'tx_hz': round(self.tx_hz), 'rx_hz': round(self.rx_hz), 'tx_msg': self.tx_msg,
                'tx_par': self.tx_par, 'busy': self.busy, 'auto_cq': self.auto_cq,
                'qso': {k: q[k] for k in ('call', 'grid', 'step', 'rst_sent', 'rst_rcvd')} if q else None,
                'bands': BANDS[self.mode], 'my_call': self.my_call(), 'my_grid': self.my_grid(),
                'udp_host': self.cfg.get('ft_udp_host', '127.0.0.1'), 'udp_port': self.cfg.get('ft_udp_port', 2237),
                'udp_on': self.cfg.get('ft_udp_on', True), 'pwr': self.cfg.get('ft_pwr', ''),
                'lib': _ftx is not None, 'lib_err': '' if _ftx else _ftx_err, 'now': time.time()}

    def update(self):
        on = bool(self._clients()) or self.tx_enable
        if on and not self.enabled:
            self.buf = np.zeros(0, np.float32)
        self.enabled = on
        if on and not self.task and _ftx:
            self.task = asyncio.create_task(self._loop())
        if _ftx and self.my_call():
            _ftx.remember(self.my_call())

    # ------------------------------------------------------------------ звук
    def feed(self, x48):
        if not self.enabled:
            return
        rig = self.st.rig.state if self.st.rig else {}
        y = self.dec.process(x48)
        now = time.time() - float(self.cfg.get('ft_rx_latency', 0.15))
        if rig.get('tx') or self.busy:
            y = np.zeros_like(y)                  # во время передачи приём не нужен — тишина
        self.buf = np.concatenate([self.buf, y])[-12000 * 40:]
        self.buf_t1 = now

    def _slot_audio(self, t0, dur):
        """Отсчёты 12 кГц от t0 (UTC) длиной dur с."""
        n = int(dur * 12000)
        start = len(self.buf) - int(round((self.buf_t1 - t0) * 12000))
        if start < 0:
            pad = np.zeros(-start, np.float32)
            seg = np.concatenate([pad, self.buf[:max(0, n + start)]])
        else:
            seg = self.buf[start:start + n]
        if len(seg) < n:
            seg = np.concatenate([seg, np.zeros(n - len(seg), np.float32)])
        return seg

    # ------------------------------------------------------------------ главный цикл по слотам
    async def _loop(self):
        try:
            while self.enabled:
                m = MODES[self.mode]
                now = time.time()
                k = math.floor(now / m['slot'])
                t_in = now - k * m['slot']
                # 1) декодирование прошлого слота
                if t_in >= m['decode_at'] and self.last_slot != k:
                    self.last_slot = k
                    asyncio.create_task(self._decode_slot(k * m['slot'], k))
                # 2) передача в своём слоте: в начале слота или с опозданием (ответ декодировался поздно)
                for kk in (k, k + 1):
                    t_s = kk * m['slot']
                    if self.tx_enable and self.tx_msg and not self.busy and (kk % 2) == self.tx_par \
                            and self.tx_slot != kk and -0.3 < now - t_s < m['late']:
                        self.tx_slot = kk
                        asyncio.create_task(self._transmit(t_s, self.tx_msg))
                # 3) сердцебиение для агента лога
                if now - self.hb_t > 15 and self.cfg.get('ft_udp_on', True):
                    self.hb_t = now
                    self.udp.heartbeat(self.cfg.get('ft_udp_host', '127.0.0.1'), self.cfg.get('ft_udp_port', 2237),
                                       self.version)
                await asyncio.sleep(0.05)
        finally:
            self.task = None

    async def _decode_slot(self, t0, k):
        m = MODES[self.mode]
        x = self._slot_audio(t0, m['slot'] - 0.3)
        if float(np.abs(x).max()) < 1e-6:
            return
        try:
            res = await asyncio.to_thread(_ftx.decode, x.astype(np.float32).tobytes(), m['ft4'], 200.0, 3000.0)
        except Exception:
            log.exception("FT8: декодирование")
            return
        slot = time.strftime('%H%M%S', time.gmtime(t0))
        mc = self.my_call()
        items = []
        for snr, dts, f, text in res:
            dtv = dts - m['dt0']             # как в WSJT-X: 0 — начало передачи в 0,5 с слота
            self.dts = (self.dts + [dtv])[-60:]
            tags = []
            w = text.split()
            if w and w[0] in ('CQ', 'QRZ'):
                tags.append('cq')
            if mc and mc in w:
                tags.append('me')
            call = self._dx_of(w)
            if call and call in self.worked:
                tags.append('worked')
            items.append({'snr': round(snr), 'dt': round(dtv, 1), 'f': round(f), 'text': text, 'tags': tags,
                          'par': k % 2, 'call': call})
        items.sort(key=lambda i: i['f'])
        self.decodes = (self.decodes + [{'slot': slot, 'items': items}])[-12:]
        self.bc({'t': 'ft_dec', 'slot': slot, 'par': k % 2, 'items': items, 'mode': self.mode, 'now': time.time(),
                 'clock': round(float(np.median(self.dts)), 2) if self.dts else None})
        self._sequence(items, k)

    @staticmethod
    def _dx_of(w):
        """Позывной передающего: CQ [DX] CALL GRID -> CALL; TO FROM ... -> FROM."""
        if not w:
            return ''
        if w[0] in ('CQ', 'QRZ'):
            rest = [x for x in w[1:] if re.fullmatch(CALL_RE, x) and not re.fullmatch(GRID_RE, x)]
            return rest[-1] if rest else ''
        return w[1] if len(w) > 1 else ''

    # ------------------------------------------------------------------ автопоследовательность
    def _msg(self, *parts):
        return ' '.join(p for p in parts if p)

    def _set_tx(self, msg, par=None, reset_tries=True):
        self.tx_msg = msg
        if par is not None:
            self.tx_par = par
        if reset_tries:
            self.tries = 0
        self.bc(self.state())

    def start_cq(self):
        mc, g = self.my_call(), self.my_grid()
        if not mc:
            return 'Укажите свой позывной'
        self.qso = None
        self._set_tx(self._msg('CQ', mc, g))
        self.tx_enable = True
        self.bc(self.state())

    def call_station(self, item):
        """Ответ на CQ или вызов станции из списка декодов."""
        mc, g = self.my_call(), self.my_grid()
        if not mc:
            return 'Укажите свой позывной'
        w = item['text'].split()
        dx = self._dx_of(w)
        if not dx or dx == mc:
            return 'В сообщении нет позывного'
        grid = next((x for x in w[1:] if re.fullmatch(GRID_RE, x) and x != 'RR73'), '')
        self.qso = {'call': dx, 'grid': grid, 'step': 'call', 'snr': item['snr'], 'rst_sent': '', 'rst_rcvd': '',
                    'on': dt.datetime.now(dt.timezone.utc).replace(tzinfo=None), 'f': item['f']}
        # если он уже зовёт нас с рапортом — сразу на нужный шаг
        par = 1 - item['par']
        if mc in w and len(w) >= 3 and re.fullmatch(REPORT_RE, w[2]):
            self._handle_reply(w, item, par)
        else:
            self._set_tx(self._msg(dx, mc, g), par)
        self.tx_enable = True
        self.bc(self.state())

    def _rep(self, snr):
        snr = int(max(-30, min(49, round(snr))))
        return '%+03d' % snr

    def _sequence(self, items, k):
        mc = self.my_call()
        if not mc or not self.tx_enable:
            return
        q = self.qso
        to_me = [i for i in items if i['text'].split()[:1] == [mc]]
        if q:
            mine = [i for i in to_me if self._dx_of(i['text'].split()) == q['call']]
            if mine:
                self._handle_reply(mine[0]['text'].split(), mine[0], 1 - mine[0]['par'])
                return
        elif to_me and self.tx_msg.startswith('CQ'):
            # на наш CQ ответили — берём первого (как MSHV: самого громкого)
            i = sorted(to_me, key=lambda x: -x['snr'])[0]
            w = i['text'].split()
            dx = w[1] if len(w) > 1 else ''
            grid = w[2] if len(w) > 2 and re.fullmatch(GRID_RE, w[2]) else ''
            self.qso = {'call': dx, 'grid': grid, 'step': 'report', 'snr': i['snr'], 'rst_sent': self._rep(i['snr']),
                        'rst_rcvd': '', 'on': dt.datetime.now(dt.timezone.utc).replace(tzinfo=None), 'f': i['f']}
            if len(w) > 2 and re.fullmatch(REPORT_RE, w[2]):        # сразу дал рапорт
                self._handle_reply(w, i, 1 - i['par'])
                return
            self._set_tx(self._msg(dx, mc, self.qso['rst_sent']), 1 - i['par'])
            self.bc({'t': 'ft_note', 'msg': 'Отвечает %s' % dx})
            return
        # ответа нет — повторяем или сдаёмся
        if (k % 2) != self.tx_par and self.tx_msg:
            self.tries += 1
            limit = int(self.cfg.get('ft_tries', 6))
            if self.tries > limit and q:
                self.bc({'t': 'ft_note', 'msg': '%s не ответил — связь прервана' % q['call']})
                self.qso = None
                if self.auto_cq:
                    self.start_cq()
                else:
                    self.tx_enable = False
                    self._set_tx('')

    def _handle_reply(self, w, item, par):
        """Ответ корреспондента нам: w = [MYCALL, DX, payload...]"""
        q = self.qso
        mc = self.my_call()
        dx = q['call']
        last = w[2] if len(w) > 2 else ''
        if re.fullmatch(GRID_RE, last) and last != 'RR73':
            q['grid'] = last
            q['rst_sent'] = self._rep(item['snr'])
            q['step'] = 'report'
            self._set_tx(self._msg(dx, mc, q['rst_sent']), par)
        elif re.fullmatch(r'[+-]\d\d', last):                       # он дал рапорт -> R+рапорт
            q['rst_rcvd'] = last
            q['rst_sent'] = q['rst_sent'] or self._rep(item['snr'])
            q['step'] = 'roger'
            self._set_tx(self._msg(dx, mc, 'R' + q['rst_sent']), par)
        elif re.fullmatch(r'R[+-]\d\d', last):                      # R-рапорт -> RR73, связь состоялась
            q['rst_rcvd'] = last[1:]
            q['step'] = 'rr73'
            self._set_tx(self._msg(dx, mc, 'RR73'), par)
            self._log_qso()
        elif last in ('RR73', 'RRR', '73'):
            if q['step'] in ('roger', 'report', 'call'):
                q['step'] = '73'
                self._set_tx(self._msg(dx, mc, '73'), par)
                self._log_qso()
            else:                                                   # 73 на наш RR73 — всё
                self._finish()
        q['last_rx'] = time.time()

    def _finish(self):
        self.qso = None
        if self.auto_cq:
            self.start_cq()
        else:
            self.tx_enable = False
            self._set_tx('')

    # ------------------------------------------------------------------ лог
    def _log_qso(self):
        q = self.qso
        if not q or q.get('logged'):
            return
        q['logged'] = True
        dial = (self.st.rig.state.get('freq_a') if self.st.rig else 0) or 0
        rec = {'call': q['call'], 'grid': q.get('grid', ''), 'mode': self.mode,
               'rst_sent': q.get('rst_sent', ''), 'rst_rcvd': q.get('rst_rcvd', ''),
               'on': q['on'], 'off': dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
               'freq': int(dial + self.tx_hz), 'my_call': self.my_call(), 'my_grid': self.my_grid(),
               'pwr': str(self.cfg.get('ft_pwr', '') or ''), 'comment': 'FTX1-Pult', 'operator': self.my_call()}
        adif = adif_record(rec)
        try:
            new = not os.path.isfile(self.adif_path)
            with open(self.adif_path, 'a', encoding='utf-8') as f:
                if new:
                    f.write('FTX1-Pult ADIF\n<adif_ver:5>3.1.0 <programid:9>FTX1-Pult <eoh>\n')
                f.write(adif)
        except OSError as e:
            log.warning("ADIF: %s", e)
        if self.cfg.get('ft_udp_on', True):
            host, port = self.cfg.get('ft_udp_host', '127.0.0.1'), self.cfg.get('ft_udp_port', 2237)
            self.udp.qso_logged(host, port, rec)
            self.udp.logged_adif(host, port, adif)
        self.worked.add(q['call'])
        item = {'time': rec['off'].strftime('%H:%M'), 'call': rec['call'], 'grid': rec['grid'],
                'sent': rec['rst_sent'], 'rcvd': rec['rst_rcvd'], 'band': band_of(rec['freq']), 'mode': self.mode}
        self.qsos = (self.qsos + [item])[-100:]
        log.info("FT8 QSO: %s %s %s/%s %s", rec['call'], rec['grid'], rec['rst_sent'], rec['rst_rcvd'], self.mode)
        self.bc({'t': 'ft_qso', 'q': item})

    def _load_worked(self):
        try:
            with open(self.adif_path, encoding='utf-8') as f:
                for m in re.finditer(r'<call:\d+>([A-Z0-9/]+)', f.read(), re.I):
                    self.worked.add(m.group(1).upper())
        except OSError:
            pass

    # ------------------------------------------------------------------ передача
    def _why_not(self):
        st = self.st
        if not st.rig or not st.running or not st.audio:
            return 'Связь с трансивером не запущена'
        if st.ptt_owner is not None or st.cw.sender.busy or st.digi.tx_busy or st.vk.task:
            return 'Передатчик занят (голос, CW, RTTY или автовызов)'
        mode = st.rig.state.get('mode') or ''
        if mode != 'DATA-U':
            return 'Для FT8 переключите трансивер в DATA-U (сейчас %s)' % (mode or '?')
        return ''

    async def _transmit(self, t_slot, msg):
        why = self._why_not()
        if why:
            self.tx_enable = False
            self.bc({'t': 'ft_note', 'msg': why})
            self.bc(self.state())
            return
        tones = _ftx.encode(msg, MODES[self.mode]['ft4'])
        if tones is None:
            self.bc({'t': 'ft_note', 'msg': 'Сообщение «%s» не кодируется в %s' % (msg, self.mode)})
            self.tx_enable = False
            return
        x = gfsk(list(tones), self.tx_hz, self.mode, amp=float(self.cfg.get('ft_tx_level', 0.5)))
        st, tx = self.st, self.st.audio.tx
        lat = float(self.cfg.get('ft_tx_latency', 0.2))
        start = t_slot + 0.5 - lat
        if time.time() + 0.2 > start:                # опоздали — отрезаем уже «прошедшую» часть сигнала
            skip = int((time.time() + 0.2 - start) * SR)
            x = x[skip:]
            start = time.time() + 0.2
        self.busy = True
        self.bc({'t': 'ft_tx', 'on': True, 'msg': msg, 'slot': time.strftime('%H%M%S', time.gmtime(t_slot))})
        try:
            tx.clear()
            await asyncio.sleep(max(0, start - 0.15 - time.time()))
            st.rig.ptt(True)
            st.push_status()
            await asyncio.sleep(max(0, start - time.time()))
            i, step, t0 = 0, SR // 20, time.monotonic()
            while i < len(x):
                if not self.tx_enable:
                    break                            # «Стоп» — обрываем
                if (i / SR) - (time.monotonic() - t0) > 0.2:
                    await asyncio.sleep(0.02)
                    continue
                tx.push(x[i:i + step])
                i += step
            await asyncio.sleep(0.3)
        except Exception:
            log.exception("FT8: передача")
        finally:
            try:
                st.rig.ptt(False)
                tx.clear()
            except Exception:
                pass
            self.busy = False
            st.push_status()
            self.bc({'t': 'ft_tx', 'on': False})
            # после RR73 / 73 связь закончена — дальше CQ или тишина
            if self.qso and self.qso.get('step') in ('rr73', '73') and self.qso.get('logged'):
                self._finish()

    def halt(self):
        self.tx_enable = False
        self.bc(self.state())

    # ------------------------------------------------------------------ сообщения пульта
    async def on_msg(self, c, m):
        t = m['t']
        if t == 'ft_hello':
            c.ft_on = bool(m.get('on', True))
            self.update()
            c.ws.send_json(self.state())
            for d in self.decodes[-6:]:
                c.ws.send_json({'t': 'ft_dec', 'slot': d['slot'], 'items': d['items'], 'mode': self.mode, 'old': True})
            c.ws.send_json({'t': 'ft_log', 'items': self.qsos[-30:]})
            if not _ftx:
                c.ws.send_json({'t': 'ft_note', 'msg': 'Модуль FT8 не загрузился: %s' % _ftx_err})
            return
        if t == 'ft_off':
            c.ft_on = False
            self.update()
            return
        if t == 'ft_mode' and m.get('mode') in MODES:
            self.mode = m['mode']
            self.decodes = []
            self.last_slot = None
        elif t == 'ft_freq':
            if m.get('tx') is not None:
                self.tx_hz = float(min(max(float(m['tx']), 200), 3000))
            if m.get('rx') is not None:
                self.rx_hz = float(min(max(float(m['rx']), 200), 3000))
        elif t == 'ft_cq':
            err = self.start_cq()
            if err:
                c.ws.send_json({'t': 'ft_note', 'msg': err})
        elif t == 'ft_call':
            err = self.call_station(m.get('item') or {})
            if err:
                c.ws.send_json({'t': 'ft_note', 'msg': err})
        elif t == 'ft_halt':
            self.halt()
            self.qso = None
            self._set_tx('')
        elif t == 'ft_enable':
            self.tx_enable = bool(m.get('on'))
        elif t == 'ft_auto_cq':
            self.auto_cq = bool(m.get('on'))
        elif t == 'ft_par':
            self.tx_par = int(m.get('par', 0)) & 1
        elif t == 'ft_text':
            txt = (m.get('text') or '').upper().strip()[:37]
            if txt:
                self._set_tx(txt)
                self.tx_enable = True
        elif t == 'ft_cfg':
            for k in ('my_grid', 'ft_pwr', 'ft_udp_host', 'ft_udp_port', 'ft_udp_on', 'ft_tries', 'ft_tx_level'):
                if k in m:
                    v = m[k]
                    if k == 'ft_udp_port':
                        v = int(v or 2237)
                    if k == 'ft_tries':
                        v = int(v or 6)
                    if k == 'ft_tx_level':
                        v = float(min(max(float(v), 0.05), 1.0))
                    self.cfg[k] = v
            if m.get('my_call'):
                self.cfg['my_call'] = re.sub(r'[^A-Z0-9/]', '', str(m['my_call']).upper())[:11]
            if 'my_grid' in m:
                g = str(m['my_grid']).strip()
                self.cfg['my_grid'] = g[:2].upper() + g[2:4] + g[4:6].lower()
            self.st.save_cfg()
        self.bc(self.state())

    def client_gone(self, c):
        self.update()

    def stop(self):
        self.tx_enable = False
        self.enabled = False
