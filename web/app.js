'use strict';
/* FTX-1 Пульт — клиент. Один и тот же код для ПК и Android (как PWA). */

// ------------------------------------------------------------------ утилиты
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const store = {
  get(k, d) { try { const v = localStorage.getItem('ftx1.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('ftx1.' + k, JSON.stringify(v)); } catch { } },
};
let toastT;
function toast(msg, ms = 2600) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('show'), ms);
}
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
function interp(table, x) {               // кусочно-линейная калибровка шкал
  if (x <= table[0][0]) return table[0][1];
  for (let i = 1; i < table.length; i++) {
    if (x <= table[i][0]) {
      const [x0, y0] = table[i - 1], [x1, y1] = table[i];
      return y0 + (y1 - y0) * (x - x0) / (x1 - x0);
    }
  }
  return table[table.length - 1][1];
}

// ------------------------------------------------------------------ справочники
const MODES = ['LSB', 'USB', 'CW-U', 'CW-L', 'AM', 'AM-N', 'FM', 'FM-N', 'DATA-U', 'DATA-L', 'DATA-FM', 'RTTY-L', 'RTTY-U', 'PSK'];
const BANDS = [
  ['160', 1810e3, 2000e3, 1850e3, 'LSB'], ['80', 3500e3, 3800e3, 3650e3, 'LSB'], ['60', 5250e3, 5450e3, 5357e3, 'USB'],
  ['40', 7000e3, 7300e3, 7100e3, 'LSB'], ['30', 10100e3, 10150e3, 10120e3, 'CW-U'], ['20', 14000e3, 14350e3, 14200e3, 'USB'],
  ['17', 18068e3, 18168e3, 18130e3, 'USB'], ['15', 21000e3, 21450e3, 21200e3, 'USB'], ['12', 24890e3, 24990e3, 24950e3, 'USB'],
  ['10', 28000e3, 29700e3, 28500e3, 'USB'], ['6', 50000e3, 54000e3, 50150e3, 'USB'], ['4', 69900e3, 70500e3, 70200e3, 'USB'],
  ['AIR', 108e6, 137e6, 118.1e6, 'AM'], ['2', 144e6, 148e6, 145.5e6, 'FM'], ['70', 430e6, 450e6, 433.5e6, 'FM'],
];
// Полосы фильтра по коду WIDTH (по таблицам Yaesu; для FTX-1 — ориентировочно)
const W_SSB = [2400, 300, 400, 600, 850, 1100, 1200, 1500, 1650, 1800, 1950, 2100, 2250, 2400, 2450, 2500, 2600, 2700, 2800, 2900, 3000, 3200, 3500, 4000];
const W_CW = [500, 50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 600, 800, 1200, 1400, 1700, 2000, 2400, 3000, 3200, 3500, 4000, 4000, 4000];
const S_CAL = [[0, 0], [12, 1], [27, 2], [40, 3], [55, 4], [65, 5], [80, 6], [95, 7], [112, 8], [130, 9], [150, 10], [172, 11], [190, 12], [220, 13], [240, 14], [255, 15]];
const PO_CAL = [[0, 0], [10, .8], [50, 8], [100, 26], [150, 54], [200, 92], [250, 140]];
const SWR_CAL = [[0, 1], [12, 1], [39, 1.35], [65, 1.5], [89, 2], [242, 5], [255, 6]];
const MENU_GROUPS = { '01': 'Виды работы', '02': 'CW', '03': 'Работа', '04': 'Дисплей', '05': 'Дата/время', '06': 'APRS', '07': 'APRS маяк', '08': 'APRS фильтр', '09': 'Пресеты', '11': 'Bluetooth' };

const isCW = m => /^CW/.test(m || '');
const isLower = m => /^(LSB|CW-L|DATA-L|RTTY-L)$/.test(m || '');
const isAMFM = m => /^(AM|FM|DATA-FM|C4FM)/.test(m || '');
const pitchHz = () => 300 + (S.key_pitch ?? 30) * 10;
const delayMs = c => c < 6 ? [30, 50, 100, 150, 200, 250][c] : (c - 6) * 100 + 300;
const bandOf = f => BANDS.find(b => f >= b[1] && f <= b[2]);
function widthHz(code, mode) {
  if (/^(AM|FM|DATA-FM|C4FM)/.test(mode || '')) return /AM/.test(mode) ? (mode === 'AM-N' ? 6000 : 9000) : 12000;
  const t = /^(LSB|USB)$/.test(mode) ? W_SSB : W_CW;
  return t[clamp(code | 0, 0, t.length - 1)];
}

// ------------------------------------------------------------------ состояние
const S = {};                       // состояние трансивера (зеркало сервера)
const C = {                         // состояние клиента
  ws: null, authed: false, local: false, info: {},
  rate: store.get('rate', 0), pw: store.get('pw', ''),          // 0 = выбрать по месту (ПК — 48 кГц, сеть — 16 кГц)
  audioOn: false, ac: null, player: null, gain: store.get('vol', 100) / 100,
  mic: null, micNode: null, micGainNode: null, micStream: null,
  pttPressed: false, txOwner: false, pttBusy: false, txStart: 0, keepalive: null,
  lockUntil: {},                    // ключ -> время, до которого не принимаем значение с сервера
  wfMax: store.get('wfMax', 0),          // выбранный обзор водопада, 0 = авто
  span: 4000, bw: 0,                      // действующий обзор и измеренная полоса звука трансивера
};
const opts = Object.assign({ echo: true, ns: true, agc: false, muteTx: true, pttMode: 'hold', space: true, palette: 'classic', micGain: 100, micId: '',
  sqMode: 'radio', sqThr: -60, tail: 500, scale: 'auto' }, store.get('opts', {}));

// ------------------------------------------------------------------ связь
function send(o) { if (C.ws && C.ws.readyState === 1) C.ws.send(JSON.stringify(o)); }
function setParam(k, v) {
  C.lockUntil[k] = performance.now() + 1200;
  S[k] = v;
  send({ t: 'set', k, v });
}
function act(a, arg) { send({ t: 'act', a, arg }); }

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  C.ws = ws;
  ws.onopen = () => send({ t: 'hello', pw: C.pw, rate: C.rate || 16000 });
  ws.onclose = () => {
    C.authed = false; setLink(false, 'нет связи с сервером');
    if (C.txOwner) { C.txOwner = false; updatePtt(); }
    setTimeout(connect, 2000);
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== 'string') return onBinary(new Uint8Array(e.data));
    const m = JSON.parse(e.data);
    switch (m.t) {
      case 'hello_ok':
        C.authed = true; C.local = m.local; C.info = m;
        if (!C.rate) {                                   // первый вход: на этом ПК — максимальное качество
          C.rate = m.local ? 48000 : 16000; $('#optRate').value = C.rate;
          C.player && C.player.port.postMessage({ rate: C.rate });
        }
        $('#login').classList.add('hidden');
        $('#btnPanel').classList.toggle('hidden', !m.local);
        fillSelect($('.ctl[data-k=ctcss] select'), m.ctcss.map((v, i) => [i, v.toFixed(1)]));
        $('#srvInfo').textContent = `Сервер v${m.version}. PTT: ${m.ptt_method}. Ограничение передачи: ${m.max_tx} с.` + (m.audio_err ? ` ЗВУК: ${m.audio_err}` : '');
        setWfMsg(m.audio_err);
        updateSpan(true);
        if (C.audioOn) send({ t: 'audio', on: true, rate: C.rate });
        break;
      case 'auth_fail':
        $('#login').classList.remove('hidden'); $('#loginMsg').textContent = m.msg; break;
      case 'state': applyState(m.s, m.full); break;
      case 'lv': break;
      case 'bw': C.bw = m.hz; updateSpan(); break;
      case 'ptt_owner':
        C.txOwner = m.you; C.pttBusy = m.busy && !m.you;
        if (C.txOwner && !C.txStart) C.txStart = performance.now();
        if (!C.txOwner) C.txStart = 0;
        updatePtt(); pushGate(); break;
      case 'note': toast(m.msg, 4000); break;
      case 'audio_status': setWfMsg(m.err); break;
      case 'raw': catLine(m.line); break;
      case 'menu_list': buildMenu(m.items); break;
      case 'menu_val': setMenuVal(m.ex, m.v); break;
    }
  };
}

