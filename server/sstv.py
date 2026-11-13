"""
SSTV — приём картинок: Martin M1/M2, Scottie S1/S2, Robot 36, PD 90/120/180.

Яркость передаётся частотой: 1500 Гц — чёрный, 2300 Гц — белый, 1200 Гц — синхроимпульсы.
Перед картинкой идёт код VIS (вид картинки). Если начало пропущено, вид узнаётся по периоду
синхроимпульсов. Наклон (расхождение часов) исправляется по синхроимпульсам каждой строки,
расстройка — по «лидеру» 1900 Гц перед VIS.

Тайминги — по N7CXI (2000) «Proposal for SSTV Mode Specifications».
"""

from i18n import tr
import time

import numpy as np

from audio import Decimator, SR

FS = 12000


class Mode:
    def __init__(self, name, vis, sync, porch, septr, px, line, w, h, enc, rows_per_line=1):
        self.name, self.vis, self.sync, self.porch, self.septr = name, vis, sync, porch, septr
        self.px, self.line, self.w, self.h, self.enc = px, line, w, h, enc
        self.rows_per_line = rows_per_line            # PD: 2 строки картинки за один период
        L = px * w
        if enc == 'GBR' and name.startswith('Scottie'):
            self.chan = [(septr, L), (2 * septr + L, L), (2 * septr + 2 * L + sync + porch, L)]
            self.sync_off = 2 * septr + 2 * L          # синхроимпульс — в середине строки, перед R
            self.first_sync = True
        elif enc == 'GBR':
            s0 = sync + porch
            self.chan = [(s0, L), (s0 + L + septr, L), (s0 + 2 * (L + septr), L)]
            self.sync_off, self.first_sync = 0.0, False
        elif enc == 'R36':
            s0 = sync + porch
            self.chan = [(s0, 2 * L), (s0 + 2 * L + septr, L)]
            self.sync_off, self.first_sync = 0.0, False
        else:                                          # PD: Y0, R-Y, B-Y, Y1
            s0 = sync + porch
            self.chan = [(s0 + k * L, L) for k in range(4)]
            self.sync_off, self.first_sync = 0.0, False
        self.frames = h // rows_per_line


MODES = [
    Mode('Martin M1', 44, 4.862e-3, 0.572e-3, 0.572e-3, 0.4576e-3, 446.446e-3, 320, 256, 'GBR'),
    Mode('Martin M2', 40, 4.862e-3, 0.572e-3, 0.572e-3, 0.2288e-3, 226.7986e-3, 320, 256, 'GBR'),
    Mode('Scottie S1', 60, 9e-3, 1.5e-3, 1.5e-3, 0.4320e-3, 428.38e-3, 320, 256, 'GBR'),
    Mode('Scottie S2', 56, 9e-3, 1.5e-3, 1.5e-3, 0.2752e-3, 277.692e-3, 320, 256, 'GBR'),
    Mode('Robot 36', 8, 9e-3, 3e-3, 6e-3, 0.1375e-3, 150e-3, 320, 240, 'R36'),
    Mode('PD 90', 99, 20e-3, 2.08e-3, 0.0, 0.532e-3, 703.04e-3, 320, 256, 'PD', 2),
    Mode('PD 120', 95, 20e-3, 2.08e-3, 0.0, 0.19e-3, 508.48e-3, 640, 496, 'PD', 2),
    Mode('PD 180', 96, 20e-3, 2.08e-3, 0.0, 0.286e-3, 754.24e-3, 640, 496, 'PD', 2),
]
BY_VIS = {m.vis: m for m in MODES}
BY_NAME = {m.name: m for m in MODES}


def _yuv2rgb(y, u, v):
    """Студийный размах (16..235), как у MMSSTV/slowrx."""
    y = 298.082 * (y - 16)
    r = (y + 408.583 * (v - 128)) / 256
    g = (y - 100.291 * (u - 128) - 208.120 * (v - 128)) / 256
    b = (y + 516.411 * (u - 128)) / 256
    return np.clip(np.stack([r, g, b], -1), 0, 255).astype(np.uint8)


