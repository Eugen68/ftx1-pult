"""
PSK31 (BPSK, 31,25 Бод): текстовые связи в узкой полосе — 14070, 7040, 3580 кГц и т. д.

Ноль — смена фазы, единица — ровная несущая; символы — Varicode (частые буквы короче),
между символами — два нуля. Приём: перенос на ноль частоты, фильтр по символу, выбор момента
отсчёта по максимуму амплитуды, разностная фаза; АПЧ подтягивает к сигналу сама.
"""
import numpy as np

from audio import Decimator, SR

VARICODE = [
    '1010101011', '1011011011', '1011101101', '1101110111', '1011101011', '1101011111', '1011101111', '1011111101',
    '1011111111', '11101111', '11101', '1101101111', '1011011101', '11111', '1101110101', '1110101011',
    '1011110111', '1011110101', '1110101101', '1110101111', '1101011011', '1101101011', '1101101101', '1101010111',
    '1101111011', '1101111101', '1110110111', '1101010101', '1101011101', '1110111011', '1011111011', '1101111111',
    '1', '111111111', '101011111', '111110101', '111011011', '1011010101', '1010111011', '101111111',
    '11111011', '11110111', '101101111', '111011111', '1110101', '110101', '1010111', '110101111',
    '10110111', '10111101', '11101101', '11111111', '101110111', '101011011', '101101011', '110101101',
    '110101011', '110110111', '11110101', '110111101', '111101101', '1010101', '111010111', '1010101111',
    '1010111101', '1111101', '11101011', '10101101', '10110101', '1110111', '11011011', '11111101',
    '101010101', '1111111', '111111101', '101111101', '11010111', '10111011', '11011101', '10101011',
    '11010101', '111011101', '10101111', '1101111', '1101101', '101010111', '110110101', '101011101',
    '101110101', '101111011', '1010101101', '111110111', '111101111', '111111011', '1010111111', '101101101',
    '1011011111', '1011', '1011111', '101111', '101101', '11', '111101', '1011011',
    '101011', '1101', '111101011', '10111111', '11011', '111011', '1111', '111',
    '111111', '110111111', '10101', '10111', '101', '110111', '1111011', '1101011',
    '11011111', '1011101', '111010101', '1010110111', '110111011', '1010110101', '1011010111', '1110110101',
]
DECODE = {c: chr(i) for i, c in enumerate(VARICODE)}
BAUD = 31.25


def psk31_tx(text, fc=1500.0, fs=SR, amp=0.5, pre=32, post=32):
    """Звук PSK31 для передачи: преамбула (смены фазы), текст, послесловие (ровная несущая)."""
    bits = '0' * pre
    for ch in text:
        o = ord(ch)
        if 0 <= o < 128:
            bits += VARICODE[o] + '00'
    bits += '1' * post
    ns = int(round(fs / BAUD))
    t = np.arange(ns) / ns
    up = np.cos(np.pi * t)                      # плавная смена фазы
    sig, s = [], 1.0
    for b in bits:
        if b == '0':
            sig.append(s * up)
            s = -s
        else:
            sig.append(np.full(ns, s))
    a = np.concatenate(sig)
    r = int(0.01 * fs)                          # плавное включение/выключение
    a[:r] *= np.linspace(0, 1, r)
    a[-r:] *= np.linspace(1, 0, r)
    n = np.arange(len(a))
    return (a * np.sin(2 * np.pi * fc * n / fs) * amp).astype(np.float32)


