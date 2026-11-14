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
const MENU_GROUPS = { '01': I18n.t('Виды работы'), '02': 'CW', '03': I18n.t('Работа'), '04': I18n.t('Дисплей'), '05': I18n.t('Дата/время'), '06': 'APRS', '07': I18n.t('APRS маяк'), '08': I18n.t('APRS фильтр'), '09': I18n.t('Пресеты'), '11': 'Bluetooth' };

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
const S = {};                       // состояние трансивера (зеркало сервера) — «как на экране»
// Если на трансивере выбран SUB (VS1), большая частота пульта — частота SUB, а маленькая — MAIN,
// как на экране FTX-1. R — сырое состояние, S — вид: freq_a/mode всегда «рабочий» приёмник.
const R = {};
const PAIR = { freq_a: 'freq_b', freq_b: 'freq_a', mode: 'mode_b', mode_b: 'mode' };
const rawKey = k => (R.vfo && PAIR[k]) ? PAIR[k] : k;     // ключ вида -> ключ трансивера (и обратно)
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
const opts = Object.assign({ echo: false, ns: false, agc: false, muteTx: true, pttMode: 'hold', space: true, palette: 'classic', micGain: 100, micId: '',
  sqMode: 'radio', sqThr: -60, tail: 500, scale: 'auto', hotkeys: true }, store.get('opts', {}));
if ((opts.v | 0) < 2) {           // 1.8: обработка браузера для эфира по умолчанию выключена (портила голос)
  opts.echo = false; opts.ns = false; opts.agc = false; opts.v = 2; store.set('opts', opts);
}

// ------------------------------------------------------------------ связь
function send(o) { if (C.ws && C.ws.readyState === 1) C.ws.send(JSON.stringify(o)); }
function setParam(k, v) {
  const rk = rawKey(k);
  C.lockUntil[k] = performance.now() + 1200;
  if (k === 'vfo') { const was = !!R.vfo; R.vfo = v; if (!!v !== was) remapVfo(); }
  else { S[k] = v; R[rk] = v; }
  send({ t: 'set', k: rk, v });
}
function remapVfo() {                     // MAIN/SUB переключили — частоты и виды меняются местами
  for (const k of Object.keys(PAIR)) { S[k] = R[rawKey(k)]; delete C.lockUntil[k]; }
  S.vfo = R.vfo;
  if (typeof renderFreq === 'function') { renderFreq(); renderBadges(); }
  if (typeof updateSpan === 'function') setTimeout(() => { updateSpan(); drawOverlay(); }, 0);
}
function act(a, arg) { send({ t: 'act', a, arg }); }

function retryConnection() {
  if (C.shuttingDown) return;
  const old = C.ws;
  C.ws = null; // Ignore late events from the abandoned socket.
  if (old) { try { old.close(); } catch {} }
  pttUp();
  C.txOwner = false;
  C.txStart = 0;
  updatePtt();
  connect();
}
function checkConnection(probe = false) {
  if (C.shuttingDown) return;
  const ws = C.ws, now = Date.now();
  if (!ws || ws.readyState > 1 || now - C.lastMessage > 20000) {
    retryConnection();
    return;
  }
  if (probe && ws.readyState === 1) {
    clearTimeout(C.probeTimer);
    const stamp = C.probeStamp = now;
    send({ t: 'ping', ts: stamp });
    C.probeTimer = setTimeout(() => {
      if (C.ws === ws && C.probeStamp === stamp) retryConnection();
    }, 4000);
  }
}
function connect() {
  if (C.shuttingDown) return;
  clearTimeout(C.reconnectTimer);
  clearTimeout(C.probeTimer);
  C.probeStamp = null;
  C.authed = false;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  C.ws = ws;
  C.lastMessage = Date.now();
  ws.onopen = () => { if (C.ws === ws) send({ t: 'hello', pw: C.pw, rate: C.rate || 16000 }); };
  ws.onclose = () => {
    if (C.ws !== ws) return;
    clearTimeout(C.probeTimer);
    pttUp();
    C.authed = false; setLink(false, C.shuttingDown ? I18n.t('Сервер остановлен — после запуска обновите страницу') : I18n.t('нет связи с сервером'));
    if (C.txOwner) { C.txOwner = false; updatePtt(); }
    if (!C.shuttingDown) C.reconnectTimer = setTimeout(retryConnection, 2000);
  };
  ws.onmessage = (e) => {
    if (C.ws !== ws) return;
    C.lastMessage = Date.now();
    if (typeof e.data !== 'string') return onBinary(new Uint8Array(e.data));
    const m = JSON.parse(e.data);
    switch (m.t) {
      case 'pong':
        if (m.ts === C.probeStamp) { clearTimeout(C.probeTimer); C.probeStamp = null; }
        break;
      case 'server_stopping':
        C.shuttingDown = true;
        C.ws.close();
        break;
      case 'language': I18n.sync(m.language); break;
      case 'language_error': $('#language').value = m.language; toast(m.msg, 5000); break;
      case 'hello_ok':
        I18n.sync(m.language);
        C.authed = true; C.local = m.local; C.info = m;
        if (!m.local && C.pw) store.set('pw', C.pw);
        if (!C.rate) {                                   // первый вход: на этом ПК — максимальное качество
          C.rate = m.local ? 48000 : 16000; $('#optRate').value = C.rate;
          C.player && C.player.port.postMessage({ rate: C.rate });
        }
        send({ t: 'audio', on: C.audioOn, rate: C.rate });
        $('#login').classList.add('hidden');
        $('#btnPanel').classList.toggle('hidden', !m.local);
        fillSelect($('.ctl[data-k=ctcss] select'), m.ctcss.map((v, i) => [i, v.toFixed(1)]));
        $('#srvInfo').textContent = I18n.t`Сервер v${m.version}. PTT: ${m.ptt_method}. Ограничение передачи: ${m.max_tx} с.` + (m.audio_err ? I18n.t` ЗВУК: ${m.audio_err}` : '');
        setWfMsg(m.audio_err);
        updateSpan(true);
        send({ t: 'cw_hello' }); cwDesk.resend(); digiDesk.resend(); digi2.resend(); ft8Desk.resend(); voiceCq.hello(); qsoRec.hello();
        if (C.audioOn) send({ t: 'audio', on: true, rate: C.rate });
        break;
      case 'auth_fail':
        if (C.pw) { store.set('pw', ''); }          // неверный сохранённый пароль больше не подставляем
        C.pw = '';
        $('#login').classList.remove('hidden');
        $('#loginMsg').textContent = m.first ? I18n.t('Введите пароль сервера') : m.msg;
        $('#loginPw').value = ''; setTimeout(() => $('#loginPw').focus(), 50);
        break;
      case 'state': applyState(m.s, m.full); if (Object.keys(m.s).some(k => /^fw\d$|^radio_id$/.test(k))) renderFw(); break;
      case 'lv': break;
      case 'bw': C.bw = m.hz; onBw(m.hz); break;
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
      default:
        if (m.t && m.t.startsWith('cw_')) cwDesk.on(m);
        else if (m.t && (m.t.startsWith('rt_') || m.t.startsWith('fax_'))) digiDesk.on(m);
        else if (m.t && /^(nx_|psk_|sstv_)/.test(m.t)) digi2.on(m);
        else if (m.t && m.t.startsWith('vk_')) voiceCq.on(m);
        else if (m.t && m.t.startsWith('ft_')) ft8Desk.on(m);
        else if (m.t && m.t.startsWith('mem_') && typeof memEd !== 'undefined') memEd.on(m);
        else if (m.t === 'rec_state') qsoRec.on(m);
    }
  };
}

setInterval(() => {
  if (C.probeStamp == null) checkConnection(true);
}, 5000);
window.addEventListener('online', () => checkConnection(true));
window.addEventListener('pageshow', () => checkConnection(true));
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) checkConnection(true);
});

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
  } else if (type === 5 || type === 6) {
    digiDesk.onBin(b);
  } else if (type === 7 || type === 8) {
    digi2.onBin(b);
  }
}

function setWfMsg(err) {
  const el = $('#wfMsg');
  el.classList.toggle('hidden', !err);
  if (err) el.innerHTML = I18n.t`<div><b>Нет звука с трансивера</b>${err.replace(/[&<]/g, c => c === '&' ? '&amp;' : '&lt;')}` +
    (C.local ? I18n.t('<br><br>Выберите устройства в панели управления сервером (кнопка ⚙ вверху).') : '') + '</div>';
}
function setLink(ok, text) {
  $('#linkDot').className = 'dot ' + (ok ? 'ok' : 'bad');
  if (text) $('#rigInfo').textContent = text;
}

// ------------------------------------------------------------------ отрисовка состояния
function applyState(d, full) {
  const now = performance.now();
  const swapWas = !!R.vfo;
  for (const [k, v] of Object.entries(d)) R[k] = v;
  if (!!R.vfo !== swapWas) remapVfo();
  for (const [k, v] of Object.entries(d)) {
    const vk = rawKey(k);                          // для freq/mode ключ вида
    if (!full && C.lockUntil[vk] > now) continue;  // пользователь только что крутил — не дёргаем
    S[vk] = v;
  }
  if ('connected' in d || 'radio_id' in d || 'fw0' in d || 'pwr_head' in d) {
    const head = S.pwr_head === 2 ? 'Optima/SPA-1' : (S.pwr_head === 1 ? 'Field' : '');
    setLink(S.connected, S.connected ? `${S.radio_id === '0840' ? 'FTX-1' : 'ID ' + (S.radio_id || '?')} ${head} ${S.fw0 ? 'FW ' + S.fw0 : ''}` : I18n.t('трансивер не отвечает'));
  }
  renderFreq();
  renderMeters();
  renderBadges();
  renderControls(d);
  drawOverlay();
  if ('tuning' in d) $('#tuneState').textContent = S.tuning ? I18n.t('Идёт настройка…') : '';
  if ('tx' in d) document.body.classList.toggle('txon', !!S.tx);
  if ('mode' in d) { C.autoLock = null; C.bwHist = []; updateSpan(true); renderSq(); pushGate(); renderCenterBtn(); }
  if ('busy' in d || 'tx' in d) pushGate();
  if ('sql' in d) renderSq();
}

function renderBadges() {
  $('#bMode').textContent = S.mode || '—';
  $('#bModeB').textContent = S.mode_b || '—';
  $('#bVfo').textContent = S.vfo ? 'SUB' : 'MAIN';
  $('#bVm').textContent = S.vm ? `MEM ${S.mem_ch != null ? String(S.mem_ch).padStart(3, '0') : ''}`.trim() : 'VFO';
  $('#bVm').classList.toggle('off', !S.vm);
  $('#bSplit').classList.toggle('off', !S.split);
  $('#bClar').classList.toggle('off', !(S.clar_rx || S.clar_tx));
  $('#bClar').textContent = (S.clar_rx || S.clar_tx) ? `CLAR ${S.clar > 0 ? '+' : ''}${S.clar}` : 'CLAR';
  $('#bLock').classList.toggle('off', !S.lock);
  $('#bTx').classList.toggle('off', !S.tx);
  $('#bBusy').classList.toggle('off', !S.busy);
  $('#freqB').textContent = S.freq_b ? fmtFreq(S.freq_b) : '—';
  $('#lblB').textContent = R.vfo ? 'A:' : 'B:';
  $('#bTxB').classList.toggle('hidden', !S.split);
  $('#btnSplit').classList.toggle('on', !!S.split);
  $('#btnLock').classList.toggle('on', !!S.lock);
  $$('#modes .btn').forEach(b => b.classList.toggle('on', b.dataset.m === S.mode));
  const bd = bandOf(S.freq_a || 0);
  $$('#bands .btn').forEach(b => b.classList.toggle('on', !!bd && b.dataset.b === bd[0]));
  if (S.power != null) $('#pwrInfo').textContent = I18n.t`${S.power} Вт`;
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
    setBar('#mPo', w / (maxW * 1.2), I18n.t`≈${w.toFixed(w < 10 ? 1 : 0)} Вт`);
    const swr = interp(SWR_CAL, S.swr || 0);
    setBar('#mSwr', (swr - 1) / 4, swr.toFixed(1));
    setBar('#mAlc', (S.alc || 0) / 255, Math.round((S.alc || 0) / 2.55) + '%');
  }
}

// ------------------------------------------------------------------ частота: цифры, ручка, ввод
let freqSendT = 0, freqPending = null;
function setFreq(f) {
  f = clamp(Math.round(f), 30000, 470000000);
  S.freq_a = f; R[rawKey('freq_a')] = f; renderFreq(); drawOverlay();
  C.lockUntil.freq_a = performance.now() + 900;
  freqPending = f;
  const now = performance.now();
  if (now - freqSendT > 60) flushFreq();
  else setTimeout(flushFreq, 60);
}
function flushFreq() {
  if (freqPending == null) return;
  freqSendT = performance.now();
  send({ t: 'set', k: rawKey('freq_a'), v: freqPending });
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
  if (e.key === 'Escape') { e.target.value = ''; e.target.blur(); return; }
  if (e.key !== 'Enter') return;
  let v = parseFloat(e.target.value.replace(',', '.'));
  if (!isFinite(v)) return;
  if (v < 1000) v *= 1e6; else if (v < 1e6) v *= 1e3;   // МГц / кГц / Гц
  setFreq(v); e.target.value = ''; e.target.blur();
});
// Основная ручка настройки — как валкодер трансивера.
// Вращение по кругу: 100 делений на оборот, одно деление = выбранный шаг.
// Бросок с разгона — ручка докручивается по инерции и плавно останавливается.
const dial = (() => {
  const cv = $('#dial'), ctx = cv.getContext('2d');
  const STEPS = 100, STEP_A = 2 * Math.PI / STEPS;
  let angle = 0, acc = 0, last = null, hist = [], vel = 0, spinning = false, size = 0;
  function resize() {
    const css = cv.clientWidth; if (!css) return;
    const px = Math.round(css * (window.devicePixelRatio || 1) * (C.zoom || 1));
    if (px !== size) { size = px; cv.width = cv.height = px; }
    draw();
  }
  function draw() {
    if (!size) return;
    const R = size / 2, c = R;
    ctx.clearRect(0, 0, size, size);
    // посадочное кольцо с меткой
    ctx.beginPath(); ctx.arc(c, c, R * 0.99, 0, 2 * Math.PI); ctx.fillStyle = '#0a0f14'; ctx.fill();
    ctx.strokeStyle = '#36c5f0'; ctx.lineWidth = R * 0.04; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(c, R * 0.02); ctx.lineTo(c, R * 0.09); ctx.stroke();
    // тело ручки
    const body = R * 0.88;
    let g = ctx.createRadialGradient(c - body * 0.35, c - body * 0.4, body * 0.1, c, c, body);
    g.addColorStop(0, '#5a6672'); g.addColorStop(0.55, '#2b333c'); g.addColorStop(1, '#14191f');
    ctx.beginPath(); ctx.arc(c, c, body, 0, 2 * Math.PI); ctx.fillStyle = g; ctx.fill();
    // накатка по краю (вращается)
    ctx.save(); ctx.translate(c, c); ctx.rotate(angle);
    ctx.lineWidth = Math.max(1, R * 0.018);
    for (let i = 0; i < 90; i++) {
      const a = i * 2 * Math.PI / 90;
      ctx.strokeStyle = i % 2 ? 'rgba(0,0,0,.55)' : 'rgba(255,255,255,.10)';
      ctx.beginPath(); ctx.moveTo(Math.cos(a) * body * 0.86, Math.sin(a) * body * 0.86);
      ctx.lineTo(Math.cos(a) * body * 0.995, Math.sin(a) * body * 0.995); ctx.stroke();
    }
    // центральный диск со «шлифовкой»
    const cap = body * 0.8;
    g = ctx.createRadialGradient(-cap * 0.3, -cap * 0.35, cap * 0.05, 0, 0, cap);
    g.addColorStop(0, '#4b5561'); g.addColorStop(1, '#1d242c');
    ctx.beginPath(); ctx.arc(0, 0, cap, 0, 2 * Math.PI); ctx.fillStyle = g; ctx.fill();
    ctx.lineWidth = Math.max(0.5, R * 0.006);
    for (let r = cap * 0.15; r < cap; r += cap * 0.06) {
      ctx.strokeStyle = 'rgba(255,255,255,.035)'; ctx.beginPath(); ctx.arc(0, 0, r, 0, 2 * Math.PI); ctx.stroke();
    }
    // лунка под палец
    const dx = 0, dy = -cap * 0.58, dr = cap * 0.2;
    g = ctx.createRadialGradient(dx + dr * 0.3, dy + dr * 0.35, dr * 0.1, dx, dy, dr);
    g.addColorStop(0, '#2c343d'); g.addColorStop(1, '#0d1116');
    ctx.beginPath(); ctx.arc(dx, dy, dr, 0, 2 * Math.PI); ctx.fillStyle = g; ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,.12)'; ctx.lineWidth = Math.max(1, R * 0.01);
    ctx.beginPath(); ctx.arc(dx, dy, dr, Math.PI * 0.15, Math.PI * 0.85); ctx.stroke();
    ctx.restore();
    // блик сверху
    g = ctx.createLinearGradient(0, c - body, 0, c + body);
    g.addColorStop(0, 'rgba(255,255,255,.10)'); g.addColorStop(0.5, 'rgba(255,255,255,0)');
    ctx.beginPath(); ctx.arc(c, c, body, 0, 2 * Math.PI); ctx.fillStyle = g; ctx.fill();
  }
  function turn(da) {                      // da — поворот в радианах (по часовой — вверх по частоте)
    angle += da; acc += da / STEP_A;
    const n = Math.trunc(acc);
    if (n) { acc -= n; nudge(n); }
    draw();
  }
  const angAt = e => { const r = cv.getBoundingClientRect(); return Math.atan2(e.clientY - (r.top + r.height / 2), e.clientX - (r.left + r.width / 2)); };
  const nearCenter = e => { const r = cv.getBoundingClientRect(); return Math.hypot(e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2)) < r.width * 0.08; };
  cv.addEventListener('pointerdown', e => {
    e.preventDefault(); cv.setPointerCapture(e.pointerId);
    spinning = false; vel = 0; last = angAt(e); hist = [[performance.now(), 0]];
  });
  cv.addEventListener('pointermove', e => {
    if (last == null || nearCenter(e)) return;
    const a = angAt(e); let d = a - last;
    if (d > Math.PI) d -= 2 * Math.PI; else if (d < -Math.PI) d += 2 * Math.PI;
    last = a; turn(d);
    const now = performance.now(); hist.push([now, d]);
    while (hist.length && now - hist[0][0] > 90) hist.shift();
  });
  const release = () => {
    if (last == null) return;
    last = null;
    if (hist.length > 1) {
      const dt = (hist[hist.length - 1][0] - hist[0][0]) / 1000;
      const sum = hist.slice(1).reduce((s, h) => s + h[1], 0);
      vel = dt > 0.01 ? sum / dt : 0;              // рад/с
    }
    if (Math.abs(vel) > 4) { spinning = true; let t0 = performance.now();
      const step = now => {
        if (!spinning) return;
        const dt = (now - t0) / 1000; t0 = now;
        turn(vel * dt); vel *= Math.exp(-dt * 2.5);
        if (Math.abs(vel) > 0.6) requestAnimationFrame(step); else spinning = false;
      };
      requestAnimationFrame(step);
    }
  };
  cv.addEventListener('pointerup', release); cv.addEventListener('pointercancel', release);
  cv.addEventListener('wheel', e => { e.preventDefault(); spinning = false; turn((e.deltaY < 0 ? 1 : -1) * STEP_A); }, { passive: false });
  return { resize, draw };
})();

