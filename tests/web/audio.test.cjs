const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function player(rate = 48000, deviceRate = 48000) {
  const processors = {};
  const scope = vm.createContext({
    sampleRate: deviceRate,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage() {} }; } },
    registerProcessor: (name, ctor) => { processors[name] = ctor; },
  });
  vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../web/worklets.js'), 'utf8'), scope);
  const rx = new processors['rx-player']();
  rx.setRate(rate);
  rx.gate.mode = 'off';
  return rx;
}
function render(rx, frames) {
  const channels = [new Float32Array(frames), new Float32Array(frames)];
  assert.equal(rx.process([], [channels]), true);
  assert.deepEqual(channels[0], channels[1]);
  for (const value of channels[0]) assert.ok(Number.isFinite(value));
  return channels[0];
}
function feed(rx, ms) {
  rx.push(new Float32Array(Math.round(rx.inRate * ms / 1000)).fill(0.2));
}

test('balanced buffer tolerates a 180 ms delivery gap that exhausts low latency mode', () => {
  for (const [ms, expectedUnderruns] of [[120, 1], [240, 0], [400, 0]]) {
    const rx = player();
    rx.setBuffer(ms);
    feed(rx, ms + 20);
    render(rx, 480); // begin normal playback
    for (let t = 0; t < 18; t++) render(rx, 480); // no packets for 180 ms
    assert.equal(rx.underruns, expectedUnderruns, `profile ${ms}`);
  }
});
test('repeated starvation increases reserve with a 600 ms ceiling', () => {
  const rx = player();
  rx.setBuffer(120);
  for (let n = 0; n < 15; n++) {
    const target = rx.targetMs;
    feed(rx, target + 20);
    render(rx, Math.ceil(rx.fill + 20));
    assert.equal(rx.buffering, true);
    assert.equal(rx.targetMs, Math.min(600, target + 40));
  }
  assert.equal(rx.targetMs, 600);
});
test('recovery waits for the larger reserve; changing quality preserves the profile', () => {
  const rx = player();
  rx.setBuffer(240);
  feed(rx, 260); render(rx, 14000);
  assert.equal(rx.targetMs, 280);
  rx.reset();
  feed(rx, 240);
  assert.ok(render(rx, 128).every(x => x === 0));
  assert.equal(rx.buffering, true);
  feed(rx, 60); render(rx, 128);
  assert.equal(rx.buffering, false);
  rx.setRate(8000);
  assert.equal(rx.targetMs, 280);
  assert.equal(rx.target, Math.round(rx.midRate * 0.28));
  rx.setBuffer(400);
  assert.equal(rx.targetMs, 400);
  assert.equal(rx.baseMs, 400);
});
test('stable playback lowers reserve gradually and never below the chosen profile', () => {
  const rx = player();
  rx.targetMs = 320; rx.target = 15360;
  for (const expected of [300, 280, 260, 240, 240]) {
    rx.reset();
    feed(rx, 400);
    rx.stableFrames = 48000 * 60 - 128;
    render(rx, 128);
    assert.equal(rx.targetMs, expected);
  }
});
for (const deviceRate of [44100, 48000]) for (const rate of [8000, 16000, 48000]) {
  test(`PCM ${rate} on ${deviceRate} Hz output retains continuous buffered playback`, () => {
    const rx = player(rate, deviceRate);
    feed(rx, 260);
    for (let n = 0; n < 50; n++) {
      feed(rx, 20);
      render(rx, Math.round(deviceRate * 0.02));
    }
    assert.equal(rx.underruns, 0);
    assert.ok(rx.fill > 0 && rx.fill < rx.ring.length);
  });
}
