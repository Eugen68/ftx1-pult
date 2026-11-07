"""
Запись связи (QSO) на ПК: приём и передача в одном файле WAV (16 кГц, моно, 16 бит).

Приём — звук с трансивера, передача — то, что ушло в трансивер (микрофон пульта, автовызов,
RTTY). Файлы — в папке records рядом с программой: ГГГГММДД_ЧЧММСС_частота_вид.wav.
"""
import os
import re
import secrets
import threading
import time
import wave

import numpy as np

from audio import Decimator

OUT_SR = 16000
MAX_SECONDS = 4 * 3600


class QsoRecorder:
    def __init__(self, base_dir):
        self.dir = os.path.join(base_dir, 'records')
        self.token = secrets.token_urlsafe(12)          # ссылки на скачивание — только для вошедших
        self.lock = threading.Lock()
        self.w = None
        self.name = ''
        self.t0 = 0.0
        self.frames = 0
        self.txq = []
        self.txn = 0
        self.dec = None

    @property
    def on(self):
        return self.w is not None

    def start(self, freq=0, mode=''):
        if self.w:
            return self.name
        os.makedirs(self.dir, exist_ok=True)
        f = ('_%.3fkHz' % (freq / 1000)).replace('.', ',') if freq else ''
        m = ('_' + re.sub(r'[^A-Za-z0-9-]', '', mode)) if mode else ''
        self.name = time.strftime('%Y%m%d_%H%M%S') + f + m + '.wav'
        w = wave.open(os.path.join(self.dir, self.name), 'wb')
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_SR)
        with self.lock:
            self.txq, self.txn = [], 0
        self.dec = Decimator(3)
        self.frames = 0
        self.t0 = time.time()
        self.w = w
        return self.name

    def stop(self):
        w, self.w = self.w, None
        if w:
            try:
                w.close()
            except Exception:
                pass
        return self.name

    # ---- передача: зовётся из звукового потока (TxBuffer.pull)
    def tap_tx(self, y):
        if self.w is None:
            return
        with self.lock:
            if self.txn < 48000 * 2:                    # не копим больше 2 с (если приём встал)
                self.txq.append(y.copy())
                self.txn += len(y)

    # ---- приём: задаёт ход времени; передачу подмешиваем той же длины
    def feed_rx(self, x):
        w = self.w
        if w is None:
            return
        n = len(x)
        with self.lock:
            tx = np.concatenate(self.txq) if self.txq else np.zeros(0, np.float32)
            take, rest = tx[:n], tx[n:]
            self.txq = [rest] if len(rest) else []
            self.txn = len(rest)
        mix = x.astype(np.float32).copy()
        mix[:len(take)] += take
        y = self.dec.process(mix)
        try:
            w.writeframes((np.clip(y, -1, 1) * 32767).astype('<i2').tobytes())
        except Exception:
            self.stop()
            return
        self.frames += len(y)
        if self.frames > MAX_SECONDS * OUT_SR:
            self.stop()

    def elapsed(self):
        return int(time.time() - self.t0) if self.w else 0

    def files(self, n=12):
        try:
            names = sorted((f for f in os.listdir(self.dir) if f.endswith('.wav')), reverse=True)[:n]
        except OSError:
            return []
        out = []
        for f in names:
            p = os.path.join(self.dir, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            out.append({'name': f, 'size': size, 'dur': max(0, (size - 44) // (OUT_SR * 2))})
        return out

    def read(self, name, key):
        """Файл записи для скачивания по ссылке /rec/<имя>?k=<ключ>."""
        if key != self.token or not re.fullmatch(r'[\w,.-]+\.wav', name or ''):
            return None
        p = os.path.join(self.dir, name)
        if not os.path.isfile(p):
            return None
        with open(p, 'rb') as f:
            return f.read()
