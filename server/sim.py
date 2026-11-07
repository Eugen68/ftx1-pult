"""
Симулятор FTX-1: отвечает на CAT-команды как трансивер и выдаёт «эфир»
(шум + несколько сигналов) вместо USB-звука. Нужен, чтобы проверить
пульт без трансивера:  python main.py --sim
"""
import threading
import time
import re
import numpy as np

from audio import SR, BLOCK, TxBuffer


class SimCwOp:
    """Корреспондент-телеграфист UA3ABC: даёт CQ, отвечает на наш вызов и проводит связь."""
    CALL, NAME, QTH = 'UA3ABC', 'IVAN', 'MOSCOW'
    FREQ = 14025500

    def __init__(self):
        self.wpm = 18
        self.on = np.zeros(0)
        self.off = np.zeros(0)
        self.busy_until = 0.0
        self.stage = 'cq'
        self.their = ''
        self.next_cq = time.monotonic() + 3
        self.lock = threading.Lock()

    def schedule(self, text, at=None):
        from cw import text_units
        at = max(at or time.monotonic(), self.busy_until)
        u = 1.2 / self.wpm
        t, on, off = at, [], []
        for k, n in text_units(text):
            if k:
                on.append(t); off.append(t + n * u)
            t += n * u
        with self.lock:
            self.on, self.off = np.array(on), np.array(off)
        self.busy_until = t + 0.3

    def keyed(self, tm):
        with self.lock:
            on, off = self.on, self.off
        if not len(on):
            return np.zeros(len(tm), bool)
        i = np.searchsorted(on, tm, 'right') - 1
        ok = i >= 0
        res = np.zeros(len(tm), bool)
        res[ok] = tm[ok] < off[i[ok]]
        return res

    def tick(self):
        now = time.monotonic()
        if self.stage in ('cq', 'done') and now > self.next_cq and now > self.busy_until:
            self.stage = 'cq'
            self.schedule('CQ CQ CQ DE %s %s K' % (self.CALL, self.CALL))
            self.next_cq = self.busy_until + 20

    def heard(self, text):
        """Мы передали text (через память ключа)."""
        t = text.upper()
        m = re.search(r'DE\s+([A-Z0-9/]+)', t)
        if self.CALL not in t or not m:
            return
        self.their = m.group(1)
        at = time.monotonic() + 1.5
        if self.stage == 'cq':
            self.stage = 'report'
            self.schedule('%s DE %s GM TNX FER CALL UR RST 599 599 NAME %s %s QTH %s %s HW? %s DE %s KN'
                          % (self.their, self.CALL, self.NAME, self.NAME, self.QTH, self.QTH, self.their, self.CALL), at)
        elif self.stage == 'report':
            self.stage = 'done'
            self.schedule('R %s DE %s TNX FER QSO 73 GL <SK>' % (self.their, self.CALL), at)
            self.next_cq = at + 30