function onBinary(b) {
  const type = b[0];
  if (type === 1 && C.player) {
    const n = (b.length - 1) >> 1;
    const dv = new DataView(b.buffer, 1, n * 2);
    const f = new Float32Array(n);
    for (let i = 0; i < n; i++) f[i] = dv.getInt16(i * 2, true) / 32768;
    C.player.port.postMessage({ pcm: f }, [f.buffer]);
  } else if (type === 2) {
    const dv = new DataView(b.buffer);
    const n = dv.getUint16(1, true), binHz = dv.getFloat32(3, true);
    wfRow(b.subarray(7, 7 + n), binHz);
  }
}

function setWfMsg(err) {
  const el = $('#wfMsg');
  el.classList.toggle('hidden', !err);
  if (err) el.innerHTML = `<div><b>Нет звука с трансивера</b>${err.replace(/[&<]/g, c => c === '&' ? '&amp;' : '&lt;')}` +
    (C.local ? '<br><br>Выберите устройства в панели управления сервером (кнопка ⚙ вверху).' : '') + '</div>';
}
function setLink(ok, text) {
  $('#linkDot').className = 'dot ' + (ok ? 'ok' : 'bad');
  if (text) $('#rigInfo').textContent = text;
}

// ------------------------------------------------------------------ отрисовка состояния
function applyState(d, full) {
  const now = performance.now();
  for (const [k, v] of Object.entries(d)) {
    if (!full && C.lockUntil[k] > now) continue;   // пользователь только что крутил — не дёргаем
    S[k] = v;
  }
  if ('connected' in d || 'radio_id' in d || 'fw0' in d || 'pwr_head' in d) {
    const head = S.pwr_head === 2 ? 'Optima/SPA-1' : (S.pwr_head === 1 ? 'Field' : '');
    setLink(S.connected, S.connected ? `${S.radio_id === '0840' ? 'FTX-1' : 'ID ' + (S.radio_id || '?')} ${head} ${S.fw0 ? 'FW ' + S.fw0 : ''}` : 'трансивер не отвечает');
  }
  renderFreq();
  renderMeters();
  renderBadges();
  renderControls(d);
  drawOverlay();
  if ('tuning' in d) $('#tuneState').textContent = S.tuning ? 'Идёт настройка…' : '';
  if ('tx' in d) document.body.classList.toggle('txon', !!S.tx);
  if ('mode' in d) { if (!C.wfMax && !C.bw) updateSpan(); }
  if ('busy' in d || 'tx' in d) pushGate();
  if ('sql' in d) renderSq();
}

function renderBadges() {
  $('#bMode').textContent = S.mode || '—';
  $('#bModeB').textContent = S.mode_b || '—';
  $('#bVfo').textContent = S.vfo ? 'SUB' : 'MAIN';
  $('#bSplit').classList.toggle('off', !S.split);
  $('#bClar').classList.toggle('off', !(S.clar_rx || S.clar_tx));
  $('#bClar').textContent = (S.clar_rx || S.clar_tx) ? `CLAR ${S.clar > 0 ? '+' : ''}${S.clar}` : 'CLAR';
  $('#bLock').classList.toggle('off', !S.lock);
  $('#bTx').classList.toggle('off', !S.tx);
  $('#bBusy').classList.toggle('off', !S.busy);
  $('#freqB').textContent = S.freq_b ? fmtFreq(S.freq_b) : '—';
  $('#btnSplit').classList.toggle('on', !!S.split);
  $('#btnLock').classList.toggle('on', !!S.lock);
  $$('#modes .btn').forEach(b => b.classList.toggle('on', b.dataset.m === S.mode));
  const bd = bandOf(S.freq_a || 0);
  $$('#bands .btn').forEach(b => b.classList.toggle('on', !!bd && b.dataset.b === bd[0]));
  if (S.power != null) $('#pwrInfo').textContent = `${S.power} Вт`;
}

const fmtFreq = f => { const s = String(Math.round(f)).padStart(7, '0'); return `${s.slice(0, -6)}.${s.slice(-6, -3)}.${s.slice(-3)}`; };

function renderFreq() {
  const el = $('#freqA'), f = Math.round(S.freq_a || 0);
  const s = String(f).padStart(7, '0');
  if (el.dataset.f === s) return;
  el.dataset.f = s;
  let html = '';
  for (let i = 0; i < s.length; i++) {
    const p = s.length - 1 - i, mult = 10 ** p;
    const cls = p < 3 ? 'd hz' : 'd';
    html += `<span class="${cls}" data-m="${mult}">${s[i]}</span>`;
    if (p === 6 || p === 3) html += `<span class="sep${p === 3 ? ' hz' : ''}">.</span>`;
  }
  el.innerHTML = html;
}

function setBar(id, frac, text) {
  const m = $(id); m.querySelector('i').style.width = (clamp(frac, 0, 1) * 100).toFixed(1) + '%';
  m.querySelector('.val').textContent = text;
}
function renderMeters() {
  const s = interp(S_CAL, S.smeter || 0);
  const sTxt = s <= 9 ? 'S' + Math.round(s) : `S9+${Math.round((s - 9) * 10)}`;
  setBar('#mS', s / 15, sTxt);
  if (S.tx) {
    let w = interp(PO_CAL, S.po || 0);
    if (S.pwr_head !== 2) w = w / 10;
    const maxW = S.pwr_head === 2 ? 100 : 10;
    setBar('#mPo', w / (maxW * 1.2), `≈${w.toFixed(w < 10 ? 1 : 0)} Вт`);
    const swr = interp(SWR_CAL, S.swr || 0);
    setBar('#mSwr', (swr - 1) / 4, swr.toFixed(1));
    setBar('#mAlc', (S.alc || 0) / 255, Math.round((S.alc || 0) / 2.55) + '%');
  }
}

