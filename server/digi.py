"""
Цифровые виды: RTTY (телетайп, код Бодо ITA2) и WEFAX (метеофакс КВ).

RTTY приём: 48 кГц -> 8 кГц -> два квадратурных смесителя (mark/space) -> согласованный фильтр
  длиной в бит -> дискриминатор (m-s)/(m+s) -> асинхронный приём старт/5 бит/стоп.
  Два приёмника работают параллельно — с прямой и обратной полярностью; выводится текст того,
  у которого меньше ошибок кадра (не нужно угадывать LSB/USB и «реверс»).
RTTY передача: AFSK с непрерывной фазой через USB-звук (режим DATA-L/U трансивера).

WEFAX: 48 кГц -> 12 кГц -> ЧМ-демодулятор вокруг 1900 Гц (чёрный 1500, белый 2300 Гц) ->
  строки 120 строк/мин, ширина 1809 точек (IOC 576). Автостарт по тону старта (300 Гц),
  фазирование по импульсу в начале строки (+ авто-коррекция наклона), стоп по тону 450 Гц.
"""

from i18n import tr
import logging
import threading
import time

import numpy as np

from audio import Decimator, SR

log = logging.getLogger("digi")

# ----------------------------------------------------------------- код Бодо (ITA2)
LTRS = ['', 'E', '\n', 'A', ' ', 'S', 'I', 'U', '\r', 'D', 'R', 'J', 'N', 'F', 'C', 'K',
        'T', 'Z', 'L', 'W', 'H', 'Y', 'P', 'Q', 'O', 'B', 'G', 'FIGS', 'M', 'X', 'V', 'LTRS']
FIGS = ['', '3', '\n', '-', ' ', "'", '8', '7', '\r', '$', '4', '\a', ',', '!', ':', '(',
        '5', '"', ')', '2', '#', '6', '0', '1', '9', '?', '&', 'FIGS', '.', '/', ';', 'LTRS']
ENC_L = {c: i for i, c in enumerate(LTRS) if len(c) == 1}
ENC_F = {c: i for i, c in enumerate(FIGS) if len(c) == 1 and c not in ENC_L}


def baudot_encode(text):
    """Текст -> коды Бодо с переключением регистров."""
    codes, figs = [31, 31], False
    for ch in text.upper().replace('\r', ''):
        if ch == '\n':
            codes += [8, 2]
            continue
        if ch == ' ':
            codes.append(4)
            continue
        if ch in ENC_L:
            if figs:
                codes.append(31)
                figs = False
            codes.append(ENC_L[ch])
        elif ch in ENC_F:
            if not figs:
                codes.append(27)
                figs = True
            codes.append(ENC_F[ch])
    return codes


def fsk_tones(fc, shift, reverse=False):
    lo, hi = fc - shift / 2, fc + shift / 2
    return (hi, lo) if reverse else (lo, hi)        # (mark, space); по умолчанию mark — нижний тон


def rtty_afsk(text, baud=45.45, shift=170, fc=2210, reverse=False, fs=SR, lead=0.4, amp=0.5):
    """Готовый звук AFSK (float32, fs) для передачи."""
    f = rtty_freqs(text, baud, shift, fc, reverse, fs, lead)
    # фаза — в float64: в float32 сумма за ~10 с теряет точность, и тоны «плывут» (передача портилась)
    ph = np.mod(2 * np.pi * np.cumsum(f, dtype=np.float64) / fs, 2 * np.pi)
    x = np.sin(ph) * amp
    r = int(0.005 * fs)
    x[:r] *= np.linspace(0, 1, r)
    x[-r:] *= np.linspace(1, 0, r)
    return x.astype(np.float32)