// ------------------------------------------------------------------ диапазоны и виды работы
const bandMem = store.get('bandMem', {});
function rememberBand() {
  const b = bandOf(S.freq_a || 0);
  if (b) { bandMem[b[0]] = [S.freq_a, S.mode]; store.set('bandMem', bandMem); }
}
for (const b of BANDS) {
  const el = document.createElement('button');
  el.className = 'btn'; el.dataset.b = b[0];
  el.textContent = /^\d+$/.test(b[0]) ? (b[0] === '70' ? I18n.t('70см') : b[0] + I18n.t('м')) : b[0];
  // кнопка диапазона = кнопка BAND на трансивере: он сам восстанавливает свою частоту, вид работы,
  // полосу и прочее для этого диапазона (код BS совпадает с порядком в BANDS)
  const code = BANDS.indexOf(b);
  el.onclick = () => act('band', code);
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
  if (f === 'width') return (v == 0 ? I18n.t('по умолч. ') : '') + widthHz(v, S.mode) + I18n.t(' Гц');
  if (f === 'apf') return ((v - 25) * 10) + I18n.t(' Гц');
  if (f === 'delay') return delayMs(v) + I18n.t(' мс');
  if (f === 'pitch') return (300 + v * 10) + I18n.t(' Гц');
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
      if (el.dataset.reset != null) b.title = I18n.t('Двойной клик — сброс');
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
        if (sel.dataset.kind !== want) { sel.dataset.kind = want; fillSelect(sel, want === 'hf' ? [[0, 'IPO'], [1, 'AMP1'], [2, 'AMP2']] : [[0, I18n.t('Выкл')], [1, I18n.t('Вкл')]]); }
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
// SPLIT как на FTX-1: частота SUB переносится в основной приёмник, частоты меняются местами (команда SV)
$('#btnSplit').onclick = () => { act('swap'); toast('SPLIT: A ⇄ B', 1300); };
// кнопки-индикаторы в верхней строке
$('#bSplit').onclick = () => { act('swap'); toast('SPLIT: A ⇄ B', 1300); };
$('#bLock').onclick = () => setParam('lock', S.lock ? 0 : 1);
$('#bClar').onclick = () => setParam('clar_rx', S.clar_rx ? 0 : 1);
$('#bVfo').onclick = () => setParam('vfo', S.vfo ? 0 : 1);
$('#bVm').onclick = () => { act(S.vm ? 'vfo_mode' : 'mem_mode'); S.vm = S.vm ? 0 : 1; renderBadges(); };
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
  if (typeof dial !== 'undefined') dial.resize();
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  for (const c of [wf.spec, wf.ov]) { c.width = Math.round(c.clientWidth * dpr); c.height = Math.round(c.clientHeight * dpr); }
  wf.rows = Math.max(50, Math.round(wf.canvas.clientHeight));
  drawOverlay();
}
// AM/FM: на водопад приходит уже продетектированный звук, несущей в нём нет. Чтобы картинка
// была привычной (сигнал по центру), показываем звук зеркально: 0 Гц в середине, ±частоты по краям.
const mirrored = () => opts.fmCenter !== false && isAMFM(S.mode);
function wfRow(row, binHz) {
  // FT8: водопад медленнее — видно 3–4 периода (как в JTDX/MSHV); соседние строки объединяются по максимуму
  const tNow = performance.now();
  if (C.wfT) C.wfRate = (C.wfRate || 30) * 0.95 + 0.05 * (1000 / Math.max(1, tNow - C.wfT));
  C.wfT = tNow;
  if (C.tab === 'ft8' && !mirrored()) {
    const secs = C.ftSecs ?? 20;     // сколько секунд видно на водопаде
    const N = secs ? Math.max(1, Math.round((C.wfRate || 30) * secs / Math.max(50, wf.rows || 300))) : 1;
    if (!C.ftAcc || C.ftAcc.length !== row.length) { C.ftAcc = Uint8Array.from(row); C.ftAccN = 0; }
    else for (let i = 0; i < row.length; i++) if (row[i] > C.ftAcc[i]) C.ftAcc[i] = row[i];
    if (++C.ftAccN < N) return;
    row = C.ftAcc; C.ftAcc = null;
  }
  if (mirrored()) {
    const n0 = row.length, r2 = new Uint8Array(n0 * 2 - 1);
    for (let i = 0; i < n0; i++) { r2[n0 - 1 - i] = row[i]; r2[n0 - 1 + i] = row[i]; }
    row = r2;
  }
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
  window.ft8Desk && ft8Desk.onRow();
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
    const i = mirrored() ? Math.min(a.length - 1, Math.round(x / W * (a.length - 1)))
      : Math.min(a.length - 1, Math.round(x / W * span / wf.binHz));
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
  const mir = mirrored();
  const ctx = c.getContext('2d'), W = c.width, H = c.height, span = C.span,
    X = hz => mir ? (0.5 + hz / (2 * span)) * W : hz / span * W;
  const dpr = W / c.clientWidth;
  ctx.clearRect(0, 0, W, H);
  let [lo, hi] = passband();
  if (mir) lo = -hi;
  ctx.fillStyle = 'rgba(255,255,255,.07)'; ctx.fillRect(X(lo), 0, X(hi) - X(lo), H);
  ctx.strokeStyle = 'rgba(255,211,77,.7)'; ctx.lineWidth = dpr;
  ctx.beginPath(); ctx.moveTo(X(lo), 0); ctx.lineTo(X(lo), H); ctx.moveTo(X(hi), 0); ctx.lineTo(X(hi), H); ctx.stroke();
  if (isCW(S.mode)) { ctx.strokeStyle = '#ff5a4d'; ctx.beginPath(); ctx.moveTo(X(pitchHz()), 0); ctx.lineTo(X(pitchHz()), H); ctx.stroke(); }
  if (C.cwMark) {                                   // частота, которую слушает декодер CW
    ctx.strokeStyle = '#3ddc84'; ctx.setLineDash([2 * dpr, 3 * dpr]); ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath(); ctx.moveTo(X(C.cwMark), 0); ctx.lineTo(X(C.cwMark), H); ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = dpr;
  }
  for (const [hz, col, bw] of C.ftMarks || []) {    // FT8: RX (зелёная) и TX (красная) — скобки шириной сигнала, как в JTDX
    const x0 = X(hz), x1 = X(hz + bw), yT = H * 0.28;
    ctx.fillStyle = col; ctx.globalAlpha = 0.18; ctx.fillRect(x0, yT, x1 - x0, H - yT); ctx.globalAlpha = 1;
    ctx.fillRect(x0, yT - 5 * dpr, x1 - x0, 3 * dpr);
    ctx.fillRect(x0, yT - 5 * dpr, 1.5 * dpr, 10 * dpr); ctx.fillRect(x1 - 1.5 * dpr, yT - 5 * dpr, 1.5 * dpr, 10 * dpr);
  }
  for (const [hz, col] of (C.dgMarks || []).concat(C.dgMarks2 || [])) {    // RTTY mark/space, факс чёрный/белый
    ctx.strokeStyle = col; ctx.setLineDash([3 * dpr, 3 * dpr]); ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath(); ctx.moveTo(X(hz), 0); ctx.lineTo(X(hz), H); ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = dpr;
  }
  if (S.notch) { ctx.strokeStyle = '#ff7ad9'; ctx.setLineDash([4 * dpr, 4 * dpr]); ctx.beginPath(); ctx.moveTo(X(S.notch_freq), 0); ctx.lineTo(X(S.notch_freq), H); ctx.stroke(); ctx.setLineDash([]); }
  // шкала: Гц звука + частота в эфире
  const stepK = span <= 4000 ? 500 : span <= 6000 ? 1000 : span <= 12000 ? 2000 : 4000;
  ctx.font = `${11 * dpr}px Consolas, monospace`; ctx.fillStyle = 'rgba(220,235,245,.75)';
  ctx.strokeStyle = 'rgba(255,255,255,.12)';
  for (let hz = stepK; hz < span; hz += stepK) {
    for (const sg of mir ? [-1, 1] : [1]) {
      const x = X(sg * hz);
      ctx.beginPath(); ctx.moveTo(x, H * 0.28 - 6 * dpr); ctx.lineTo(x, H * 0.28); ctx.stroke();
      ctx.fillText((sg < 0 ? '−' : mir ? '+' : '') + (hz / 1000).toFixed(stepK < 1000 ? 1 : 0) + 'k', x + 3 * dpr, H * 0.28 - 4 * dpr);
    }
  }
  if (mir) {                                         // центр = частота настройки
    ctx.strokeStyle = 'rgba(255,211,77,.9)'; ctx.setLineDash([5 * dpr, 4 * dpr]);
    ctx.beginPath(); ctx.moveTo(W / 2, 0); ctx.lineTo(W / 2, H); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(255,211,77,.95)'; ctx.fillText(fmtFreq(S.freq_a || 0), W / 2 + 4 * dpr, 14 * dpr);
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
  if (isAMFM(m)) { toast(I18n.t('В AM/FM подстройка по водопаду недоступна')); return; }
  const target = isCW(m) ? pitchHz() : 1500;
  setFreq(Math.round((isLower(m) ? f - (a - target) : f + (a - target)) / 10) * 10);
}
(() => {
  const w = $('#wfWrap'), cur = $('#wfCursor');
  const aAt = e => {
    const r = w.getBoundingClientRect(), f = clamp((e.clientX - r.left) / r.width, 0, 1);
    return mirrored() ? Math.abs(f * 2 - 1) * C.span : f * C.span;
  };
  w.addEventListener('pointermove', e => {
    const a = aAt(e), r = w.getBoundingClientRect();
    cur.style.display = 'block'; cur.style.left = (e.clientX - r.left) + 'px';
    cur.dataset.f = I18n.t`${Math.round(a)} Гц · ${fmtFreq(audioToRf(a))}`;
  });
  w.addEventListener('pointerleave', () => { cur.style.display = 'none'; });
  w.addEventListener('dblclick', e => { if (C.tab === 'ft8') ft8Desk.pickWf(aAt(e), 'both'); else tuneToAudio(aAt(e)); });
  w.addEventListener('click', e => { if (C.tab === 'ft8') ft8Desk.pickWf(aAt(e), e.shiftKey ? 'tx' : 'rx'); });
  w.addEventListener('contextmenu', e => {            // правый клик — декодер CW слушает эту частоту
    e.preventDefault();
    if (C.tab === 'digi') digiDesk.setFreq(Math.round(aAt(e)));
    else if (C.tab === 'ft8') ft8Desk.pickWf(aAt(e), 'tx');
    else cwDesk.setFreq(Math.round(aAt(e)));
  });
  let lastTap = 0;
  w.addEventListener('pointerup', e => {           // двойной тап на телефоне
    if (e.pointerType !== 'touch') return;
    const now = performance.now();
    if (now - lastTap < 350) { if (C.tab === 'ft8') ft8Desk.pickWf(aAt(e), 'both'); else tuneToAudio(aAt(e)); }
    lastTap = now;
  });
  w.addEventListener('wheel', e => { e.preventDefault(); nudge(e.deltaY < 0 ? 1 : -1); }, { passive: false });
})();
// Обзор водопада. «Авто» — по измеренной полосе звука трансивера (в FM она шире, чем в SSB),
// пока не измерена — по виду работы.
// Авто-обзор: по измеренной полосе звука, но без дёрганья — только стандартные шаги,
// решение после 3 одинаковых замеров подряд, дальше обзор только расширяется, а сбрасывается
// при смене вида работы.
const SPAN_STEPS = [3000, 4000, 6000, 8000, 12000, 24000];
function autoSpan() {
  const m = S.mode || '';
  if (C.tab === 'ft8' && /^DATA/.test(m)) return 3000;     // FT8: весь участок 0–3000 Гц, как в JTDX
  if (C.autoLock && C.autoLock.mode === m) return C.autoLock.span;
  return /FM|C4FM/.test(m) ? 4000 : /^AM/.test(m) ? 4000 : isCW(m) ? 3000 : 4000;
}
function onBw(hz) {
  const m = S.mode || '';
  const cand = SPAN_STEPS.find(v => v >= hz * 1.05) || 24000;
  const L = C.autoLock && C.autoLock.mode === m ? C.autoLock : null;
  C.bwHist = (C.bwHist || []).concat(cand).slice(-5);
  const same = n => C.bwHist.length >= n && C.bwHist.slice(-n).every(v => v === C.bwHist[C.bwHist.length - 1]);
  if (!L && same(3)) C.autoLock = { mode: m, span: cand };
  else if (L && cand > L.span && same(5)) L.span = cand;
  updateSpan();
}
function updateSpan(force) {
  const span = C.wfMax || autoSpan();
  $$('#spanSeg button').forEach(x => x.classList.toggle('on', +x.dataset.v === C.wfMax));
  const ab = $('#spanSeg button[data-v="0"]');
  ab.textContent = C.wfMax ? I18n.t('Авто') : I18n.t`Авто ${(span / 1000).toFixed(span % 1000 ? 1 : 0)}k`;
  if (span === C.span && !force) return;
  C.span = span; wf.canvas.width = 0;
  send({ t: 'wf', on: true, max: span }); drawOverlay();
}
$$('#spanSeg button').forEach(b => {
  b.onclick = () => { C.wfMax = +b.dataset.v; store.set('wfMax', C.wfMax); updateSpan(true); };
});
function renderCenterBtn() {
  const b = $('#wfCenter'); if (!b) return;
  b.classList.toggle('hidden', !isAMFM(S.mode));
  b.classList.toggle('on', opts.fmCenter !== false);
}
$('#wfCenter').onclick = () => { opts.fmCenter = opts.fmCenter === false; saveOpts(); renderCenterBtn(); updateSpan(true); };
renderCenterBtn();
window.addEventListener('resize', sizeCanvases);

// ------------------------------------------------------------------ шумодав
const isFMmode = m => /FM/.test(m || '');            // FM, FM-N, DATA-FM, C4FM…
function pushGate() {
  if (!C.player) return;
  C.player.port.postMessage({ gate: {
    // на SSB/CW/AM/DATA шумодав не нужен — только в FM
    mode: isFMmode(S.mode) ? opts.sqMode : 'off',
    busy: S.busy === undefined ? true : !!S.busy, thr: opts.sqThr,
    tx: !!S.tx || (C.pttPressed && C.txOwner), tailMs: opts.tail,
  } });
}
function renderSq() {
  $('#sqRow').classList.toggle('hidden', !isFMmode(S.mode));
  $$('#sqMode button').forEach(b => b.classList.toggle('on', b.dataset.v === opts.sqMode));
  $('#sqRadioCtl').classList.toggle('hidden', opts.sqMode === 'level');
  $('#sqLevelCtl').classList.toggle('hidden', opts.sqMode !== 'level');
  if (S.sql != null && !(C.lockUntil.sql > performance.now())) $('#sqRadio').value = S.sql;
  $('#sqRadioV').textContent = S.sql ?? '—';
  $('#sqThr').value = opts.sqThr; $('#sqThrV').textContent = opts.sqThr + I18n.t(' дБ');
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
  if (C.ac) { if (C.ac.state === 'suspended' || C.ac.state === 'interrupted') await C.ac.resume(); return C.ac; }
  const AC = window.AudioContext || window.webkitAudioContext;
  let ac;
  try { ac = new AC({ latencyHint: 'interactive', sampleRate: 48000 }); }   // 48 кГц — как у трансивера
  catch { ac = new AC({ latencyHint: 'interactive' }); }
  await ac.audioWorklet.addModule('worklets.js');
  C.ac = ac;
  C.player = new AudioWorkletNode(ac, 'rx-player', { outputChannelCount: [2] });
  // A real media element lets mobile browsers treat RX as background playback.
  // Keep the worklet as the only signal path: never play both destinations.
  if (typeof ac.createMediaStreamDestination === 'function') {
    C.rxDestination = ac.createMediaStreamDestination();
    C.rxAudio = document.createElement('audio');
    C.rxAudio.setAttribute('playsinline', '');
    C.rxAudio.hidden = true;
    C.rxAudio.srcObject = C.rxDestination.stream;
    document.body.appendChild(C.rxAudio);
    C.player.connect(C.rxDestination);
  } else {
    C.player.connect(ac.destination);
  }
  C.player.port.postMessage({ rate: C.rate || 16000 });
  C.player.port.onmessage = e => {
    if (e.data.sq) {
      const q = e.data.sq;
      $('#sqLamp').className = 'sq-lamp ' + (C.audioOn ? (q.open ? 'open' : 'closed') : '');
      $('#sqLvl').textContent = C.audioOn ? I18n.t`${Math.round(q.lvl)} дБ` : '';
      return;
    }
    const s = e.data.stat; if (!s) return;
    $('#audioStat').textContent = I18n.t`Приём: ${C.rate / 1000} кГц, звуковая карта ${ac.sampleRate / 1000} кГц, буфер ${s.ms} мс, ` +
      I18n.t`пропусков ${s.under}, подстройка ${((s.adj - 1) * 100).toFixed(2)} %`;
  };
  C.player.port.postMessage({ gain: C.gain });
  pushGate();
  if (ac.state === 'suspended' || ac.state === 'interrupted') await ac.resume();
  return ac;
}
function updateReceiveAudio(on) {
  C.audioOn = on;
  C.player && C.player.port.postMessage({ reset: true });
  send({ t: 'audio', on, rate: C.rate });
  $('#btnAudio').textContent = on ? I18n.t('🔊 Звук') : I18n.t('🔇 Звук');
  $('#btnAudio').classList.toggle('on', on);
  store.set('audioOn', on);
  if (navigator.mediaSession) {
    try { navigator.mediaSession.playbackState = on ? 'playing' : 'paused'; } catch {}
  }
}
async function setReceiveAudio(on) {
  // Serialize user/lock-screen controls without allowing an old play() to undo Stop.
  const request = C.rxRequest = (C.rxRequest || 0) + 1;
  if (!on) {
    if (C.rxAudio) C.rxAudio.pause();
    updateReceiveAudio(false);
    return;
  }
  try {
    await ensureAudioContext();
    if (request !== C.rxRequest) return;
    if (C.rxAudio) await C.rxAudio.play();
    if (request !== C.rxRequest) {
      if (!C.audioOn && C.rxAudio) C.rxAudio.pause();
      return;
    }
    updateReceiveAudio(true);
    if (navigator.mediaSession && window.MediaMetadata) {
      try {
        navigator.mediaSession.metadata = new MediaMetadata({
          title: 'FTX-1', artist: I18n.t('Приём'),
          artwork: [{ src: new URL('icon-512.png', location.href).href, sizes: '512x512', type: 'image/png' }],
        });
      } catch {}
    }
  } catch (e) {
    if (request !== C.rxRequest) return;
    if (C.rxAudio) C.rxAudio.pause();
    updateReceiveAudio(false);
    toast(I18n.t('Звук недоступен: ') + e.message);
  }
}
$('#btnAudio').onclick = () => setReceiveAudio(!C.audioOn);
if (navigator.mediaSession) {
  for (const [action, on] of [['play', true], ['pause', false], ['stop', false]]) {
    try { navigator.mediaSession.setActionHandler(action, () => setReceiveAudio(on)); } catch {}
  }
}
// Recover interrupted contexts when returning to the app; never enable RX by itself.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && C.audioOn) setReceiveAudio(true);
});
$('#volume').value = C.gain * 100;
$('#volume').oninput = e => { C.gain = e.target.value / 100; store.set('vol', +e.target.value); C.player && C.player.port.postMessage({ gain: C.gain }); };

// ------------------------------------------------------------------ прошивки трансивера
function renderFw() {
  const names = ['MAIN', 'DISPLAY', 'SDR', 'DSP', 'SPA-1', 'FC-80'];
  const parts = names.map((n, i) => S['fw' + i] && S['fw' + i] !== '?' ? `${n} ${S['fw' + i]}` : '').filter(Boolean);
  $('#fwInfo').textContent = parts.length ? I18n.t('Прошивки трансивера: ') + parts.join(' · ') : '';
}