// ------------------------------------------------------------------ частота: цифры, ручка, ввод
let freqSendT = 0, freqPending = null;
function setFreq(f) {
  f = clamp(Math.round(f), 30000, 470000000);
  S.freq_a = f; renderFreq(); drawOverlay();
  C.lockUntil.freq_a = performance.now() + 700;
  freqPending = f;
  const now = performance.now();
  if (now - freqSendT > 60) flushFreq();
  else setTimeout(flushFreq, 60);
}
function flushFreq() {
  if (freqPending == null) return;
  freqSendT = performance.now();
  send({ t: 'set', k: 'freq_a', v: freqPending });
  freqPending = null;
  rememberBand();
}
const stepHz = () => +$('#step').value;
function nudge(n) { setFreq(Math.round(((S.freq_a || 0) + n * stepHz()) / stepHz()) * stepHz()); }

$('#freqA').addEventListener('wheel', e => {
  const d = e.target.closest('.d'); e.preventDefault();
  const m = d ? +d.dataset.m : stepHz();
  setFreq((S.freq_a || 0) + (e.deltaY < 0 ? m : -m));
}, { passive: false });
$('#freqA').addEventListener('click', e => {
  const d = e.target.closest('.d'); if (!d) return;
  const r = d.getBoundingClientRect(), up = e.clientY < r.top + r.height / 2;
  setFreq((S.freq_a || 0) + (up ? 1 : -1) * +d.dataset.m);
});
$$('[data-step]').forEach(b => b.addEventListener('click', () => nudge(+b.dataset.step)));
$('#step').value = store.get('step', 10);
$('#step').addEventListener('change', () => store.set('step', stepHz()));
$('#freqInput').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  let v = parseFloat(e.target.value.replace(',', '.'));
  if (!isFinite(v)) return;
  if (v < 1000) v *= 1e6; else if (v < 1e6) v *= 1e3;   // МГц / кГц / Гц
  setFreq(v); e.target.value = ''; e.target.blur();
});
// Ручка настройки: перетаскивание и колесо
(() => {
  const k = $('#knob'), dial = $('#knobDial');
  let x0 = null, acc = 0, off = 0;
  k.addEventListener('pointerdown', e => { x0 = e.clientX; acc = 0; k.setPointerCapture(e.pointerId); });
  k.addEventListener('pointermove', e => {
    if (x0 == null) return;
    const dx = e.clientX - x0; x0 = e.clientX; acc += dx; off += dx;
    dial.style.backgroundPosition = `${off}px 0`;
    const n = Math.trunc(acc / 6);
    if (n) { acc -= n * 6; nudge(n); }
  });
  const end = () => { x0 = null; };
  k.addEventListener('pointerup', end); k.addEventListener('pointercancel', end);
  k.addEventListener('wheel', e => { e.preventDefault(); nudge(e.deltaY < 0 ? 1 : -1); off += e.deltaY < 0 ? 6 : -6; dial.style.backgroundPosition = `${off}px 0`; }, { passive: false });
})();
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea')) return;
  if (e.key === 'ArrowUp' || e.key === 'ArrowRight') { nudge(1); e.preventDefault(); }
  if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') { nudge(-1); e.preventDefault(); }
  if (e.key === 'PageUp') { nudge(10); e.preventDefault(); }
  if (e.key === 'PageDown') { nudge(-10); e.preventDefault(); }
});

// ------------------------------------------------------------------ диапазоны и виды работы
const bandMem = store.get('bandMem', {});
function rememberBand() {
  const b = bandOf(S.freq_a || 0);
  if (b) { bandMem[b[0]] = [S.freq_a, S.mode]; store.set('bandMem', bandMem); }
}
for (const b of BANDS) {
  const el = document.createElement('button');
  el.className = 'btn'; el.dataset.b = b[0];
  el.textContent = /^\d+$/.test(b[0]) ? (b[0] === '70' ? '70см' : b[0] + 'м') : b[0];
  el.onclick = () => {
    rememberBand();
    const [f, m] = bandMem[b[0]] || [b[3], b[4]];
    setFreq(f);
    if (m && m !== S.mode) setTimeout(() => setParam('mode', m), 120);
  };
  $('#bands').appendChild(el);
}
for (const m of MODES) {
  const el = document.createElement('button');
  el.className = 'btn'; el.dataset.m = m; el.textContent = m;
  el.onclick = () => { setParam('mode', m); renderBadges(); setTimeout(rememberBand, 300); };
  $('#modes').appendChild(el);
}

// ------------------------------------------------------------------ регуляторы
function fillSelect(sel, pairs) {
  if (!sel) return;
  sel.innerHTML = pairs.map(([v, t]) => `<option value="${v}">${t}</option>`).join('');
}
function fmtVal(el, v) {
  const f = el.dataset.fmt;
  if (v == null) return '—';
  if (f === 'width') return (v == 0 ? 'по умолч. ' : '') + widthHz(v, S.mode) + ' Гц';
  if (f === 'apf') return ((v - 25) * 10) + ' Гц';
  if (f === 'delay') return delayMs(v) + ' мс';
  if (f === 'pitch') return (300 + v * 10) + ' Гц';
  return String(v);
}
function ctlValue(k) {
  if (k === 'preamp') return S['preamp' + bandType()];
  return S[k];
}
const bandType = () => (S.freq_a >= 420e6 ? 2 : S.freq_a >= 144e6 ? 1 : 0);