def rtty_freqs(text, baud=45.45, shift=170, fc=2210, reverse=False, fs=SR, lead=0.4):
    """Мгновенная частота AFSK по отсчётам (для передачи и симулятора)."""
    mark, space = fsk_tones(fc, shift, reverse)
    spb = fs / baud
    bits = [(1, lead * baud)]                       # несущая mark перед текстом
    for c in baudot_encode(text):
        bits.append((0, 1))
        for i in range(5):
            bits.append(((c >> i) & 1, 1))
        bits.append((1, 1.5))
    bits.append((1, 0.3 * baud))
    n = int(sum(d for _, d in bits) * spb) + 1
    f = np.empty(n, np.float32)
    pos = 0.0
    for b, d in bits:
        a, e = int(pos), int(pos + d * spb)
        f[a:e] = mark if b else space
        pos += d * spb
    f[int(pos):] = mark
    return f


class _Uart:
    """Асинхронный приёмник одного направления полярности."""

    def __init__(self, sign):
        self.sign = sign
        self.state = 0                      # 0 — ждём старт, 1 — приём битов
        self.cnt = 0.0
        self.bits = []
        self.prev = 1.0
        self.errs = []                      # 1 — ошибка кадра, 0 — хороший знак (скользящее окно)
        self.figs = False
        self.out = []

    def score(self):
        return sum(self.errs) / len(self.errs) if len(self.errs) >= 8 else 1.0

    def feed(self, d, spb, usos, emit):
        d = d * self.sign
        if self.state == 0:
            if self.prev > 0 >= d:            # фронт mark -> space: старт-бит
                self.state, self.cnt, self.bits = 1, spb * 0.5, []
            self.prev = d
            return
        self.prev = d
        self.cnt -= 1
        if self.cnt > 0:
            return
        k = len(self.bits)
        if k == 0:                            # середина старт-бита — должен быть space
            if d >= 0:
                self.state = 0
                return
            self.bits.append(0)
            self.cnt += spb
            return
        if k <= 5:
            self.bits.append(1 if d > 0 else 0)
            self.cnt += spb
            return
        # стоп-бит
        self.state = 0
        if d <= 0:
            self.errs = (self.errs + [1])[-40:]
            return
        self.errs = (self.errs + [0])[-40:]
        code = sum(b << i for i, b in enumerate(self.bits[1:6]))
        ch = (FIGS if self.figs else LTRS)[code]
        if ch == 'FIGS':
            self.figs = True
        elif ch == 'LTRS':
            self.figs = False
        elif ch == '\r' or ch == '\a' or ch == '':
            pass
        else:
            if ch == ' ' and usos:
                self.figs = False
            emit(ch)


