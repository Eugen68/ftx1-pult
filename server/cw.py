"""
CW (азбука Морзе): декодер звука приёма и передатчик текста.

Декодер — классическая цифровая обработка, как в fldigi / CW Skimmer:
  48 кГц -> 8 кГц -> выделение тона (автопоиск пика или заданная частота) ->
  огибающая (квадратурный смеситель + ФНЧ ~100 Гц) -> адаптивный порог по шуму и пику ->
  длительности посылок/пауз -> оценка «точки» (2-средних по последним посылкам) ->
  таблица Морзе (латиница или кириллица).
Языковая модель здесь не нужна и не годится (она не «слышит» звук); она подключается
следом — исправляет ошибки распознавания и понимает смысл (см. ai.py).

Передатчик — два способа:
  'cat' — память ключа трансивера: KM5<до 50 символов>; KY05; ждём, пока отпустит TX; следующий кусок.
  'dtr'/'rts' — манипуляция линией второго (Standard) COM-порта, тайминг наш
                (в меню трансивера CW_PC_KEYING = DTR/RTS, включён BREAK-IN).
"""
import logging
import re
import threading
import time

import numpy as np

from audio import Decimator, SR

log = logging.getLogger("cw")

LATIN = {
    '.-': 'A', '-...': 'B', '-.-.': 'C', '-..': 'D', '.': 'E', '..-.': 'F', '--.': 'G', '....': 'H',
    '..': 'I', '.---': 'J', '-.-': 'K', '.-..': 'L', '--': 'M', '-.': 'N', '---': 'O', '.--.': 'P',
    '--.-': 'Q', '.-.': 'R', '...': 'S', '-': 'T', '..-': 'U', '...-': 'V', '.--': 'W', '-..-': 'X',
    '-.--': 'Y', '--..': 'Z',
    '-----': '0', '.----': '1', '..---': '2', '...--': '3', '....-': '4', '.....': '5', '-....': '6',
    '--...': '7', '---..': '8', '----.': '9',
    '.-.-.-': '.', '--..--': ',', '..--..': '?', '-..-.': '/', '-...-': '=', '.-.-.': '+',
    '-....-': '-', '-.--.': '(', '-.--.-': ')', '.----.': "'", '---...': ':', '.-..-.': '"',
    '.--.-.': '@', '-.-.--': '!',
    '...-.-': '<SK>', '...-.': '<SN>', '........': '<ERR>', '-.-.-': '<KA>',
}
# Русская азбука Морзе (соответствие по латинским кодам)
CYRILLIC = {
    '.-': 'А', '-...': 'Б', '.--': 'В', '--.': 'Г', '-..': 'Д', '.': 'Е', '...-': 'Ж', '--..': 'З',
    '..': 'И', '.---': 'Й', '-.-': 'К', '.-..': 'Л', '--': 'М', '-.': 'Н', '---': 'О', '.--.': 'П',
    '.-.': 'Р', '...': 'С', '-': 'Т', '..-': 'У', '..-.': 'Ф', '....': 'Х', '-.-.': 'Ц', '---.': 'Ч',
    '----': 'Ш', '--.-': 'Щ', '--.--': 'Ъ', '-.--': 'Ы', '-..-': 'Ь', '..-..': 'Э', '..--': 'Ю',
    '.-.-': 'Я',
}
for k, v in LATIN.items():               # цифры и знаки — общие
    if not v[0].isalpha() or v.startswith('<'):
        CYRILLIC.setdefault(k, v)

ENCODE = {v: k for k, v in LATIN.items() if len(v) == 1}
ENCODE.update({'<AR>': '.-.-.', '<BT>': '-...-', '<KN>': '-.--.', '<SK>': '...-.-', '<AS>': '.-...',
               '<HH>': '........'})
ENCODE_CYR = {v: k for k, v in CYRILLIC.items() if len(v) == 1 and v.isalpha()}


def clean_tx_text(text, cyr=False):
    """Текст для передачи: верхний регистр, только символы азбуки, прососигналы в <..>."""
    t = (text or '').upper().replace('\n', ' ').replace('Ё', 'Е')
    out, i = [], 0
    for m in re.finditer(r'<[A-Z]{2}>|.', t):
        tok = m.group(0)
        if tok == ' ' or tok in ENCODE or (cyr and tok in ENCODE_CYR):
            out.append(tok)
    s = re.sub(r' +', ' ', ''.join(out)).strip()
    return s