class SimRadio:
    def __init__(self):
        self.s = {
            'FA': '014074000', 'FB': '007100000', 'MD0': '2', 'MD1': '1', 'TX': '0',
            'AG0': '128', 'RG0': '255', 'SQ0': '000', 'PC': '2050', 'MG': '050', 'VX': '0',
            'VG': '050', 'VD': '10', 'NL0': '000', 'RL0': '00', 'BC0': '0', 'BP00': '000',
            'BP01': '100', 'CO00': '0000', 'CO01': '1000', 'CO02': '0000', 'CO03': '0025',
            'IS0': '0+0000', 'SH0': '012', 'GT0': '2', 'PA0': '1', 'PA1': '0', 'PA2': '0',
            'RA0': '0', 'NA0': '0', 'FT': '0', 'VS': '0', 'FR': '00', 'PR0': '1', 'PL': '050',
            'ML0': '000', 'ML1': '050', 'KS': '020', 'BI': '0', 'KP': '30', 'KR': '1', 'SD': '05',
            'CF000': '00000', 'CF001': '+0000', 'LK': '0', 'AC': '000', 'CT0': '0', 'CN00': '012',
            'CN01': '000', 'OS0': '0', 'TS': '0', 'AO': '050', 'AI': '0', 'MS': '00', 'DA': '10',
        }
        self.menu = {}
        self.cwop = SimCwOp()
        self.kmem = {}
        self.tx_level = 0.0
        self.t0 = time.time()

    def freq(self):
        return int(self.s['FA'])

    def handle(self, cmd):
        """Одна команда без ';' -> ответ (строка с ';') или ''."""
        if cmd == 'ID':
            return 'ID0840;'
        if cmd.startswith('VE') and len(cmd) == 3:
            return {'VE0': 'VE00110;', 'VE1': 'VE10107;', 'VE2': 'VE20103;', 'VE3': 'VE30104;', 'VE4': 'VE40102;'}.get(cmd, '?;')
        if cmd == 'RI0':
            sig = 60 + 40 * np.sin(time.time() / 3)
            busy = self.s['TX'] == '0' and sig > int(self.s['SQ0'])
            return 'RI0000000%d;' % (1 if busy else 0)
        if cmd in ('SM0', 'SM1'):
            v = 0 if self.s['TX'] != '0' else int(60 + 40 * np.sin(time.time() / 3) + np.random.randint(0, 12))
            return '%s%03d;' % (cmd, max(0, v))
        m = re.match(r'^RM(\d)$', cmd)
        if m:
            n = int(m.group(1))
            tx = self.s['TX'] != '0'
            pw = int(self.s['PC'][1:]) if self.s['PC'][0] == '2' else 10
            v = {5: int(min(255, self.tx_level * 900 * pw / 50)) if tx else 0,
                 6: 30 if tx else 0, 4: int(min(255, self.tx_level * 300)) if tx else 0,
                 3: int(min(255, self.tx_level * 400)) if tx else 0, 8: 196, 7: 120 if tx else 20}.get(n, 0)
            return 'RM%d%03d000;' % (n, v)
        m = re.match(r'^EX(\d{6})(.*)$', cmd)
        if m:
            ex, val = m.groups()
            if val:
                self.menu[ex] = val
                return ''
            return 'EX%s%s;' % (ex, self.menu.get(ex, '0'))
        acts = {'AB': lambda: self.s.update(FB=self.s['FA']), 'BA': lambda: self.s.update(FA=self.s['FB']),
                'SV': lambda: self.s.update(FA=self.s['FB'], FB=self.s['FA']),
                'UP': lambda: self.s.update(FA='%09d' % (self.freq() + 100)),
                'DN': lambda: self.s.update(FA='%09d' % (self.freq() - 100))}
        if cmd in acts:
            acts[cmd]()
            return ''
        if cmd.startswith('BS0'):
            bands = [1840000, 3650000, 5357000, 7074000, 10136000, 14074000, 18100000, 21074000,
                     24915000, 28074000, 50313000, 70200000, 118100000, 145500000, 433500000]
            i = int(cmd[3:5])
            if i < len(bands):
                self.s['FA'] = '%09d' % bands[i]
            return ''
        m = re.match(r'^KM([1-5])(.*)$', cmd)
        if m:
            if m.group(2):
                self.kmem[m.group(1)] = m.group(2)
                return ''
            return 'KM%s%s;' % (m.group(1), self.kmem.get(m.group(1), ''))
        m = re.match(r'^KY0([0-5])$', cmd)
        if m:
            n = m.group(1)
            if n == '0':
                self.s['TX'] = '0'
                return ''
            text = self.kmem.get(n, '')
            if text and self.s['MD0'] in ('3', '7') and self.s.get('BI') == '1':
                from cw import text_units
                dur = sum(u for _, u in text_units(text)) * 1.2 / max(4, int(self.s.get('KS', '020')))
                self.s['TX'] = '1'
                def done(txt=text):
                    if self.s['TX'] != '0':
                        self.s['TX'] = '0'
                        self.cwop.heard(txt)
                threading.Timer(0.3 + dur, done).start()
            return ''
        if cmd in ('BU0', 'BD0', 'ZI0', 'QI', 'QR') or cmd.startswith(('KY', 'KM', 'PB', 'CS', 'SC', 'VM', 'MC', 'PS')):
            return ''
        # чтение: ключ целиком совпадает
        if cmd in self.s:
            return cmd + self.s[cmd] + ';'
        # запись: самый длинный подходящий ключ
        for k in sorted(self.s, key=len, reverse=True):
            if cmd.startswith(k) and len(cmd) > len(k):
                v = cmd[len(k):]
                if k == 'TX':
                    v = '0' if v == '0' else '1'
                if k == 'PC' and v[0] == '2':
                    v = '2%03d' % max(5, min(100, int(float(v[1:]))))
                self.s[k] = v
                if k == 'AC' and v.endswith('3'):
                    threading.Timer(2.5, lambda: self.s.update(AC='000')).start()
                return ''
        return '?;'


class SimTransport:
    def __init__(self, radio):
        self.radio = radio
        self.buf = ''
        self.on_bytes = None
        self.failed = False

    def open(self, on_bytes):
        self.on_bytes = on_bytes

    def write(self, data):
        self.buf += data.decode()
        while ';' in self.buf:
            cmd, self.buf = self.buf.split(';', 1)
            ans = self.radio.handle(cmd)
            if ans:
                threading.Timer(0.004, self.on_bytes, args=(ans.encode(),)).start()

    def close(self):
        pass