function initControls() {
  for (const el of $$('.ctl')) {
    const k = el.dataset.k, label = el.dataset.label;
    if (el.classList.contains('tog')) {
      el.innerHTML = `<span>${label}</span><span class="sw"></span>`;
      el.onclick = () => { const v = ctlValue(k) ? 0 : 1; setParam(k, v); renderControls({ [k]: v }); };
    } else if (el.classList.contains('sel')) {
      el.innerHTML = `<div class="hd"><span>${label}</span></div><select></select>`;
      const sel = $('select', el);
      const o = el.dataset.opts;
      if (o === 'modes') fillSelect(sel, MODES.map(m => [m, m]));
      else if (o !== 'ctcss') fillSelect(sel, o.split(',').map(p => p.split(':')));
      sel.onchange = () => setParam(k, isNaN(sel.value) ? sel.value : +sel.value);
    } else {
      el.innerHTML = `<div class="hd"><span>${label}</span><b>—</b></div><input type="range" min="${el.dataset.min}" max="${el.dataset.max}" step="${el.dataset.step || 1}">`;
      const r = $('input', el), b = $('b', el);
      let t = 0;
      r.oninput = () => {
        const v = +r.value; b.textContent = fmtVal(el, v);
        C.lockUntil[k] = performance.now() + 1200;
        const now = performance.now();
        if (now - t > 90) { t = now; setParam(k, v); }
        clearTimeout(r._t); r._t = setTimeout(() => setParam(k, +r.value), 120);
      };
      if (el.dataset.reset != null) b.ondblclick = () => { r.value = el.dataset.reset; r.oninput(); };
      if (el.dataset.reset != null) b.title = 'Двойной клик — сброс';
    }
  }
}
function renderControls(d) {
  for (const el of $$('.ctl')) {
    const k = el.dataset.k;
    if (d && !(k in d) && !(k === 'preamp' && Object.keys(d).some(x => x.startsWith('preamp') || x === 'freq_a')) &&
        !(k === 'width' && 'mode' in d) && !(k === 'power' && 'pwr_head' in d)) continue;
    const v = ctlValue(k);
    el.classList.toggle('stale', v == null);
    if (el.classList.contains('tog')) { el.classList.toggle('on', !!v); continue; }
    if (el.classList.contains('sel')) {
      const sel = $('select', el);
      if (k === 'preamp') {
        const want = bandType() ? 'vhf' : 'hf';
        if (sel.dataset.kind !== want) { sel.dataset.kind = want; fillSelect(sel, want === 'hf' ? [[0, 'IPO'], [1, 'AMP1'], [2, 'AMP2']] : [[0, 'Выкл'], [1, 'Вкл']]); }
      }
      if (v != null && document.activeElement !== sel) sel.value = v;
      continue;
    }
    const r = $('input', el), b = $('b', el);
    if (k === 'power') {
      const spa = S.pwr_head === 2;
      r.min = spa ? 5 : 0.5; r.max = spa ? 100 : 10; r.step = spa ? 1 : 0.5;
    }
    if (v != null && !(C.lockUntil[k] > performance.now())) r.value = v;
    b.textContent = fmtVal(el, v);
  }
}
$$('[data-act]').forEach(b => b.addEventListener('click', () => act(b.dataset.act)));
$$('[data-set]').forEach(b => b.addEventListener('click', () => { const [k, v] = b.dataset.set.split(':'); setParam(k, +v); }));
$('#btnSplit').onclick = () => setParam('split', S.split ? 0 : 1);
$('#btnLock').onclick = () => setParam('lock', S.lock ? 0 : 1);
$('#btnMemCh').onclick = () => act('mem_ch', +$('#memCh').value);

// ------------------------------------------------------------------ водопад
const wf = { canvas: $('#wf'), spec: $('#spec'), ov: $('#overlay'), lut: null, avg: null, binHz: 11.71875, n: 0, rows: 0 };
const PALETTES = {
  classic: [[0, 0, 0, 0], [0.15, 0, 0, 90], [0.35, 0, 110, 200], [0.55, 0, 220, 200], [0.72, 240, 230, 0], [0.88, 255, 80, 0], [1, 255, 255, 255]],
  yaesu: [[0, 0, 4, 20], [0.3, 0, 40, 120], [0.6, 30, 140, 255], [0.85, 160, 230, 255], [1, 255, 255, 255]],
  gray: [[0, 0, 0, 0], [1, 255, 255, 255]],
  fire: [[0, 0, 0, 0], [0.3, 90, 0, 0], [0.6, 230, 80, 0], [0.85, 255, 210, 40], [1, 255, 255, 230]],
};
function buildLut() {
  const floor = -130 + +$('#wfFloor').value * 0.5, range = +$('#wfRange').value;
  const pal = PALETTES[opts.palette] || PALETTES.classic;
  wf.lut = new Uint32Array(256);
  for (let v = 0; v < 256; v++) {
    const db = v / 2 - 130, x = clamp((db - floor) / range, 0, 1);
    let i = 1; while (i < pal.length - 1 && pal[i][0] < x) i++;
    const [x0, r0, g0, b0] = pal[i - 1], [x1, r1, g1, b1] = pal[i];
    const t = (x - x0) / ((x1 - x0) || 1);
    const r = r0 + (r1 - r0) * t, g = g0 + (g1 - g0) * t, b = b0 + (b1 - b0) * t;
    wf.lut[v] = (255 << 24) | (b << 16) | (g << 8) | r;   // little-endian RGBA
  }
  store.set('wfFloor', +$('#wfFloor').value); store.set('wfRange', +$('#wfRange').value);
}
$('#wfFloor').value = store.get('wfFloor', 70); $('#wfRange').value = store.get('wfRange', 80);
$('#wfFloor').oninput = buildLut; $('#wfRange').oninput = buildLut;