def text_units(text, cyr=False):
    """Последовательность (вкл/выкл, длительность в «точках») для текста."""
    seq = []
    toks = re.findall(r'<[A-Z]{2}>|.', text)
    for i, tok in enumerate(toks):
        if tok == ' ':
            if seq and not seq[-1][0]:
                seq[-1] = (False, 7)
            continue
        code = ENCODE.get(tok) or (ENCODE_CYR.get(tok) if cyr else None)
        if not code:
            continue
        for j, s in enumerate(code):
            seq.append((True, 1 if s == '.' else 3))
            seq.append((False, 1 if j < len(code) - 1 else 3))
    return seq


# ======================================================================= декодер
class CwDecoder:
    """Поток 48 кГц (float32) -> символы. Вызывается из потока звука; события — через on_event."""
    FS = 8000
    STEP = 16                      # отсчётов 8 кГц на шаг огибающей = 2 мс

    def __init__(self, on_event):
        self.on_event = on_event           # callback(dict)
        self.dec = Decimator(6)
        self.enabled = False
        self.cyr = False
        self.fixed_freq = 0                # 0 — автопоиск
        self.freq = 700.0
        self.lo, self.hi = 300, 1200       # где искать тон
        self.dit = 60.0
        self.sq = 15.0                     # «есть сигнал»: разброс огибающей, дБ (шум даёт ~11)
        self.reset()

    def reset(self):
        self.phase = 0.0
        self.tail = np.zeros(0, np.complex64)
        self.spec_buf = np.zeros(0, np.float32)
        self.cand, self.cand_n = 0.0, 0
        self.locked = bool(self.fixed_freq)
        self.hist_db = np.full(1500, np.nan, np.float32)    # 3 с огибающей для порога
        self.hpos = 0
        self.nstep = 0
        self.delay = []
        self.floor, self.peak = -120.0, -120.0
        self.snr = 0.0
        self.key = False
        self.run = 0.0
        self.segs = []                     # отрезки (вкл?, мс), ещё не окончательные
        self.learn = []                    # отрезки до того, как оценена скорость
        self.learned = False
        self.marks, self.spaces = [], []
        self.code = ''
        self.char_done = self.word_done = True
        self.last_info = 0.0
        self.quiet = 0.0                   # сколько мс нет сигнала

    def _restart_learning(self):
        self.segs, self.learn, self.learned = [], [], False
        self.marks, self.spaces = [], []
        self.code = ''
        self.char_done = self.word_done = True

    def configure(self, enabled=None, cyr=None, freq=None, wpm=None, band=None, sq=None):
        if sq is not None:
            self.sq = float(max(11, min(30, sq)))
        if enabled is not None:
            if enabled and not self.enabled:
                self.reset()
            self.enabled = bool(enabled)
        if cyr is not None:
            self.cyr = bool(cyr)
        if freq is not None:
            self.fixed_freq = float(freq or 0)
            if self.fixed_freq:
                self.freq = self.fixed_freq
                self.locked = True
            self._restart_learning()
        if wpm:
            self.dit = 1200.0 / max(5, min(60, wpm))
        if band:
            self.lo, self.hi = band

    # --------------------------------------------------------------- обработка
    def process(self, x48):
        if not self.enabled:
            return
        x = self.dec.process(x48)
        self._track_freq(x)
        n = len(x)
        w = 2 * np.pi * self.freq / self.FS
        ph = self.phase + w * np.arange(n)
        self.phase = float((self.phase + w * n) % (2 * np.pi))
        bb = (x * np.exp(-1j * ph)).astype(np.complex64)
        buf = np.concatenate([self.tail, bb])
        L = 80                                 # ФНЧ 10 мс: полоса ~100 Гц, хватает до ~45 WPM
        if len(buf) < L + self.STEP:
            self.tail = buf
            return
        cs = np.cumsum(np.concatenate([[0], buf]))
        avg = (cs[L:] - cs[:-L]) / L
        k = (len(avg) // self.STEP) * self.STEP
        env = np.abs(avg[:k:self.STEP])
        self.tail = buf[k:]
        for v in env:
            self._step(20 * np.log10(v + 1e-9))
        now = time.monotonic()
        if now - self.last_info > 0.5:
            self.last_info = now
            self.on_event({'t': 'cw_info', 'wpm': round(1200 / self.dit) if self.learned else 0,
                           'freq': round(self.freq), 'snr': round(self.snr), 'auto': not self.fixed_freq,
                           'sig': self.snr >= self.sq})

    def _track_freq(self, x):
        if self.fixed_freq:
            return
        self.spec_buf = np.concatenate([self.spec_buf, x])
        N = 2048
        if len(self.spec_buf) < N:
            return
        seg = self.spec_buf[-N:] * np.hanning(N)
        self.spec_buf = self.spec_buf[-N // 2:]
        sp = np.abs(np.fft.rfft(seg))
        f = np.fft.rfftfreq(N, 1 / self.FS)
        band = (f >= self.lo) & (f <= self.hi)
        if not band.any():
            return
        sb = sp[band]
        i = int(np.argmax(sb))
        if sb[i] < np.median(sb) * 6:          # нет явного тона (≈ 15 дБ над шумом)
            return
        if 0 < i < len(sb) - 1:
            a, b, c = np.log(sb[i - 1:i + 2] + 1e-12)
            den = a - 2 * b + c
            d = 0.5 * (a - c) / den if den else 0
        else:
            d = 0
        fpk = float(f[band][i] + d * (f[1] - f[0]))
        if not self.locked:                    # первый уверенный тон — сразу берём
            self.freq, self.locked = fpk, True
            self._restart_learning()
        elif abs(fpk - self.freq) < 25:
            self.freq += (fpk - self.freq) * 0.3
            self.cand_n = 0
        elif abs(fpk - self.cand) < 25:
            self.cand_n += 1
            if self.cand_n >= 3 and (self.snr < self.sq or self.quiet > 1500):   # прежний сигнал пропал — переходим
                self.freq, self.cand_n = fpk, 0
                self._end_char(); self._end_word()
                self._restart_learning()
        else:
            self.cand, self.cand_n = fpk, 1

    def _step(self, db_new):
        dt = self.STEP * 1000.0 / self.FS          # 2 мс
        # решение принимаем с задержкой 100 мс: порог уже «видит» начало посылки,
        # и первая точка передачи не теряется
        self.delay.append(db_new)
        if len(self.delay) < 50:
            db = None
        else:
            db = self.delay.pop(0)
        self.hist_db[self.hpos] = db_new
        self.hpos = (self.hpos + 1) % len(self.hist_db)
        self.nstep += 1
        if self.nstep % 25 == 0 or self.nstep < 60:
            h = self.hist_db[~np.isnan(self.hist_db)]
            if len(h) > 20:
                self.floor = float(np.percentile(h, 25))
                self.peak = float(np.percentile(h, 97))
                self.snr = self.peak - self.floor
        if db is None:
            return
        if self.snr < self.sq:
            on = False
        else:
            thr = self.floor + self.snr * 0.5
            hyst = min(2.0, self.snr * 0.1)
            on = db > (thr - hyst if self.key else thr + hyst)
        self.quiet = 0.0 if on else self.quiet + dt
        if on == self.key:
            self.run += dt
            if not on:
                self._silence_check()
            return
        self._segment(self.key, self.run)
        self.key, self.run = on, dt

    # отрезки -> подавление выбросов -> посылки/паузы
    def _glitch(self):
        return max(6.0, 0.3 * self.dit)

    def _segment(self, on, ms):
        segs = self.segs
        segs.append([on, ms])
        while len(segs) >= 3 and segs[-2][1] < self._glitch() and segs[-3][0] == segs[-1][0]:
            a, b, c = segs[-3:]
            segs[-3:] = [[a[0], a[1] + b[1] + c[1]]]
        while len(segs) > 2:
            self._emit(*segs.pop(0))

    def _silence_check(self):
        """Долгая тишина: дожимаем отложенные отрезки и закрываем знак/слово."""
        if self.run < 2.5 * self.dit or (not self.segs and self.char_done and self.word_done and not self.learn):
            return
        while self.segs:
            self._emit(*self.segs.pop(0))
        if not self.learned and self.learn and self.run > 1500:
            self._learn_now()                      # передача кончилась раньше, чем набралось 6 посылок
        if not self.learned:
            return
        if not self.char_done:
            self._end_char()
        if self.run >= 5.5 * self.dit and not self.word_done:
            self._end_word()
        if self.run > 6000 and self.learned:       # долгий перерыв — следующий корреспондент, скорость заново
            self._restart_learning()

    def _emit(self, on, ms):
        if not self.learned:
            self.learn.append((on, ms))
            if sum(1 for o, _ in self.learn if o) >= 8:
                self._learn_now()
            return
        self._apply(on, ms)

    def _learn_now(self):
        marks = [m for o, m in self.learn if o]
        spaces = [m for o, m in self.learn if not o and m < 2000]
        d = self._dit_from(marks, spaces)
        if d:
            self.dit = d
        self.learned = True
        q, self.learn = self.learn, []
        for on, ms in q:
            self._apply(on, ms)

    @staticmethod
    def _two_means(v):
        lg = np.log(np.asarray(v, float))
        c1, c2 = lg.min(), lg.max()
        s = None
        for _ in range(10):
            s = np.abs(lg - c1) <= np.abs(lg - c2)
            if s.all() or (~s).all():
                return None
            c1, c2 = lg[s].mean(), lg[~s].mean()
        return np.exp(c1), np.exp(c2)

    def _dit_from(self, marks, spaces):
        if len(marks) >= 3:
            r = self._two_means(marks)
            if r and 2.0 < r[1] / r[0] < 4.5:
                return float(np.clip(0.5 * (r[0] + r[1] / 3), 20, 240))
        # одни точки или одни тире: точку подскажут паузы внутри знака (1 точка)
        sp = sorted(x for x in spaces if x > 10)
        if marks and sp:
            u = float(np.percentile(sp, 20))
            m = float(np.median(marks))
            d = m if m < 2 * u else m / 3
            return float(np.clip(0.5 * (u + d), 20, 240))
        return None

    def _apply(self, on, ms):
        if on:
            self.marks = (self.marks + [ms])[-24:]
            if len(self.marks) >= 6:
                r = self._two_means(self.marks)
                if r and 2.0 < r[1] / r[0] < 4.5:
                    d = 0.5 * (r[0] + r[1] / 3)
                    self.dit = float(np.clip(self.dit + (d - self.dit) * 0.3, 20, 240))
            self.code += '.' if ms < 2 * self.dit else '-'
            self.char_done = self.word_done = False
            if len(self.code) > 9:
                self.code = ''
        else:
            if ms >= 2 * self.dit:
                self._end_char()
            if ms >= 5 * self.dit:
                self._end_word()

    def _end_char(self):
        if not self.char_done and self.code:
            table = CYRILLIC if self.cyr else LATIN
            ch = table.get(self.code, '*')
            self.on_event({'t': 'cw_rx', 'text': ch, 'wpm': round(1200 / self.dit)})
        self.code = ''
        self.char_done = True

    def _end_word(self):
        if not self.word_done:
            self.on_event({'t': 'cw_rx', 'text': ' ', 'wpm': round(1200 / self.dit)})
        self.word_done = True


# ===================================================================== передатчик
class CwSender:
    """Передача текста. Работает в отдельном потоке; on_event(dict) — прогресс и конец."""

    def __init__(self, rig_send, get_state, on_event):
        self.rig_send = rig_send           # функция: отправить CAT-команду (потокобезопасно)
        self.get_state = get_state         # функция: словарь состояния трансивера
        self.on_event = on_event
        self.busy = False
        self._stop = threading.Event()
        self.line = None                   # pyserial для dtr/rts

    def stop(self):
        self._stop.set()

    def send(self, text, method='cat', wpm=20, port='', cyr=False, max_s=180):
        if self.busy:
            return False
        self._stop.clear()
        self.busy = True
        threading.Thread(target=self._run, args=(text, method, wpm, port, cyr, max_s), daemon=True).start()
        return True

    def _run(self, text, method, wpm, port, cyr, max_s):
        t0 = time.monotonic()
        try:
            if method in ('dtr', 'rts'):
                self._run_line(text, method, wpm, port, cyr, max_s)
            else:
                self._run_cat(text, wpm, cyr, max_s)
            self.on_event({'t': 'cw_tx_done', 'ok': not self._stop.is_set(), 'secs': round(time.monotonic() - t0, 1)})
        except Exception as e:
            log.exception("Ошибка передачи CW")
            self.on_event({'t': 'cw_tx_done', 'ok': False, 'err': str(e)})
        finally:
            self.busy = False

    # --- память ключа трансивера
    @staticmethod
    def chunks(text, n=50):
        words, out, cur = text.split(' '), [], ''
        for w in words:
            while len(w) > n:
                if cur:
                    out.append(cur)
                    cur = ''
                out.append(w[:n])
                w = w[n:]
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= n:
                cur += ' ' + w
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
        return out

    def _cat_text(self, s):
        # прососигналы в памяти трансивера: AR -> +, BT -> =, KN -> (, остальные — буквами
        return (s.replace('<AR>', '+').replace('<BT>', '=').replace('<KN>', '(')
                 .replace('<SK>', 'SK').replace('<AS>', 'AS').replace('<HH>', ''))

    def _run_cat(self, text, wpm, cyr, max_s):
        t_start = time.monotonic()
        parts = self.chunks(self._cat_text(text))
        sent = 0
        self.rig_send('BI1;')                              # без BREAK-IN память не передаётся
        for part in parts:
            if self._stop.is_set():
                break
            self.rig_send('KM5%s;' % part)
            self.rig_send('KY05;')
            # ждём начала передачи (до 2 с), затем конца
            dur = sum(u for _, u in text_units(part, cyr)) * 1.2 / max(4, wpm)
            t1 = time.monotonic()
            while time.monotonic() - t1 < 2.0 and not self.get_state().get('tx') and not self._stop.is_set():
                time.sleep(0.05)
            t_tx = time.monotonic()
            while not self._stop.is_set():
                el = time.monotonic() - t_tx
                self.on_event({'t': 'cw_tx_progress', 'pos': sent + min(len(part), int(len(part) * el / max(dur, 0.1)))})
                if not self.get_state().get('tx') and el > 0.5:
                    break
                if el > dur + 6 or time.monotonic() - t_start > max_s:
                    log.warning("CW: передача куска затянулась — останавливаю")
                    self._stop.set()
                    break
                time.sleep(0.1)
            sent += len(part) + 1
        if self._stop.is_set():
            self.rig_send('KY00;')
        self.on_event({'t': 'cw_tx_progress', 'pos': len(text)})

    # --- манипуляция линией DTR/RTS
    def _run_line(self, text, method, wpm, port, cyr, max_s):
        import serial
        if not port:
            raise RuntimeError("Не выбран порт для манипуляции (Standard COM Port)")
        ser = serial.Serial(port, 9600, timeout=0)
        set_line = (lambda v: setattr(ser, 'dtr', v)) if method == 'dtr' else (lambda v: setattr(ser, 'rts', v))
        set_line(False)
        unit = 1.2 / max(4, wpm)
        seq = text_units(text, cyr)
        # прогресс: позиция символа для каждого элемента
        pos_map, pos = [], 0
        for tok in re.findall(r'<[A-Z]{2}>|.', text):
            code = ENCODE.get(tok) or (ENCODE_CYR.get(tok) if cyr else None)
            n = 1 if tok == ' ' else (len(code) * 2 if code else 0)
            pos += len(tok)
            pos_map += [pos] * n
        try:
            t = time.perf_counter()
            t_start = t
            last_ev = 0
            for i, (on, u) in enumerate(seq):
                if self._stop.is_set() or t - t_start > max_s:
                    break
                set_line(on)
                t += u * unit
                while True:                       # точная пауза: sleep + досыпание
                    rest = t - time.perf_counter()
                    if rest <= 0:
                        break
                    time.sleep(rest - 0.002 if rest > 0.003 else 0)
                if time.perf_counter() - last_ev > 0.15:
                    last_ev = time.perf_counter()
                    self.on_event({'t': 'cw_tx_progress', 'pos': pos_map[min(i, len(pos_map) - 1)] if pos_map else 0})
        finally:
            set_line(False)
            ser.close()
        self.on_event({'t': 'cw_tx_progress', 'pos': len(text)})