def _rgb2yuv(rgb):
    r, g, b = [rgb[..., k].astype(float) for k in range(3)]
    y = 16 + (65.738 * r + 129.057 * g + 25.064 * b) / 256
    u = 128 + (-37.945 * r - 74.494 * g + 112.439 * b) / 256
    v = 128 + (112.439 * r - 94.154 * g - 18.285 * b) / 256
    return y, u, v


# ---------------------------------------------------------------------- генератор (проверка, симулятор)
def sstv_synth(rgb, mode_name='Martin M1', fs=SR, amp=0.3, vis=True):
    m = BY_NAME[mode_name]
    rgb = np.asarray(rgb)
    H = m.h
    seg = []

    def tone(f, dur):
        seg.append((f, dur))

    def pixels(vals, dur):                 # vals 0..255 -> частоты
        seg.append((1500 + np.asarray(vals, float) / 255 * 800, dur))
    if vis:
        tone(1900, 0.3); tone(1200, 0.01); tone(1900, 0.3); tone(1200, 0.03)
        bits = [(m.vis >> k) & 1 for k in range(7)]
        bits.append(sum(bits) & 1)
        for b in bits:
            tone(1100 if b else 1300, 0.03)
        tone(1200, 0.03)
    L = m.px * m.w
    if m.enc == 'GBR':
        if m.first_sync:
            tone(1200, m.sync)
        for i in range(H):
            G, B, R = rgb[i, :, 1], rgb[i, :, 2], rgb[i, :, 0]
            if m.first_sync:                                   # Scottie
                tone(1500, m.septr); pixels(G, L); tone(1500, m.septr); pixels(B, L)
                tone(1200, m.sync); tone(1500, m.porch); pixels(R, L)
            else:                                              # Martin
                tone(1200, m.sync); tone(1500, m.porch); pixels(G, L); tone(1500, m.septr)
                pixels(B, L); tone(1500, m.septr); pixels(R, L); tone(1500, m.septr)
    elif m.enc == 'R36':
        y, u, v = _rgb2yuv(rgb)
        for i in range(H):
            tone(1200, m.sync); tone(1500, m.porch); pixels(y[i], 2 * L)
            even = i % 2 == 0
            tone(1500 if even else 2300, 4.5e-3); tone(1900, 1.5e-3)
            pixels((v if even else u)[i], L)
    else:                                                      # PD
        y, u, v = _rgb2yuv(rgb)
        for k in range(m.frames):
            a, b = 2 * k, 2 * k + 1
            tone(1200, m.sync); tone(1500, m.porch)
            pixels(y[a], L); pixels((v[a] + v[b]) / 2, L); pixels((u[a] + u[b]) / 2, L); pixels(y[b], L)
    # склейка с точным учётом дробных отсчётов
    out, t_acc, n_acc = [], 0.0, 0
    for f, dur in seg:
        t_acc += dur
        n = int(round(t_acc * fs)) - n_acc
        n_acc += n
        if np.ndim(f) == 0:
            out.append(np.full(n, float(f)))
        else:
            idx = np.minimum((np.arange(n) / n * len(f)).astype(int), len(f) - 1)
            out.append(f[idx])
    fr = np.concatenate(out)
    lead = np.zeros(int(0.3 * fs))
    x = np.sin(2 * np.pi * np.cumsum(fr) / fs) * amp
    return np.concatenate([lead, x, lead]).astype(np.float32)