// ------------------------------------------------------------------ микрофон и PTT
function micErrorText(e) {
  const n = e && e.name, m = (e && e.message) || '';
  if (n === 'NotAllowedError' && /system/i.test(m))
    return I18n.t('Windows запрещает программам доступ к микрофону. Параметры Windows → Конфиденциальность и защита → ') +
      I18n.t('Микрофон: включите «Доступ к микрофону» и «Разрешить классическим приложениям доступ к микрофону», затем перезапустите браузер.');
  if (n === 'NotAllowedError' || n === 'SecurityError')
    return I18n.t('браузер запретил микрофон для этого сайта. Нажмите значок слева от адреса → «Микрофон» → разрешить и обновите страницу.');
  if (n === 'NotFoundError' || n === 'OverconstrainedError')
    return I18n.t('микрофон не найден. Подключите микрофон/гарнитуру или выберите другой в списке «Микрофон этого устройства» ') +
      I18n.t('(и проверьте, что он не отключён в Параметры → Система → Звук → Ввод).');
  if (n === 'NotReadableError' || n === 'AbortError')
    return I18n.t('микрофон занят или не запускается: закройте программы, которые держат его монопольно (Discord, Zoom, OBS), ') +
      I18n.t('или снимите галочку «Разрешить приложениям монопольный режим» в свойствах микрофона.');
  return (n ? n + ': ' : '') + m;
}
async function startMic() {
  if (C.micStream) return true;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast(I18n.t('Микрофон недоступен: откройте пульт по https:// (или http://localhost на самом ПК)'), 6000);
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
  } catch (e) {
    const why = micErrorText(e);
    toast(I18n.t('Нет доступа к микрофону: ') + why, 9000);
    const r = $('#micTestRes'); if (r) r.textContent = '✗ ' + why;
    return false;
  }
  const src = ac.createMediaStreamSource(C.micStream);
  const hp = new BiquadFilterNode(ac, { type: 'highpass', frequency: 120 });
  const lp = new BiquadFilterNode(ac, { type: 'lowpass', frequency: C.rate === 8000 ? 3200 : 3800, Q: 0.7 });
  C.micGainNode = new GainNode(ac, { gain: opts.micGain / 100 });
  C.micNode = new AudioWorkletNode(ac, 'mic-capture');
  C.micRate = C.rate; C.micNode.port.postMessage({ rate: C.rate });
  const sink = new GainNode(ac, { gain: 0 });
  src.connect(hp).connect(lp).connect(C.micGainNode).connect(C.micNode).connect(sink).connect(ac.destination);
  C.micNode.port.onmessage = e => {
    const { pcm, rms } = e.data;
    $('#micLevel').style.width = clamp((20 * Math.log10(rms + 1e-6) + 50) / 50, 0, 1) * 100 + '%';   // -50..0 дБ
    if (!C.txOwner || !C.pttPressed) return;
    // [3][частота/8000][PCM int16]: частота едет с каждым кадром — сервер не может её перепутать
    const b = new Uint8Array(2 + pcm.length * 2), dv = new DataView(b.buffer);
    b[0] = 3; b[1] = C.micRate / 8000;
    for (let i = 0; i < pcm.length; i++) dv.setInt16(2 + i * 2, clamp(pcm[i], -1, 1) * 32767, true);
    if (C.ws && C.ws.readyState === 1 && C.ws.bufferedAmount < 64000) C.ws.send(b);
  };
  updatePtt();
  listMics();
  return true;
}
async function pttDown() {
  if (C.pttPressed) return;
  if (C.pttBusy) { toast(I18n.t('Передача занята другим подключением')); return; }
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
  $('#pttState').textContent = C.pttBusy ? I18n.t('Передаёт другой клиент')
    : C.pttPressed ? (C.txOwner ? I18n.t('ПЕРЕДАЧА') : I18n.t('Включаю…'))
      : C.micStream ? I18n.t('Микрофон готов') : I18n.t('Микрофон — по PTT');
}
setInterval(() => {
  $('#txTimer').textContent = (C.txOwner && C.txStart) ? I18n.t`TX ${Math.floor((performance.now() - C.txStart) / 1000)} с` : '';
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
  window.addEventListener('blur', () => { if (opts.pttMode === 'hold') pttUp(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) pttUp(); });
})();

// ------------------------------------------------------------------ вкладки
$$('#tabs button').forEach(b => b.onclick = () => {
  $$('#tabs button').forEach(x => x.classList.toggle('on', x === b));
  $$('.tab').forEach(t => t.classList.toggle('on', t.id === 'tab-' + b.dataset.tab));
  if (b.dataset.tab === 'menu' && !menu.items) send({ t: 'menu_list' });
  C.tab = b.dataset.tab;
  document.body.classList.toggle('tab-ft8', C.tab === 'ft8');
  const hint = $('#wfHint');
  if (hint) hint.textContent = C.tab === 'ft8' ? I18n.t('Клик — RX, правый клик / Shift — TX, двойной — оба') : I18n.t('Двойной клик — настроиться на сигнал');
  window.ft8Desk && ft8Desk.marks();
  if (typeof updateSpan === 'function') updateSpan();
  requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
  store.set('tab', b.dataset.tab);
});

// ------------------------------------------------------------------ меню EX
const menu = { items: null, vals: {} };
function buildMenu(items) {
  menu.items = items;
  const groups = [...new Set(items.map(i => i.ex.slice(0, 4)))];
  const secName = {}; items.forEach(i => secName[i.ex.slice(0, 4)] = i.section);
  fillSelect($('#menuGroup'), [['', I18n.t('Все разделы')], ...groups.map(g => [g, `${g} ${MENU_GROUPS[g.slice(0, 2)] || ''}: ${secName[g]}`])]);
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
    const range = i.string ? I18n.t('текст') : `${i.min}…${i.max}`;
    const v = menu.vals[i.ex] ?? '';
    html += `<div class="mi" data-ex="${i.ex}"><span class="ex">EX${i.ex}</span><span class="nm">${i.name}${i.spa1 ? I18n.t(' <small>только с SPA-1</small>') : ''}<small>${range}${i.hint ? ' · ' + i.hint : ''}</small></span><input value="${v}" placeholder="?"><button class="btn small">✓</button></div>`;
  }
  if (list.length > 450) html += I18n.t`<p class="muted small">Показаны первые 450 из ${list.length}. Уточните поиск.</p>`;
  box.innerHTML = html || I18n.t('<p class="muted">Ничего не найдено</p>');
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
  if (l.length > 120 && !confirm(I18n.t`Прочитать ${l.length} пунктов? Это займёт около ${Math.ceil(l.length / 8)} с.`)) return;
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
    if (!isFinite(n) || n < item.min || n > item.max) { toast(I18n.t`Допустимо ${item.min}…${item.max}`); return; }
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
    row.innerHTML = I18n.t`<b>${i}</b><input class="text mono" maxlength="50"><button class="btn small">Записать</button><button class="btn small">▶ Передать</button>`;
    const inp = $('input', row); inp.value = texts[i - 1] || '';
    const [wr, play] = $$('button', row);
    inp.onchange = () => { texts[i - 1] = inp.value.toUpperCase(); inp.value = texts[i - 1]; store.set('cwMem', texts); };
    wr.onclick = () => { act('cw_store', [i, inp.value]); toast(I18n.t`Ячейка ${i} записана в трансивер`); };
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
    el.innerHTML = I18n.t`<button class="btn">${f.name ? f.name + ' · ' : ''}${fmtFreq(f.f)} ${f.m || ''}</button><button class="btn small" title="Удалить">×</button>`;
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
    fillSelect(sel, [['', I18n.t('По умолчанию')], ...devs.map((d, i) => [d.deviceId, d.label || I18n.t`Микрофон ${i + 1}`])]);
    sel.value = cur;
  } catch { }
}
function saveOpts() { store.set('opts', opts); }
function resetMic() {
  if (C.micStream) { C.micStream.getTracks().forEach(t => t.stop()); C.micStream = null; }
  updatePtt();
}
function initOpts() {
  $('#language').onchange = e => {
    if (!C.authed) { e.target.value = I18n.language; return; }
    send({ t: 'set_language', language: e.target.value });
  };
  $('#optTail').value = opts.tail; $('#optTailV').textContent = opts.tail;
  $('#optTail').oninput = e => { opts.tail = +e.target.value; $('#optTailV').textContent = opts.tail; saveOpts(); pushGate(); };
  $('#optScale').value = String(opts.scale);
  $('#optScale').onchange = e => { opts.scale = e.target.value; saveOpts(); applyScale(); };
  $('#optRate').value = C.rate;
  $('#optRate').onchange = e => {
    C.rate = +e.target.value; store.set('rate', C.rate);
    C.player && C.player.port.postMessage({ rate: C.rate });
    if (C.micNode) { C.micRate = C.rate; C.micNode.port.postMessage({ rate: C.rate }); }
    send({ t: 'audio', on: C.audioOn, rate: C.rate });
  };
  $('#optMic').onchange = e => { opts.micId = e.target.value; saveOpts(); resetMic(); };
  $('#micTest').onclick = async () => {
    $('#micTestRes').textContent = I18n.t('Включаю микрофон…');
    if (await startMic()) {
      const lab = C.micStream.getAudioTracks()[0]?.label || I18n.t('микрофон');
      $('#micTestRes').textContent = /USB Audio (Device|CODEC)/i.test(lab)
        ? I18n.t('⚠ Сейчас выбран «') + lab + I18n.t('» — это звуковая карта трансивера, а не ваш микрофон. Выберите свой в списке выше.')
        : I18n.t('✓ Работает: ') + lab + I18n.t('. Скажите что-нибудь — полоска слева от PTT должна двигаться.');
    }
  };
  $('#optMicGain').value = opts.micGain; $('#optMicGainV').textContent = opts.micGain;
  $('#optMicGain').oninput = e => { opts.micGain = +e.target.value; $('#optMicGainV').textContent = opts.micGain; saveOpts(); if (C.micGainNode) C.micGainNode.gain.value = opts.micGain / 100; };
  for (const [id, k] of [['optEcho', 'echo'], ['optNs', 'ns'], ['optAgc', 'agc']]) {
    $('#' + id).checked = opts[k];
    $('#' + id).onchange = e => { opts[k] = e.target.checked; saveOpts(); resetMic(); };
  }
  for (const [id, k] of [['optMuteTx', 'muteTx']]) {
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
  C.pw = $('#loginPw').value;              // запоминаем, только когда сервер пароль принял (hello_ok)
  $('#loginMsg').textContent = I18n.t('Проверяю…');
  if (C.ws && C.ws.readyState === 1) send({ t: 'hello', pw: C.pw, rate: C.rate || 16000 });
  else { try { C.ws && C.ws.close(); } catch { } connect(); }
};
$('#loginPw').addEventListener('keydown', e => { if (e.key === 'Enter') $('#loginBtn').click(); });
$('#btnPanel').onclick = () => window.open('panel.html', 'ftx1panel', 'width=1080,height=860');
$('#btnFull').onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen && document.documentElement.requestFullscreen().catch(() => { });
};
if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('sw.js').catch(() => { });

// ------------------------------------------------------------------ CW · Морзе: декодер, ИИ, передача
const cwDesk = (() => {
  const st = { dec: false, cyr: store.get('cwCyr', false), freq: 0, sq: store.get('cwSq', 15), auto: false,
    lastCall: '', lastRst: '599', analysis: null, op: {}, propTimer: null, txText: '', aiOk: null };
  const scr = $('#cwScreen');
  let cur = null;                                  // строка текущей передачи корреспондента
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const atBottom = () => scr.scrollTop + scr.clientHeight >= scr.scrollHeight - 30;
  function line(cls, text) {
    $('#cwHint')?.remove();
    const d = document.createElement('span'); d.className = cls; d.textContent = text;
    if (cur && cur.parentNode) scr.insertBefore(d, cur); else scr.appendChild(d);
    while (scr.children.length > 120) scr.removeChild(scr.firstChild);
    return d;
  }
  function curLine() {
    if (!cur || !cur.parentNode) { $('#cwHint')?.remove(); cur = document.createElement('span'); cur.className = 'rx-over cur'; scr.appendChild(cur); }
    return cur;
  }
  function addRx(ch) { const end = atBottom(); curLine().textContent += ch; if (end) scr.scrollTop = scr.scrollHeight; }
  function closeOver(item) {
    if (cur) { cur.classList.remove('cur'); cur.textContent = item.text; cur = null; }
    else line('rx-over', item.text);
    scr.scrollTop = scr.scrollHeight;
  }
  // ---- декодер
  function sendDec() { send({ t: 'cw_dec', on: st.dec, cyr: st.cyr, freq: st.freq, sq: st.sq }); }
  function renderDec() {
    $('#cwDecBtn').classList.toggle('on', st.dec);
    $('#cwDecBtn').textContent = st.dec ? I18n.t('● Декодер вкл') : I18n.t('○ Декодер выкл');
    $$('#cwAlpha button').forEach(b => b.classList.toggle('on', (b.dataset.v === 'cyr') === st.cyr));
    $('#cwSq').value = st.sq;
    const sel = $('#cwFreq');
    if (st.freq && ![...sel.options].some(o => +o.value === st.freq)) sel.add(new Option(st.freq + I18n.t(' Гц'), st.freq));
    sel.value = st.freq;
    if (!st.dec) { $('#cwInfo').textContent = '—'; C.cwMark = 0; drawOverlay(); }
  }
  function toggle(on = !st.dec) { st.dec = on; store.set('cwDec', on); sendDec(); renderDec(); if (on) note(I18n.t('Декодер Морзе включён')); }
  function setFreq(hz) {
    if (!st.dec) toggle(true);
    st.freq = clamp(hz, 200, 3000); sendDec(); renderDec(); note(I18n.t`Декодер слушает ${st.freq} Гц`);
  }
  const note = m => toast(m, 1600);
  for (let f = 400; f <= 1000; f += 50) $('#cwFreq').add(new Option(f + I18n.t(' Гц'), f));
  $('#cwDecBtn').onclick = () => toggle();
  $$('#cwAlpha button').forEach(b => b.onclick = () => { st.cyr = b.dataset.v === 'cyr'; store.set('cwCyr', st.cyr); sendDec(); renderDec(); });
  $('#cwFreq').onchange = e => { st.freq = +e.target.value; sendDec(); };
  $('#cwSq').oninput = e => { st.sq = +e.target.value; store.set('cwSq', st.sq); clearTimeout(e.target._t); e.target._t = setTimeout(sendDec, 150); };
  $('#cwClear').onclick = () => { scr.innerHTML = ''; cur = null; send({ t: 'cw_clear' }); };
  $('#cwNew').onclick = () => send({ t: 'cw_new' });

  // ---- ИИ
  function renderAnalysis(a) {
    st.analysis = a;
    if (!a) { $('#cwAiBody').innerHTML = I18n.t('<span class="muted small">Разбора пока нет.</span>'); return; }
    if (a.call) st.lastCall = a.call;
    const me = a.calls_me && a.calls_me_strict;
    const chip = (k, v) => v ? `<span class="chip">${k} <b>${esc(v)}</b></span>` : '';
    const kind = { cq: I18n.t('общий вызов CQ'), answer: I18n.t('ответ на вызов'), report: I18n.t('рапорт'), final: I18n.t('завершение связи') }[a.kind] || '';
    $('#cwAiBody').innerHTML = `<div class="clean">${esc(a.clean || a.raw)}</div><div class="ru">${esc(a.ru)}</div>
      <div class="cw-chips">${me ? I18n.t('<span class="chip me">Зовёт вас</span>') : ''}${chip(I18n.t('Позывной'), a.call)}${chip(I18n.t('Кому'), a.to)}
      ${chip(I18n.t('RST вам'), a.rst)}${chip(I18n.t('Имя'), a.name)}${chip('QTH', a.qth)}${kind ? `<span class="chip">${kind}</span>` : ''}
      ${a.confidence ? I18n.t`<span class="chip">уверенность <b>${Math.round(a.confidence * 100)}%</b></span>` : ''}</div>`;
  }
  function aiStatus(text, bad) { const s = $('#cwAiStatus'); s.textContent = text; s.style.color = bad ? 'var(--tx)' : ''; }
  $('#cwAnalyze').onclick = () => send({ t: 'cw_ai_analyze' });
  $('#cwReply').onclick = () => send({ t: 'cw_ai_reply' });

  // ---- предложение ответа (ИИ / автоответ)
  function showProposal(p) {
    clearInterval(st.propTimer);
    $('#cwProposal').classList.toggle('hidden', !p);
    if (!p) return;
    const tpl = p.model === I18n.t('шаблон');
    $('#cwPropTitle').textContent = (p.auto ? I18n.t('Автоответ') : I18n.t('Ответ')) + (tpl ? I18n.t(' (шаблон)') : I18n.t(' ИИ'));
    $('#cwPropText').textContent = p.text; $('#cwPropRu').textContent = p.ru || '';
    const end = performance.now() + p.left * 1000;
    const tick = () => {
      const s = Math.max(0, Math.ceil((end - performance.now()) / 1000));
      $('#cwPropCount').textContent = p.auto ? I18n.t`передача через ${s} с` : '';
    };
    tick(); if (p.auto) st.propTimer = setInterval(tick, 200);
    st.prop = p;
  }
  $('#cwPropSend').onclick = () => { if (st.prop) send({ t: 'cw_send', text: st.prop.text }); send({ t: 'cw_proposal_cancel' }); };
  $('#cwPropEdit').onclick = () => { if (st.prop) $('#cwText').value = st.prop.text; send({ t: 'cw_proposal_cancel' }); $('#cwText').focus(); };
  $('#cwPropCancel').onclick = () => send({ t: 'cw_proposal_cancel' });

  // ---- передача
  function fill(t) {
    const o = st.op;
    return t.replace(/\{CALL\}/g, st.lastCall || '?').replace(/\{MY\}/g, o.my_call || '?').replace(/\{NAME\}/g, o.my_name || '')
      .replace(/\{QTH\}/g, o.my_qth || '').replace(/\{RST\}/g, st.lastRst || '599').replace(/\s+/g, ' ').trim();
  }
  const MACROS = [
    ['CQ', 'CQ CQ CQ DE {MY} {MY} {MY} K'],
    [I18n.t('Ответ'), '{CALL} DE {MY} {MY} K'],
    [I18n.t('Рапорт'), '{CALL} DE {MY} GM TNX FER CALL UR RST {RST} {RST} NAME {NAME} {NAME} QTH {QTH} {QTH} HW? {CALL} DE {MY} KN'],
    ['73', '{CALL} DE {MY} TNX FER QSO 73 GL <SK>'],
    ['AGN?', 'AGN? AGN?'],
    ['QRZ?', 'QRZ? DE {MY} K'],
    [I18n.t('Позывной'), '{MY}'],
  ];
  $('#cwMacros').innerHTML = MACROS.map(([n, t], i) => `<button class="btn" data-i="${i}" title="${esc(t)}">${n}</button>`).join('');
  $('#cwMacros').onclick = e => {
    const b = e.target.closest('button'); if (!b) return;
    const tpl = MACROS[+b.dataset.i][1], t = fill(tpl);
    const missing = (tpl.includes('{CALL}') && !st.lastCall) || (tpl.includes('{MY}') && !st.op.my_call);
    if (e.shiftKey || missing) {                                          // Shift — только вставить в поле
      $('#cwText').value = t; $('#cwText').focus();
      if (missing) toast(!st.op.my_call ? I18n.t('Укажите свой позывной в разделе «Оператор»') : I18n.t('Позывной корреспондента ещё неизвестен — впишите его вместо «?»'), 3500);
    } else send({ t: 'cw_send', text: t });
  };
  function sendText() {
    const t = fill($('#cwText').value);
    if (!t) return;
    send({ t: 'cw_send', text: t });
  }
  $('#cwSend').onclick = sendText;
  $('#cwStop').onclick = () => stop();
  $('#cwText').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendText(); }
  });
  function stop() { send({ t: 'cw_stop' }); }
  function txStart(item) {
    line('tx-line', item.text);
    st.txText = item.text;
    $('#cwTxLine').classList.remove('hidden');
    txProgress(0);
    $('#cwText').value = '';
  }
  function txProgress(pos) {
    const t = st.txText;
    $('#cwTxLine').innerHTML = `<span class="done">${esc(t.slice(0, pos))}</span>${esc(t.slice(pos))}`;
  }
  // ---- автоответ и оператор
  $('#cwAuto').onchange = e => {
    if (e.target.checked && !(st.op.my_call)) { e.target.checked = false; toast(I18n.t('Сначала укажите свой позывной в разделе «Оператор»'), 3000); return; }
    send({ t: 'cw_auto', on: e.target.checked });
    if (e.target.checked && !st.dec) toggle(true);
  };
  const opIds = { opCall: 'my_call', opName: 'my_name', opQth: 'my_qth', opExtra: 'my_extra' };
  for (const [id, k] of Object.entries(opIds)) {
    $('#' + id).addEventListener('change', e => send({ t: 'cw_op', [k]: e.target.value }));
  }
  $('#cwAutoDelay').onchange = e => send({ t: 'cw_op', cw_auto_delay: +e.target.value });
  $('#cwAutoMax').onchange = e => send({ t: 'cw_op', cw_auto_max: +e.target.value });
  function renderOp(op, delay, max) {
    st.op = op || st.op;
    for (const [id, k] of Object.entries(opIds)) if (document.activeElement !== $('#' + id)) $('#' + id).value = st.op[k] || '';
    if (delay) $('#cwAutoDelay').value = delay;
    if (max) $('#cwAutoMax').value = max;
  }

  // ---- сообщения сервера
  function on(m) {
    switch (m.t) {
      case 'language': I18n.sync(m.language); break;
      case 'language_error': $('#language').value = m.language; toast(m.msg, 5000); break;
      case 'cw_state':
        st.freq = m.dec.freq || 0;
        renderOp(m.op, m.auto_delay, m.auto_max);
        scr.innerHTML = ''; cur = null;
        for (const h of m.history) line(h.dir === 'tx' ? 'tx-line' : 'rx-over', h.clean && h.dir === 'rx' ? h.text : h.text);
        if (m.over) curLine().textContent = m.over;
        if (!m.history.length && !m.over) scr.innerHTML = I18n.t('<span class="muted" id="cwHint">Включите декодер и настройтесь на телеграфную станцию: двойной клик по сигналу на водопаде (в режиме CW). Правый клик по сигналу — декодер будет слушать именно его.</span>');
        renderAnalysis(m.analysis); showProposal(m.proposal);
        if (m.busy_tx && m.tx_text) { st.txText = m.tx_text; $('#cwTxLine').classList.remove('hidden'); txProgress(0); }
        $('#cwAuto').checked = false;
        aiStatus(I18n.t`LM Studio: ${m.ai_url}${m.ai_model ? ' · ' + m.ai_model : ''} — проверяю…`);
        send({ t: 'cw_ai_test' });
        renderDec();
        break;
      case 'cw_rx': addRx(m.text); break;
      case 'cw_info': {
        $('#cwInfo').innerHTML = I18n.t`<span class="sig ${m.sig ? 'on' : ''}"></span>${m.wpm ? m.wpm + ' WPM · ' : ''}${m.freq} Гц${m.auto ? I18n.t(' (авто)') : ''} · ${m.snr} дБ`;
        if (C.cwMark !== m.freq) { C.cwMark = m.freq; drawOverlay(); }
        break;
      }
      case 'cw_over': closeOver(m.item); break;
      case 'cw_clear': if (cur) { cur.remove(); cur = null; } break;
      case 'cw_new': scr.innerHTML = ''; cur = null; renderAnalysis(null); st.lastCall = ''; toast(I18n.t('Новая связь')); break;
      case 'cw_dec': break;
      case 'cw_ai_busy': aiStatus(m.on ? m.what : (st.aiOk ? I18n.t('LM Studio: готов') : 'LM Studio'), false); $('#cwAiStatus').classList.toggle('cw-busy', m.on); break;
      case 'cw_ai': renderAnalysis(m.a); aiStatus(m.a.note ? m.a.note : (m.a.model === I18n.t('правила (без ИИ)') ? I18n.t('Разбор по правилам (ИИ выключен)') : I18n.t('LM Studio: готов · ') + (m.a.model || '')), !!m.a.note); break;
      case 'cw_ai_err': aiStatus(I18n.t('ИИ недоступен — работаю по правилам'), true); toast(m.msg, 6000); break;
      case 'cw_ai_test':
        st.aiOk = m.ok;
        aiStatus(m.ok ? I18n.t`LM Studio: готов · ${m.model || m.models[0] || I18n.t('модель не загружена')}` : m.msg, !m.ok);
        if (m.ok && !m.models.length) aiStatus(I18n.t('LM Studio работает, но модель не загружена — загрузите её в LM Studio'), true);
        break;
      case 'cw_proposal': showProposal(m.p); break;
      case 'cw_tx_start': txStart(m.item); break;
      case 'cw_tx_progress': txProgress(m.pos); break;
      case 'cw_tx_done':
        txProgress(st.txText.length);
        setTimeout(() => $('#cwTxLine').classList.add('hidden'), 1200);
        if (m.msg) toast(m.msg, 5000); else if (!m.ok) toast(I18n.t('Передача CW остановлена'));
        break;
      case 'cw_auto': if (!m.on) $('#cwAuto').checked = false; break;
      case 'cw_op': renderOp(m.op, m.auto_delay, m.auto_max); break;
      case 'cw_note': toast(m.msg, 4500); break;
    }
  }
  function resend() { if (store.get('cwDec', false)) { st.dec = true; sendDec(); } renderDec(); }
  return { on, toggle, setFreq, stop, resend };
})();