function sizeCanvases() {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  for (const c of [wf.spec, wf.ov]) { c.width = Math.round(c.clientWidth * dpr); c.height = Math.round(c.clientHeight * dpr); }
  wf.rows = Math.max(50, Math.round(wf.canvas.clientHeight));
  drawOverlay();
}
function wfRow(row, binHz) {
  if (!wf.lut) buildLut();
  const n = row.length;
  wf.binHz = binHz;
  const cv = wf.canvas, ctx = cv.getContext('2d');
  if (cv.width !== n || cv.height !== wf.rows) {
    // при смене размера сохраняем накопленную картинку
    const keep = cv.width && cv.height ? Object.assign(document.createElement('canvas'), { width: cv.width, height: cv.height }) : null;
    if (keep) keep.getContext('2d').drawImage(cv, 0, 0);
    cv.width = n; cv.height = wf.rows; wf.avg = null; wf.img = ctx.createImageData(n, 1);
    if (keep) ctx.drawImage(keep, 0, 0, keep.width, keep.height, 0, 0, n, keep.height);
  }
  ctx.drawImage(cv, 0, 0, n, cv.height - 1, 0, 1, n, cv.height - 1);
  const px = new Uint32Array(wf.img.data.buffer);
  for (let i = 0; i < n; i++) px[i] = wf.lut[row[i]];
  ctx.putImageData(wf.img, 0, 0);
  // спектр — со сглаживанием
  if (!wf.avg || wf.avg.length !== n) wf.avg = Float32Array.from(row);
  for (let i = 0; i < n; i++) wf.avg[i] = wf.avg[i] * 0.6 + row[i] * 0.4;
  drawSpectrum();
}
function drawSpectrum() {
  const c = wf.spec, ctx = c.getContext('2d'), W = c.width, H = c.height, a = wf.avg;
  ctx.clearRect(0, 0, W, H);
  if (!a) return;
  const floor = -130 + +$('#wfFloor').value * 0.5, range = +$('#wfRange').value;
  const span = C.span;
  ctx.beginPath();
  for (let x = 0; x < W; x++) {
    const hz = x / W * span, i = Math.min(a.length - 1, Math.round(hz / wf.binHz));
    const y = H - clamp((a[i] / 2 - 130 - floor) / range, 0, 1) * (H - 4);
    x ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.lineTo(W, H); ctx.lineTo(0, H); ctx.closePath();
  ctx.fillStyle = 'rgba(54,197,240,.18)'; ctx.fill();
  ctx.strokeStyle = '#7fe3ff'; ctx.lineWidth = Math.max(1, W / 900); ctx.stroke();
}
// Наложение: шкала, полоса фильтра, режектор, тон CW
function passband() {
  const m = S.mode || '', bw = widthHz(S.width || 0, m), sh = S.ifshift_on ? (S.ifshift || 0) : 0;
  if (isCW(m)) return [pitchHz() - bw / 2 + sh, pitchHz() + bw / 2 + sh];
  if (isAMFM(m)) return [0, bw / 2];
  const center = bw <= 2400 ? 1500 : 300 + bw / 2;
  return [Math.max(0, center - bw / 2 + sh), center + bw / 2 + sh];
}
function drawOverlay() {
  const c = wf.ov; if (!c.width) return;
  const ctx = c.getContext('2d'), W = c.width, H = c.height, span = C.span, X = hz => hz / span * W;
  const dpr = W / c.clientWidth;
  ctx.clearRect(0, 0, W, H);
  const [lo, hi] = passband();
  ctx.fillStyle = 'rgba(255,255,255,.07)'; ctx.fillRect(X(lo), 0, X(hi) - X(lo), H);
  ctx.strokeStyle = 'rgba(255,211,77,.7)'; ctx.lineWidth = dpr;
  ctx.beginPath(); ctx.moveTo(X(lo), 0); ctx.lineTo(X(lo), H); ctx.moveTo(X(hi), 0); ctx.lineTo(X(hi), H); ctx.stroke();
  if (isCW(S.mode)) { ctx.strokeStyle = '#ff5a4d'; ctx.beginPath(); ctx.moveTo(X(pitchHz()), 0); ctx.lineTo(X(pitchHz()), H); ctx.stroke(); }
  if (S.notch) { ctx.strokeStyle = '#ff7ad9'; ctx.setLineDash([4 * dpr, 4 * dpr]); ctx.beginPath(); ctx.moveTo(X(S.notch_freq), 0); ctx.lineTo(X(S.notch_freq), H); ctx.stroke(); ctx.setLineDash([]); }
  // шкала: Гц звука + частота в эфире
  const stepK = span <= 4000 ? 500 : span <= 6000 ? 1000 : span <= 12000 ? 2000 : 4000;
  ctx.font = `${11 * dpr}px Consolas, monospace`; ctx.fillStyle = 'rgba(220,235,245,.75)';
  ctx.strokeStyle = 'rgba(255,255,255,.12)';
  for (let hz = stepK; hz < span; hz += stepK) {
    const x = X(hz);
    ctx.beginPath(); ctx.moveTo(x, H * 0.28 - 6 * dpr); ctx.lineTo(x, H * 0.28); ctx.stroke();
    ctx.fillText((hz / 1000).toFixed(stepK < 1000 ? 1 : 0) + 'k', x + 3 * dpr, H * 0.28 - 4 * dpr);
  }
}
function audioToRf(a) {
  const f = S.freq_a || 0, m = S.mode;
  if (isCW(m)) return isLower(m) ? f - (a - pitchHz()) : f + (a - pitchHz());
  if (isAMFM(m)) return f;
  return isLower(m) ? f - a : f + a;
}
function tuneToAudio(a) {
  const m = S.mode, f = S.freq_a || 0;
  if (isAMFM(m)) { toast('В AM/FM подстройка по водопаду недоступна'); return; }
  const target = isCW(m) ? pitchHz() : 1500;
  setFreq(Math.round((isLower(m) ? f - (a - target) : f + (a - target)) / 10) * 10);
}
(() => {
  const w = $('#wfWrap'), cur = $('#wfCursor');
  const aAt = e => { const r = w.getBoundingClientRect(); return clamp((e.clientX - r.left) / r.width, 0, 1) * C.span; };
  w.addEventListener('pointermove', e => {
    const a = aAt(e), r = w.getBoundingClientRect();
    cur.style.display = 'block'; cur.style.left = (e.clientX - r.left) + 'px';
    cur.dataset.f = `${Math.round(a)} Гц · ${fmtFreq(audioToRf(a))}`;
  });
  w.addEventListener('pointerleave', () => { cur.style.display = 'none'; });
  w.addEventListener('dblclick', e => tuneToAudio(aAt(e)));
  let lastTap = 0;
  w.addEventListener('pointerup', e => {           // двойной тап на телефоне
    if (e.pointerType !== 'touch') return;
    const now = performance.now();
    if (now - lastTap < 350) tuneToAudio(aAt(e));
    lastTap = now;
  });
  w.addEventListener('wheel', e => { e.preventDefault(); nudge(e.deltaY < 0 ? 1 : -1); }, { passive: false });
})();
// Обзор водопада. «Авто» — по измеренной полосе звука трансивера (в FM она шире, чем в SSB),
// пока не измерена — по виду работы.
function autoSpan() {
  if (C.bw) return clamp(Math.ceil(C.bw * 1.05 / 500) * 500, 2000, 24000);
  const m = S.mode || '';
  return /FM|C4FM/.test(m) ? 9000 : /^AM/.test(m) ? 6000 : isCW(m) ? 3000 : 4000;
}
function updateSpan(force) {
  const span = C.wfMax || autoSpan();
  $$('#spanSeg button').forEach(x => x.classList.toggle('on', +x.dataset.v === C.wfMax));
  const ab = $('#spanSeg button[data-v="0"]');
  ab.textContent = C.wfMax ? 'Авто' : `Авто ${(span / 1000).toFixed(span % 1000 ? 1 : 0)}k`;
  if (span === C.span && !force) return;
  C.span = span; wf.canvas.width = 0;
  send({ t: 'wf', on: true, max: span }); drawOverlay();
}
$$('#spanSeg button').forEach(b => {
  b.onclick = () => { C.wfMax = +b.dataset.v; store.set('wfMax', C.wfMax); updateSpan(true); };
});
window.addEventListener('resize', sizeCanvases);

// ------------------------------------------------------------------ шумодав
function pushGate() {
  if (!C.player) return;
  C.player.port.postMessage({ gate: {
    mode: opts.sqMode, busy: S.busy === undefined ? true : !!S.busy, thr: opts.sqThr,
    tx: !!S.tx || (C.pttPressed && C.txOwner), tailMs: opts.tail,
  } });
}
function renderSq() {
  $$('#sqMode button').forEach(b => b.classList.toggle('on', b.dataset.v === opts.sqMode));
  $('#sqRadioCtl').classList.toggle('hidden', opts.sqMode === 'level');
  $('#sqLevelCtl').classList.toggle('hidden', opts.sqMode !== 'level');
  if (S.sql != null && !(C.lockUntil.sql > performance.now())) $('#sqRadio').value = S.sql;
  $('#sqRadioV').textContent = S.sql ?? '—';
  $('#sqThr').value = opts.sqThr; $('#sqThrV').textContent = opts.sqThr + ' дБ';
}
$$('#sqMode button').forEach(b => b.onclick = () => { opts.sqMode = b.dataset.v; saveOpts(); renderSq(); pushGate(); });
$('#sqThr').oninput = e => { opts.sqThr = +e.target.value; saveOpts(); renderSq(); pushGate(); };
$('#sqRadio').oninput = e => {
  $('#sqRadioV').textContent = e.target.value;
  clearTimeout(e.target._t); e.target._t = setTimeout(() => setParam('sql', +e.target.value), 80);
  C.lockUntil.sql = performance.now() + 1200;
};

// ------------------------------------------------------------------ звук приёма
async function ensureAudioContext() {
  if (C.ac) { if (C.ac.state === 'suspended') await C.ac.resume(); return C.ac; }
  const AC = window.AudioContext || window.webkitAudioContext;
  let ac;
  try { ac = new AC({ latencyHint: 'interactive', sampleRate: 48000 }); }   // 48 кГц — как у трансивера
  catch { ac = new AC({ latencyHint: 'interactive' }); }
  await ac.audioWorklet.addModule('worklets.js');
  C.ac = ac;
  C.player = new AudioWorkletNode(ac, 'rx-player', { outputChannelCount: [2] });
  C.player.connect(ac.destination);
  C.player.port.postMessage({ rate: C.rate || 16000 });
  C.player.port.onmessage = e => {
    if (e.data.sq) {
      const q = e.data.sq;
      $('#sqLamp').className = 'sq-lamp ' + (C.audioOn ? (q.open ? 'open' : 'closed') : '');
      $('#sqLvl').textContent = C.audioOn ? `${Math.round(q.lvl)} дБ` : '';
      return;
    }
    const s = e.data.stat; if (!s) return;
    $('#audioStat').textContent = `Приём: ${C.rate / 1000} кГц, звуковая карта ${ac.sampleRate / 1000} кГц, буфер ${s.ms} мс, ` +
      `пропусков ${s.under}, подстройка ${((s.adj - 1) * 100).toFixed(2)} %`;
  };
  C.player.port.postMessage({ gain: C.gain });
  pushGate();
  return ac;
}
$('#btnAudio').onclick = async () => {
  try {
    await ensureAudioContext();
  } catch (e) { toast('Звук недоступен: ' + e.message); return; }
  C.audioOn = !C.audioOn;
  C.player.port.postMessage({ reset: true });
  send({ t: 'audio', on: C.audioOn, rate: C.rate });
  $('#btnAudio').textContent = C.audioOn ? '🔊 Звук' : '🔇 Звук';
  $('#btnAudio').classList.toggle('on', !!C.audioOn);
  store.set('audioOn', C.audioOn);
};
$('#volume').value = C.gain * 100;
$('#volume').oninput = e => { C.gain = e.target.value / 100; store.set('vol', +e.target.value); C.player && C.player.port.postMessage({ gain: C.gain }); };

// ------------------------------------------------------------------ микрофон и PTT
async function startMic() {
  if (C.micStream) return true;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast('Микрофон недоступен: откройте пульт по https:// (или http://localhost на самом ПК)', 6000);
    return false;
  }
  const ac = await ensureAudioContext();
  try {
    C.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: opts.micId ? { exact: opts.micId } : undefined, channelCount: 1,
        echoCancellation: opts.echo, noiseSuppression: opts.ns, autoGainControl: opts.agc,
      },
    });
  } catch (e) { toast('Нет доступа к микрофону: ' + e.message, 5000); return false; }
  const src = ac.createMediaStreamSource(C.micStream);
  const hp = new BiquadFilterNode(ac, { type: 'highpass', frequency: 120 });
  const lp = new BiquadFilterNode(ac, { type: 'lowpass', frequency: C.rate === 8000 ? 3200 : 3800, Q: 0.7 });
  C.micGainNode = new GainNode(ac, { gain: opts.micGain / 100 });
  C.micNode = new AudioWorkletNode(ac, 'mic-capture');
  C.micNode.port.postMessage({ rate: C.rate });
  const sink = new GainNode(ac, { gain: 0 });
  src.connect(hp).connect(lp).connect(C.micGainNode).connect(C.micNode).connect(sink).connect(ac.destination);
  C.micNode.port.onmessage = e => {
    const { pcm, rms } = e.data;
    $('#micLevel').style.width = clamp((20 * Math.log10(rms + 1e-6) + 50) / 50, 0, 1) * 100 + '%';   // -50..0 дБ
    if (!C.txOwner || !C.pttPressed) return;
    const b = new Uint8Array(1 + pcm.length * 2), dv = new DataView(b.buffer);
    b[0] = 3;
    for (let i = 0; i < pcm.length; i++) dv.setInt16(1 + i * 2, clamp(pcm[i], -1, 1) * 32767, true);
    if (C.ws && C.ws.readyState === 1 && C.ws.bufferedAmount < 64000) C.ws.send(b);
  };
  updatePtt();
  listMics();
  return true;
}
async function pttDown() {
  if (C.pttPressed) return;
  if (C.pttBusy) { toast('Передача занята другим подключением'); return; }
  C.pttPressed = true; updatePtt();
  if (!(await startMic())) { C.pttPressed = false; updatePtt(); return; }
  if (!C.pttPressed) return;                // отпустили, пока включался микрофон
  send({ t: 'ptt', on: true });
  if (opts.muteTx && C.player) C.player.port.postMessage({ mute: true });
  pushGate();
  clearInterval(C.keepalive);
  C.keepalive = setInterval(() => send({ t: 'ptt_alive' }), 400);
}
function pttUp() {
  if (!C.pttPressed) return;
  C.pttPressed = false;
  clearInterval(C.keepalive);
  send({ t: 'ptt', on: false });
  if (C.player) C.player.port.postMessage({ mute: false });
  pushGate();
  updatePtt();
}
function updatePtt() {
  const b = $('#ptt');
  b.classList.toggle('ready', !!C.micStream);
  b.classList.toggle('active', !!(C.pttPressed && C.txOwner));
  b.classList.toggle('busy', !!C.pttBusy);
  $('#pttState').textContent = C.pttBusy ? 'Передаёт другой клиент'
    : C.pttPressed ? (C.txOwner ? 'ПЕРЕДАЧА' : 'Включаю…')
      : C.micStream ? 'Микрофон готов' : 'Микрофон — по PTT';
}
setInterval(() => {
  $('#txTimer').textContent = (C.txOwner && C.txStart) ? `TX ${Math.floor((performance.now() - C.txStart) / 1000)} с` : '';
}, 250);
(() => {
  const b = $('#ptt');
  b.addEventListener('pointerdown', e => {
    e.preventDefault(); b.setPointerCapture(e.pointerId);
    if (opts.pttMode === 'toggle') { C.pttPressed ? pttUp() : pttDown(); } else pttDown();
  });
  const up = () => { if (opts.pttMode === 'hold') pttUp(); };
  b.addEventListener('pointerup', up); b.addEventListener('pointercancel', up);
  b.addEventListener('contextmenu', e => e.preventDefault());
  document.addEventListener('keydown', e => {
    if (!opts.space || e.code !== 'Space' || e.repeat || e.target.matches('input, select, textarea')) return;
    e.preventDefault(); opts.pttMode === 'toggle' ? (C.pttPressed ? pttUp() : pttDown()) : pttDown();
  });
  document.addEventListener('keyup', e => { if (opts.space && e.code === 'Space' && opts.pttMode === 'hold') pttUp(); });
  window.addEventListener('blur', () => { if (opts.pttMode === 'hold') pttUp(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) pttUp(); });
})();