# ---------------------------------------------------------------------- приёмник
class SstvDecoder:
    def __init__(self, on_event, on_row):
        self.on_event, self.on_row = on_event, on_row
        self.enabled = False
        self.auto = True              # без VIS — по периоду синхроимпульсов
        self.forced = None            # вид, выбранный вручную
        self.dec = Decimator(4)
        h = np.sinc(2 * 1000 / FS * (np.arange(41) - 20)) * np.blackman(41)
        self.lp = (h / h.sum()).astype(np.float32)
        self.reset()

    def reset(self):
        self.ph = 0.0
        self.z_tail = np.zeros(len(self.lp) - 1, np.complex64)
        self.prev = 1 + 0j
        self.f = np.zeros(0, np.float32)
        self.fb = 0                   # абсолютный номер отсчёта f[0]
        self.n = 0                    # всего отсчётов (после децимации)
        self.state = 'idle'
        self.vis_scan = 0
        self.syncs = []               # (время, длительность) синхроимпульсов в режиме ожидания
        self.sync_scan = 0
        self.shift = 0.0              # расстройка, Гц (по лидеру 1900)
        self.mode = None
        self.rows = []
        self.t_info = 0.0

    def configure(self, enabled=None, auto=None, mode=None):
        if auto is not None:
            self.auto = bool(auto)
        if mode is not None:
            self.forced = BY_NAME.get(mode)
        if enabled is not None and bool(enabled) != self.enabled:
            self.enabled = bool(enabled)
            if not self.enabled and self.state == 'image':
                self.finish(tr('приём выключен'))
            self.reset()

    # ---------------------------------------------------------------- звук -> частота
    def process(self, x48):
        if not self.enabled:
            return
        x = self.dec.process(x48)
        n = len(x)
        w = 2 * np.pi * 1900 / FS
        ph = self.ph + w * np.arange(n)
        self.ph = float((self.ph + w * n) % (2 * np.pi))
        z = x * np.exp(-1j * ph)
        zz = np.concatenate([self.z_tail, z.astype(np.complex64)])
        self.z_tail = zz[-(len(self.lp) - 1):]
        zf = np.convolve(zz, self.lp, 'valid')
        dz = zf * np.conj(np.concatenate([[self.prev], zf[:-1]]))
        self.prev = zf[-1] if len(zf) else self.prev
        f = (1900 + np.angle(dz) * FS / (2 * np.pi)).astype(np.float32)
        # пропадание сигнала: амплитуда почти ноль -> частота «шумит»; оставляем как есть
        self.f = np.concatenate([self.f, f])
        self.n += n
        if self.state == 'idle':
            self._scan_vis()
            if self.state == 'idle' and self.auto:
                self._scan_syncs()
        if self.state == 'image':
            self._render()
        # держим в памяти не больше ~12 с
        keep = 12 * FS
        if len(self.f) > keep and self.state == 'idle':
            cut = len(self.f) - keep
            self.f = self.f[cut:]
            self.fb += cut

    def _avg(self, a, b):
        """Средняя частота между абсолютными отсчётами a..b."""
        i, j = int(a - self.fb), int(b - self.fb)
        if i < 0 or j > len(self.f) or j <= i:
            return None
        return float(np.mean(self.f[i:j]))

    # ---------------------------------------------------------------- VIS
    def _scan_vis(self):
        fr = int(0.01 * FS)                                  # кадры по 10 мс
        start = max(self.vis_scan, self.fb)
        end = self.fb + len(self.f) - 60 * fr
        while start <= end:
            a = start
            # стартовый бит 1200 Гц (30 мс), перед ним лидер 1900 (≥ 200 мс)
            sb = self._avg(a, a + 3 * fr)
            if sb is not None and 950 < sb < 1450:
                lead = self._avg(a - 22 * fr, a - 2 * fr)
                # лидер 1900 и стартовый бит 1200: разность 700 Гц держится при любой расстройке
                if lead is not None and abs(lead - 1900) < 250 and abs((lead - sb) - 700) < 70 \
                        and np.std(self.f[int(a - 22 * fr - self.fb):int(a - 2 * fr - self.fb)]) < 120:
                    # уточняем фронт стартового бита по отсчётам
                    i0 = int(a - 3 * fr - self.fb)
                    seg = self.f[max(0, i0):i0 + 6 * fr]
                    edge = np.nonzero(seg < 1550)[0]
                    if len(edge):
                        a = self.fb + max(0, i0) + int(edge[0])
                    shift = lead - 1900
                    bits = []
                    for k in range(8):
                        v = self._avg(a + (3 + 3 * k) * fr + fr // 2, a + (6 + 3 * k) * fr - fr // 2)
                        if v is None:
                            return
                        v -= shift
                        bits.append(1 if v < 1200 else 0)
                    code = sum(b << k for k, b in enumerate(bits[:7]))
                    stop = self._avg(a + 27 * fr + fr // 2, a + 30 * fr - fr // 2)
                    if (sum(bits[:7]) & 1) == bits[7] and code in BY_VIS and stop is not None \
                            and abs(stop - shift - 1200) < 80:
                        self.shift = shift
                        m = self.forced or BY_VIS[code]
                        self._begin(m, a + 30 * fr + (m.sync * FS if m.first_sync else 0), 'VIS')
                        return
                    start = a + 3 * fr
                    continue
            start += fr // 2
        self.vis_scan = max(start, self.fb)

    # ---------------------------------------------------------------- без VIS: по синхроимпульсам
    def _scan_syncs(self):
        i0 = max(self.sync_scan - self.fb, 0)
        seg = self.f[i0:]
        if len(seg) < FS // 10:
            return
        sm = np.convolve(seg - self.shift, np.ones(15, np.float32) / 15, 'same')   # ~1 мс: щелчки шума не рвут импульс
        low = sm < 1350
        d = np.diff(low.astype(np.int8))
        starts = np.nonzero(d == 1)[0] + 1
        ends = np.nonzero(d == -1)[0] + 1
        done = i0
        for s in starts:
            e = ends[ends > s]
            if not len(e):
                break
            dur = (e[0] - s) / FS
            done = i0 + e[0]
            if 0.004 <= dur <= 0.024:
                self.syncs.append((self.fb + i0 + s, dur))
        self.sync_scan = self.fb + done
        self.syncs = self.syncs[-160:]
        if len(self.syncs) < 6:
            return
        for m in ([self.forced] if self.forced else MODES):
            T = m.line * FS
            pts = [t for t, dd in self.syncs if abs(dd - m.sync) < 0.004]
            if len(pts) < 5:
                continue
            # последние импульсы, идущие ровно через период
            chain = [pts[-1]]
            for t in reversed(pts[:-1]):
                k = round((chain[-1] - t) / T)
                if k >= 1 and abs(chain[-1] - t - k * T) < 0.004 * FS and k <= 2:
                    chain.append(t)
            if len(chain) >= 5:
                chain.reverse()
                line0 = chain[0] - m.sync_off * FS
                self._begin(m, line0, tr('синхроимпульсы'))
                return

    # ---------------------------------------------------------------- картинка
    def _begin(self, m, t0, how):
        self.mode = m
        self.state = 'image'
        self.t0 = float(t0)
        self.T = m.line * FS
        self.next_line = 0
        self.sync_pts = []
        self.lost = 0
        self.rows = []
        self.uv = [None, None]                     # Robot 36: последние R-Y и B-Y
        self.on_event({'t': 'sstv_start', 'mode': m.name, 'w': m.w, 'h': m.h, 'how': how,
                       'shift': round(self.shift)})

    def _sync_at(self, k):
        """Ищем синхроимпульс строки k рядом с ожидаемым местом; -> абсолютное время или None."""
        m = self.mode
        exp = self.t0 + k * self.T + m.sync_off * FS
        w = int(0.006 * FS)
        i = int(exp - w - self.fb)
        j = int(exp + w + m.sync * FS - self.fb)
        if i < 0 or j > len(self.f):
            return None
        seg = (self.f[i:j] - self.shift) < 1350
        L = max(2, int(m.sync * FS))
        c = np.convolve(seg.astype(np.float32), np.ones(L, np.float32), 'valid')
        if not len(c) or c.max() < 0.7 * L:
            return None
        return self.fb + i + int(np.argmax(c))

    def _fit(self):
        """Наклон и начало строк по синхроимпульсам: устойчивая оценка (медиана наклонов, Тейл—Сен)."""
        P = self.sync_pts[-48:]
        if len(P) < 6:
            return
        k = np.array([p[0] for p in P], float)
        t = np.array([p[1] for p in P], float)
        i, j = np.triu_indices(len(k), 1)
        dk = k[j] - k[i]
        ok = dk > 0
        b = float(np.median((t[j] - t[i])[ok] / dk[ok]))
        a = float(np.median(t - b * k))
        nominal = self.mode.line * FS
        if abs(b / nominal - 1) < 0.01:
            self.T = b
            self.t0 = a - self.mode.sync_off * FS

    def _pix(self, start, ch):
        m = self.mode
        cs, cl = m.chan[ch]
        a = start + cs * FS
        W = m.w
        edges = a + np.arange(W + 1) * (cl * FS / W)
        i = edges - self.fb
        cum = np.concatenate([[0.0], np.cumsum(self.f - self.shift, dtype=np.float64)])
        lo = np.clip(np.floor(i).astype(int), 0, len(self.f))
        idx = np.arange(len(cum))
        val = np.interp(i, idx, cum)
        width = np.maximum(np.diff(i), 1e-6)
        mean = np.diff(val) / width
        return np.clip((mean - 1500) / 800 * 255, 0, 255)

    def _render(self):
        m = self.mode
        while self.next_line < m.frames:
            k = self.next_line
            line_end = self.t0 + (k + 1) * self.T + 0.01 * FS
            if line_end > self.fb + len(self.f):
                break
            s = self._sync_at(k)
            if s is not None:
                self.sync_pts.append((k, s))
                self.lost = 0
                if len(self.sync_pts) % 3 == 0:
                    self._fit()
            else:
                self.lost += 1
            start = self.t0 + k * self.T
            if m.enc == 'GBR':
                g, b, r = self._pix(start, 0), self._pix(start, 1), self._pix(start, 2)
                self._row(np.stack([r, g, b], -1).astype(np.uint8))
            elif m.enc == 'R36':
                y = self._pix(start, 0)
                c = self._pix(start, 1)
                sep = self._avg(start + (m.chan[1][0] - 5e-3) * FS, start + (m.chan[1][0] - 2e-3) * FS)
                odd = (sep - self.shift) > 1900 if sep is not None else (k % 2 == 1)
                self.uv[0 if odd else 1] = c              # 0 — B-Y (U), 1 — R-Y (V)
                u = self.uv[0] if self.uv[0] is not None else np.full(m.w, 128.0)
                v = self.uv[1] if self.uv[1] is not None else np.full(m.w, 128.0)
                self._row(_yuv2rgb(y, u, v))
            else:
                y0, v, u, y1 = (self._pix(start, c) for c in range(4))
                self._row(_yuv2rgb(y0, u, v))
                self._row(_yuv2rgb(y1, u, v))
            self.next_line += 1
            if self.lost >= 12:
                self.finish(tr('сигнал пропал'))
                return
        if self.state == 'image' and self.next_line >= m.frames:
            self.finish(tr('картинка принята'))
            return
        # освобождаем память за уже отрисованными строками
        keep_from = int(self.t0 + (self.next_line - 2) * self.T) - self.fb
        if keep_from > FS:
            self.f = self.f[keep_from:]
            self.fb += keep_from

    def _row(self, rgb):
        i = len(self.rows)
        self.rows.append(rgb)
        self.on_row(i, rgb)

    def finish(self, why):
        if self.state != 'image':
            return
        m = self.mode
        img = np.stack(self.rows) if self.rows else None
        self.state = 'idle'
        self.syncs = []
        self.sync_scan = self.fb + len(self.f)
        self.vis_scan = self.fb + len(self.f)
        self.on_event({'t': 'sstv_done', 'why': why, 'mode': m.name, 'rows': len(self.rows), 'img': img,
                       'slant': round((self.T / (m.line * FS) - 1) * 1e6)})