// ------------------------------------------------------------------ RTTY и метеофакс
const digiDesk = (() => {
  const st = { sub: store.get('dgSub', 'rtty'), rt: false, fx: false, fc: 2210, shift: 170, baud: 45.45,
    rows: [], W: 1809, fxState: 'idle', txText: '', slant: 0, fxC: 1900, fxD: 400 };
  const scr = $('#rtScreen'), cv = $('#fxCanvas'), wrap = $('#fxWrap');
  const RT_PRESETS = [
    ['', I18n.t('— выбрать —')],
    ['ham', I18n.t('Любители 45,45 Бод / 170 Гц'), { baud: 45.45, shift: 170 }],
    ['dwd1', I18n.t('DWD метео 4583 кГц (50/450)'), { baud: 50, shift: 450, f: 4583000 }],
    ['dwd2', I18n.t('DWD метео 7646 кГц (50/450)'), { baud: 50, shift: 450, f: 7646000 }],
    ['dwd3', I18n.t('DWD метео 10100,8 кГц (50/450)'), { baud: 50, shift: 450, f: 10100800 }],
    ['dwd4', I18n.t('DWD метео 11039 кГц (50/450)'), { baud: 50, shift: 450, f: 11039000 }],
    ['dwd5', I18n.t('DWD метео 14467,3 кГц (50/450)'), { baud: 50, shift: 450, f: 14467300 }],
    ['dwdlf', I18n.t('DWD метео 147,3 кГц ДВ (50/85)'), { baud: 50, shift: 85, f: 147300 }],
    ['h80', I18n.t('Любители 80 м — 3590 кГц'), { baud: 45.45, shift: 170, f: 3590000, lsb: true }],
    ['h40', I18n.t('Любители 40 м — 7043 кГц'), { baud: 45.45, shift: 170, f: 7043000, lsb: true }],
    ['h20', I18n.t('Любители 20 м — 14085 кГц'), { baud: 45.45, shift: 170, f: 14085000, lsb: true }],
    ['h15', I18n.t('Любители 15 м — 21085 кГц'), { baud: 45.45, shift: 170, f: 21085000, lsb: true }],
  ];
  const FX_PRESETS = [
    ['', I18n.t('— выбрать —')],
    ['d1', I18n.t('DWD Пиннеберг 3855 кГц'), 3855000], ['d2', I18n.t('DWD Пиннеберг 7880 кГц'), 7880000],
    ['d3', I18n.t('DWD Пиннеберг 13882,5 кГц'), 13882500],
    ['g1', I18n.t('Northwood GYA (Англия) 2618,5 кГц — ночью'), 2618500], ['g2', I18n.t('Northwood GYA 4610 кГц'), 4610000],
    ['g3', I18n.t('Northwood GYA 8040 кГц'), 8040000], ['g4', I18n.t('Northwood GYA 11086,5 кГц — днём'), 11086500],
    ['a1', I18n.t('Афины SVJ4 4481 кГц (утро)'), 4481000], ['a2', I18n.t('Афины SVJ4 8105 кГц (утро)'), 8105000],
    ['m1', I18n.t('Мурманск RKS-72 6328,5 кГц'), 6328500], ['m2', I18n.t('Мурманск RGS-47 8444 кГц'), 8444000],
    ['p1', I18n.t('С.-Петербург UGC 4212 кГц'), 4212000],
  ];
  const fill = (sel, list) => { sel.innerHTML = list.map(p => `<option value="${p[0]}">${p[1]}</option>`).join(''); };
  fill($('#rtPreset'), RT_PRESETS); fill($('#fxPreset'), FX_PRESETS);

  // ---- переключатель RTTY / факс
  const PANES = { rtty: 'dgRtty', fax: 'dgFax', nx: 'dgNx', sstv: 'dgSstv', psk: 'dgPsk' };
  if (!PANES[st.sub]) st.sub = 'rtty';
  function renderSub() {
    $$('#dgSw button').forEach(b => b.classList.toggle('on', b.dataset.v === st.sub));
    for (const [k, id] of Object.entries(PANES)) $('#' + id).classList.toggle('hidden', st.sub !== k);
  }
  $$('#dgSw button').forEach(b => b.onclick = () => {
    st.sub = b.dataset.v; store.set('dgSub', st.sub); renderSub(); marks(); window.digi2 && digi2.marks();
  });
  renderSub();

  // ---- метки на водопаде
  function marks() {
    const m = [];
    const rt = st.rt && (st.sub === 'rtty' || !st.fx), fx = st.fx && (st.sub === 'fax' || !st.rt);
    if (rt) { m.push([st.fc - st.shift / 2, '#ffb547'], [st.fc + st.shift / 2, '#4fc3ff']); }
    if (fx) { m.push([st.fxC - st.fxD, '#bbbbbb'], [st.fxC + st.fxD, '#ffffff']); }
    C.dgMarks = m; drawOverlay();
  }

  // ================= RTTY
  function sendRt(extra = {}) {
    send({ t: 'rt_dec', on: st.rt, baud: +$('#rtBaud').value, shift: +$('#rtShift').value, fc: +$('#rtFc').value,
      pol: +$('#rtPol').value, usos: $('#rtUsos').checked, ...extra });
  }
  function renderRt() {
    $('#rtDecBtn').classList.toggle('on', st.rt);
    $('#rtDecBtn').textContent = st.rt ? I18n.t('● Декодер вкл') : I18n.t('○ Декодер выкл');
  }
  $('#rtDecBtn').onclick = () => { st.rt = !st.rt; store.set('rtOn', st.rt); renderRt(); sendRt(); marks(); };
  ['#rtBaud', '#rtShift', '#rtFc', '#rtPol', '#rtUsos'].forEach(id => $(id).onchange = () => {
    store.set('rtCfg', { baud: $('#rtBaud').value, shift: $('#rtShift').value, fc: $('#rtFc').value, pol: $('#rtPol').value });
    st.fc = +$('#rtFc').value; st.shift = +$('#rtShift').value; sendRt(); marks();
  });
  const saved = store.get('rtCfg', null);
  if (saved) { $('#rtBaud').value = saved.baud; $('#rtShift').value = saved.shift; $('#rtFc').value = saved.fc; $('#rtPol').value = saved.pol; }
  st.fc = +$('#rtFc').value; st.shift = +$('#rtShift').value;
  $('#rtTune').onclick = () => { if (!st.rt) { st.rt = true; renderRt(); sendRt(); setTimeout(() => send({ t: 'rt_autotune' }), 2500); toast(I18n.t('Слушаю 2 с…')); } else send({ t: 'rt_autotune' }); };
  $('#rtClear').onclick = () => { scr.textContent = ''; };
  $('#rtPreset').onchange = e => {
    const p = RT_PRESETS.find(x => x[0] === e.target.value); if (!p || !p[2]) return;
    $('#rtBaud').value = String(p[2].baud); $('#rtShift').value = String(p[2].shift);
    st.shift = p[2].shift; st.fc = +$('#rtFc').value;
    if (p[2].f && p[2].lsb) {                       // любительский RTTY — по традиции в LSB, «частота» = mark
      setParam('mode', 'LSB'); setFreq(p[2].f + 2125);
      toast(I18n.t`LSB ${((p[2].f + 2125) / 1000).toFixed(3)} кГц: mark 2125, space 2295 Гц. Правый клик по сигналу — подстроить.`, 6000);
      $('#rtFc').value = 2210; st.fc = 2210;
    } else if (p[2].f) {                            // частота станции — середина между тонами; ставим её на центр декодера
      setParam('mode', 'USB'); setFreq(Math.round((p[2].f - st.fc) / 10) * 10);
      toast(I18n.t`USB, ${(p[2].f / 1000).toFixed(1)} кГц на центре ${st.fc} Гц. Расписание станций меняется — сверяйтесь со свежим.`, 6000);
    }
    if (!st.rt) { st.rt = true; renderRt(); }
    sendRt(); marks(); e.target.value = '';
  };
  function addText(t) {
    $('#rtHintS')?.remove();
    const end = scr.scrollTop + scr.clientHeight >= scr.scrollHeight - 30;
    scr.append(t.replace(/\r/g, ''));
    if (scr.textContent.length > 30000) scr.textContent = scr.textContent.slice(-20000);
    if (end) scr.scrollTop = scr.scrollHeight;
  }
  $('#rtAiBtn').onclick = () => {
    const t = scr.textContent.trim(); if (!t) return toast(I18n.t('Пока нечего переводить'));
    $('#rtAi').classList.remove('hidden'); $('#rtAiBody').textContent = I18n.t('Перевожу…'); $('#rtAiModel').textContent = '';
    send({ t: 'rt_ai', text: t.slice(-3000) });
  };
  $('#rtAiClose').onclick = () => $('#rtAi').classList.add('hidden');
  // передача
  const myCall = () => ($('#opCall').value || '').toUpperCase();
  const RT_MACROS = [['CQ', 'RYRYRY CQ CQ CQ DE {MY} {MY} {MY} PSE K'], ['RYRY', 'RYRYRYRYRYRYRYRYRY'],
    ['DE', 'DE {MY} {MY} K'], ['73', 'TNX FER QSO 73 GL DE {MY} SK']];
  $('#rtMacros').innerHTML = RT_MACROS.map((m, i) => `<button class="btn" data-i="${i}">${m[0]}</button>`).join('');
  $$('#rtMacros button').forEach(b => b.onclick = () => {
    const t = RT_MACROS[+b.dataset.i][1];
    if (t.includes('{MY}') && !myCall()) { toast(I18n.t('Укажите свой позывной на вкладке CW · Морзе')); return; }
    rtSend(t);
  });
  function rtSend(t) {
    t = (t || '').replace(/\{MY\}/g, myCall()).toUpperCase().trim();
    if (!t) return;
    send({ t: 'rt_send', text: '\n' + t + '\n', reverse: $('#rtRev').checked });
  }
  $('#rtSend').onclick = () => rtSend($('#rtText').value);
  $('#rtStop').onclick = () => send({ t: 'rt_stop' });
  $('#rtText').addEventListener('keydown', e => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); rtSend($('#rtText').value); } });
  function txProgress(p) {
    const el = $('#rtTxLine'), t = st.txText;
    el.innerHTML = `<span class="done">${esc(t.slice(0, p))}</span>${esc(t.slice(p))}`;
  }
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // ================= факс
  const ctx = cv.getContext('2d');
  let rowImg = null;
  function setW(W) {
    if (W === st.W && cv.width === W) return;
    st.W = W; cv.width = W; redraw();
  }
  function redraw() {
    const n = st.rows.length;
    cv.width = st.W; cv.height = Math.max(1, n);
    if (!n) { $('#fxEmpty').classList.remove('hidden'); return; }
    $('#fxEmpty').classList.add('hidden');
    const img = ctx.createImageData(st.W, n), d = img.data;
    for (let y = 0; y < n; y++) {
      const r = st.rows[y], o = y * st.W * 4;
      for (let x = 0; x < st.W; x++) { const v = r[x], k = o + x * 4; d[k] = d[k + 1] = d[k + 2] = v; d[k + 3] = 255; }
    }
    ctx.putImageData(img, 0, 0);
  }
  function addRow(i, r) {
    const n = st.rows.length;
    if (i < n) st.rows.length = i;                 // сервер начал заново
    st.rows.push(r);
    if (st.rows.length === 1 || cv.width !== r.length) { st.W = r.length; redraw(); return; }
    // растим холст порциями, чтобы не перерисовывать каждую строку
    if (cv.height < st.rows.length) {
      const keep = ctx.getImageData(0, 0, cv.width, cv.height);
      cv.height = st.rows.length + 60; ctx.fillStyle = '#0b1117'; ctx.fillRect(0, 0, cv.width, cv.height); ctx.putImageData(keep, 0, 0);
    }
    if (!rowImg || rowImg.width !== r.length) rowImg = ctx.createImageData(r.length, 1);
    const d = rowImg.data;
    for (let x = 0; x < r.length; x++) { const k = x * 4; d[k] = d[k + 1] = d[k + 2] = r[x]; d[k + 3] = 255; }
    ctx.putImageData(rowImg, 0, st.rows.length - 1);
    $('#fxEmpty').classList.add('hidden');
    const end = wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 40;
    if (end) wrap.scrollTop = wrap.scrollHeight;
  }
  function sendFx(center) {
    send({ t: 'fax_dec', on: st.fx, lpm: +$('#fxLpm').value, ioc: +$('#fxIoc').value, auto: $('#fxAuto').checked,
      afc: $('#fxAfc').checked, ...(center ? { center } : {}) });
  }
  function renderFx() {
    $('#fxDecBtn').classList.toggle('on', st.fx);
    $('#fxDecBtn').textContent = st.fx ? I18n.t('● Приём вкл') : I18n.t('○ Приём выкл');
  }
  $('#fxDecBtn').onclick = () => { st.fx = !st.fx; store.set('fxOn', st.fx); renderFx(); sendFx(); marks(); };
  ['#fxLpm', '#fxIoc', '#fxAuto', '#fxAfc'].forEach(id => $(id).onchange = () => sendFx());
  $('#fxCenter').onchange = () => sendFx(+$('#fxCenter').value);
  $('#fxStart').onclick = () => { if (!st.fx) { st.fx = true; renderFx(); sendFx(); marks(); } st.rows = []; redraw(); send({ t: 'fax_start' }); };
  $('#fxStop').onclick = () => send({ t: 'fax_stop' });
  const slant = (e, s) => send({ t: 'fax_slant', d: s * (e.shiftKey ? 10 : 50) });
  $('#fxSlM').onclick = e => slant(e, -1); $('#fxSlP').onclick = e => slant(e, 1);
  $('#fxPreset').onchange = e => {
    const p = FX_PRESETS.find(x => x[0] === e.target.value); if (!p || !p[2]) return;
    setParam('mode', 'USB'); setFreq(p[2] - 1900);
    $('#fxLpm').value = '120'; $('#fxIoc').value = '576';
    if (!st.fx) { st.fx = true; renderFx(); }
    sendFx(); marks();
    toast(I18n.t`USB ${((p[2] - 1900) / 1000).toFixed(1)} кГц (на 1,9 кГц ниже ${(p[2] / 1000).toFixed(1)}). Ждём тон старта — карты идут по расписанию DWD.`, 6000);
    e.target.value = '';
  };
  cv.addEventListener('click', e => {               // клик — здесь левый край карты
    const r = cv.getBoundingClientRect(), px = Math.round((e.clientX - r.left) / r.width * st.W);
    if (!st.rows.length) return;
    send({ t: 'fax_shift', px });
    st.rows = st.rows.map(row => { const o = new Uint8Array(row.length); o.set(row.subarray(px)); o.set(row.subarray(0, px), row.length - px); return o; });
    redraw(); toast(I18n.t('Край карты сдвинут'));
  });
  $('#fxPng').onclick = () => {
    if (!st.rows.length) return toast(I18n.t('Картинки пока нет'));
    const a = document.createElement('a');
    const d = new Date(), z = n => String(n).padStart(2, '0');
    a.download = `fax_${d.getFullYear()}${z(d.getMonth() + 1)}${z(d.getDate())}_${z(d.getHours())}${z(d.getMinutes())}.png`;
    const c2 = document.createElement('canvas'); c2.width = st.W; c2.height = st.rows.length;
    c2.getContext('2d').drawImage(cv, 0, 0);
    a.href = c2.toDataURL('image/png'); a.click();
  };
  $('#fxBig').onclick = () => { wrap.classList.toggle('big'); };
  $('#fxClose').onclick = () => wrap.classList.remove('big');
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && wrap.classList.contains('big')) wrap.classList.remove('big'); });
  const FX_ST = { idle: I18n.t('ждёт тон старта'), phasing: I18n.t('фазирование…'), image: I18n.t('приём карты') };

  // ================= сообщения
  function on(m) {
    switch (m.t) {
      case 'language': I18n.sync(m.language); break;
      case 'language_error': $('#language').value = m.language; toast(m.msg, 5000); break;
      case 'rt_state':
        st.fc = m.fc; st.shift = m.shift; st.baud = m.baud;
        $('#rtFc').value = m.fc; $('#rtShift').value = String(m.shift); $('#rtBaud').value = String(m.baud);
        marks(); break;
      case 'rt_rx': addText(m.text); break;
      case 'rt_info':
        $('#rtInfo').textContent = I18n.t`${m.fc} Гц · ${m.shift}/${m.baud} · ${m.pol} · ошибок ${m.err}%`;
        if (m.fc !== st.fc) { st.fc = m.fc; marks(); }
        break;
      case 'rt_note': toast(m.msg, 5000); break;
      case 'rt_tx_start':
        st.txText = m.text.trim(); $('#rtTxLine').classList.remove('hidden'); txProgress(0);
        addText('\n» ' + st.txText + '\n'); break;
      case 'rt_tx_progress': txProgress(m.pos); break;
      case 'rt_tx_done':
        txProgress(st.txText.length); setTimeout(() => $('#rtTxLine').classList.add('hidden'), 1200);
        if (m.msg) toast(m.msg, 6000); break;
      case 'rt_ai': $('#rtAiBody').textContent = m.ru; $('#rtAiModel').textContent = m.model || ''; break;
      case 'rt_ai_err': $('#rtAi').classList.remove('hidden'); $('#rtAiBody').textContent = I18n.t('Перевод недоступен: ') + m.msg + I18n.t(' Приём и передача RTTY работают и без ИИ.'); break;
      case 'fax_state':
        if (m.state === 'phasing' || (m.state === 'image' && !m.rows && st.fxState !== 'image')) { st.rows = []; redraw(); }
        st.fxState = m.state; if (m.W) st.W = m.W;
        if (m.slant !== undefined) $('#fxSlant').textContent = m.slant;
        if (m.lpm) $('#fxLpm').value = String(m.lpm);
        if (m.center) { st.fxC = m.center; st.fxD = m.dev || 400; $('#fxCenter').value = m.center; $('#fxAfc').checked = m.afc !== false; marks(); }
        if (m.ioc) $('#fxIoc').value = String(m.ioc);
        if (m.saved && m.saved.length) $('#fxSaved').textContent = I18n.t('Сохранено на сервере (папка fax): ') + m.saved.slice(-4).join(', ');
        $('#fxInfo').textContent = (FX_ST[m.state] || m.state) + (m.rows ? I18n.t` · ${m.rows} строк` : '');
        break;
      case 'fax_info': {
        const sig = m.q === undefined ? '' : m.q >= 0.9 ? I18n.t` · сигнал ✓ ${m.lo}–${m.hi} Гц` : I18n.t(' · сигнала факса нет (шум)');
        $('#fxInfo').textContent = (FX_ST[m.state] || m.state) + (m.rows ? I18n.t` · ${m.rows} строк` : '') + sig;
        $('#fxSlant').textContent = m.slant;
        if (m.center && (m.center !== st.fxC || m.dev !== st.fxD)) {
          st.fxC = m.center; st.fxD = m.dev || 400;
          if (document.activeElement !== $('#fxCenter')) $('#fxCenter').value = m.center;
          marks();
        }
        break;
      }
      case 'fax_note': toast(I18n.t('Факс: ') + m.msg, 4500); break;
      case 'fax_saved': toast(I18n.t('Факс сохранён: ') + m.path, 6000); break;
    }
  }
  function onBin(b) {
    const dv = new DataView(b.buffer, b.byteOffset);
    const a = dv.getUint16(1, true), W = dv.getUint16(3, true);
    if (b[0] === 5) addRow(a, new Uint8Array(b.slice(5, 5 + W)));
    else if (b[0] === 6) {
      st.rows = []; for (let y = 0; y < a; y++) st.rows.push(new Uint8Array(b.slice(5 + y * W, 5 + (y + 1) * W)));
      st.W = W; redraw(); wrap.scrollTop = wrap.scrollHeight;
    }
  }
  function pickAudio(hz) {                       // правый клик по водопаду на этой вкладке
    if (st.sub === 'nx' || st.sub === 'psk' || st.sub === 'sstv') return digi2.pick(st.sub, hz);
    if (st.sub === 'rtty') { $('#rtFc').value = Math.round(hz); st.fc = Math.round(hz); if (!st.rt) { st.rt = true; renderRt(); } sendRt(); marks(); toast(I18n.t`RTTY: центр ${st.fc} Гц`); }
    else { $('#fxCenter').value = Math.round(hz); st.fxC = Math.round(hz); st.fxD = 400; sendFx(st.fxC); marks(); toast(I18n.t`Факс: центр ${st.fxC} Гц (чёрный ${st.fxC - 400}, белый ${st.fxC + 400})`); }
  }
  function resend() {
    st.rt = store.get('rtOn', false); st.fx = store.get('fxOn', false);
    renderRt(); renderFx();
    send({ t: 'rt_hello' });
    if (st.rt) sendRt();
    if (st.fx) sendFx(); else send({ t: 'fax_hello' });
    marks();
  }
  function stop() { send({ t: 'rt_stop' }); if (wrap.classList.contains('big')) wrap.classList.remove('big'); }
  return { on, onBin, setFreq: pickAudio, resend, stop, sub: () => st.sub };
})();