// ------------------------------------------------------------------ вкладки
$$('#tabs button').forEach(b => b.onclick = () => {
  $$('#tabs button').forEach(x => x.classList.toggle('on', x === b));
  $$('.tab').forEach(t => t.classList.toggle('on', t.id === 'tab-' + b.dataset.tab));
  if (b.dataset.tab === 'menu' && !menu.items) send({ t: 'menu_list' });
  store.set('tab', b.dataset.tab);
});

// ------------------------------------------------------------------ меню EX
const menu = { items: null, vals: {} };
function buildMenu(items) {
  menu.items = items;
  const groups = [...new Set(items.map(i => i.ex.slice(0, 4)))];
  const secName = {}; items.forEach(i => secName[i.ex.slice(0, 4)] = i.section);
  fillSelect($('#menuGroup'), [['', 'Все разделы'], ...groups.map(g => [g, `${g} ${MENU_GROUPS[g.slice(0, 2)] || ''}: ${secName[g]}`])]);
  renderMenu();
}
function menuFiltered() {
  const q = $('#menuSearch').value.trim().toUpperCase(), g = $('#menuGroup').value;
  let re = null; try { re = q ? new RegExp(q.replace(/\s+/g, '.*')) : null; } catch { }
  return menu.items.filter(i => (!g || i.ex.startsWith(g)) && (!re || re.test(i.name) || i.ex.includes(q) || re.test((i.hint || '').toUpperCase())));
}
function renderMenu() {
  if (!menu.items) return;
  const list = menuFiltered(), box = $('#menuList');
  let html = '', sec = '';
  for (const i of list.slice(0, 450)) {
    const s = i.ex.slice(0, 4);
    if (s !== sec) { sec = s; html += `<div class="menu-sec">EX${s} · ${MENU_GROUPS[s.slice(0, 2)] || ''} · ${i.section}</div>`; }
    const range = i.string ? 'текст' : `${i.min}…${i.max}`;
    const v = menu.vals[i.ex] ?? '';
    html += `<div class="mi" data-ex="${i.ex}"><span class="ex">EX${i.ex}</span><span class="nm">${i.name}${i.spa1 ? ' <small>только с SPA-1</small>' : ''}<small>${range}${i.hint ? ' · ' + i.hint : ''}</small></span><input value="${v}" placeholder="?"><button class="btn small">✓</button></div>`;
  }
  if (list.length > 450) html += `<p class="muted small">Показаны первые 450 из ${list.length}. Уточните поиск.</p>`;
  box.innerHTML = html || '<p class="muted">Ничего не найдено</p>';
}
function setMenuVal(ex, v) {
  menu.vals[ex] = v;
  const row = $(`.mi[data-ex="${ex}"]`);
  if (row && document.activeElement !== $('input', row)) { $('input', row).value = v ?? '—'; row.classList.remove('dirty'); }
}
function readMenu(exList) {
  for (let i = 0; i < exList.length; i += 50) send({ t: 'menu_get', ex: exList.slice(i, i + 50) });
}
$('#menuSearch').oninput = () => { clearTimeout(menu.t); menu.t = setTimeout(renderMenu, 200); };
$('#menuGroup').onchange = renderMenu;
$('#menuReadAll').onclick = () => {
  if (!menu.items) return;
  const l = menuFiltered();
  if (l.length > 120 && !confirm(`Прочитать ${l.length} пунктов? Это займёт около ${Math.ceil(l.length / 8)} с.`)) return;
  readMenu(l.map(i => i.ex));
};
$('#menuUsbShow').onclick = () => {
  $('#menuGroup').value = ''; $('#menuSearch').value = 'MOD_SOURCE|USB_MOD|USB_OUT|RPTT';
  renderMenu(); readMenu(menuFiltered().map(i => i.ex));
};
$('#menuList').addEventListener('input', e => { const r = e.target.closest('.mi'); r && r.classList.add('dirty'); });
function menuCommit(row) {
  const ex = row.dataset.ex, item = menu.items.find(i => i.ex === ex), v = $('input', row).value.trim();
  if (!item.string) {
    const n = parseInt(v, 10);
    if (!isFinite(n) || n < item.min || n > item.max) { toast(`Допустимо ${item.min}…${item.max}`); return; }
    send({ t: 'menu_set', ex, v: n });
  } else send({ t: 'menu_set', ex, v });
}
$('#menuList').addEventListener('click', e => { if (e.target.matches('button')) menuCommit(e.target.closest('.mi')); });
$('#menuList').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.matches('input')) menuCommit(e.target.closest('.mi')); });