class RttyDecoder:
    FS = 8000

    def __init__(self, on_event):
        self.on_event = on_event
        self.dec = Decimator(6)
        self.enabled = False
        self.baud, self.shift, self.fc = 45.45, 170.0, 2210.0
        self.usos = True
        self.fix_pol = 0                    # 0 — авто, 1 — прямая, -1 — обратная
        self.reset()

    def reset(self):
        self.ph = np.zeros(2)
        self.tail = np.zeros(0, np.complex64), np.zeros(0, np.complex64)
        self.u = [_Uart(1), _Uart(-1)]
        self.best = 0
        self.lvl = (0.0, 0.0)
        self.t_info = 0.0
        self.spec = np.zeros(0, np.float32)
        self.last_char = 0.0

    def configure(self, enabled=None, baud=None, shift=None, fc=None, usos=None, pol=None):
        changed = False
        for name, v in (('baud', baud), ('shift', shift), ('fc', fc)):
            if v is not None and float(v) != getattr(self, name):
                setattr(self, name, float(v))
                changed = True
        if usos is not None:
            self.usos = bool(usos)
        if pol is not None:
            self.fix_pol = int(pol)
        if enabled is not None and bool(enabled) != self.enabled:
            self.enabled = bool(enabled)
            changed = True
        if changed:
            self.reset()

    def _psd(self):
        """Спектр, усреднённый по нескольким секундам: одиночные всплески не сбивают поиск."""
        s = self.spec
        if len(s) < 8192:
            return None, None
        win = np.hanning(8192)
        sp = sum(np.abs(np.fft.rfft(s[i:i + 8192] * win)) ** 2
                 for i in range(len(s) - 8192, -1, -4096)[:3])
        f = np.fft.rfftfreq(8192, 1 / self.FS)
        old = getattr(self, 'psd', None)
        self.psd = sp if old is None or len(old) != len(sp) else 0.6 * old + 0.4 * sp
        sm = np.convolve(self.psd, np.ones(15) / 15, mode='same')       # ±7 Гц
        return f, sm

    def _scores(self, f, sm, lo_c, hi_c):
        """Оценка каждого возможного центра: слабейший из двух тонов (оба должны быть сильными)."""
        df = f[1]
        c = np.arange(max(lo_c, 250 + self.shift / 2), min(hi_c, 3450 - self.shift / 2), 2.0)
        if not len(c):
            return c, c
        lo = np.interp(c - self.shift / 2, f, sm)
        hi = np.interp(c + self.shift / 2, f, sm)
        return c, np.minimum(lo, hi)

    def autotune(self, x8k=None):
        """Найти пару тонов с заданным сдвигом во всей полосе."""
        f, sm = self._psd()
        if f is None:
            return None
        c, sc = self._scores(f, sm, 0, 4000)
        noise = np.median(sm[(f > 200) & (f < 3500)])
        # пара считается найденной, если оба тона хотя бы вдвое (на 3 дБ) выше шума — слабые станции тоже ловим
        if not len(sc) or sc.max() < noise * 2:
            return None
        self.fc = float(c[int(np.argmax(sc))])
        self.reset()
        return self.fc

    def _afc(self):
        """АПЧ: если рядом (±400 Гц) пара тонов заметно сильнее, чем на текущем центре — переходим туда;
        если декодер долго выдаёт ошибки — ищем во всей полосе."""
        f, sm = self._psd()
        if f is None:
            return
        noise = np.median(sm[(f > 200) & (f < 3500)])
        cur = float(np.minimum(np.interp(self.fc - self.shift / 2, f, sm), np.interp(self.fc + self.shift / 2, f, sm)))
        bad = self.u[self.best].score() > 0.35 if hasattr(self, 'u') else True
        span = 4000 if bad else 400
        c, sc = self._scores(f, sm, self.fc - span, self.fc + span)
        if not len(sc):
            return
        k = int(np.argmax(sc))
        if sc[k] > noise * 3 and sc[k] > 2.5 * max(cur, noise) and abs(c[k] - self.fc) > 6:
            self.fc = float(c[k])
        elif sc[k] > noise * 2:                       # тонкая подстройка в пределах ±15 Гц
            near = np.abs(c - self.fc) <= 15
            if near.any():
                j = np.nonzero(near)[0][int(np.argmax(sc[near]))]
                self.fc += 0.5 * (c[j] - self.fc)

    def process(self, x48):
        if not self.enabled:
            return
        x = self.dec.process(x48)
        self.spec = np.concatenate([self.spec, x])[-16384:]
        self.afc_n = getattr(self, 'afc_n', 0) + len(x)
        if self.afc_n >= 2 * self.FS:
            self.afc_n = 0
            self._afc()
        n = len(x)
        spb = self.FS / self.baud
        L = max(8, int(round(spb)))          # согласованный фильтр — длина бита
        mags = []
        for j, f in enumerate(fsk_tones(self.fc, self.shift)):
            w = 2 * np.pi * f / self.FS
            ph = self.ph[j] + w * np.arange(n)
            self.ph[j] = (self.ph[j] + w * n) % (2 * np.pi)
            bb = (x * np.exp(-1j * ph)).astype(np.complex64)
            buf = np.concatenate([self.tail[j], bb])
            if len(buf) <= L:
                tl = list(self.tail); tl[j] = buf; self.tail = tuple(tl)
                return
            cs = np.cumsum(np.concatenate([[0], buf]))
            mags.append(np.abs(cs[L:] - cs[:-L]) / L)
            tl = list(self.tail); tl[j] = buf[-L:]; self.tail = tuple(tl)
        m = min(len(mags[0]), len(mags[1]))
        mk, sp = mags[0][-m:], mags[1][-m:]
        d = (mk - sp) / (mk + sp + 1e-9)
        self.lvl = (float(np.mean(mk)), float(np.mean(sp)))
        out = {0: [], 1: []}
        for v in d:
            for i, u in enumerate(self.u):
                u.feed(float(v), spb, self.usos, out[i].append)
        if self.fix_pol:
            self.best = 0 if self.fix_pol > 0 else 1
        else:
            s0, s1 = self.u[0].score(), self.u[1].score()
            if abs(s0 - s1) > 0.15:
                self.best = 0 if s0 < s1 else 1
        txt = ''.join(out[self.best])
        if txt and self.u[self.best].score() < 0.5:
            self.last_char = time.monotonic()
            self.on_event({'t': 'rt_rx', 'text': txt})
        now = time.monotonic()
        if now - self.t_info > 0.5:
            self.t_info = now
            mk_, sp_ = self.lvl
            sig = 20 * np.log10((mk_ + sp_ + 1e-9) / (abs(mk_ - sp_) * 0 + np.percentile(np.abs(d), 10) + 1e-3))
            self.on_event({'t': 'rt_info', 'fc': round(self.fc), 'shift': self.shift, 'baud': self.baud,
                           'pol': tr('прямая') if self.best == 0 else tr('обратная'),
                           'err': round(self.u[self.best].score() * 100), 'mark': self.lvl[0], 'space': self.lvl[1]})