class SimDigi:
    """Метеостанции в эфире симулятора: телетайп DWD (10100,8 кГц, 50 Бод / 450 Гц)
    и метеофакс DWD (3855 кГц): циклы, которые слышны при настройке на них."""
    RTTY_F, FAX_F = 10100800, 3855000

    def __init__(self):
        self.rt = self.fx = None
        threading.Thread(target=self._build, daemon=True).start()

    def _build(self):
        import digi
        txt = ('\nZCZC 123\nSEEWETTERBERICHT DWD HAMBURG / FORECAST BLACK SEA:\n'
               'WESTERN PART: SW 4 TO 5, LATER W 6, SEA 1.5 M. VIS GOOD.\n'
               'EASTERN PART: S 3 TO 4, RAIN SHOWERS, VIS MODERATE. 73 NNNN\n')
        f = digi.rtty_freqs(txt * 3, baud=50, shift=450, fc=0, lead=1.0)
        self.rt = np.cumsum(2 * np.pi * f / SR)
        self.rt_w = self.rt[-1] + 2 * np.pi * f[-1] / SR
        # «карта»: сетка, побережье, изобары и рамка
        H, W = 160, 600
        yy, xx = np.mgrid[0:H, 0:W]
        img = np.ones((H, W))
        img[(xx % 75 < 2) | (yy % 40 < 2)] = 0.55
        coast = 80 + 30 * np.sin(xx / 70.0) + 10 * np.sin(xx / 17.0)
        img[np.abs(yy - coast) < 2] = 0
        for r in (30, 50, 70):
            d = np.hypot(xx - 380, (yy - 70) * 1.3)
            img[np.abs(d - r) < 1.2] = 0.2
        img[:4], img[-4:], img[:, :4], img[:, -4:] = 0, 0, 0, 0
        v = digi.fax_values(img, phasing=20)
        gap = np.full(int(15 * SR), 0.5)
        v = np.concatenate([v, gap])
        self.fx = np.cumsum(2 * np.pi * (v * 800 - 400) / SR)
        self.fx_w = self.fx[-1]

    def block(self, f0, t, n0):
        out = np.zeros(len(t), np.float32)
        for kind, fs, arr in (('rt', self.RTTY_F, self.rt), ('fx', self.FAX_F, self.fx)):
            if arr is None:
                continue
            af = fs - f0
            if not (300 < af < 3200):
                continue
            L = len(arr)
            i = (n0 + np.arange(len(t))) % L
            loops = (n0 + np.arange(len(t))) // L
            ph = arr[i] + loops * (self.rt_w if kind == 'rt' else self.fx_w)
            out += 0.02 * np.sin(2 * np.pi * af * t + ph)
        return out


class SimAudio:
    """«Эфир»: шум + CW + FT8-подобные тоны + SSB-подобный сигнал."""

    def __init__(self, radio):
        self.radio = radio
        self.tx = TxBuffer()
        self.tx_gain = 1.0
        self.rx_level = 0.0
        self.n = 0
        self._stop = False
        self.digi = SimDigi()

    def start(self, on_rx):
        def run():
            next_t = time.monotonic()
            while not self._stop:
                x = self._block()
                self.rx_level = float(np.sqrt(np.mean(x * x)))
                on_rx(x)
                y = self.tx.pull(BLOCK)
                self.radio.tx_level = float(np.sqrt(np.mean(y * y)))
                next_t += BLOCK / SR
                time.sleep(max(0, next_t - time.monotonic()))
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._stop = True

    def _lp(self, cut):
        if getattr(self, '_cut', None) != cut:
            n = np.arange(255) - 127
            h = np.sinc(2 * cut / SR * n) * np.blackman(255)
            self._h, self._cut, self._zi = (h / h.sum()).astype(np.float32), cut, np.zeros(254, np.float32)
        return self._h

    def _block(self):
        t = (self.n + np.arange(BLOCK)) / SR
        self.n += BLOCK
        mode = self.radio.s.get('MD0', '2')
        h = self._lp(8000 if mode in ('4', 'A', 'B', 'F') else 3000)
        w = np.random.randn(BLOCK).astype(np.float32) * 0.01
        buf = np.concatenate([self._zi, w]); self._zi = buf[-254:]
        x = np.convolve(buf, h, mode='valid').astype(np.float32) + np.random.randn(BLOCK).astype(np.float32) * 2e-5
        if self.radio.s['TX'] != '0':
            return x * 0.1
        f0 = self.radio.freq()
        # «станции» на фиксированных частотах эфира: слышны, если попадают в полосу 0..3 кГц
        stations = [(14076150, 0.01, 'ft8'), (14074800, 0.008, 'ft8'),
                    (14075700, 0.012, 'ft8'), (14077100, 0.015, 'ssb'), (SimCwOp.FREQ, 0.012, 'op')]
        x += self.digi.block(f0, t, self.n - BLOCK)
        cwop = self.radio.cwop
        cwop.tick()
        tm = time.monotonic() + np.arange(BLOCK) / SR
        for fs, a, kind in stations:
            af = fs - f0
            if not (80 < af < 4000):
                continue
            if kind == 'op':
                # в CW слышим тон = расстройка; плавные фронты 5 мс сглаживанием ключа
                tx = np.concatenate([tm[0] - np.arange(239, 0, -1) / SR, tm])
                k = np.convolve(cwop.keyed(tx).astype(np.float32), np.ones(240) / 240, mode='valid')
                x += a * k * np.sin(2 * np.pi * af * t)
            elif kind == 'ft8':
                sym = np.floor(t / 0.16) % 8
                on = (t % 15) < 12.6
                x += a * on * np.sin(2 * np.pi * (af + sym * 6.25) * t)
            else:
                env = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
                for k in range(6):
                    x += a * env * np.sin(2 * np.pi * (af - 2400 + 300 + k * 330 + 40 * np.sin(t)) * t) / 3
        return x.astype(np.float32)