// ------------------------------------------------------------------ горячие клавиши
// Клавиши привязаны к физическим кнопкам (event.code), поэтому работают в любой раскладке.
// Любое действие можно переназначить во вкладке «Настройки». Справка — «?» или F1.
const HOTKEYS = (() => {
  const MODE_CYCLE = ['LSB', 'USB', 'CW-U', 'CW-L', 'AM', 'FM', 'DATA-U', 'DATA-L'];
  const SPANS = [0, 3000, 4000, 6000, 8000, 12000, 24000];
  const note = m => toast(m, 1300);
  const onOff = v => v ? I18n.t('вкл') : I18n.t('выкл');
  const toggle = (k, name) => { const v = S[k] ? 0 : 1; setParam(k, v); renderControls({ [k]: v }); renderBadges(); note(`${name}: ${onOff(v)}`); };
  const setMode = m => { setParam('mode', m); renderBadges(); note(I18n.t('Вид работы: ') + m); setTimeout(rememberBand, 300); };
  const bandIdx = () => { const f = S.freq_a || 0; const i = BANDS.findIndex(b => f >= b[1] && f <= b[2]); return i; };
  const goBand = d => {
    let i = bandIdx();
    if (i < 0) { const f = S.freq_a || 0; i = d > 0 ? BANDS.findIndex(b => b[1] > f) : BANDS.map(b => b[2] < f).lastIndexOf(true); if (i < 0) return; }
    else i = (i + d + BANDS.length) % BANDS.length;
    $$('#bands .btn')[i].click(); note(I18n.t('Диапазон ') + $$('#bands .btn')[i].textContent);
  };
  const lvlToggle = (k, name) => {
    const cur = S[k] | 0;
    if (cur) { opts[k + 'Last'] = cur; saveOpts(); setParam(k, 0); note(name + I18n.t(': выкл')); }
    else { const v = opts[k + 'Last'] || 5; setParam(k, v); note(`${name}: ${v}`); }
    renderControls({ [k]: S[k] });
  };
  const A = [
    // id, группа, название, клавиши по умолчанию, действие, [отпускание], [повтор при удержании]
    ['ptt', I18n.t('Передача'), I18n.t('PTT — передача (держать)'), ['Space'],
      () => { opts.pttMode === 'toggle' ? (C.pttPressed ? pttUp() : pttDown()) : pttDown(); },
      () => { if (opts.pttMode === 'hold') pttUp(); }],
    ['stop', I18n.t('Передача'), I18n.t('Стоп: снять передачу, остановить тюнер и передачу CW, отменить автоответ'), ['Escape'],
      () => { pttUp(); if (S.tuning) act('tune_stop'); cwDesk.stop(); digiDesk.stop(); digi2.stop(); voiceCq.stop(); }],
    ['tuner', I18n.t('Передача'), I18n.t('Тюнер: настроить'), ['KeyT'], () => { act('tune'); note(I18n.t('Тюнер: настройка')); }],
    ['proc', I18n.t('Передача'), I18n.t('Компрессор PROC'), [], () => toggle('proc', 'PROC')],
    ['vox', I18n.t('Передача'), 'VOX', [], () => toggle('vox', 'VOX')],
    ['up', I18n.t('Частота'), I18n.t('Вверх на шаг'), ['ArrowUp', 'ArrowRight'], () => nudge(1), null, true],
    ['down', I18n.t('Частота'), I18n.t('Вниз на шаг'), ['ArrowDown', 'ArrowLeft'], () => nudge(-1), null, true],
    ['up10', I18n.t('Частота'), I18n.t('Вверх на 10 шагов'), ['PageUp', 'Shift+ArrowUp', 'Shift+ArrowRight'], () => nudge(10), null, true],
    ['down10', I18n.t('Частота'), I18n.t('Вниз на 10 шагов'), ['PageDown', 'Shift+ArrowDown', 'Shift+ArrowLeft'], () => nudge(-10), null, true],
    ['step+', I18n.t('Частота'), I18n.t('Шаг крупнее'), ['KeyS'], () => stepMove(1)],
    ['step-', I18n.t('Частота'), I18n.t('Шаг мельче'), ['Shift+KeyS'], () => stepMove(-1)],
    ['entry', I18n.t('Частота'), I18n.t('Ввод частоты с клавиатуры'), ['Enter', 'NumpadEnter'], () => { const i = $('#freqInput'); i.focus(); i.select(); }],
    ['band+', I18n.t('Частота'), I18n.t('Следующий диапазон'), ['BracketRight'], () => goBand(1)],
    ['band-', I18n.t('Частота'), I18n.t('Предыдущий диапазон'), ['BracketLeft'], () => goBand(-1)],
    ['lock', I18n.t('Частота'), I18n.t('Блокировка LOCK'), ['KeyK'], () => toggle('lock', 'LOCK')],
    ['mode+', I18n.t('Вид работы'), I18n.t('Следующий вид работы'), ['KeyM'], () => { const i = MODE_CYCLE.indexOf(S.mode); setMode(MODE_CYCLE[(i + 1) % MODE_CYCLE.length]); }],
    ['mode-', I18n.t('Вид работы'), I18n.t('Предыдущий вид работы'), ['Shift+KeyM'], () => { const i = MODE_CYCLE.indexOf(S.mode); setMode(MODE_CYCLE[(i - 1 + MODE_CYCLE.length) % MODE_CYCLE.length]); }],
    ['usb', I18n.t('Вид работы'), 'USB', ['KeyU'], () => setMode('USB')],
    ['lsb', I18n.t('Вид работы'), 'LSB', ['KeyL'], () => setMode('LSB')],
    ['cw', I18n.t('Вид работы'), I18n.t('CW (повторно — CW-U ⇄ CW-L)'), ['KeyC'], () => setMode(S.mode === 'CW-U' ? 'CW-L' : 'CW-U')],
    ['am', I18n.t('Вид работы'), 'AM', ['KeyA'], () => setMode('AM')],
    ['fm', I18n.t('Вид работы'), I18n.t('FM (повторно — FM ⇄ FM-N)'), ['KeyF'], () => setMode(S.mode === 'FM' ? 'FM-N' : 'FM')],
    ['data', I18n.t('Вид работы'), I18n.t('DATA (повторно — DATA-U ⇄ DATA-L)'), ['KeyD'], () => setMode(S.mode === 'DATA-U' ? 'DATA-L' : 'DATA-U')],
    ['swap', 'VFO', 'A ⇄ B', ['KeyV'], () => { act('swap'); note('A ⇄ B'); }],
    ['a2b', 'VFO', 'A → B', ['KeyB'], () => { act('a_to_b'); note('A → B'); }],
    ['split', 'VFO', 'SPLIT', ['KeyX'], () => toggle('split', 'SPLIT')],
    ['nb', I18n.t('Приёмник'), I18n.t('Подавитель импульсных помех NB'), ['KeyN'], () => lvlToggle('nb', 'NB')],
    ['nr', I18n.t('Приёмник'), I18n.t('Шумоподавление DNR'), ['KeyR'], () => lvlToggle('nr', 'DNR')],
    ['dnf', I18n.t('Приёмник'), I18n.t('Авто-режектор DNF'), ['KeyJ'], () => toggle('dnf', 'DNF')],
    ['att', I18n.t('Приёмник'), I18n.t('Аттенюатор ATT'), ['KeyO'], () => toggle('att', 'ATT')],
    ['preamp', I18n.t('Приёмник'), I18n.t('Предусилитель IPO → AMP1 → AMP2'), ['KeyP'], () => {
      const max = bandType() ? 1 : 2, v = ((ctlValue('preamp') | 0) + 1) % (max + 1);
      setParam('preamp', v); S['preamp' + bandType()] = v; renderControls({ preamp: v });
      note(I18n.t('Предусилитель: ') + (bandType() ? onOff(v) : ['IPO', 'AMP1', 'AMP2'][v]));
    }],
    ['rec', I18n.t('Приёмник'), I18n.t('Запись связи на ПК — старт/стоп'), ['KeyH'], () => qsoRec.toggle()],
    ['agc', I18n.t('Приёмник'), I18n.t('АРУ: FAST → MID → SLOW → AUTO'), ['KeyG'], () => {
      const v = (Math.min(S.agc | 0, 4) % 4) + 1; setParam('agc', v); renderControls({ agc: v }); note(I18n.t('АРУ: ') + ['OFF', 'FAST', 'MID', 'SLOW', 'AUTO'][v]);
    }],
    ['narrow', I18n.t('Приёмник'), I18n.t('Узкий фильтр NAR'), [], () => toggle('narrow', 'NAR')],
    ['sq', I18n.t('Приёмник'), I18n.t('Шумодав пульта (FM): выкл → по ТРХ → по уровню'), ['KeyQ'], () => {
      const L = ['off', 'radio', 'level']; opts.sqMode = L[(L.indexOf(opts.sqMode) + 1) % 3]; saveOpts(); renderSq(); pushGate();
      note(I18n.t('Шумодав: ') + { off: I18n.t('выкл'), radio: I18n.t('по трансиверу'), level: I18n.t('по уровню') }[opts.sqMode] + (isFMmode(S.mode) ? '' : I18n.t(' (работает только в FM)')));
    }],
    ['audio', I18n.t('Звук и экран'), I18n.t('Звук приёма вкл/выкл'), ['KeyZ'], () => $('#btnAudio').click()],
    ['vol+', I18n.t('Звук и экран'), I18n.t('Громкость +'), ['Equal', 'NumpadAdd'], () => volStep(10), null, true],
    ['vol-', I18n.t('Звук и экран'), I18n.t('Громкость −'), ['Minus', 'NumpadSubtract'], () => volStep(-10), null, true],
    ['span', I18n.t('Звук и экран'), I18n.t('Обзор водопада: Авто → 3k → … → 24k'), ['KeyW'], () => {
      const i = SPANS.indexOf(C.wfMax); C.wfMax = SPANS[(i + 1) % SPANS.length]; store.set('wfMax', C.wfMax); updateSpan(true);
      note(I18n.t('Обзор: ') + (C.wfMax ? C.wfMax / 1000 + I18n.t(' кГц') : I18n.t('авто')));
    }],
    ['full', I18n.t('Звук и экран'), I18n.t('Во весь экран'), [], () => $('#btnFull').click()],
    ['help', I18n.t('Звук и экран'), I18n.t('Справка по клавишам'), ['Shift+Slash', 'F1'], () => showHelp()],
    ['cwdec', 'CW', I18n.t('Декодер Морзе вкл/выкл'), ['KeyE'], () => cwDesk.toggle()],
    ['cwai', 'CW', I18n.t('ИИ: разобрать принятое'), ['KeyI'], () => send({ t: 'cw_ai_analyze' })],
    ['cwreply', 'CW', I18n.t('ИИ: составить ответ'), ['KeyY'], () => send({ t: 'cw_ai_reply' })],
    ['cwtext', 'CW', I18n.t('Перейти к тексту для передачи'), ['Backquote'], () => { $('#tabs button[data-tab="cw"]').click(); $('#cwText').focus(); }],
  ];
  for (let n = 1; n <= 5; n++) A.push(['cw' + n, 'CW', I18n.t`Передать CW-память ${n}`, ['Shift+Digit' + n], () => { act('cw_play', n); note(I18n.t('CW-память ') + n); }]);
  const actions = A.map(([id, group, name, def, down, up, repeat]) => ({ id, group, name, def, down, up, repeat }));

  function stepMove(d) {
    const sel = $('#step'), i = clamp(sel.selectedIndex + d, 0, sel.options.length - 1);
    sel.selectedIndex = i; sel.dispatchEvent(new Event('change')); note(I18n.t('Шаг: ') + sel.options[i].text);
  }
  function volStep(d) {
    const v = $('#volume'); v.value = clamp(+v.value + d, 0, 200); v.dispatchEvent(new Event('input')); note(I18n.t('Громкость: ') + v.value + ' %');
  }

  // --- раскладка клавиш: по умолчанию + переназначения пользователя
  let custom = store.get('keys', {});
  const keysOf = a => custom[a.id] ?? a.def;
  let map = new Map();
  function rebuild() { map = new Map(); for (const a of actions) for (const k of keysOf(a)) map.set(k, a); }
  rebuild();
  const comboOf = e => (e.ctrlKey ? 'Ctrl+' : '') + (e.altKey ? 'Alt+' : '') + (e.shiftKey ? 'Shift+' : '') + e.code;
  const NAMES = { Space: I18n.t('Пробел'), Enter: 'Enter', NumpadEnter: 'Num Enter', Escape: 'Esc', ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→',
    PageUp: 'PgUp', PageDown: 'PgDn', BracketLeft: '[', BracketRight: ']', Equal: '=', Minus: '−', NumpadAdd: 'Num +', NumpadSubtract: 'Num −',
    Slash: '/', Backslash: '\\', Backquote: '`', Semicolon: ';', Quote: "'", Comma: ',', Period: '.', Tab: 'Tab', Home: 'Home', End: 'End', Insert: 'Ins', Delete: 'Del' };
  function label(k) {
    if (k === 'Shift+Slash') return '?';
    const parts = k.split('+'), code = parts.pop();
    let n = NAMES[code] || code.replace(/^Key/, '').replace(/^Digit/, '').replace(/^Numpad/, 'Num ');
    return [...parts, n].join(' + ');
  }

  // --- обработка
  let capture = null;                         // переназначение: ждём нажатия
  let held = new Set();
  document.addEventListener('keydown', e => {
    if (capture) { e.preventDefault(); e.stopPropagation(); capture(e); return; }
    const t = e.target;
    const typing = t.matches('input[type=text], input[type=password], input[type=number], input:not([type]), select, textarea');
    if (typing) { if (e.key === 'Escape') t.blur(); return; }
    if (!$('#help').classList.contains('hidden') && (e.code === 'Escape' || e.code === 'F1' || comboOf(e) === 'Shift+Slash')) { e.preventDefault(); hideHelp(); return; }
    if (!opts.hotkeys) return;
    if (['ShiftLeft', 'ShiftRight', 'ControlLeft', 'ControlRight', 'AltLeft', 'AltRight', 'MetaLeft', 'MetaRight'].includes(e.code)) return;
    if (e.metaKey) return;
    // цифры — сразу начинают ввод частоты
    if (!e.ctrlKey && !e.altKey && !e.shiftKey && /^(Digit\d|Numpad\d|NumpadDecimal|Period)$/.test(e.code)) {
      const i = $('#freqInput'); i.focus(); i.value = e.code.includes('Decimal') || e.code === 'Period' ? '.' : e.code.slice(-1);
      e.preventDefault(); return;
    }
    const a = map.get(comboOf(e));
    if (!a) return;
    e.preventDefault();
    if (t.matches('input[type=range]')) t.blur();       // стрелки не должны двигать ползунок
    if (e.repeat && !a.repeat) return;
    held.add(e.code); a.down();
  }, true);
  document.addEventListener('keyup', e => {
    if (!held.has(e.code)) return;
    held.delete(e.code); e.preventDefault();             // чтобы пробел не «нажал» кнопку в фокусе
    for (const a of actions) if (a.up && keysOf(a).some(k => k.split('+').pop() === e.code)) a.up();
  }, true);

  // --- справка
  function helpHtml() {
    const groups = [...new Set(actions.map(a => a.group))];
    return groups.map(g => `<div class="hk-group"><h4>${g}</h4>` + actions.filter(a => a.group === g && keysOf(a).length).map(a =>
      `<div class="hk-row"><span>${a.name}</span><span>${keysOf(a).map(k => `<kbd>${label(k)}</kbd>`).join(' ')}</span></div>`).join('') + '</div>').join('') +
      I18n.t`<div class="hk-group"><h4>Ещё</h4><div class="hk-row"><span>Набрать частоту: цифры, затем Enter (7.074 — МГц, 14200 — кГц)</span><span><kbd>0</kbd>…<kbd>9</kbd></span></div>
       <div class="hk-row"><span>Колесо мыши над частотой — менять разряд под курсором</span><span><kbd>🖱</kbd></span></div></div>`;
  }
  function showHelp() { $('#helpBody').innerHTML = helpHtml(); $('#help').classList.remove('hidden'); }
  function hideHelp() { $('#help').classList.add('hidden'); }
  $('#help').addEventListener('click', e => { if (e.target.id === 'help' || e.target.closest('[data-close]')) hideHelp(); });
  $('#helpEdit').onclick = () => { hideHelp(); $('#tabs button[data-tab="opts"]').click(); $('#hkList').scrollIntoView({ block: 'start' }); };
  $('#btnKeys').onclick = showHelp;

  // --- переназначение в настройках
  function renderEditor() {
    const groups = [...new Set(actions.map(a => a.group))];
    $('#hkList').innerHTML = groups.map(g => `<h4>${g}</h4>` + actions.filter(a => a.group === g).map(a =>
      I18n.t`<div class="hk-edit" data-id="${a.id}"><span>${a.name}</span><span class="hk-keys">${keysOf(a).map(k => `<kbd>${label(k)}</kbd>`).join(' ') || '<span class="muted">—</span>'}</span>
       <button class="btn small" data-act="set">Назначить</button><button class="btn small" data-act="clear" title="Убрать клавиши">×</button></div>`).join('')).join('');
    $('#btnKeys').title = I18n.t('Горячие клавиши (?)');
    const pk = keysOf(actions[0]); $('#ptt').title = pk.length ? I18n.t('PTT — клавиша ') + pk.map(label).join(' / ') : 'PTT';
  }
  $('#hkList').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    const row = b.closest('.hk-edit'), a = actions.find(x => x.id === row.dataset.id);
    if (b.dataset.act === 'clear') { custom[a.id] = []; save(); return; }
    $('.hk-keys', row).innerHTML = I18n.t('<span class="hk-wait">Нажмите клавишу… (Esc — отмена)</span>');
    capture = ev => {
      if (['ShiftLeft', 'ShiftRight', 'ControlLeft', 'ControlRight', 'AltLeft', 'AltRight', 'MetaLeft', 'MetaRight'].includes(ev.code)) return;
      capture = null;
      if (ev.code === 'Escape' && !ev.shiftKey) { renderEditor(); return; }
      const k = comboOf(ev);
      const other = actions.find(x => x !== a && keysOf(x).includes(k));
      if (other) { custom[other.id] = keysOf(other).filter(x => x !== k); toast(I18n.t`«${label(k)}» снята с действия «${other.name}»`, 3000); }
      custom[a.id] = [k];
      save();
    };
  });
  function save() { store.set('keys', custom); rebuild(); renderEditor(); }
  $('#hkReset').onclick = () => { custom = {}; save(); toast(I18n.t('Клавиши по умолчанию')); };
  $('#optHotkeys').checked = opts.hotkeys;
  $('#optHotkeys').onchange = e => { opts.hotkeys = e.target.checked; saveOpts(); };
  renderEditor();
  return { showHelp };
})();