// ------------------------------------------------------------------ консоль CAT
const POLL_RE = /^[<>](SM\d|RM\d|FA|FB|MD0|TX)/;
function catLine(l) {
  if (POLL_RE.test(l) && performance.now() > (catLine.showAll || 0)) return;   // опрос не засоряет журнал
  const o = $('#catOut');
  o.textContent += l + '\n';
  if (o.textContent.length > 40000) o.textContent = o.textContent.slice(-30000);
  o.scrollTop = o.scrollHeight;
}
$('#catSend').onclick = () => {
  const c = $('#catCmd').value.trim(); if (!c) return;
  if (!$('#catLog').checked) { $('#catLog').checked = true; send({ t: 'console', on: true }); }
  catLine.showAll = performance.now() + 1500;
  send({ t: 'raw', c }); $('#catCmd').select();
};
$('#catCmd').addEventListener('keydown', e => { if (e.key === 'Enter') $('#catSend').click(); });
$('#catLog').onchange = e => send({ t: 'console', on: e.target.checked });
$('#catClear').onclick = () => { $('#catOut').textContent = ''; };

// ------------------------------------------------------------------ CW-память
(() => {
  const texts = store.get('cwMem', ['CQ CQ DE R6AAF R6AAF K', 'TU 5NN', 'R6AAF', '73 TU', 'QRZ?']);
  const box = $('#cwMems');
  for (let i = 1; i <= 5; i++) {
    const row = document.createElement('div'); row.className = 'cw-mem';
    row.innerHTML = `<b>${i}</b><input class="text mono" maxlength="50"><button class="btn small">Записать</button><button class="btn small">▶ Передать</button>`;
    const inp = $('input', row); inp.value = texts[i - 1] || '';
    const [wr, play] = $$('button', row);
    inp.onchange = () => { texts[i - 1] = inp.value.toUpperCase(); inp.value = texts[i - 1]; store.set('cwMem', texts); };
    wr.onclick = () => { act('cw_store', [i, inp.value]); toast(`Ячейка ${i} записана в трансивер`); };
    play.onclick = () => act('cw_play', i);
    box.appendChild(row);
  }
})();

