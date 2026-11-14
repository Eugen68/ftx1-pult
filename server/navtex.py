"""
NAVTEX (SITOR-B): морские навигационные и метеопредупреждения на 518 / 490 / 4209,5 кГц.

Сигнал: FSK 100 Бод, сдвиг 170 Гц. Код CCIR 476 — 7 бит, в каждом знаке ровно 4 «единицы»
(так ошибки видны сразу). Каждый знак передаётся дважды: первый раз (DX) и повтор (RX) через
4 других знака; приёмник берёт целую копию из двух. Сообщение: ZCZC B1B2B3B4 … NNNN, где
B1 — станция, B2 — тема, B3B4 — номер.

Приём в USB: трансивер ставится на 1,7 кГц ниже частоты станции (518 кГц -> 516,3 кГц USB),
сигнал оказывается около 1700 Гц звука.
"""
from i18n import tr

import re
import time

import numpy as np

from audio import Decimator, SR

# CCIR 476: код (первый принятый бит — младший) -> буква / цифра
_L = {0x47: 'A', 0x72: 'B', 0x1D: 'C', 0x53: 'D', 0x56: 'E', 0x1B: 'F', 0x35: 'G', 0x69: 'H', 0x4D: 'I',
      0x17: 'J', 0x1E: 'K', 0x65: 'L', 0x39: 'M', 0x59: 'N', 0x71: 'O', 0x2D: 'P', 0x2E: 'Q', 0x55: 'R',
      0x4B: 'S', 0x74: 'T', 0x4E: 'U', 0x3C: 'V', 0x27: 'W', 0x3A: 'X', 0x2B: 'Y', 0x63: 'Z'}
_F = {0x47: '-', 0x72: '?', 0x1D: ':', 0x53: '$', 0x56: '3', 0x1B: '!', 0x35: '&', 0x69: '#', 0x4D: '8',
      0x17: "'", 0x1E: '(', 0x65: ')', 0x39: '.', 0x59: ',', 0x71: '9', 0x2D: '0', 0x2E: '1', 0x55: '4',
      0x4B: '\a', 0x74: '5', 0x4E: '7', 0x3C: ';', 0x27: '2', 0x3A: '/', 0x2B: '6', 0x63: '"'}
C_CR, C_LF, C_SP, C_LTRS, C_FIGS = 0x78, 0x6C, 0x5C, 0x5A, 0x36
C_ALPHA, C_BETA, C_REP = 0x0F, 0x33, 0x66
VALID = {c for c in range(128) if bin(c).count('1') == 4}

SUBJECTS = {
    'A': 'навигационное предупреждение', 'B': 'метеопредупреждение', 'C': 'ледовая обстановка',
    'D': 'поиск и спасение, пиратство', 'E': 'прогноз погоды', 'F': 'лоцманская служба',
    'G': 'AIS', 'H': 'LORAN', 'J': 'спутниковая навигация', 'K': 'другие радионавигационные средства',
    'L': 'навигационное предупреждение (доп.)', 'V': 'спец. служба', 'W': 'спец. служба',
    'X': 'спец. служба', 'Y': 'спец. служба', 'Z': 'нет сообщений',
}
# Станции Чёрного и Средиземного морей рядом с нами (B1 на 518 кГц)
STATIONS_518 = {'A': 'Новороссийск', 'B': 'Керчь', 'C': 'Одесса', 'D': 'Стамбул', 'E': 'Самсун',
                'F': 'Анталья', 'H': 'Ираклион', 'I': 'Измир', 'J': 'Варна', 'L': 'Лимнос'}


def _encode_text(text):
    """Текст -> коды CCIR 476 с переключением регистров."""
    rl = {v: k for k, v in _L.items()}
    rf = {v: k for k, v in _F.items()}
    out, figs = [], False
    for ch in text.upper():
        if ch == '\n':
            out += [C_CR, C_LF]
        elif ch == ' ':
            out.append(C_SP)
        elif ch in rl:
            if figs:
                out.append(C_LTRS)
                figs = False
            out.append(rl[ch])
        elif ch in rf:
            if not figs:
                out.append(C_FIGS)
                figs = True
            out.append(rf[ch])
    return out


def navtex_synth(text, fc=1700, fs=SR, amp=0.4, phasing=40):
    """Сигнал NAVTEX для проверки: фазирование, текст в режиме FEC (DX/RX), окончание."""
    dx = [C_REP] * phasing + _encode_text(text) + [C_ALPHA] * 3
    # поток: DX и RX по очереди; RX — повтор DX, отстающий на 5 позиций потока (4 знака между)
    stream = []
    rx = [C_ALPHA] * 2 + dx                       # RX отстаёт на 2 знака своего потока
    for i in range(len(dx) + 2):
        stream.append(dx[i] if i < len(dx) else C_ALPHA)
        stream.append(rx[i] if i < len(rx) else C_ALPHA)
    bits = [(c >> k) & 1 for c in stream for k in range(7)]
    spb = fs / 100.0
    f = np.repeat(np.array(bits, float), int(round(spb)))
    f = np.where(f > 0, fc + 85, fc - 85)          # 1 (B) — верхний тон
    ph = 2 * np.pi * np.cumsum(f) / fs
    lead = np.zeros(int(0.3 * fs))
    return np.concatenate([lead, np.sin(ph) * amp, lead]).astype(np.float32)