// ------------------------------------------------------------------ раскладка и масштаб
// На мониторе (окно шире 900 и выше 480 точек) всё помещается в окно без прокрутки страницы:
// слева дисплей и ручка, справа водопад на всю свободную площадь, вкладки — со своей прокруткой.
// «Авто»-масштаб подбирается так, чтобы левая колонка целиком влезала по высоте.
// На телефоне — обычная прокручиваемая колонка.
function setZoom(z) {
  const w = window.innerWidth, h = window.innerHeight, fit = w >= 900 && h >= 480;
  document.documentElement.style.zoom = z;
  C.zoom = z;
  const root = document.documentElement.style;
  root.setProperty('--app-h', (h / z) + 'px');
  document.body.classList.toggle('fit', fit);
  document.body.classList.toggle('wide', fit && w / z >= 1560);
  return fit;
}
function applyScale() {
  const w = window.innerWidth, h = window.innerHeight;
  let z = parseFloat(opts.scale);
  const auto = !(z > 0);
  if (auto) z = w < 760 ? 1 : clamp(Math.min(w / 1440, h / 860), 0.55, 2.4);
  const fit = setZoom(z);
  if (fit && auto) {
    z = Math.min(z, w / 840);                     // левая колонка + водопад по ширине
    setZoom(z);
    const cl = $('#colLeft');
    document.body.classList.add('measure');       // мерить со строкой шумодава, чтобы масштаб не прыгал
    for (let i = 0; i < 4; i++) {
      const need = cl.scrollHeight, have = cl.clientHeight;
      if (need <= have + 1) break;
      z = Math.max(0.5, z * have / need * 0.99);
      setZoom(z);
    }
    document.body.classList.remove('measure');
  }
  sizeCanvases();
}
let resizeT;
window.addEventListener('resize', () => { clearTimeout(resizeT); resizeT = setTimeout(applyScale, 60); });

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

// ------------------------------------------------------------------ голосовой автовызов
const voiceCq = (() => {
  const st = { slots: [], run: null, rec: null, recSlot: 0, fileSlot: 0, audio: null };
  const box = $('#vkSlots');
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  ['vkPause', 'vkRep', 'vkSig'].forEach(id => {
    const v = store.get(id, null); if (v != null) $('#' + id).value = v;
    $('#' + id).onchange = e => store.set(id, e.target.value);
  });
  function render() {
    box.innerHTML = st.slots.map(s => {
      const live = st.run && st.run.slot === s.slot, rec = st.recSlot === s.slot;
      return I18n.t`<div class="vk-slot${live ? ' live' : ''}${rec ? ' rec' : ''}" data-s="${s.slot}">
        <b class="mono">${s.slot}</b>
        <input class="nm" value="${esc(s.name)}" placeholder="${s.dur ? '' : I18n.t('пусто — запишите или загрузите')}" maxlength="40">
        <span class="dur mono muted">${s.dur ? s.dur.toFixed(1) + I18n.t(' с') : ''}</span>
        <button class="btn small" data-a="rec">${rec ? I18n.t('■ Стоп записи') : I18n.t('● Запись')}</button>
        <button class="btn small" data-a="file" title="Загрузить звуковой файл (wav, mp3, ogg…)">📁</button>
        <button class="btn small" data-a="play" ${s.dur ? '' : 'disabled'} title="Прослушать у себя">▶</button>
        <button class="btn small primary" data-a="go" ${s.dur ? '' : 'disabled'}>📢 В эфир</button>
        <button class="btn small" data-a="del" ${s.dur ? '' : 'disabled'} title="Удалить">✕</button>
      </div>`;
    }).join('');
    $$('.vk-slot', box).forEach(row => {
      const slot = +row.dataset.s;
      $$('button', row).forEach(b => b.onclick = () => action(b.dataset.a, slot, row));
      $('.nm', row).onchange = e => { const s = st.slots.find(x => x.slot === slot); if (s && s.dur) rename(slot, e.target.value); };
    });
    const r = st.run;
    $('#vkStatus').textContent = r ? `${r.name}: ${r.n}/${r.repeats} · ${r.phase === 'tx' ? I18n.t('передача') : I18n.t('слушаю, ещё ') + r.left + I18n.t(' с')}` : '—';
  }
  const nameOf = (slot, row) => ($('.nm', row)?.value || '').trim() || 'CQ ' + slot;
  async function action(a, slot, row) {
    if (a === 'rec') return st.recSlot === slot ? stopRec() : startRec(slot, row);
    if (a === 'file') { st.fileSlot = slot; st.fileName = nameOf(slot, row); $('#vkFile').value = ''; $('#vkFile').click(); return; }
    if (a === 'play') return send({ t: 'vk_get', slot });
    if (a === 'del') { if (confirm(I18n.t('Удалить сообщение ') + slot + '?')) send({ t: 'vk_delete', slot }); return; }
    if (a === 'go') {
      const sv = +$('#vkSig').value;
      const raw = sv ? (S_CAL.find(p => p[1] >= sv) || [130])[0] : 0;
      send({ t: 'vk_start', slot, pause: +$('#vkPause').value, repeats: +$('#vkRep').value, sig: raw });
    }
  }
  // ---- запись с микрофона этого устройства
  async function startRec(slot, row) {
    if (st.rec) return;
    if (!navigator.mediaDevices?.getUserMedia) return toast(I18n.t('Запись недоступна: откройте пульт по https:// или localhost'));
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: opts.micId ? { exact: opts.micId } : undefined,
        channelCount: 1, echoCancellation: opts.echo, noiseSuppression: opts.ns, autoGainControl: opts.agc } });
    } catch (e) { return toast(I18n.t('Нет доступа к микрофону: ') + micErrorText(e), 8000); }
    const chunks = [], mr = new MediaRecorder(stream);
    mr.ondataavailable = e => e.data.size && chunks.push(e.data);
    mr.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      st.rec = null; st.recSlot = 0; render();
      await upload(slot, st.recName, await new Blob(chunks).arrayBuffer());
    };
    st.rec = mr; st.recSlot = slot; st.recName = nameOf(slot, row);
    mr.start(); render(); toast(I18n.t('Говорите… «■ Стоп записи», когда закончите'));
    st.recTimer = setTimeout(() => st.rec && stopRec(), 120000);
  }
  function stopRec() { clearTimeout(st.recTimer); if (st.rec && st.rec.state !== 'inactive') st.rec.stop(); }
  $('#vkFile').onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    if (f.size > 30e6) return toast(I18n.t('Файл слишком большой'));
    await upload(st.fileSlot, st.fileName || f.name.replace(/\.[^.]+$/, ''), await f.arrayBuffer());
  };
  // ---- перекодирование в 48 кГц моно, обрезка тишины, нормализация -> сервер
  async function upload(slot, name, buf) {
    let dec;
    try { dec = await new AudioContext().decodeAudioData(buf); }
    catch (e) { return toast(I18n.t('Не удалось прочитать звук: ') + e.message); }
    const len = Math.ceil(dec.duration * 48000);
    if (dec.duration > 120) return toast(I18n.t('Сообщение длиннее 2 минут'));
    const oc = new OfflineAudioContext(1, Math.max(len, 1), 48000);
    const src = oc.createBufferSource(); src.buffer = dec;
    const hp = new BiquadFilterNode(oc, { type: 'highpass', frequency: 120 });
    const lp = new BiquadFilterNode(oc, { type: 'lowpass', frequency: 3000 });
    src.connect(hp).connect(lp).connect(oc.destination); src.start();
    let x = (await oc.startRendering()).getChannelData(0);
    // обрезаем тишину по краям (порог −40 дБ от пика)
    let peak = 0; for (const v of x) peak = Math.max(peak, Math.abs(v));
    if (peak < 1e-3) return toast(I18n.t('В записи тишина — проверьте микрофон'));
    const thr = peak * 0.01; let a = 0, b = x.length - 1;
    while (a < b && Math.abs(x[a]) < thr) a++;
    while (b > a && Math.abs(x[b]) < thr) b--;
    x = x.subarray(Math.max(0, a - 2400), Math.min(x.length, b + 4800));
    const k = 0.8 / peak, pcm = new Int16Array(x.length);
    for (let i = 0; i < x.length; i++) pcm[i] = Math.max(-1, Math.min(1, x[i] * k)) * 32767;
    let bin = ''; const u8 = new Uint8Array(pcm.buffer);
    for (let i = 0; i < u8.length; i += 0x8000) bin += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    send({ t: 'vk_save', slot, name, pcm: btoa(bin) });
  }
  function rename(slot, name) { send({ t: 'vk_rename', slot, name }); }
  function playLocal(m) {
    const bin = atob(m.pcm), pcm = new Int16Array(bin.length / 2), dv = new DataView(new ArrayBuffer(bin.length));
    for (let i = 0; i < bin.length; i++) dv.setUint8(i, bin.charCodeAt(i));
    for (let i = 0; i < pcm.length; i++) pcm[i] = dv.getInt16(i * 2, true);
    const ac = new AudioContext({ sampleRate: 48000 }), ab = ac.createBuffer(1, pcm.length, 48000), ch = ab.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    const s = ac.createBufferSource(); s.buffer = ab; s.connect(ac.destination); s.start();
    s.onended = () => ac.close();
  }
  $('#vkStop').onclick = () => send({ t: 'vk_stop' });
  function on(m) {
    if (m.t === 'vk_state') { st.slots = m.slots; st.run = m.run; render(); }
    else if (m.t === 'vk_note') toast(m.msg, 5000);
    else if (m.t === 'vk_audio') playLocal(m);
  }
  return { on, hello: () => send({ t: 'vk_hello' }), stop: () => { if (st.run) send({ t: 'vk_stop' }); stopRec(); }, running: () => !!st.run };
})();

// ------------------------------------------------------------------ запись связи на ПК
const qsoRec = (() => {
  const st = { on: false, t0: 0, k: '', timer: 0 };
  const btn = $('#btnRec');
  const mmss = s => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  const kb = n => n > 1048576 ? (n / 1048576).toFixed(1) + I18n.t(' МБ') : Math.round(n / 1024) + I18n.t(' КБ');
  function tick() { btn.textContent = st.on ? '⏺ ' + mmss(Math.floor((Date.now() - st.t0) / 1000)) : I18n.t('⏺ Запись'); }
  btn.onclick = () => send({ t: st.on ? 'rec_stop' : 'rec_start' });
  function on(m) {
    const was = st.on;
    st.on = m.on; st.k = m.k;
    if (m.on) st.t0 = Date.now() - m.elapsed * 1000;
    btn.classList.toggle('on', m.on);
    clearInterval(st.timer); if (m.on) st.timer = setInterval(tick, 500); tick();
    btn.title = m.on ? I18n.t('Идёт запись: ') + m.name + I18n.t(' — нажмите, чтобы остановить') : I18n.t('Записать связь (приём и передачу) в файл на ПК с сервером');
    $('#recList').innerHTML = (m.files || []).map(f =>
      `<div class="row"><a href="rec/${encodeURIComponent(f.name)}?k=${encodeURIComponent(m.k)}" download="${f.name}">💾 ${f.name}</a>
       <span class="muted">${mmss(f.dur)} · ${kb(f.size)}</span></div>`).join('') || I18n.t('<span class="muted">Записей пока нет</span>');
    if (was && !m.on && m.name) toast(I18n.t('Запись сохранена: ') + m.name + I18n.t(' (папка records)'), 6000);
  }
  return { on, hello: () => send({ t: 'rec_hello' }), toggle: () => btn.click() };
})();

