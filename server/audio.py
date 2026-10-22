"""
Звук: USB Audio CODEC трансивера FTX-1 <-> сеть.

  RX: 48 кГц моно с трансивера -> ФНЧ+прореживание до 16/8 кГц -> клиентам.
      Параллельно БПФ 4096 точек -> строки водопада (0..24 кГц).
  TX: 16/8 кГц от клиента -> интерполяция до 48 кГц -> джиттер-буфер ->
      выход на трансивер (модуляция с USB).
"""
import logging
import threading
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


class Interpolator:
    """rate -> 48 кГц: вставка нулей + ФНЧ."""

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

    def process(self, x):
        """Возвращает список строк (np.uint8, N/2 бинов)."""
        self.buf = np.concatenate([self.buf, x])
        rows = []
        while len(self.buf) >= self.N:
            seg = self.buf[:self.N] * self.win
            self.buf = self.buf[self.HOP:]
            spec = np.abs(np.fft.rfft(seg))[:self.N // 2] + 1e-12
            db = 20 * np.log10(spec) - self.norm          # дБ относительно полной шкалы
            rows.append(np.clip((db + 130) * 2, 0, 255).astype(np.uint8))  # 0.5 дБ/ступень
        return rows


class TxBuffer:
    """Кольцевой буфер передачи с предзаполнением (джиттер-буфер)."""

    def __init__(self, prefill_ms=80, max_ms=600):
        self.lock = threading.Lock()
        self.data = np.zeros(0, np.float32)
        self.prefill = SR * prefill_ms // 1000
        self.max = SR * max_ms // 1000
        self.running = False
        self.level = 0.0

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
            out = self.data[:n]
            self.data = self.data[n:]
            if len(out) < n:                              # недобор — тишина и снова предзаполнение
                self.running = False
                out = np.concatenate([out, np.zeros(n - len(out), np.float32)])
            return out

    def clear(self):
        with self.lock:
            self.data = np.zeros(0, np.float32)
            self.running = False
            self.level = 0.0


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


def find_device(name_part, kind, api_pref=''):
    """Ищем USB-кодек трансивера по части имени (по умолчанию 'USB Audio CODEC').
    Предпочитаем выбранный API, иначе MME/WASAPI на Windows."""
    import sounddevice as sd
    key = 'max_input_channels' if kind == 'in' else 'max_output_channels'
    cands = []
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0 and name_part.lower() in d['name'].lower():
            api = sd.query_hostapis(d['hostapi'])['name']
            prio = 0 if api == api_pref else {'MME': 1, 'Windows WASAPI': 2, 'Windows DirectSound': 3}.get(api, 4)
            cands.append((prio, i))
    if not cands:
        return None
    return sorted(cands)[0][1]


class SoundDeviceAudio:
    """Реальный USB-звук через PortAudio (sounddevice)."""

    def __init__(self, dev_name='USB Audio CODEC', in_dev=None, out_dev=None, tx_gain=1.0, api=''):
        self.dev_name = dev_name
        self.api = api
        self.in_dev, self.out_dev = in_dev, out_dev
        self.tx = TxBuffer()
        self.tx_gain = tx_gain
        self.on_rx = None
        self.streams = []
        self.rx_level = 0.0

    def start(self, on_rx):
        import sounddevice as sd
        self.on_rx = on_rx
        ind = self.in_dev if self.in_dev is not None else find_device(self.dev_name, 'in', self.api)
        outd = self.out_dev if self.out_dev is not None else find_device(self.dev_name, 'out', self.api)
        if ind is None or outd is None:
            raise RuntimeError("Не найдено звуковое устройство «%s». Доступные:\n%s" % (
                self.dev_name, "\n".join("  [%d] %s (%s) in=%d out=%d" % (
                    d['index'], d['name'], d['api'], d['in'], d['out']) for d in list_devices())))
        log.info("Звук: вход [%s] %s, выход [%s] %s", ind, sd.query_devices(ind)['name'],
                 outd, sd.query_devices(outd)['name'])

        def in_cb(indata, frames, t, status):
            x = indata[:, 0].astype(np.float32)
            self.rx_level = float(np.sqrt(np.mean(x * x)))
            self.on_rx(x)

        def out_cb(outdata, frames, t, status):
            y = self.tx.pull(frames) * self.tx_gain
            outdata[:, 0] = np.clip(y, -1, 1)

        sin = sd.InputStream(device=ind, samplerate=SR, channels=1, dtype='float32',
                             blocksize=BLOCK, callback=in_cb, latency='low')
        sout = sd.OutputStream(device=outd, samplerate=SR, channels=1, dtype='float32',
                               blocksize=BLOCK, callback=out_cb, latency='low')
        sin.start()
        sout.start()
        self.streams = [sin, sout]

    def stop(self):
        for s in self.streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