class NavtexDecoder:
    FS = 8000
    BAUD = 100.0

    def __init__(self, on_event):
        self.on_event = on_event
        self.enabled = False
        self.fc = 1700.0
        self.dec = Decimator(6)
        self.reset()

    def reset(self):
        self.ph = 0.0
        self.bits = []            # мягкие решения (+ — «1»)
        self.bit_phase = 0.0
        self.prev_d = 0.0
        self.sync = None          # (смещение бита 0..6, чётность DX 0/1, полярность ±1)
        self.pos = 0              # позиция (в битах) следующей пары знаков
        self.figs = False
        self.bad = 0
        self.line = ''
        self.msg = None
        self.t_info = 0
        self.last = time.monotonic()
        self._build_filters()

    def _build_filters(self):
        n = 80                                         # 10 мс — длительность бита
        t = np.arange(n) / self.FS
        self.hm = np.exp(2j * np.pi * 85 * t)          # согласованные фильтры тонов (в базовой полосе)
        self.hs = np.exp(-2j * np.pi * 85 * t)
        self.tail = np.zeros(n - 1, np.complex64)

    def configure(self, enabled=None, fc=None):
        if fc:
            self.fc = float(min(max(fc, 300), 3000))
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
        z = x * np.exp(-1j * ph)
        zz = np.concatenate([self.tail, z])
        self.tail = zz[-(len(self.hm) - 1):]
        m = np.abs(np.convolve(zz, self.hm[::-1].conj(), 'valid'))
        s = np.abs(np.convolve(zz, self.hs[::-1].conj(), 'valid'))
        d = (m - s) / (m + s + 1e-9)
        # цифровая ФАПЧ тактов: переход должен приходиться на фазу 0
        spb = self.FS / self.BAUD
        for v in d:
            if (v > 0) != (self.prev_d > 0):
                err = self.bit_phase if self.bit_phase < 0.5 else self.bit_phase - 1
                self.bit_phase -= 0.08 * err
            self.prev_d = v
            self.bit_phase += 1 / spb
            if self.bit_phase >= 1:
                self.bit_phase -= 1
            elif self.bit_phase >= 0.5 and self.bit_phase - 1 / spb < 0.5:
                self.bits.append(float(v))
        self._decode()

    # ------------------------------------------------------------------ знаки
    def _code(self, i, pol):
        c = 0
        for k in range(7):
            if self.bits[i + k] * pol > 0:
                c |= 1 << k
        return c

    def _find_sync(self):
        """Ищем смещение знаков, чётность DX/RX и полярность по совпадениям DX == RX(+5)."""
        B = len(self.bits)
        if B < 7 * 40:
            return
        start = B - 7 * 40
        best = (0, None)
        for off in range(7):
            for pol in (1, -1):
                codes = [self._code(i, pol) for i in range(start + off, B - 6, 7)]
                for par in (0, 1):
                    hit = sum(1 for k in range(par, len(codes) - 5, 2)
                              if codes[k] in VALID and codes[k] == codes[k + 5])
                    if hit > best[0]:
                        best = (hit, (off, par, pol, start + off + par * 7))
        if best[0] >= 10:
            off, par, pol, p0 = best[1]
            self.sync = (pol,)
            self.pos = p0                       # указывает на DX-знак
            self.bad = 0
            self.figs = False
            self.on_event({'t': 'nx_info', 'sync': True, 'fc': round(self.fc)})

    def _decode(self):
        if self.sync is None:
            self._find_sync()
            if self.sync is None:
                if len(self.bits) > 7 * 400:
                    self.bits = self.bits[-7 * 80:]
                return
        pol = self.sync[0]
        out = []
        # нужен RX-повтор: он на 5 позиций (35 бит) позже DX
        while self.pos + 35 + 7 <= len(self.bits):
            dx = self._code(self.pos, pol)
            rx = self._code(self.pos + 35, pol)
            c = dx if dx in VALID else (rx if rx in VALID else None)
            if c is None:
                self.bad += 1
            elif dx in VALID and rx in VALID and dx != rx:
                self.bad += 0.5
            else:
                self.bad = max(0, self.bad - 0.25)
            self.pos += 14
            if c is not None:
                ch = self._char(c)
                if ch:
                    out.append(ch)
            elif c is None:
                out.append('_')
            if self.bad > 12:
                self.sync = None
                self.on_event({'t': 'nx_info', 'sync': False, 'fc': round(self.fc)})
                break
        if self.pos > 7 * 300:
            cut = self.pos - 7 * 20
            self.bits = self.bits[cut:]
            self.pos -= cut
        if out:
            self._emit(''.join(out))

    def _char(self, c):
        if c in (C_ALPHA, C_BETA, C_REP):
            return ''
        if c == C_LTRS:
            self.figs = False
            return ''
        if c == C_FIGS:
            self.figs = True
            return ''
        if c == C_SP:
            return ' '
        if c == C_CR:
            return ''
        if c == C_LF:
            return '\n'
        ch = (_F if self.figs else _L).get(c, '')
        return '' if ch == '\a' else ch

    def _emit(self, text):
        text = re.sub(r'_{3,}', '', text)
        if not text:
            return
        self.on_event({'t': 'nx_rx', 'text': text})
        # разбор заголовков ZCZC B1B2B3B4 ... NNNN
        self.line = (self.line + text)[-400:]
        m = re.search(r'ZCZC ?([A-Z])([A-Z])(\d\d)', self.line)
        if m and (not self.msg or self.msg['id'] != m.group(0)):
            b1, b2, num = m.groups()
            self.msg = {'id': m.group(0)}
            self.on_event({'t': 'nx_msg', 'b1': b1, 'b2': b2, 'num': num,
                           'station': tr(STATIONS_518.get(b1, '')), 'subject': tr(SUBJECTS.get(b2, '')),
                           'time': time.strftime('%H:%M')})
        if 'NNNN' in self.line[-12:] and self.msg:
            self.on_event({'t': 'nx_end'})
            self.msg = None
            self.line = ''