// ------------------------------------------------------------------ NAVTEX, SSTV, PSK31
const digi2 = (() => {
  const st = { nx: false, sv: false, pk: false, nxFc: 1700, pkFc: 1000, txText: '', svW: 320, svH: 256, svRows: 0 };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fill = (sel, list) => { sel.innerHTML = list.map(p => `<option value="${p[0]}">${p[1]}</option>`).join(''); };
  const myCall = () => ($('#opCall').value || '').toUpperCase();
  function addText(el, hint, t) {
    $(hint)?.remove();
    const end = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.append(t);
    if (el.textContent.length > 40000) el.textContent = el.textContent.slice(-30000);
    if (end) el.scrollTop = el.scrollHeight;
  }
  // ---- метки на водопаде (только для открытого раздела)
  function marks() {
    const sub = digiDesk.sub(), m = [];
    if (sub === 'nx' && st.nx) m.push([st.nxFc - 85, '#ffb547'], [st.nxFc + 85, '#4fc3ff']);
    if (sub === 'psk' && st.pk) m.push([st.pkFc, '#3ddc84']);
    if (sub === 'sstv' && st.sv) m.push([1200, '#ff5a4d'], [1500, '#bbbbbb'], [2300, '#ffffff']);
    C.dgMarks2 = m; drawOverlay();
  }
  function pick(sub, hz) {
    hz = Math.round(hz);
    if (sub === 'nx') { st.nxFc = hz; $('#nxFc').value = hz; if (!st.nx) { st.nx = true; renderBtns(); } sendNx(); toast(I18n.t`NAVTEX: центр ${hz} Гц`); }
    else if (sub === 'psk') { st.pkFc = hz; $('#pkFc').value = hz; if (!st.pk) { st.pk = true; renderBtns(); } sendPk(); toast(I18n.t`PSK31: ${hz} Гц`); }
    else toast(I18n.t('SSTV: отдельная подстройка не нужна — сигнал занимает 1200–2300 Гц'));
    marks();
  }
  function renderBtns() {
    for (const [id, on] of [['#nxDecBtn', st.nx], ['#svDecBtn', st.sv], ['#pkDecBtn', st.pk]]) {
      $(id).classList.toggle('on', on); $(id).textContent = on ? I18n.t('● Приём вкл') : I18n.t('○ Приём выкл');
    }
  }

  // ================= NAVTEX
  const NX = [['', I18n.t('— выбрать —')],
    ['A', I18n.t('A · Новороссийск 518 кГц — 03,07,11,15,19,23 UTC'), 518000], ['B', I18n.t('B · Керчь 518 кГц — 01,05,09…'), 518000],
    ['C', I18n.t('C · Одесса 518 кГц — 02:30, 06:30…'), 518000], ['D', I18n.t('D · Стамбул 518 кГц — 00:30, 04:30…'), 518000],
    ['E', I18n.t('E · Самсун 518 кГц — 00:40, 04:40…'), 518000], ['J', I18n.t('J · Варна 518 кГц — 01:30, 05:30…'), 518000],
    ['I', I18n.t('I · Измир 518 кГц — 01:20, 05:20…'), 518000], ['L', I18n.t('L · Лимнос 518 кГц — 01:50, 05:50…'), 518000],
    ['n490', I18n.t('490 кГц — турецкие станции (на турецком)'), 490000],
    ['n4209', I18n.t('4209,5 кГц — Стамбул (M), Ираклион (S)'), 4209500]];
  fill($('#nxPreset'), NX);
  function sendNx() { send({ t: 'nx_dec', on: st.nx, fc: st.nxFc }); marks(); }
  $('#nxDecBtn').onclick = () => { st.nx = !st.nx; store.set('nxOn', st.nx); renderBtns(); sendNx(); };
  $('#nxFc').onchange = e => { st.nxFc = +e.target.value; sendNx(); };
  $('#nxPreset').onchange = e => {
    const p = NX.find(x => x[0] === e.target.value); if (!p || !p[2]) return;
    setParam('mode', 'USB'); setFreq(p[2] - 1700);
    st.nxFc = 1700; $('#nxFc').value = 1700; if (!st.nx) { st.nx = true; renderBtns(); }
    sendNx(); toast(I18n.t`USB ${((p[2] - 1700) / 1000).toFixed(1)} кГц. Сообщения идут по расписанию — время в списке (UTC).`, 6000);
    e.target.value = '';
  };
  $('#nxClear').onclick = () => { $('#nxScreen').textContent = ''; $('#nxMsgs').innerHTML = ''; };
  $('#nxAiBtn').onclick = () => {
    const t = $('#nxScreen').textContent.trim(); if (!t) return toast(I18n.t('Пока нечего переводить'));
    $('#nxAi').classList.remove('hidden'); $('#nxAiBody').textContent = I18n.t('Перевожу…'); send({ t: 'nx_ai', text: t.slice(-3000) });
  };
  $('#nxAiClose').onclick = () => $('#nxAi').classList.add('hidden');

  // ================= SSTV
  const SV = [['', I18n.t('— выбрать —')], ['s20', I18n.t('14,230 МГц USB — главная'), 14230000, 'USB'], ['s20b', I18n.t('14,233 МГц USB'), 14233000, 'USB'],
    ['s15', I18n.t('21,340 МГц USB'), 21340000, 'USB'], ['s10', I18n.t('28,680 МГц USB'), 28680000, 'USB'],
    ['s40', I18n.t('7,165 МГц LSB'), 7165000, 'LSB'], ['s80', I18n.t('3,730 МГц LSB'), 3730000, 'LSB'],
    ['iss', I18n.t('145,800 МГц FM — МКС (по объявлениям)'), 145800000, 'FM']];
  fill($('#svPreset'), SV);
  const cv = $('#svCanvas'), ctx = cv.getContext('2d');
  function sendSv() { send({ t: 'sstv_dec', on: st.sv, auto: $('#svAuto').checked, mode: $('#svMode').value }); marks(); }
  $('#svDecBtn').onclick = () => { st.sv = !st.sv; store.set('svOn', st.sv); renderBtns(); sendSv(); };
  $('#svAuto').onchange = sendSv; $('#svMode').onchange = sendSv;
  $('#svStop').onclick = () => send({ t: 'sstv_stop' });
  $('#svPreset').onchange = e => {
    const p = SV.find(x => x[0] === e.target.value); if (!p || !p[2]) return;
    setParam('mode', p[3]); setFreq(p[2]); if (!st.sv) { st.sv = true; renderBtns(); } sendSv();
    toast(I18n.t`${p[3]} ${(p[2] / 1e6).toFixed(3)} МГц — ждём картинку`); e.target.value = '';
  };
  function svSize(w, h) {
    st.svW = w; st.svH = h; st.svRows = 0;
    cv.width = w; cv.height = h; ctx.fillStyle = '#000'; ctx.fillRect(0, 0, w, h);
  }
  function svRow(i, rgb) {
    const w = rgb.length / 3, img = ctx.createImageData(w, 1);
    for (let x = 0; x < w; x++) { img.data[x * 4] = rgb[x * 3]; img.data[x * 4 + 1] = rgb[x * 3 + 1]; img.data[x * 4 + 2] = rgb[x * 3 + 2]; img.data[x * 4 + 3] = 255; }
    if (w !== cv.width) svSize(w, st.svH);
    ctx.putImageData(img, 0, i); st.svRows = i + 1;
  }
  $('#svPng').onclick = () => {
    if (!st.svRows) return toast(I18n.t('Картинки пока нет'));
    const a = document.createElement('a'); a.download = 'sstv_' + Date.now() + '.png'; a.href = cv.toDataURL('image/png'); a.click();
  };

  // ================= PSK31
  const PK = [['', I18n.t('— выбрать —')], ['80', I18n.t('80 м — 3580 кГц'), 3580000], ['40', I18n.t('40 м — 7040 кГц'), 7040000],
    ['30', I18n.t('30 м — 10142 кГц'), 10142000], ['20', I18n.t('20 м — 14070 кГц'), 14070000], ['17', I18n.t('17 м — 18100 кГц'), 18100000],
    ['15', I18n.t('15 м — 21070 кГц'), 21070000], ['12', I18n.t('12 м — 24920 кГц'), 24920000], ['10', I18n.t('10 м — 28120 кГц'), 28120000]];
  fill($('#pkPreset'), PK);
  function sendPk() { send({ t: 'psk_dec', on: st.pk, fc: st.pkFc, afc: $('#pkAfc').checked }); marks(); }
  $('#pkDecBtn').onclick = () => { st.pk = !st.pk; store.set('pkOn', st.pk); renderBtns(); sendPk(); };
  $('#pkFc').onchange = e => { st.pkFc = +e.target.value; sendPk(); };
  $('#pkAfc').onchange = sendPk;
  $('#pkPreset').onchange = e => {
    const p = PK.find(x => x[0] === e.target.value); if (!p || !p[2]) return;
    setParam('mode', 'DATA-U'); setFreq(p[2]); if (!st.pk) { st.pk = true; renderBtns(); } sendPk();
    toast('DATA-U ' + (p[2] / 1000) + I18n.t(' кГц. Правый клик по «ниточке» сигнала на водопаде — настроиться.'), 6000); e.target.value = '';
  };
  $('#pkClear').onclick = () => { $('#pkScreen').textContent = ''; };
  const PKM = [['CQ', 'CQ CQ CQ de {MY} {MY} {MY} pse k\n'], ['DE', 'de {MY} k\n'], ['73', 'tnx fer QSO, 73 de {MY} sk\n']];
  $('#pkMacros').innerHTML = PKM.map((m, i) => `<button class="btn" data-i="${i}">${m[0]}</button>`).join('');
  $$('#pkMacros button').forEach(b => b.onclick = () => {
    const t = PKM[+b.dataset.i][1]; if (!myCall()) return toast(I18n.t('Укажите свой позывной на вкладке CW · Морзе'));
    pkSend(t);
  });
  function pkSend(t) { t = (t || '').replace(/\{MY\}/g, myCall()); if (t.trim()) send({ t: 'psk_send', text: '\n' + t }); }
  $('#pkSend').onclick = () => pkSend($('#pkText').value);
  $('#pkStop').onclick = () => send({ t: 'psk_stop' });
  $('#pkText').addEventListener('keydown', e => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); pkSend($('#pkText').value); } });
  const txProg = p => { $('#pkTxLine').innerHTML = `<span class="done">${esc(st.txText.slice(0, p))}</span>${esc(st.txText.slice(p))}`; };

  // ================= сообщения
  function on(m) {
    switch (m.t) {
      case 'language': I18n.sync(m.language); break;
      case 'language_error': $('#language').value = m.language; toast(m.msg, 5000); break;
      case 'nx_state': if (m.fc) { st.nxFc = m.fc; $('#nxFc').value = m.fc; } break;
      case 'nx_rx': addText($('#nxScreen'), '#nxHint', m.text); break;
      case 'nx_info': $('#nxInfo').textContent = m.sync ? I18n.t`синхронизация ✓ · ${m.fc} Гц` : I18n.t`поиск сигнала · ${m.fc} Гц`; break;
      case 'nx_msg':
        $('#nxMsgs').insertAdjacentHTML('afterbegin', `<span class="m">${m.time} <b>${esc(m.b1)}${esc(m.b2)}${esc(m.num)}</b> ${esc(m.station || I18n.t('станция ') + m.b1)} — ${esc(m.subject || m.b2)}</span>`);
        addText($('#nxScreen'), '#nxHint', `\n──── ${m.station || m.b1}: ${m.subject || m.b2} (${m.time}) ────\n`); break;
      case 'nx_end': addText($('#nxScreen'), '#nxHint', I18n.t('\n──── конец сообщения ────\n')); break;
      case 'nx_ai': $('#nxAiBody').textContent = m.ru; $('#nxAiModel').textContent = m.model || ''; break;
      case 'nx_ai_err': $('#nxAi').classList.remove('hidden'); $('#nxAiBody').textContent = I18n.t('Перевод недоступен: ') + m.msg + I18n.t(' Приём NAVTEX работает и без ИИ.'); break;
      case 'sstv_state':
        if (m.modes && $('#svMode').options.length < 2) $('#svMode').insertAdjacentHTML('beforeend', m.modes.map(x => `<option>${x}</option>`).join(''));
        if (m.state === 'image' && m.w) svSize(m.w, m.h);
        $('#svInfo').textContent = m.state === 'image' ? I18n.t('приём: ') + m.mode : I18n.t('ждёт картинку');
        $('#svSaved').innerHTML = (m.saved || []).length ? I18n.t('<b>Сохранены на ПК (папка sstv):</b>') + m.saved.slice().reverse().map(n => `<div class="muted">${esc(n)}</div>`).join('') : '';
        break;
      case 'sstv_start':
        svSize(m.w, m.h); $('#svInfo').textContent = I18n.t`приём: ${m.mode} (${m.how}${m.shift ? I18n.t(', расстройка ') + m.shift + I18n.t(' Гц') : ''})`;
        toast(I18n.t('SSTV: началась картинка ') + m.mode); break;
      case 'sstv_done':
        $('#svInfo').textContent = I18n.t`${m.mode}: ${m.why}, ${m.rows} строк` + (m.slant ? I18n.t`, наклон ${m.slant} ppm` : '');
        if (m.name) { toast(I18n.t('SSTV сохранена: ') + m.name, 5000); $('#svSaved').insertAdjacentHTML('afterbegin', `<div class="muted">${esc(m.name)}</div>`); }
        break;
      case 'psk_state': if (m.fc) { st.pkFc = Math.round(m.fc); $('#pkFc').value = st.pkFc; } break;
      case 'psk_rx': addText($('#pkScreen'), '#pkHint', m.text); break;
      case 'psk_info':
        $('#pkInfo').textContent = I18n.t`${m.fc} Гц · качество ${Math.round(m.q * 100)}%`;
        if (Math.abs(m.fc - st.pkFc) >= 1) { st.pkFc = Math.round(m.fc); marks(); }
        break;
      case 'psk_tx_start': st.txText = m.text.trim(); $('#pkTxLine').classList.remove('hidden'); txProg(0); addText($('#pkScreen'), '#pkHint', '\n» ' + st.txText + '\n'); break;
      case 'psk_tx_progress': txProg(m.pos); break;
      case 'psk_tx_done': txProg(st.txText.length); setTimeout(() => $('#pkTxLine').classList.add('hidden'), 1200); if (m.msg) toast(m.msg, 6000); break;
    }
  }
  function onBin(b) {
    const dv = new DataView(b.buffer, b.byteOffset), a = dv.getUint16(1, true), W = dv.getUint16(3, true);
    if (b[0] === 7) svRow(a, b.subarray(5, 5 + W * 3));
    else if (b[0] === 8) { svSize(W, Math.max(st.svH, a)); for (let y = 0; y < a; y++) svRow(y, b.subarray(5 + y * W * 3, 5 + (y + 1) * W * 3)); }
  }
  function resend() {
    st.nx = store.get('nxOn', false); st.sv = store.get('svOn', false); st.pk = store.get('pkOn', false);
    renderBtns();
    if (st.nx) sendNx();
    send({ t: 'sstv_dec', on: st.sv, auto: $('#svAuto').checked, mode: $('#svMode').value });
    if (st.pk) sendPk();
    marks();
  }
  return { on, onBin, resend, pick, marks, stop: () => send({ t: 'psk_stop' }) };
})();
window.digi2 = digi2;

