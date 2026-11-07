"""
Звук: USB Audio CODEC трансивера FTX-1 <-> сеть.

  RX: 48 кГц моно с трансивера -> ФНЧ+прореживание до 16/8 кГц -> клиентам.
      Параллельно БПФ 4096 точек -> строки водопада (0..24 кГц).
  TX: 16/8 кГц от клиента -> интерполяция до 48 кГц -> джиттер-буфер ->
      выход на трансивер (модуляция с USB).
"""
import logging
import threading
import time
import numpy as np

log = logging.getLogger("audio")

SR = 48000          # частота дискретизации USB-кодека трансивера
BLOCK = 960         # 20 мс


def lowpass_taps(cutoff, sr, n=95):
    """ФНЧ-фильтр: оконный sinc (окно Блэкмана)."""
    t = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff / sr * t) * np.blackman(n)
    return (h / h.sum()).astype(np.float32)


class Decimator:
    """48 кГц -> 48/factor кГц с сохранением хвоста между блоками."""

    def __new__(cls, factor):
        if factor == 1:
            return _Passthrough(1)
        return super().__new__(cls)

    def __init__(self, factor):
        self.f = factor
        self.taps = lowpass_taps(0.45 * SR / factor, SR)
        self.hist = np.zeros(len(self.taps) - 1, np.float32)
        self.phase = 0

    def process(self, x):
        buf = np.concatenate([self.hist, x])
        y = np.convolve(buf, self.taps, mode='valid')
        self.hist = buf[-(len(self.taps) - 1):]
        out = y[self.phase::self.f]
        self.phase = (self.phase - len(y)) % self.f
        return out


class _Passthrough:
    """48 кГц без пересчёта."""

    def __init__(self, factor=1):
        self.f = 1

    def process(self, x):
        return x


class Interpolator:
    """rate -> 48 кГц: вставка нулей + ФНЧ."""

    def __new__(cls, factor):
        if factor == 1:
            return _Passthrough(1)
        return super().__new__(cls)

    def __init__(self, factor):
        self.f = factor
        self.taps = lowpass_taps(0.45 * SR / factor, SR) * factor
        self.hist = np.zeros(len(self.taps) - 1, np.float32)

    def process(self, x):
        up = np.zeros(len(x) * self.f, np.float32)
        up[::self.f] = x
        buf = np.concatenate([self.hist, up])
        y = np.convolve(buf, self.taps, mode='valid')
        self.hist = buf[-(len(self.taps) - 1):]
        return y