// ------------------------------------------------------------------ избранные частоты
function renderFavs() {
  const favs = store.get('favs', []), box = $('#favList');
  box.innerHTML = '';
  favs.forEach((f, i) => {
    const el = document.createElement('span'); el.className = 'fav';
    el.innerHTML = `<button class="btn">${f.name ? f.name + ' · ' : ''}${fmtFreq(f.f)} ${f.m || ''}</button><button class="btn small" title="Удалить">×</button>`;
    const [go, del] = $$('button', el);
    go.onclick = () => { setFreq(f.f); if (f.m) setTimeout(() => setParam('mode', f.m), 120); };
    del.onclick = () => { favs.splice(i, 1); store.set('favs', favs); renderFavs(); };
    box.appendChild(el);
  });
}
$('#favAdd').onclick = () => {
  const favs = store.get('favs', []);
  favs.push({ name: $('#favName').value.trim(), f: S.freq_a, m: S.mode });
  store.set('favs', favs); $('#favName').value = ''; renderFavs();
};

// ------------------------------------------------------------------ настройки клиента
async function listMics() {
  try {
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput');
    const sel = $('#optMic'), cur = opts.micId;
    fillSelect(sel, [['', 'По умолчанию'], ...devs.map((d, i) => [d.deviceId, d.label || `Микрофон ${i + 1}`])]);
    sel.value = cur;
  } catch { }
}
function saveOpts() { store.set('opts', opts); }
function resetMic() {
  if (C.micStream) { C.micStream.getTracks().forEach(t => t.stop()); C.micStream = null; }
  updatePtt();
}
function initOpts() {
  $('#optTail').value = opts.tail; $('#optTailV').textContent = opts.tail;
  $('#optTail').oninput = e => { opts.tail = +e.target.value; $('#optTailV').textContent = opts.tail; saveOpts(); pushGate(); };
  $('#optScale').value = String(opts.scale);
  $('#optScale').onchange = e => { opts.scale = e.target.value; saveOpts(); applyScale(); };
  $('#optRate').value = C.rate;
  $('#optRate').onchange = e => {
    C.rate = +e.target.value; store.set('rate', C.rate);
    C.player && C.player.port.postMessage({ rate: C.rate });
    C.micNode && C.micNode.port.postMessage({ rate: C.rate });
    send({ t: 'audio', on: C.audioOn, rate: C.rate });
  };
  $('#optMic').onchange = e => { opts.micId = e.target.value; saveOpts(); resetMic(); };
  $('#optMicGain').value = opts.micGain; $('#optMicGainV').textContent = opts.micGain;
  $('#optMicGain').oninput = e => { opts.micGain = +e.target.value; $('#optMicGainV').textContent = opts.micGain; saveOpts(); if (C.micGainNode) C.micGainNode.gain.value = opts.micGain / 100; };
  for (const [id, k] of [['optEcho', 'echo'], ['optNs', 'ns'], ['optAgc', 'agc']]) {
    $('#' + id).checked = opts[k];
    $('#' + id).onchange = e => { opts[k] = e.target.checked; saveOpts(); resetMic(); };
  }
  for (const [id, k] of [['optMuteTx', 'muteTx'], ['optSpace', 'space']]) {
    $('#' + id).checked = opts[k];
    $('#' + id).onchange = e => { opts[k] = e.target.checked; saveOpts(); };
  }
  $('#optPttMode').value = opts.pttMode;
  $('#optPttMode').onchange = e => { opts.pttMode = e.target.value; saveOpts(); pttUp(); };
  $('#optPalette').value = opts.palette;
  $('#optPalette').onchange = e => { opts.palette = e.target.value; saveOpts(); buildLut(); };
}

// ------------------------------------------------------------------ вход, полноэкранный режим, PWA
$('#loginBtn').onclick = () => {
  C.pw = $('#loginPw').value; store.set('pw', C.pw);
  send({ t: 'hello', pw: C.pw, rate: C.rate });
};
$('#loginPw').addEventListener('keydown', e => { if (e.key === 'Enter') $('#loginBtn').click(); });
$('#btnPanel').onclick = () => window.open('panel.html', 'ftx1panel', 'width=1080,height=860');
$('#btnFull').onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen && document.documentElement.requestFullscreen().catch(() => { });
};
if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('sw.js').catch(() => { });

// ------------------------------------------------------------------ масштаб интерфейса
// «Авто»: макет рассчитан на окно ~1440×860; на больших экранах увеличиваем, на маленьких
// ноутбуках — немного уменьшаем. На телефонах (узкий экран) — 100 %.
function applyScale() {
  let z = parseFloat(opts.scale);
  if (!(z > 0)) {
    const w = window.innerWidth, h = window.innerHeight;
    z = w < 760 ? 1 : clamp(Math.min(w / 1440, h / 860), 0.8, 2.4);
    z = Math.round(z * 20) / 20;
  }
  if (document.documentElement.style.zoom !== String(z)) {
    document.documentElement.style.zoom = z;
    C.zoom = z;
    sizeCanvases();
  }
}
window.addEventListener('resize', () => { if (opts.scale === 'auto') applyScale(); });

// ------------------------------------------------------------------ старт
initControls();
initOpts();
applyScale();
renderSq();
renderFavs();
buildLut();
sizeCanvases();
{
  const t = store.get('tab', 'rx'); const b = $(`#tabs button[data-tab="${t}"]`); b && b.click();
}
connect();