// ======================================================================= FT8 / FT4 (в духе JTDX / MSHV)
const ft8Desk = (() => {
  const st = { on: store.get('ftOn', false), mode: 'FT8', txHz: 1500, rxHz: store.get('ftRx', 1500), txEnable: false, tx: false,
    par: 0, clockOff: 0, rows: 0, rxRows: 0, qsoKey: '', txMsg: '', myCall: '', myGrid: '',
    rowCount: 0, slotRow: {}, lastK: null, labSpan: 0, labOn: store.get('ftWfLab', true) };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const list = $('#ftList'), rxList = $('#ftRxList'), lab = $('#ftLab');
  const SLOT = () => st.mode === 'FT4' ? 7.5 : 15;
  const BW = () => st.mode === 'FT4' ? 83 : 50;
  const now = () => Date.now() / 1000 + st.clockOff;
  const fmtUtc = slot => slot.slice(0, 2) + slot.slice(2, 4) + slot.slice(4, 6);
  const rpt = v => { v = clamp(Math.round(+v || 0), -30, 49); return (v < 0 ? '-' : '+') + String(Math.abs(v)).padStart(2, '0'); };
  const active = () => C.tab === 'ft8';

  // ---------------------------------------------------------------- кнопки
  function renderOn() {
    $('#ftOnBtn').classList.toggle('on', st.on);
    $('#ftOnBtn').textContent = st.on ? I18n.t('● Приём вкл') : I18n.t('○ Приём выкл');
  }
  const turnOn = () => { if (!st.on) { st.on = true; store.set('ftOn', true); renderOn(); send({ t: 'ft_hello', on: true }); } };
  $('#ftOnBtn').onclick = () => { st.on = !st.on; store.set('ftOn', st.on); renderOn(); send({ t: 'ft_hello', on: st.on }); };
  $$('#ftModeSw button').forEach(b => b.onclick = () => send({ t: 'ft_mode', mode: b.dataset.v }));
  $('#ftBand').onchange = e => {
    const kHz = +e.target.value; if (!kHz) return;
    if (S.mode !== 'DATA-U') setParam('mode', 'DATA-U');
    setFreq(Math.round(kHz * 1000)); turnOn();
    toast(I18n.t`${st.mode}: DATA-U ${kHz} кГц`); e.target.value = '';
  };
  $('#ftCq').onclick = () => { turnOn(); send({ t: 'ft_cq' }); };
  $('#ftEnable').onclick = () => send({ t: 'ft_enable', on: !st.txEnable });
  $('#ftHalt').onclick = () => send({ t: 'ft_halt' });
  $('#ftTune').onclick = () => send({ t: 'ft_tune', on: !st.tune });
  $('#ftAutoSeq').onchange = e => send({ t: 'ft_auto_seq', on: e.target.checked });
  $('#ftAutoCq').onchange = e => send({ t: 'ft_auto_cq', on: e.target.checked });
  $('#ftPar').onchange = e => send({ t: 'ft_par', par: +e.target.value });
  $('#ftTxHz').onchange = e => setTx(+e.target.value);
  $('#ftRxHz').onchange = e => setRx(+e.target.value);
  $('#ftTx2Rx').onclick = () => setRx(st.txHz);
  $('#ftRx2Tx').onclick = () => setTx(st.rxHz);
  $('#ftClear').onclick = () => { list.innerHTML = ''; st.rows = 0; };
  
  $('#ftClear2').onclick = () => { rxList.innerHTML = ''; st.rxRows = 0; };
  $('#ftFilter').value = store.get('ftFilter', 'all');
  $('#ftFilter').onchange = e => { store.set('ftFilter', e.target.value); applyFilter(); };
  $('#ftAlertSnd').checked = store.get('ftAlertSnd', false);
  $('#ftAlertSnd').onchange = e => store.set('ftAlertSnd', e.target.checked);
  $('#ftHeardBtn').onclick = () => send({ t: 'ft_heard' });
  $('#ftAdifClear').onclick = () => send({ t: 'ft_import_clear' });
  $('#ftAdifFile').onchange = async e => {
    const files = [...e.target.files]; e.target.value = '';
    if (!files.length) return;
    let recs = [];
    for (const f of files) recs = recs.concat(parseAdif(await f.text()));
    if (!recs.length) return toast(I18n.t('В файле не нашлось связей ADIF'));
    toast(I18n.t`Загружаю ${recs.length} связей…`);
    const N = 5000;
    for (let i = 0; i < recs.length || i === 0; i += N)
      send({ t: 'ft_import', recs: recs.slice(i, i + N), first: i === 0, last: i + N >= recs.length,
        replace: $('#ftAdifReplace').checked });
  };
  function parseAdif(text) {               // ADIF -> [[позывной, диапазон, вид, локатор]]
    const body = text.split(/<eoh>/i).pop(), out = [];
    for (const rec of body.split(/<eor>/i)) {
      const f = {}; const re = /<(\w+):(\d+)(?::\w)?>/g; let m;
      while ((m = re.exec(rec))) { const n = +m[2]; f[m[1].toLowerCase()] = rec.substr(re.lastIndex, n); re.lastIndex += n; }
      if (!f.call) continue;
      let band = (f.band || '').toLowerCase();
      if (!band && f.freq) band = bandOf(+f.freq * 1e6);
      out.push([f.call.trim().toUpperCase(), band, (f.submode || f.mode || '').toUpperCase(), (f.gridsquare || '').slice(0, 4).toUpperCase()]);
    }
    return out;
  }
  function bandOf(hz) {
    const B = [[1.8, 2, '160m'], [3.5, 4, '80m'], [5.3, 5.5, '60m'], [7, 7.3, '40m'], [10.1, 10.15, '30m'], [14, 14.35, '20m'],
      [18.068, 18.168, '17m'], [21, 21.45, '15m'], [24.89, 24.99, '12m'], [28, 29.7, '10m'], [50, 54, '6m'], [70, 71, '4m'], [144, 148, '2m'], [430, 440, '70cm']];
    const mhz = hz / 1e6; const b = B.find(x => mhz >= x[0] && mhz <= x[1]); return b ? b[2] : '';
  }
  function beep() {
    try {
      const a = new (window.AudioContext || window.webkitAudioContext)(), o = a.createOscillator(), g = a.createGain();
      o.frequency.value = 880; g.gain.value = 0.15; o.connect(g); g.connect(a.destination);
      o.start(); o.frequency.setValueAtTime(1320, a.currentTime + 0.12); o.stop(a.currentTime + 0.3);
      setTimeout(() => a.close(), 600);
    } catch (e) { /* без звука */ }
  }
  C.ftSecs = +store.get('ftWfSpeed', 20) || 0;
  $('#ftWfSpeed').value = String(C.ftSecs);
  $('#ftWfSpeed').onchange = e => { C.ftSecs = +e.target.value; store.set('ftWfSpeed', C.ftSecs); };
  $('#ftWfLab').checked = st.labOn;
  $('#ftWfLab').onchange = e => { st.labOn = e.target.checked; store.set('ftWfLab', st.labOn); marks(); };
  const CFG = { ftCall: 'my_call', ftGrid: 'my_grid', ftPwr: 'ft_pwr', ftTries: 'ft_tries', ftLevel: 'ft_tx_level',
    ftUdpOn: 'ft_udp_on', ftUdpHost: 'ft_udp_host', ftUdpPort: 'ft_udp_port', ftWide: 'ft_wide',
    ftPskOn: 'psk_on', ftPskAnt: 'psk_antenna', ftAlertDx: 'ft_alert_dxcc' };
  for (const [id, k] of Object.entries(CFG)) {
    $('#' + id).addEventListener('change', e => {
      const el = e.target;
      const v = el.type === 'checkbox' ? el.checked : (el.type === 'number' || el.type === 'range') ? +el.value
        : (id === 'ftUdpHost' || id === 'ftPskAnt') ? el.value.trim() : el.value.trim().toUpperCase();
      send({ t: 'ft_cfg', [k]: v });
    });
  }

  function setTx(hz) {
    hz = clamp(Math.round(hz), 200, 3000); st.txHz = hz; $('#ftTxHz').value = hz;
    send({ t: 'ft_freq', tx: hz }); marks();
  }
  function setRx(hz) {
    hz = clamp(Math.round(hz), 200, 3000); st.rxHz = hz; $('#ftRxHz').value = hz; store.set('ftRx', hz);
    $('#ftRxLbl').textContent = hz + I18n.t(' Гц');
    send({ t: 'ft_freq', rx: hz }); marks();
  }
  function pickWf(hz, how) {          // водопад: клик — RX, правый/Shift — TX, двойной — оба
    if (how !== 'tx') setRx(hz);
    if (how !== 'rx') setTx(hz);
  }
  function marks() {
    C.ftMarks = active() ? [[st.rxHz, '#3ddc84', BW()], [st.txHz, '#ff5a4d', BW()]] : [];
    lab.style.display = active() && st.labOn ? '' : 'none';
    drawOverlay(); drawLab();
  }

  // ---------------------------------------------------------------- стандартные сообщения Tx1…Tx6
  const MSG_N = 6;
  $('#ftMsgs').innerHTML = Array.from({ length: MSG_N }, (_, i) =>
    I18n.t`<div class="ft-msg" data-i="${i}"><button class="btn small" title="Передавать это сообщение">Tx${i + 1}</button><input class="text" maxlength="37"></div>`).join('') +
    I18n.t`<div class="ft-msg"><button class="btn small" id="ftFreeBtn" title="Передать свой текст">Свой</button><input class="text" id="ftText" maxlength="37" placeholder="свободный текст, Enter — передать"></div>`;
  const msgInputs = $$('#ftMsgs .ft-msg[data-i] input');
  function genMsgs(push = true) {
    const call = $('#ftDxCall').value.trim().toUpperCase(), grid = $('#ftDxGrid').value.trim().toUpperCase();
    const my = st.myCall || 'MYCALL', g = st.myGrid || '', r = rpt($('#ftRpt').value);
    const m = call ? [`${call} ${my} ${g}`.trim(), `${call} ${my} ${r}`, `${call} ${my} R${r}`, `${call} ${my} RR73`, `${call} ${my} 73`]
      : ['', '', '', '', ''];
    m.push(`CQ ${my} ${g}`.trim());
    msgInputs.forEach((el, i) => { el.value = m[i]; });
    if (push && call) send({ t: 'ft_dx', call, grid, rst: r });
    renderCur();
  }
  $('#ftGen').onclick = () => { if (!$('#ftDxCall').value.trim()) send({ t: 'ft_dx', call: '' }); genMsgs(); };
  ['#ftDxCall', '#ftDxGrid', '#ftRpt'].forEach(id => $(id).addEventListener('change', () => genMsgs()));
  $$('#ftMsgs .ft-msg[data-i] .btn').forEach(b => b.onclick = () => {
    const i = +b.parentElement.dataset.i, text = msgInputs[i].value.trim();
    if (!text) return toast(I18n.t('Сначала укажите DX позывной и нажмите «Сформировать»'));
    turnOn();
    if (i === 5) { send({ t: 'ft_cq' }); return; }
    const call = $('#ftDxCall').value.trim().toUpperCase();
    if (call) send({ t: 'ft_dx', call, grid: $('#ftDxGrid').value.trim().toUpperCase(), rst: rpt($('#ftRpt').value) });
    send({ t: 'ft_text', text });
  });
  const sendText = () => { const t = $('#ftText').value.trim(); if (t) { turnOn(); send({ t: 'ft_text', text: t }); } };
  $('#ftFreeBtn').onclick = sendText;
  $('#ftText').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); sendText(); } });
  function renderCur() {
    $$('#ftMsgs .ft-msg').forEach(r => { const inp = $('input', r); r.classList.toggle('cur', !!st.txMsg && inp.value.trim() === st.txMsg); });
  }

  // ---------------------------------------------------------------- часы и полоска периода
  setInterval(() => {
    const t = now();
    $('#ftClock').textContent = new Date(t * 1000).toISOString().slice(11, 19) + ' UTC';
    const s = SLOT(), k = Math.floor(t / s);
    $('#ftSlotBar').style.width = ((t - k * s) / s * 100).toFixed(1) + '%';
    $('#ftSlotBar').parentElement.classList.toggle('tx', st.tx);
  }, 200);

  // ---------------------------------------------------------------- водопад: время периодов и позывные
  // Метки хранятся с номером строки водопада и перерисовываются целиком на каждой новой строке.
  const labs = [];                         // {row, hz|null, text, col}
  function clearLab() { labs.length = 0; st.slotRow = {}; drawLab(); }
  function drawLab() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (!lab.clientWidth) return;
    const W = Math.round(lab.clientWidth * dpr), H = Math.round(lab.clientHeight * dpr);
    if (lab.width !== W || lab.height !== H) { lab.width = W; lab.height = H; }
    const ctx = lab.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    if (!active() || !st.labOn || mirrored()) return;
    const sh = H / (wf.rows || lab.clientHeight);
    ctx.font = `${11 * dpr}px Consolas, monospace`;
    while (labs.length && (st.rowCount - labs[0].row) * sh > H + 20 * dpr) labs.shift();
    for (const l of labs) {
      const y = (st.rowCount - l.row) * sh;
      if (l.hz == null) {                  // начало периода: линия и время
        ctx.fillStyle = 'rgba(255,255,255,.4)'; ctx.fillRect(0, y, W, Math.max(1, dpr));
        ctx.fillStyle = 'rgba(255,255,255,.85)'; ctx.fillText(l.text, 4 * dpr, y + 12 * dpr);
      } else {
        const x = l.hz / C.span * W + 2 * dpr, yy = y - l.dy * dpr;
        if (yy < 10 * dpr) continue;
        ctx.font = `bold ${11 * dpr}px Consolas, monospace`;
        ctx.lineWidth = 3 * dpr; ctx.strokeStyle = 'rgba(0,0,0,.8)'; ctx.strokeText(l.text, x, yy);
        ctx.fillStyle = l.col; ctx.fillText(l.text, x, yy);
        ctx.font = `${11 * dpr}px Consolas, monospace`;
      }
    }
  }
  function onRow() {                       // вызывается на каждую новую строку водопада
    st.rowCount++;
    const s = SLOT(), k = Math.floor(now() / s);
    if (k !== st.lastK) {
      st.lastK = k; st.slotRow[k] = st.rowCount;
      labs.push({ row: st.rowCount, hz: null, text: new Date(k * s * 1000).toISOString().slice(11, 19) });
      for (const kk in st.slotRow) if (kk < k - 40) delete st.slotRow[kk];
    }
    if (active() && st.labOn) drawLab();
  }
  function labelDecodes(m) {
    if (m.t0 == null) return;
    const r0 = st.slotRow[Math.round(m.t0 / SLOT())];
    if (r0 == null) return;
    const used = [];
    for (const it of m.items) {
      if (!it.call) continue;
      let dy = 4;                          // подпись чуть выше линии начала периода, без наложений
      while (used.some(u => Math.abs(u[0] - it.f) < 160 && u[1] === dy)) dy += 12;
      used.push([it.f, dy]);
      const col = it.tags.includes('me') ? '#ff6b6b' : it.tags.includes('newdxcc') ? '#ff7bff' : it.tags.includes('newband') ? '#ffb347'
        : it.tags.includes('cq') ? '#7dffa8' : '#ffffff';
      labs.push({ row: r0, hz: it.f, text: it.call, col, dy });
    }
    labs.sort((a, b) => a.row - b.row);
    drawLab();
  }

  // ---------------------------------------------------------------- списки расшифровок
  const nearRx = it => Math.abs(it.f - st.rxHz) <= BW() + 10;
  function applyFilter() {
    const f = $('#ftFilter').value, has = (r, ...c) => c.some(x => r.classList.contains(x));
    $$('#ftList .ft-row').forEach(r => r.classList.toggle('hidden',
      f === 'cq' ? !has(r, 'cq', 'me') : f === 'new' ? !has(r, 'me', 'newdxcc', 'newband', 'newcall') : false));
  }
  function row(it, utc, extra = '') {
    const r = document.createElement('div');
    r.className = 'ft-row ' + it.tags.join(' ') + extra;
    const badge = it.tags.includes('newdxcc') ? I18n.t('<b class="bd bd-dx" title="Новая страна">страна</b>')
      : it.tags.includes('newband') ? I18n.t('<b class="bd bd-band" title="Страна не сработана на этом диапазоне">диап.</b>') : it.tags.includes('newcall') ? I18n.t('<b class="bd bd-call" title="С этим позывным связи не было">нов.</b>') : '';
    r.innerHTML = `<span>${utc}</span><span class="n">${it.snr}</span><span class="n">${it.dt.toFixed(1)}</span><span class="n">${it.f}</span>` +
      `<span class="tx"><span class="msg">${esc(it.text)}</span>${badge}<span class="cty">${esc(it.country || '')}</span></span>`;
    r.title = (it.call ? I18n.t('Нажмите — позвать ') + it.call : '') + (it.country ? `\n${it.country}${it.cont ? ' (' + it.cont + ')' : ''}` : '') +
      (it.km != null ? I18n.t`, ${it.km} км` : '') + (it.tags.includes('worked') ? I18n.t('\nУже работали на этом диапазоне') : '') +
      (it.tags.includes('newgrid') ? I18n.t('\nНовый квадрат') : '');
    r.onclick = () => {
      if (!st.myCall) { toast(I18n.t('Укажите свой позывной в настройках FT8')); $('.ft-cfg').open = true; return; }
      turnOn(); setRx(it.f); send({ t: 'ft_call', item: it });
    };
    return r;
  }
  function trim(el, key) {
    while (st[key] > 500 && el.firstChild) { if (el.firstChild.classList.contains('ft-row')) st[key]--; el.firstChild.remove(); }
  }
  function sep(text) { const d = document.createElement('div'); d.className = 'ft-sep'; d.textContent = text; return d; }
  const seen = new Set();
  // списки прокручиваются к новым строкам, пока пользователь сам не отмотал вверх
  const follow = new Map([[list, true], [rxList, true]]);
  for (const el of [list, rxList]) el.addEventListener('scroll', () => follow.set(el, el.scrollTop + el.clientHeight >= el.scrollHeight - 30));
  const toEnd = el => { if (follow.get(el)) el.scrollTop = el.scrollHeight; };
  function addSlot(m) {
    const key = m.mode + m.slot;
    if (seen.has(key)) return;                // повтор при переподключении
    seen.add(key);
    $('#ftHint')?.remove();
    const utc = fmtUtc(m.slot), hh = `${m.slot.slice(0, 2)}:${m.slot.slice(2, 4)}:${m.slot.slice(4, 6)}`;
    list.append(sep(`────── ${hh} ${m.mode} ${m.items.length ? '' : I18n.t('· тишина ')}──────`));
    for (const it of m.items) { list.append(row(it, utc)); st.rows++; }
    trim(list, 'rows'); applyFilter();
    toEnd(list);
    const mine = m.items.filter(it => nearRx(it) || it.tags.includes('me'));
    if (mine.length) {
      for (const it of mine) { rxList.append(row(it, utc)); st.rxRows++; }
      trim(rxList, 'rxRows');
      toEnd(rxList);
    }
  }
  function addMine(text, slot) {
    const r = document.createElement('div');
    r.className = 'ft-row mine';
    r.innerHTML = `<span>${slot ? fmtUtc(slot) : ''}</span><span class="n">TX</span><span class="n"></span><span class="n">${st.txHz}</span><span class="tx">${esc(text)}</span>`;
    rxList.append(r); st.rxRows++;
    follow.set(rxList, true); toEnd(rxList);
  }
  function addLog(q) {
    const L = $('#ftLog'); L.querySelector('.muted')?.remove();
    const d = document.createElement('div');
    d.textContent = `${q.time}  ${q.call.padEnd(10)} ${(q.grid || '').padEnd(4)}  ${q.sent}/${q.rcvd}  ${q.band} ${q.mode}`;
    L.prepend(d);
    $('#ftLogInfo').textContent = '· ' + L.children.length;
  }

  // ---------------------------------------------------------------- состояние сервера
  function render(m) {
    st.mode = m.mode; st.txEnable = m.tx_enable; st.par = m.tx_par; st.tune = m.tune; st.txMsg = m.tx_msg || '';
    st.myCall = m.my_call || ''; st.myGrid = m.my_grid || '';
    if (document.activeElement !== $('#ftTxHz')) { st.txHz = m.tx_hz; $('#ftTxHz').value = m.tx_hz; }
    if (m.now) st.clockOff = m.now - Date.now() / 1000;
    $$('#ftModeSw button').forEach(b => b.classList.toggle('on', b.dataset.v === m.mode));
    const bandsHtml = I18n.t('<option value="">— выбрать —</option>') + m.bands.map(b => I18n.t`<option value="${b[1]}">${b[0]} — ${b[1]} кГц</option>`).join('');
    if ($('#ftBand').dataset.mode !== m.mode) { $('#ftBand').innerHTML = bandsHtml; $('#ftBand').dataset.mode = m.mode; }
    $('#ftEnable').textContent = m.tx_enable ? I18n.t('TX включён') : I18n.t('Включить TX');
    $('#ftEnable').classList.toggle('tx-on', m.tx_enable);
    $('#ftTune').classList.toggle('tune-on', !!m.tune);
    $('#ftTune').textContent = m.tune ? I18n.t('Настройка…') : I18n.t('Настройка');
    $('#ftAutoCq').checked = m.auto_cq; $('#ftAutoSeq').checked = m.auto_seq !== false;
    $('#ftPar').value = String(m.tx_par);
    $('#ftTxMsg').textContent = m.tx_msg || '—';
    const q = m.qso;
    const STEP = { call: I18n.t('зовём'), report: I18n.t('дали рапорт'), roger: I18n.t('подтвердили (R)'), rr73: I18n.t('RR73 — связь в логе'), '73': I18n.t('73 — связь в логе') };
    $('#ftQso').textContent = q ? I18n.t`Связь с ${q.call} ${q.grid || ''} · ${STEP[q.step] || q.step} · рапорт ${q.rst_sent || '—'} / ${q.rst_rcvd || '—'}` :
      (st.txMsg.startsWith('CQ') && m.tx_enable ? I18n.t('Зовём CQ…') : I18n.t('Связи нет'));
    // DX позывной / локатор / рапорт — как в JTDX: подставляются из текущей связи
    const key = q ? `${q.call}|${q.grid}|${q.rst_sent}` : '';
    if (key && key !== st.qsoKey && ![$('#ftDxCall'), $('#ftDxGrid'), $('#ftRpt')].includes(document.activeElement)) {
      $('#ftDxCall').value = q.call; $('#ftDxGrid').value = q.grid || '';
      if (q.rst_sent) $('#ftRpt').value = parseInt(q.rst_sent, 10);
      genMsgs(false);
    }
    st.qsoKey = key;
    if (!msgInputs[5].value) genMsgs(false);
    const set = (id, v) => { const el = $('#' + id); if (document.activeElement === el) return; if (el.type === 'checkbox') el.checked = v !== false && !!v; else el.value = v ?? ''; };
    set('ftCall', m.my_call); set('ftGrid', m.my_grid); set('ftPwr', m.pwr); set('ftUdpOn', m.udp_on);
    set('ftUdpHost', m.udp_host); set('ftUdpPort', m.udp_port); set('ftWide', m.wide);
    if (!m.lib) $('#ftQso').textContent = I18n.t('Модуль FT8 не загружен: ') + m.lib_err;
    set('ftPskOn', m.psk_on); set('ftPskAnt', m.psk_antenna); set('ftAlertDx', m.alert_dxcc);
    const w = m.worked || {};
    $('#ftWorkedInfo').textContent = I18n.t`${w.dxcc ?? 0} стран, ${w.calls ?? 0} позывных, ${w.qso ?? 0} связей` +
      (w.imported ? I18n.t` (из загруженного лога ${w.imported})` : I18n.t(' — загрузите свой журнал, иначе «новыми» считаются все'));
    $('#ftCtyInfo').textContent = m.cty && m.cty.n ? I18n.t`Справочник стран cty.dat: ${m.cty.n} стран, версия ${m.cty.date}`
      : I18n.t('Справочник стран cty.dat не найден — страны не определяются (программа скачает его сама при доступе в интернет)');
    if (st.myCall) $('#ftHeardMap').href = `https://pskreporter.info/pskmap.html?preset&callsign=${encodeURIComponent(st.myCall)}&timerange=1800&txrx=tx`;
    $('#ftHeardInfo').textContent = m.psk_on ? I18n.t`PSK Reporter, 30 мин · отправлено ${m.psk_sent}` : I18n.t('PSK Reporter выключен');
    renderCur(); marks();
  }

  function on(m) {
    switch (m.t) {
      case 'language': I18n.sync(m.language); break;
      case 'language_error': $('#language').value = m.language; toast(m.msg, 5000); break;
      case 'ft_state': render(m); break;
      case 'ft_dec':
        if (m.now) st.clockOff = m.now - Date.now() / 1000;
        addSlot(m);
        if (!m.old) labelDecodes(m);
        if (m.clock != null) {
          const el = $('#ftDt'); el.textContent = 'dt ' + (m.clock >= 0 ? '+' : '') + m.clock.toFixed(2) + I18n.t(' с');
          el.style.color = Math.abs(m.clock) > 0.7 ? 'var(--tx)' : Math.abs(m.clock) > 0.4 ? 'var(--warn)' : '';
        }
        break;
      case 'ft_tx':
        st.tx = m.on;
        $('#ftTxMsg').classList.toggle('on', m.on);
        if (m.on) addMine(m.msg, m.slot);
        break;
      case 'ft_qso': addLog(m.q); toast(I18n.t`В лог: ${m.q.call} ${m.q.sent}/${m.q.rcvd}`, 4000); break;
      case 'ft_log': $('#ftLog').innerHTML = m.items.length ? '' : I18n.t('<span class="muted">Пока нет</span>'); m.items.forEach(addLog); break;
      case 'ft_note': toast(m.msg, 6000); break;
      case 'ft_alert':
        toast(I18n.t`★ Новая страна: ${m.country} — ${m.call} (${m.f} Гц)`, 8000);
        if ($('#ftAlertSnd').checked) beep();
        break;
      case 'ft_heard': {
        const H = $('#ftHeard');
        if (m.err && !m.items.length) { H.innerHTML = I18n.t`<span class="muted">PSK Reporter не ответил: ${esc(m.err)}</span>`; break; }
        if (!m.items.length) { H.innerHTML = I18n.t('<span class="muted">За 30 минут вас никто не отметил</span>'); break; }
        const ago = t => { const s = Math.max(0, Date.now() / 1000 - t); return s < 3600 ? Math.round(s / 60) + I18n.t(' мин') : Math.round(s / 3600) + I18n.t(' ч'); };
        H.innerHTML = I18n.t`<div class="muted">${m.n} станций, дальше всех — ${esc(m.items[0].call)} ${m.items[0].km ?? '?'} км</div>` +
          m.items.map(r => `<div title="${esc(r.country || '')}">${esc(r.call.padEnd(10))} ${esc((r.grid || '').slice(0, 4).padEnd(4))} ` +
            I18n.t`${String(r.km ?? '').padStart(5)} км ${r.snr != null ? String(r.snr).padStart(3) + I18n.t(' дБ') : '      '} ${esc(r.band)} · ${ago(r.t)} назад</div>`).join('');
        break;
      }
    }
  }
  function resend() { renderOn(); send({ t: 'ft_hello', on: st.on }); send({ t: 'ft_freq', rx: st.rxHz }); }
  renderOn(); setRxLabel();
  function setRxLabel() { $('#ftRxHz').value = st.rxHz; $('#ftRxLbl').textContent = st.rxHz + I18n.t(' Гц'); }
  return { on, resend, setTx, pickWf, marks, onRow };
})();
window.ft8Desk = ft8Desk;