class Waterfall:
    """БПФ входного потока -> строки водопада (дБ, uint8)."""
    N = 4096
    HOP = 1920      # 25 строк в секунду

    def __init__(self):
        self.win = np.hanning(self.N).astype(np.float32)
        self.norm = 20 * np.log10(self.win.sum() / 2)
        self.buf = np.zeros(0, np.float32)
        self.bin_hz = SR / self.N
        self.avg = None          # усреднённый спектр, дБ — для измерения полосы
        self.nrows = 0
        self.bandwidth = 0       # реальная полоса звука трансивера, Гц (0 — ещё не измерена)

    def _measure(self, db):
        """Где кончается звук трансивера: дальше по частоте — только шум USB-кодека (на 20+ дБ ниже)."""
        self.avg = db if self.avg is None else self.avg * 0.9 + db * 0.1
        self.nrows += 1
        if self.nrows % 25:
            return
        a = self.avg
        lo, hi = int(300 / self.bin_hz), int(2500 / self.bin_hz)
        ref = float(np.median(a[lo:hi]))
        floor = float(np.median(a[int(20000 / self.bin_hz):]))
        if ref - floor < 15:                 # тишина (закрыт шумоподавитель) — не меряем
            return
        thr = max(ref - 20, floor + 6)          # край полосы — где уровень упал на 20 дБ
        sm = np.convolve(np.pad(a, 4, mode='edge'), np.ones(9) / 9, mode='valid')
        above = np.nonzero(sm[lo:] > thr)[0]
        if len(above):
            self.bandwidth = int((lo + above[-1]) * self.bin_hz)

    def process(self, x):
        """Возвращает список строк (np.uint8, N/2 бинов)."""
        self.buf = np.concatenate([self.buf, x])
        rows = []
        while len(self.buf) >= self.N:
            seg = self.buf[:self.N] * self.win
            self.buf = self.buf[self.HOP:]
            spec = np.abs(np.fft.rfft(seg))[:self.N // 2] + 1e-12
            db = 20 * np.log10(spec) - self.norm          # дБ относительно полной шкалы
            self._measure(db)
            rows.append(np.clip((db + 130) * 2, 0, 255).astype(np.uint8))  # 0.5 дБ/ступень
        return rows


class TxBuffer:
    """Кольцевой буфер передачи с предзаполнением (джиттер-буфер)."""

    def __init__(self, prefill_ms=80, max_ms=600):
        self.lock = threading.Lock()
        self.data = np.zeros(0, np.float32)
        self.prefill = self.prefill0 = SR * prefill_ms // 1000
        self.max = SR * max_ms // 1000
        self.running = False
        self.level = 0.0
        self.tap = None                   # запись QSO: копия того, что уходит в трансивер

    def push(self, x):
        with self.lock:
            self.data = np.concatenate([self.data, x])
            if len(self.data) > self.max:                 # отстаём — выбрасываем старое
                self.data = self.data[-self.prefill * 2:]
            if len(x):
                self.level = float(np.sqrt(np.mean(x * x)))

    def pull(self, n):
        with self.lock:
            if not self.running:
                if len(self.data) >= self.prefill:
                    self.running = True
                else:
                    return np.zeros(n, np.float32)
            # часы микрофона и звуковой карты трансивера чуть расходятся: вместо обрывов
            # плавно подтягиваем запас — берём на 0,5 % больше/меньше и пересчитываем в n отсчётов
            target = self.prefill + SR // 20
            m = n
            if len(self.data) > target + SR // 10:
                m = n + max(1, n // 200)
            elif len(self.data) < self.prefill // 2:
                m = n - max(1, n // 200)
            out = self.data[:m]
            self.data = self.data[m:]
            if len(out) < m:                              # недобор — тишина и предзаполнение с запасом
                self.running = False
                self.prefill = min(self.prefill + SR // 50, SR // 4)
                out = np.concatenate([out, np.zeros(m - len(out), np.float32)])
            if m != n:
                out = np.interp(np.linspace(0, m - 1, n), np.arange(m), out).astype(np.float32)
        if self.tap:
            self.tap(out)
        return out

    def clear(self):
        with self.lock:
            self.data = np.zeros(0, np.float32)
            self.running = False
            self.level = 0.0
            self.prefill = self.prefill0


def list_devices(refresh=False):
    import sounddevice as sd
    res = []
    if refresh:                       # перечитать список (трансивер могли подключить позже);
        try:                          # только когда звук не используется
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
    for i, d in enumerate(sd.query_devices()):
        res.append({'index': i, 'name': d['name'], 'in': d['max_input_channels'],
                    'out': d['max_output_channels'],
                    'api': sd.query_hostapis(d['hostapi'])['name']})
    return res


import re as _re

# Имена, под которыми Windows показывает USB-звук трансивера
CODEC_HINTS = ('usb audio codec', 'usb audio device')


def core_name(name):
    """«Микрофон (4- USB Audio Device)» -> «usb audio device»: общая часть имени входа и выхода."""
    m = _re.search(r'\(([^()]*)\)?\s*$', name)
    core = m.group(1) if m and m.group(1) else name
    core = _re.sub(r'^\s*\d+\s*-\s*', '', core)          # «4- » — номер экземпляра
    return core.strip().lower()


# Порядок драйверов «Авто»: WDM-KS отдаёт звук в трансивер напрямую, мимо микшера Windows
# (громкость, «улучшения», приглушение при звонках), поэтому передача через него чище всего.
API_ORDER = ['Windows WDM-KS', 'Windows WASAPI', 'Windows DirectSound', 'MME']


def find_pair(core, api):
    """Вход и выход одного устройства (по общей части имени) у заданного драйвера."""
    import sounddevice as sd
    core = (core or '').lower()
    ins, outs = [], []
    for i, d in enumerate(sd.query_devices()):
        if sd.query_hostapis(d['hostapi'])['name'] != api:
            continue
        c = core_name(d['name'])
        if not (c == core or (core and core in d['name'].lower())):
            continue
        if d['max_input_channels'] > 0:
            ins.append(i)
        if d['max_output_channels'] > 0:
            outs.append(i)
    return (ins[0] if ins else None), (outs[0] if outs else None)


def _api_prio(api, api_pref):
    return 0 if api_pref and api == api_pref else {'MME': 1, 'Windows WASAPI': 2, 'Windows DirectSound': 3}.get(api, 4)


def find_device(name, kind, api_pref=''):
    """Ищем устройство по имени: точное совпадение -> часть имени -> общая часть («USB Audio Device»).
    MME обрезает имена до 31 символа, поэтому сравниваем и по началу."""
    import sounddevice as sd
    if not name:
        return None
    key = 'max_input_channels' if kind == 'in' else 'max_output_channels'
    want, want_core = name.lower(), core_name(name)
    cands = []
    for i, d in enumerate(sd.query_devices()):
        if d[key] <= 0:
            continue
        dn = d['name'].lower()
        api = sd.query_hostapis(d['hostapi'])['name']
        if dn == want:
            score = 0
        elif want.startswith(dn) or dn.startswith(want) or want in dn:
            score = 1
        elif want_core and core_name(d['name']) == want_core:
            score = 2
        elif want_core and want_core in dn:
            score = 3
        else:
            continue
        cands.append((score, _api_prio(api, api_pref), i))
    return sorted(cands)[0][2] if cands else None


def guess_codec(devices):
    """Подбор входа и выхода трансивера из списка list_devices()."""
    def pick(kind):
        best = None
        for d in devices:
            if d[kind] <= 0:
                continue
            n = d['name'].lower()
            hint = next((k for k, h in enumerate(CODEC_HINTS) if h in n), None)
            if hint is None:
                continue
            rank = (hint, _api_prio(d['api'], ''), d['index'])
            if best is None or rank < best[0]:
                best = (rank, d)
        return best[1] if best else None
    di, do = pick('in'), pick('out')
    if di and do:        # выход — той же пары, что и вход
        same = [d for d in devices if d['out'] > 0 and core_name(d['name']) == core_name(di['name'])
                and d['api'] == di['api']]
        if same:
            do = same[0]
    return di, do


class SoundDeviceAudio:
    """Реальный USB-звук через PortAudio (sounddevice)."""

    def __init__(self, in_name='', out_name='', api='', in_dev=None, out_dev=None, tx_gain=1.0):
        self.in_name, self.out_name, self.api = in_name, out_name, api
        self.in_dev, self.out_dev = in_dev, out_dev
        self.tx = TxBuffer()
        self.tx_gain = tx_gain
        self.on_rx = None
        self.streams = []
        self.api_used = ''
        self.rx_level = 0.0
        self.names = ('', '')

    def start(self, on_rx):
        import sounddevice as sd
        self.on_rx = on_rx
        if self.in_dev is None and self.out_dev is None:
            core = core_name(self.in_name or self.out_name or '')
            if not core:
                gi, _ = guess_codec(list_devices())
                core = core_name(gi['name']) if gi else ''
            if core:
                have = {sd.query_hostapis(i)['name'] for i in range(len(sd.query_hostapis()))}
                apis = [self.api] if self.api else [a for a in API_ORDER if a in have]
                errs = []
                for api in apis:
                    ind, outd = find_pair(core, api)
                    if ind is None or outd is None:
                        continue
                    try:
                        self._open(ind, outd)
                        self.api_used = api
                        return
                    except Exception as e:
                        errs.append('%s: %s' % (api, e))
                        log.warning("Звук через %s не открылся: %s", api, e)
                        self.stop()
                if errs:
                    raise RuntimeError('Звуковая карта трансивера не открылась (%s)' % '; '.join(errs))
        devs = list_devices()
        gi, go = guess_codec(devs)
        ind = self.in_dev if self.in_dev is not None else find_device(self.in_name, 'in', self.api)
        if ind is None and not self.in_name and gi:
            ind = gi['index']
        outd = self.out_dev if self.out_dev is not None else find_device(self.out_name, 'out', self.api)
        if outd is None and ind is not None:      # выход не задан/не найден — ищем пару к входу
            outd = find_device(core_name(sd.query_devices(ind)['name']), 'out',
                               sd.query_hostapis(sd.query_devices(ind)['hostapi'])['name'])
        if outd is None and not self.out_name and go:
            outd = go['index']
        if ind is None or outd is None:
            what = []
            if ind is None:
                what.append("вход «%s»" % (self.in_name or 'USB Audio CODEC/Device'))
            if outd is None:
                what.append("выход «%s»" % (self.out_name or 'пара к входу'))
            for d in devs:
                log.info("  звук [%d] %s (%s) in=%d out=%d", d['index'], d['name'], d['api'], d['in'], d['out'])
            raise RuntimeError("Не найден %s. Выберите устройства трансивера в панели управления "
                               "(список всех устройств — в журнале)." % ' и '.join(what))
        self._open(ind, outd)

    def _open(self, ind, outd):
        import sounddevice as sd
        self.names = (sd.query_devices(ind)['name'], sd.query_devices(outd)['name'])
        api = sd.query_hostapis(sd.query_devices(outd)['hostapi'])['name']
        log.info("Звук: приём с [%s] %s, передача на [%s] %s (%s)", ind, self.names[0], outd, self.names[1], api)

        self.overflows = self.underflows = 0
        self._last_report = time.monotonic()

        def in_cb(indata, frames, t, status):
            if status.input_overflow:
                self.overflows += 1
            x = indata[:, 0].astype(np.float32)
            self.rx_level = float(np.sqrt(np.mean(x * x)))
            self.on_rx(x)
            now = time.monotonic()
            if now - self._last_report > 60 and (self.overflows or self.underflows):
                log.warning("Звук: потерь при приёме %d, при передаче %d за минуту", self.overflows, self.underflows)
                self.overflows = self.underflows = 0
                self._last_report = now

        def out_cb(outdata, frames, t, status):
            if status.output_underflow:
                self.underflows += 1
            y = self.tx.pull(frames) * self.tx_gain
            outdata[:, 0] = np.clip(y, -1, 1)

        # 'high' — запас по задержке у драйвера (MME): без потерь блоков; на общую задержку
        # почти не влияет, её определяет буфер в браузере
        sin = sd.InputStream(device=ind, samplerate=SR, channels=1, dtype='float32',
                             blocksize=BLOCK, callback=in_cb, latency='high')
        try:
            sout = sd.OutputStream(device=outd, samplerate=SR, channels=1, dtype='float32',
                                   blocksize=BLOCK, callback=out_cb, latency='high')
            sin.start()
            sout.start()
        except Exception:
            sin.close()
            raise
        self.streams = [sin, sout]
        self.tx.clear()
        log.info("Звук: задержка драйвера приём %.0f мс, передача %.0f мс", sin.latency * 1000, sout.latency * 1000)

    def stop(self):
        for s in self.streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.streams = []
