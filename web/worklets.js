// Аудио-ворклеты: воспроизведение приёма и захват микрофона.
//
// Приём: поток с сервера (8/16/48 кГц) -> полифазный FIR-апсемплер в целое кратное ->
// кольцевой буфер -> плавная подстройка скорости (±0,5 %) по заполнению буфера, чтобы
// компенсировать расхождение тактовых частот звуковых карт без выбросов и пауз ->
// кубическая интерполяция на частоту звуковой карты браузера.
// Микрофон: частота браузера -> FIR-фильтр -> прореживание до 8/16/48 кГц.

function lowpass(n, cutoff) {            // cutoff — доля от частоты дискретизации (0..0.5)
  const h = new Float32Array(n), m = (n - 1) / 2;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    const t = i - m;
    const sinc = t === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * t) / (Math.PI * t);
    // окно Блэкмана
    const w = 0.42 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1)) + 0.08 * Math.cos(4 * Math.PI * i / (n - 1));
    h[i] = sinc * w; sum += h[i];
  }
  for (let i = 0; i < n; i++) h[i] /= sum;
  return h;
}

class RxPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.gain = 1; this.mute = false;
    this.ring = new Float32Array(sampleRate * 4);
    this.baseMs = 240; this.targetMs = this.baseMs;
    this.setRate(16000);
    this.statT = 0; this.underruns = 0;
    // шумодав: решение принимается по мере поступления звука (синхронно с ним)
    this.gate = { mode: 'radio', busy: true, thr: -60, tx: false, tailMs: 500 };
    this.gGain = 1; this.levelHang = 0; this.tail = 0; this.wasTx = false; this.lvl = -120;
    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.pcm) this.push(this.squelch(m.pcm));
      else if (m.gate) Object.assign(this.gate, m.gate);
      else if (m.rate) this.setRate(m.rate);
      else if (m.bufferMs) this.setBuffer(m.bufferMs);
      else if ('gain' in m) this.gain = m.gain;
      else if ('mute' in m) this.mute = m.mute;
      else if (m.reset) this.reset();
    };
  }
  setRate(rate) {
    this.inRate = rate;
    // целое кратное, ближайшее к частоте браузера (48000/16000 = 3, 44100/16000 -> 3)
    this.L = Math.max(1, Math.round(sampleRate / rate));
    this.midRate = rate * this.L;
    const taps = 32;                                   // отводов на фазу
    this.h = this.L > 1 ? lowpass(taps * this.L, 0.45 / this.L) : null;
    if (this.h) for (let i = 0; i < this.h.length; i++) this.h[i] *= this.L;
    this.taps = taps;
    this.hist = new Float32Array(taps);
    this.hpos = 0;
    this.target = Math.round(this.midRate * this.targetMs / 1000);
    this.reset();
  }
  setBuffer(ms) {
    if (![120, 240, 400].includes(ms)) return;
    this.baseMs = ms;
    this.targetMs = ms;
    this.target = Math.round(this.midRate * ms / 1000);
    this.reset();
  }
  reset() {
    this.stableFrames = 0;
    this.w = 0; this.r = 0; this.fill = 0; this.frac = 0;
    this.buffering = true; this.err = 0;
  }
  squelch(pcm) {
    const g = this.gate, n = pcm.length, rate = this.inRate;
    let s = 0; for (let i = 0; i < n; i++) s += pcm[i] * pcm[i];
    this.lvl = 10 * Math.log10(s / n + 1e-12);
    // хвост после передачи: держим закрытым tailMs после окончания TX
    if (g.tx) { this.wasTx = true; this.tail = 0; }
    else if (this.wasTx) { this.wasTx = false; this.tail = Math.round(g.tailMs / 1000 * rate); }
    let hold = g.tx;
    if (!hold && this.tail > 0) { hold = true; this.tail -= n; }
    let open;
    if (g.mode === 'off') open = true;
    else if (g.mode === 'radio') open = g.busy !== false;
    else {                                            // по уровню, с удержанием 400 мс
      if (this.lvl > g.thr) this.levelHang = Math.round(0.4 * rate);
      else this.levelHang = Math.max(0, this.levelHang - n);
      open = this.levelHang > 0;
    }
    const target = (open && !hold) ? 1 : 0;
    if (target === 1 && this.gGain === 1) return pcm;
    if (target === 0 && this.gGain === 0) return new Float32Array(n);
    const step = 1 / (0.006 * rate);                  // плавно за 6 мс, без щелчков
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      this.gGain = target > this.gGain ? Math.min(1, this.gGain + step) : Math.max(0, this.gGain - step);
      out[i] = pcm[i] * this.gGain;
    }
    return out;
  }
  _put(v) {
    this.ring[this.w] = v; this.w = (this.w + 1) % this.ring.length; this.fill++;
  }
  push(pcm) {
    const L = this.L, R = this.ring.length;
    if (this.fill + pcm.length * L >= R - 8) this.reset();      // переполнение — сброс
    if (!this.h) { for (let i = 0; i < pcm.length; i++) this._put(pcm[i]); }
    else {
      const T = this.taps, h = this.h, hist = this.hist;
      for (let i = 0; i < pcm.length; i++) {
        this.hpos = (this.hpos + T - 1) % T; hist[this.hpos] = pcm[i];
        for (let p = 0; p < L; p++) {
          let acc = 0;
          for (let k = 0; k < T; k++) acc += h[k * L + p] * hist[(this.hpos + k) % T];
          this._put(acc);
        }
      }
    }
    if (this.fill > this.midRate * 0.8) {              // накопилось больше 0,8 с — резко догоняем
      const drop = this.fill - this.target;
      this.r = (this.r + drop) % R; this.fill -= drop;
    }
  }
  process(_, outputs) {
    const out = outputs[0][0], R = this.ring.length;
    if (this.buffering) {
      if (this.fill >= this.target) this.buffering = false;
      else { out.fill(0); for (let c = 1; c < outputs[0].length; c++) outputs[0][c].fill(0); return true; }
    }
    // подстройка скорости по заполнению буфера (сглаженно)
    const e = (this.fill - this.target) / this.target;
    this.err += (e - this.err) * 0.02;
    const adj = 1 + Math.max(-0.005, Math.min(0.005, this.err * 0.005));
    const step = this.midRate / sampleRate * adj;
    const g = this.mute ? 0 : this.gain;
    const ring = this.ring;
    for (let i = 0; i < out.length; i++) {
      if (this.fill < 4) {
        out.fill(0, i); this.buffering = true; this.underruns++;
        // Repeated gaps need more reserve, rather than identical short refills.
        this.targetMs = Math.min(600, this.targetMs + 40);
        this.target = Math.round(this.midRate * this.targetMs / 1000);
        this.stableFrames = 0;
        break;
      }
      const r = this.r, t = this.frac;
      const y0 = ring[(r + R - 1) % R], y1 = ring[r], y2 = ring[(r + 1) % R], y3 = ring[(r + 2) % R];
      // кубическая интерполяция (Катмулл-Ром)
      const a = -0.5 * y0 + 1.5 * y1 - 1.5 * y2 + 0.5 * y3;
      const b = y0 - 2.5 * y1 + 2 * y2 - 0.5 * y3;
      const c = -0.5 * y0 + 0.5 * y2;
      out[i] = (((a * t + b) * t + c) * t + y1) * g;
      this.frac += step;
      while (this.frac >= 1) { this.frac -= 1; this.r = (this.r + 1) % R; this.fill--; }
    }
    for (let ch = 1; ch < outputs[0].length; ch++) outputs[0][ch].set(out);
    if (!this.buffering) {
      this.stableFrames += out.length;
      // Give latency back slowly only after a full minute without starvation.
      if (this.stableFrames >= sampleRate * 60) {
        this.stableFrames = 0;
        this.targetMs = Math.max(this.baseMs, this.targetMs - 20);
        this.target = Math.round(this.midRate * this.targetMs / 1000);
      }
    }
    this.statT += out.length;
    if (this.statT >= sampleRate) {
      this.statT = 0;
      this.port.postMessage({ stat: { ms: Math.round(this.fill / this.midRate * 1000), under: this.underruns, target: this.targetMs, adj } });
    }
    if (this.statT % 4800 < out.length) {             // ~10 раз в секунду: уровень и состояние шумодава
      this.port.postMessage({ sq: { lvl: this.lvl, open: this.gGain > 0.5 } });
    }
    return true;
  }
}
registerProcessor('rx-player', RxPlayer);

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.setRate(16000);
    this.port.onmessage = (e) => { if (e.data.rate) this.setRate(e.data.rate); };
  }
  setRate(rate) {
    this.dst = rate;
    this.frame = new Float32Array(Math.round(rate / 50));   // 20 мс
    this.n = 0; this.sum = 0; this.cnt = 0;
    this.M = sampleRate / rate;
    this.integer = Math.abs(this.M - Math.round(this.M)) < 1e-6;
    if (this.integer) {
      this.M = Math.round(this.M);
      this.h = this.M > 1 ? lowpass(32 * this.M, 0.45 / this.M) : null;
      this.hist = new Float32Array(this.h ? this.h.length : 1);
      this.hpos = 0; this.phase = 0;
    } else {
      // нецелое отношение (например 44100 -> 16000): ФНЧ + линейная интерполяция
      this.h = lowpass(63, 0.45 * rate / sampleRate);
      this.hist = new Float32Array(this.h.length);
      this.hpos = 0; this.pos = 0; this.prev = 0;
    }
  }
  _emit(v) {
    this.frame[this.n++] = v;
    if (this.n === this.frame.length) {
      this.port.postMessage({ pcm: this.frame.slice(0), rms: Math.sqrt(this.sum / Math.max(1, this.cnt)) });
      this.n = 0; this.sum = 0; this.cnt = 0;
    }
  }
  _filt(x) {                              // свёртка с историей
    const h = this.h, N = h.length, hist = this.hist;
    this.hpos = (this.hpos + N - 1) % N; hist[this.hpos] = x;
    let acc = 0;
    for (let k = 0; k < N; k++) acc += h[k] * hist[(this.hpos + k) % N];
    return acc;
  }
  process(inputs) {
    const inp = inputs[0] && inputs[0][0];
    if (!inp) return true;
    for (let i = 0; i < inp.length; i++) {
      const x = inp[i];
      this.sum += x * x; this.cnt++;
      if (this.integer) {
        if (!this.h) { this._emit(x); continue; }
        // фильтруем только те отсчёты, что пойдут на выход
        const N = this.h.length;
        this.hpos = (this.hpos + N - 1) % N; this.hist[this.hpos] = x;
        if (++this.phase >= this.M) {
          this.phase = 0;
          let acc = 0;
          for (let k = 0; k < N; k++) acc += this.h[k] * this.hist[(this.hpos + k) % N];
          this._emit(acc);
        }
      } else {
        const y = this._filt(x), step = sampleRate / this.dst;
        while (this.pos <= 1) { this._emit(this.prev + (y - this.prev) * this.pos); this.pos += step; }
        this.pos -= 1; this.prev = y;
      }
    }
    return true;
  }
}
registerProcessor('mic-capture', MicCapture);