# ======================================================================= WEFAX
class FaxDecoder:
    FS = 12000

    def __init__(self, on_event, on_row):
        self.on_event = on_event
        self.on_row = on_row                # callback(index, np.uint8[W])
        self.dec = Decimator(4)
        self.enabled = False
        self.lpm = 120
        self.ioc = 576
        self.dev = 400.0                    # отклонение: чёрный = 1900-dev, белый = 1900+dev
        self.center = 1900.0
        self.auto = True
        self.slant = 0.0                    # ppm поправка частоты дискретизации
        self.afc = True                     # подстройка центра/девиации по тону старта и фазированию
        h = np.sinc(2 * 750 / self.FS * (np.arange(63) - 31)) * np.blackman(63)   # ±750: сигнал ±400 + запас на АПЧ
        self.lp = (h / h.sum()).astype(np.float32)
        self.reset()

    @property
    def W(self):
        return int(round(np.pi * self.ioc))     # 1809 для IOC 576

    def reset(self):
        self.ph = 0.0
        self.zt = np.zeros(62, np.complex64)
        self.prev = 1 + 0j
        self.val = np.zeros(0, np.float32)      # демодулированная яркость, ждущая разбиения на строки
        self.pos = 0.0                          # начало текущей строки в self.val
        self.tone_buf = np.zeros(0, np.float32)
        self.ftail = np.zeros(0, np.float32)
        self.fbuf = np.zeros(0, np.float32)     # мгновенная частота (Гц), последние 4 с — для АПЧ и индикатора
        self.sig = {'q': 0.0, 'lo': 0, 'hi': 0}
        self.stat_t = 0
        self.tone_t = 0                         # счётчик отсчётов: время по сигналу, а не по часам
        self.nsamp = 0
        self.tone_hist = []                     # последние обнаруженные тоны (300/450/675/None)
        self.state = 'idle'                     # idle | phasing | image
        self.rows = []
        self.phase_px = []
        self.started = 0.0
        self.t_info = 0.0

    def configure(self, enabled=None, lpm=None, ioc=None, auto=None, slant=None, dev=None, center=None, afc=None):
        if center:
            self.center = float(min(max(float(center), 1000), 2800))
        if afc is not None:
            self.afc = bool(afc)
        if lpm:
            self.lpm = int(lpm)
        if ioc:
            self.ioc = int(ioc)
        if auto is not None:
            self.auto = bool(auto)
        if slant is not None:
            self.slant = float(slant)
        if dev:
            self.dev = float(dev)
        if enabled is not None and bool(enabled) != self.enabled:
            self.enabled = bool(enabled)
            st, rows = self.state, self.rows
            self.reset()
            if not self.enabled and st == 'image' and rows:
                self.rows = rows
                self.finish(tr('приём остановлен'))

    def spl(self):
        return self.FS * 60.0 / self.lpm * (1 + self.slant * 1e-6)

    # --- управление
    def start_now(self):
        if self.state != 'image':
            self.rows, self.phase_px = [], []
            self._set_state('image')

    def finish(self, why=tr('стоп')):
        rows = self.rows
        self.rows = []
        if self.state != 'idle':
            self._set_state('idle')
        if len(rows) > 20:
            self.on_event({'t': 'fax_done', 'why': why, 'rows': len(rows), 'img': np.array(rows, np.uint8)})

    def shift_start(self, px):
        """Пользователь показал, где начинается строка (клик по изображению)."""
        self.pos += px / self.W * self.spl()

    def _set_state(self, s):
        self.state = s
        if s in ('phasing', 'image'):
            self.started = self.nsamp
        self.on_event({'t': 'fax_state', 'state': s, 'rows': len(self.rows), 'W': self.W, 'slant': round(self.slant)})

    # --- обработка
    def process(self, x48):
        if not self.enabled:
            return
        x = self.dec.process(x48)
        n = len(x)
        w = 2 * np.pi * self.center / self.FS
        ph = self.ph + w * np.arange(n)
        self.ph = float((self.ph + w * n) % (2 * np.pi))
        bb = np.concatenate([self.zt, (x * np.exp(-1j * ph)).astype(np.complex64)])
        z = np.convolve(bb, self.lp, mode='valid')
        self.zt = bb[-62:]
        zz = np.concatenate([[self.prev], z])
        self.prev = z[-1] if len(z) else self.prev
        dphi = np.angle(zz[1:] * np.conj(zz[:-1]))
        f = (self.center + dphi * self.FS / (2 * np.pi)).astype(np.float32)
        # медиана по 3 отсчётам: «щелчки» ЧМ-детектора на шуме не превращаются в чёрно-белые точки
        ff = np.concatenate([self.ftail, f])
        if len(ff) >= 3:
            self.ftail = ff[-2:]
            f = np.median(np.stack([ff[:-2], ff[1:-1], ff[2:]]), axis=0).astype(np.float32)
        v = np.clip((f - (self.center - self.dev)) / (2 * self.dev), 0, 1).astype(np.float32)
        self.fbuf = np.concatenate([self.fbuf, f])[-4 * self.FS:]
        self._stats()
        self._tones(v)
        if self.state == 'idle':
            return
        self.val = np.concatenate([self.val, v])
        spl = self.spl()
        W = self.W
        while len(self.val) - self.pos >= spl + 2:
            a = self.pos
            edges = (a + np.arange(W + 1) * spl / W).astype(np.int64)
            cs = np.cumsum(np.concatenate([[0], self.val[edges[0]:edges[-1]]]))
            e = edges - edges[0]
            cnt = np.maximum(e[1:] - e[:-1], 1)
            row = (cs[e[1:]] - cs[e[:-1]]) / cnt
            self.pos += spl
            self._line(row)
            if self.state == 'idle':
                break
        cut = min(int(self.pos) - 10, len(self.val))      # pos может уйти дальше накопленного (сдвиг фазирования)
        if cut > 0:
            self.val = self.val[cut:]
            self.pos -= cut
        now = time.monotonic()
        if now - self.t_info > 1:
            self.t_info = now
            self.on_event(self.info())

    def info(self):
        return {'t': 'fax_info', 'state': self.state, 'rows': len(self.rows), 'slant': round(self.slant),
                'center': round(self.center), 'dev': round(self.dev), **self.sig}

    def _stats(self):
        """Есть ли в полосе ЧМ-сигнал факса и где его чёрный/белый (для индикатора)."""
        if self.nsamp - self.stat_t < self.FS or len(self.fbuf) < self.FS:
            return
        self.stat_t = self.nsamp
        f = self.fbuf[-2 * self.FS:]
        lo, hi = np.percentile(f, [2, 98])
        span = hi - lo
        # у шума частота размазана по всей полосе фильтра (~1400 Гц и шире),
        # у факса она между чёрным и белым — около 800 Гц, где бы ни стоял центр
        fax = 550 < span < 1150
        self.sig = {'q': 1.0 if fax else 0.0, 'lo': int(lo), 'hi': int(hi)}
        # постоянная мягкая АПЧ: если центр ушёл (например, правый клик по белой линии) — вернётся сам
        if self.afc and fax and self.state != 'idle':
            # по гистограмме: самый частый уровень — фон карты (обычно белый); шум его не сдвигает
            edges = np.arange(self.center - 900, self.center + 901, 20)
            h, _ = np.histogram(f, edges)
            h = np.convolve(h, np.ones(3) / 3, 'same')
            k = int(np.argmax(h))
            pk = (edges[k] + edges[k + 1]) / 2
            if h[k] > 4 * (np.median(h) + 1):
                c = pk - self.dev if pk > self.center - 100 else pk + self.dev
                if abs(c - self.center) > 30:
                    self.center += 0.5 * (c - self.center)

    def _afc(self, what):
        """Тон старта и фазирование — чистые чёрный и белый: по ним уточняем центр и девиацию."""
        if not self.afc or len(self.fbuf) < self.FS // 2:
            return
        f = self.fbuf[-self.FS // 2:]
        lo, hi = np.percentile(f, [4, 96])
        if 500 < hi - lo < 1100 and 1000 < lo and hi < 2900:
            c, d = (lo + hi) / 2, (hi - lo) / 2
            if abs(c - self.center) > 25 or abs(d - self.dev) > 25:
                self.center, self.dev = float(c), float(d)
                self.on_event({'t': 'fax_note', 'msg': tr('АПЧ (%s): чёрный %d Гц, белый %d Гц') % (what, lo, hi)})

    def _tones(self, v):
        """Тоны старта/стопа: яркость меняется с частотой 300 / 675 Гц (старт) или 450 Гц (стоп)."""
        self.tone_buf = np.concatenate([self.tone_buf, v])[-6000:]
        self.nsamp += len(v)
        if self.nsamp - self.tone_t < 0.25 * self.FS or len(self.tone_buf) < 6000:
            return
        self.tone_t = self.nsamp
        s = self.tone_buf - self.tone_buf.mean()
        if s.std() < 0.15:
            tone = None
        else:
            sp = np.abs(np.fft.rfft(s * np.hanning(len(s))))
            f = np.fft.rfftfreq(len(s), 1 / self.FS)
            i = int(np.argmax(sp[1:]) + 1)
            tone = None
            if sp[i] > 25 * np.median(sp):
                for t in (300, 450, 675):
                    if abs(f[i] - t) < 12:
                        tone = t
        self.tone_hist = (self.tone_hist + [tone])[-12:]
        last = self.tone_hist[-8:]
        if len(last) == 8 and all(t == last[0] for t in last) and last[0]:
            t = last[0]
            if t in (300, 675) and self.auto and self.state != 'phasing':
                if self.state == 'image':
                    self.finish(tr('новая карта'))
                self.ioc = 576 if t == 300 else 288
                self._afc(tr('тон старта'))
                self.rows, self.phase_px = [], []
                self.val, self.pos = np.zeros(0, np.float32), 0.0
                self._set_state('phasing')
                self.on_event({'t': 'fax_note', 'msg': tr('Тон старта (IOC %d) — фазирование') % self.ioc})
            elif t == 450 and self.state == 'image' and self.auto:
                self.finish(tr('тон стопа'))
            self.tone_hist = []

    def _line(self, row):
        W = self.W
        if self.state == 'phasing':
            # строка фазирования: вся одного цвета + один сплошной импульс другого цвета ~5 % строки
            hi = row > 0.5
            frac = hi.mean()
            minority = hi if frac < 0.5 else ~hi
            mf = minority.mean()
            ok = False
            if 0.02 < mf < 0.10:
                idx = np.nonzero(minority)[0]
                ang = idx / W * 2 * np.pi
                res = np.mean(np.exp(1j * ang))
                if abs(res) > 0.9:                      # все точки импульса рядом — это один отрезок
                    ok = True
                    self.phase_px.append((np.angle(res) % (2 * np.pi)) / (2 * np.pi) * W)
            if ok:
                self.bad_phase = 0
                if len(self.phase_px) == 6:
                    self._afc(tr('фазирование'))
                if len(self.phase_px) >= 40:
                    self._end_phasing()
            else:
                self.bad_phase = getattr(self, 'bad_phase', 0) + 1
                # фазирование кончилось (пошла карта) или его не было вовсе
                if (len(self.phase_px) >= 6 and self.bad_phase >= 2) or self.nsamp - self.started > 45 * self.FS:
                    self._end_phasing()
            return
        if self.state == 'image':
            g = (np.clip(row, 0, 1) * 255).astype(np.uint8)
            self.rows.append(g)
            self.on_row(len(self.rows) - 1, g)
            if len(self.rows) >= 3000:
                self.finish(tr('предел 3000 строк'))

    def _end_phasing(self):
        p = np.unwrap(np.array(self.phase_px) / self.W * 2 * np.pi) / (2 * np.pi) * self.W if self.phase_px else []
        if len(p) >= 6:
            k = np.arange(len(p))
            slope, icpt = np.polyfit(k, p, 1)               # уход импульса за строку -> наклон
            for _ in range(2):                              # отбрасываем выбросы (строки на стыке)
                res = np.abs(p - (icpt + slope * k))
                good = res <= max(3.0, 3 * np.median(res))
                if good.sum() >= 6:
                    slope, icpt = np.polyfit(k[good], p[good], 1)
            res_sd = float(np.std(p - (icpt + slope * k)))
            # наклон берём только по уверенному фазированию: ≥10 строк и импульс ровно ложится на прямую
            if len(p) >= 10 and res_sd < 3 and 0.05 < abs(slope) < 0.01 * self.W:
                self.slant += slope / self.W * 1e6
                self.slant = float(np.clip(self.slant, -3000, 3000))
            cur = (icpt + slope * len(p)) % self.W         # где импульс сейчас
            target = 0.025 * self.W
            self.pos += ((cur - target) % self.W) / self.W * self.spl()
        self.rows = []
        self._set_state('image')
        self.on_event({'t': 'fax_note', 'msg': tr('Фазирование: %d строк, наклон %+d ppm') % (len(self.phase_px), round(self.slant))})


def fax_synth(img, lpm=120, ioc=576, fs=SR, start=True, phasing=20, stop=True, dev=400, center=1900, amp=0.3):
    """Сигнал WEFAX из картинки (для проверки и симулятора). img: float 0..1, 1 — белое."""
    v = fax_values(img, lpm, ioc, fs, start, phasing, stop)
    f = center - dev + v * 2 * dev
    return (np.sin(2 * np.pi * np.cumsum(f) / fs) * amp).astype(np.float32)


def fax_values(img, lpm=120, ioc=576, fs=SR, start=True, phasing=20, stop=True):
    """Яркость 0..1 по отсчётам: тон старта, фазирование, строки картинки, тон стопа."""
    W = int(round(np.pi * ioc))
    spl = fs * 60.0 / lpm
    seg = []
    t_tone = 3.0
    if start:
        n = int(t_tone * fs)
        t = np.arange(n) / fs
        seg.append(np.where(np.sin(2 * np.pi * (300 if ioc == 576 else 675) * t) > 0, 1.0, 0.0))
    if phasing:
        line = np.ones(W)                               # белая строка с чёрным импульсом в начале (5 %)
        line[:int(0.05 * W)] = 0.0
        for _ in range(phasing):
            seg.append(np.interp(np.arange(int(spl)) / spl * W, np.arange(W), line))
    for r in img:
        rr = np.interp(np.arange(W), np.linspace(0, W - 1, len(r)), r)
        seg.append(np.interp(np.arange(int(spl)) / spl * W, np.arange(W), rr))
    if stop:
        n = int(t_tone * fs)
        t = np.arange(n) / fs
        seg.append(np.where(np.sin(2 * np.pi * 450 * t) > 0, 1.0, 0.0))
    return np.concatenate(seg)
