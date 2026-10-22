// Аудио-ворклеты: воспроизведение приёма с джиттер-буфером и захват микрофона.

class RxPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.src = 16000;
    this.buf = new Float32Array(16000 * 4);   // 4 с кольцевой буфер
    this.w = 0; this.r = 0; this.fill = 0;    // в отсчётах исходной частоты
    this.frac = 0;
    this.buffering = true;
    this.gain = 1;
    this.mute = false;
    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.pcm) this.push(m.pcm);
      else if (m.rate) { this.src = m.rate; this.reset(); }
      else if ('gain' in m) this.gain = m.gain;
      else if ('mute' in m) this.mute = m.mute;
      else if (m.reset) this.reset();
    };
  }
  reset() { this.w = this.r = this.fill = 0; this.frac = 0; this.buffering = true; }
  push(pcm) {
    const n = pcm.length, L = this.buf.length;
    if (this.fill + n >= L) this.reset();
    for (let i = 0; i < n; i++) { this.buf[this.w] = pcm[i]; this.w = (this.w + 1) % L; }
    this.fill += n;
    const maxFill = this.src * 0.45, target = this.src * 0.15;
    if (this.fill > maxFill) {               // накопилась задержка — догоняем
      const drop = this.fill - target;
      this.r = (this.r + drop) % L; this.fill -= drop;
    }
  }
  process(_, outputs) {
    const out = outputs[0][0];
    const L = this.buf.length, step = this.src / sampleRate;
    if (this.buffering) {
      if (this.fill >= this.src * 0.1) this.buffering = false;
      else { out.fill(0); return true; }
    }
    const g = this.mute ? 0 : this.gain;
    for (let i = 0; i < out.length; i++) {
      if (this.fill < 2) { out.fill(0, i); this.buffering = true; break; }
      const a = this.buf[this.r], b = this.buf[(this.r + 1) % L];
      out[i] = (a + (b - a) * this.frac) * g;
      this.frac += step;
      while (this.frac >= 1) { this.frac -= 1; this.r = (this.r + 1) % L; this.fill--; }
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}
registerProcessor('rx-player', RxPlayer);

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.dst = 16000;
    this.frame = new Float32Array(320);
    this.n = 0;
    this.pos = 0;
    this.prev = 0;
    this.sum = 0; this.cnt = 0;
    this.port.onmessage = (e) => {
      if (e.data.rate) { this.dst = e.data.rate; this.frame = new Float32Array(this.dst / 50); this.n = 0; }
    };
  }
  process(inputs) {
    const inp = inputs[0] && inputs[0][0];
    if (!inp) return true;
    const step = sampleRate / this.dst;
    for (let i = 0; i < inp.length; i++) {
      const x = inp[i];
      this.sum += x * x; this.cnt++;
      // линейная интерполяция между соседними отсчётами входа
      while (this.pos <= 1) {
        this.frame[this.n++] = this.prev + (x - this.prev) * this.pos;
        if (this.n === this.frame.length) {
          this.port.postMessage({ pcm: this.frame.slice(0), rms: Math.sqrt(this.sum / this.cnt) });
          this.n = 0; this.sum = 0; this.cnt = 0;
        }
        this.pos += step;
      }
      this.pos -= 1;
      this.prev = x;
    }
    return true;
  }
}
registerProcessor('mic-capture', MicCapture);