class Psk31Decoder:
    FS = 8000
    SPS = 16                                    # отсчётов на символ после второй децимации

    def __init__(self, on_event):
        self.on_event = on_event
        self.enabled = False
        self.fc = 1500.0
        self.fc_set = 1500.0
        self.afc = True
        self.sql = 0.75
        self.dec = Decimator(6)
        n = int(self.FS / BAUD)                 # фильтр длиной в символ (cos^2)
        h = np.cos(np.pi * (np.arange(n) - n / 2) / n) ** 2
        self.h = (h / h.sum()).astype(np.float32)
        self.reset()

    def reset(self):
        self.ph = 0.0
        self.tail = np.zeros(len(self.h) - 1, np.complex64)
        self.dpos = 0
        self.energy = np.zeros(self.SPS)
        self.zbuf = np.zeros(0, np.complex64)
        self.coarse_t = 0
        self.best = 0
        self.k = 0
        self.prev = 0j
        self.bits = ''
        self.q = 0.0
        self.cnt = 0

    def configure(self, enabled=None, fc=None, afc=None, sql=None):
        if fc:
            self.fc = self.fc_set = float(min(max(fc, 200), 3500))
            self.q = 0.0
        if afc is not None:
            self.afc = bool(afc)
        if sql is not None:
            self.sql = float(sql)
        if enabled is not None and bool(enabled) != self.enabled:
            self.enabled = bool(enabled)
            self.reset()

    def process(self, x48):
        if not self.enabled:
            return
        x = self.dec.process(x48)
        n = len(x)
        w = 2 * np.pi * self.fc / self.FS
        ph = self.ph + w * np.arange(n)
        self.ph = float((self.ph + w * n) % (2 * np.pi))
        z = (x * np.exp(-1j * ph)).astype(np.complex64)
        zz = np.concatenate([self.tail, z])
        self.tail = zz[-(len(self.h) - 1):]
        y = np.convolve(zz, self.h, 'valid')
        step = int(self.FS / BAUD / self.SPS)   # 16 -> 500 Гц
        i0 = (-self.dpos) % step
        ys = y[i0::step]
        self.dpos = (self.dpos + n) % step
        # грубая АПЧ: квадрат сигнала снимает BPSK, остаётся линия на удвоенной расстройке
        self.zbuf = np.concatenate([self.zbuf, ys])[-1024:]
        self.coarse_t += len(ys)
        if self.afc and self.coarse_t >= 250 and len(self.zbuf) >= 1024:
            self.coarse_t = 0
            sq = self.zbuf ** 2
            sp = np.abs(np.fft.fft(sq * np.hanning(len(sq))))
            fr = np.fft.fftfreq(len(sq), 1 / (self.FS / step))
            lim = np.abs(fr) <= 2 * 60
            k = int(np.argmax(np.where(lim, sp, 0)))
            if sp[k] > 8 * np.median(sp[lim]):
                off = fr[k] / 2
                if abs(off) > 2:
                    self.fc = float(np.clip(self.fc + off, self.fc_set - 60, self.fc_set + 60))
                    self.zbuf = self.zbuf[:0]
        out = []
        for v in ys:
            p = self.k % self.SPS
            self.energy[p] = self.energy[p] * 0.98 + abs(v) ** 2
            self.k += 1
            if p == 0:                              # момент отсчёта выбираем раз за символ
                best = int(np.argmax(self.energy))
                if self.energy[best] > 1.05 * self.energy[self.best]:
                    self.best = best
            if p != self.best:
                continue
            d = v * np.conj(self.prev) if self.prev else 0j
            self.prev = v
            if not d:
                continue
            ang = np.angle(d)
            # качество: насколько чисто фаза 0 или 180°
            self.q = self.q * 0.95 + 0.05 * abs(np.cos(ang))
            if self.afc:
                err = np.angle(d * d) / 2           # остаток после снятия BPSK (точная АПЧ, ±7 Гц)
                self.fc += (0.08 if self.q > 0.7 else 0.03) * err / (2 * np.pi) * BAUD
                self.fc = float(np.clip(self.fc, self.fc_set - 60, self.fc_set + 60))
            bit = '1' if d.real > 0 else '0'
            self.bits += bit
            if self.bits.endswith('00'):
                code = self.bits[:-2].strip('0')
                self.bits = ''
                if code and self.q >= self.sql:
                    ch = DECODE.get(code)
                    if ch and (ch.isprintable() or ch == '\n'):
                        out.append(ch)
            elif len(self.bits) > 14:
                self.bits = self.bits[-12:]
        if out:
            self.on_event({'t': 'psk_rx', 'text': ''.join(out).replace('\r', '')})
        self.cnt += n
        if self.cnt > self.FS:
            self.cnt = 0
            self.on_event({'t': 'psk_info', 'fc': round(float(self.fc), 1), 'q': round(float(self.q), 2)})
