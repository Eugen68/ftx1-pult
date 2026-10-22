'use strict';
/* Панель управления сервером FTX-1 Пульт (открывается только на ПК с трансивером). */
const $ = s => document.querySelector(s);
let ws, cfg = {}, ports = [], audio = [], quitting = false, saveT, lastStatus = {};

function toast(msg, ms = 2600) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), ms);
}
const send = o => ws && ws.readyState === 1 && ws.send(JSON.stringify(o));
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => send({ t: 'admin_hello' });
  ws.onclose = () => {
    if (quitting) { $('#gone').classList.remove('hidden'); setTimeout(() => window.close(), 800); return; }
    setLamp('bad', 'Нет связи с сервером', 'Программа закрыта? Запустите FTX-1 Пульт снова.');
    setTimeout(connect, 2000);
  };
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    switch (m.t) {
      case 'admin_denied':
        setLamp('bad', 'Панель доступна только на ПК с трансивером', ''); break;
      case 'admin_init': init(m); break;
      case 'admin_devices': ports = m.ports; audio = m.audio; fillDevices(); renderUrls(m.urls); applyDetectIfEmpty(m.detect); break;
      case 'admin_status': renderStatus(m.s); break;
      case 'admin_log': addLog(m.line); break;
      case 'admin_saved':
        cfg = m.cfg; flashSaved();
        if (m.port_change) toast('Новые порты заработают после перезапуска программы', 5000);
        break;
      case 'admin_autostart': $('#autostart').checked = !!m.on; break;
    }
  };
}

// ------------------------------------------------------------ инициализация
function init(m) {
  cfg = m.cfg; ports = m.ports; audio = m.audio;
  $('#ver').textContent = `версия ${m.version}`;
  fillDevices();
  for (const k of ['cat_baud', 'ptt_method', 'max_tx_seconds', 'http_port', 'https_port']) $('#' + k).value = cfg[k];
  for (const k of ['simulate', 'auto_connect', 'open_panel', 'open_pult']) $('#' + k).checked = !!cfg[k];
  $('#password').value = cfg.password || '';
  if (m.win) $('#autostart').checked = !!m.autostart; else $('#autostartRow').classList.add('hidden');
  renderUrls(m.urls);
  $('#log').innerHTML = ''; m.log.forEach(addLog);
  pttPortVisibility(); pwHint();
  if (m.first_run) {
    $('#wizard').classList.remove('hidden');
    applyDetect(m.detect, true);
  }
}
function applyDetect(d, quiet) {
  if (!d || !Object.keys(d).length) { if (!quiet) toast('FTX-1 не найден: проверьте кабель, питание и драйвер CP210x', 5000); return; }
  if (d.cat_port) $('#cat_port').value = d.cat_port;
  if (d.ptt_port) $('#ptt_port').value = d.ptt_port;
  if (d.audio_in_name) {
    cfg.audio_in_name = d.audio_in_name; cfg.audio_out_name = d.audio_out_name; cfg.audio_api = d.audio_api;
    fillAudio();
  }
  save();
  if (!quiet) toast('Найдено: ' + [d.cat_port && 'CAT ' + d.cat_port, d.audio_in_name && 'звук ' + d.audio_in_name].filter(Boolean).join(', '));
}
function applyDetectIfEmpty(d) {
  if (d && ((!cfg.cat_port && d.cat_port) || (!cfg.audio_in_name && d.audio_in_name))) applyDetect(d, true);
}

function opt(v, t, sel) { return `<option value="${esc(v)}"${sel ? ' selected' : ''}>${esc(t)}</option>`; }
function fillDevices() {
  const portOpts = (cur, prefer) => {
    let h = '';
    if (!ports.length) h += opt('', '— COM-порты не найдены —', !cur);
    else if (!cur) h += opt('', '— выберите —', true);
    for (const p of ports) {
      const star = p.kind === prefer ? '★ ' : '';
      h += opt(p.device, `${star}${p.device} — ${p.desc}`, p.device === cur);
    }
    if (cur && !ports.some(p => p.device === cur)) h += opt(cur, `${cur} (сейчас не подключён)`, true);
    return h;
  };
  $('#cat_port').innerHTML = portOpts(cfg.cat_port, 'enhanced');
  $('#ptt_port').innerHTML = portOpts(cfg.ptt_port, 'standard');
  fillAudio();
}
// Звук: отдельно вход (приём) и выход (передача). Значение опции — «имя|API».
const isCodec = n => /usb audio (codec|device)/i.test(n);
const coreName = n => { const m = /\(([^()]*)\)?\s*$/.exec(n); return ((m && m[1]) || n).replace(/^\s*\d+\s*-\s*/, '').trim().toLowerCase(); };
function fillAudio() {
  const build = (kind, curName) => {
    const list = audio.filter(d => d[kind] > 0);
    const curKey = curName ? curName + '|' + (cfg.audio_api || '') : '';
    let h = opt('', kind === 'in' ? 'Автоматически (USB Audio CODEC/Device)' : 'Автоматически (пара к входу)', !curName);
    const seen = new Set(); let found = !curName;
    // сначала устройства трансивера, потом остальные
    for (const d of [...list.filter(d => isCodec(d.name)), ...list.filter(d => !isCodec(d.name))]) {
      const key = d.name + '|' + d.api;
      if (seen.has(key)) continue; seen.add(key);
      const sel = curName && d.name === curName && (!cfg.audio_api || d.api === cfg.audio_api) && !found;
      if (sel) found = true;
      h += opt(key, `${isCodec(d.name) ? '★ ' : ''}${d.name} — ${d.api}`, sel);
    }
    if (!found) h += opt(curKey, `${curName} (не найдено)`, true);
    return h;
  };
  $('#audio_in').innerHTML = build('in', cfg.audio_in_name);
  $('#audio_out').innerHTML = build('out', cfg.audio_out_name);
  audioHint();
}
function audioHint() {
  const i = $('#audio_in').value.split('|')[0], o = $('#audio_out').value.split('|')[0];
  let t = '';
  if (i && !isCodec(i)) t = '⚠ Для приёма выбрано не USB-устройство трансивера.';
  else if (i && o && coreName(i) !== coreName(o)) t = '⚠ Вход и выход от разных устройств — проверьте, что оба от трансивера.';
  else if (!audio.some(d => isCodec(d.name))) t = '⚠ USB-звук трансивера не виден: проверьте кабель и нажмите ↻.';
  $('#audioHint').textContent = t;
}
$('#audio_in').addEventListener('change', () => {
  // подставить выход той же пары
  const [name, api] = $('#audio_in').value.split('|');
  if (name) {
    const pair = audio.find(d => d.out > 0 && d.api === api && coreName(d.name) === coreName(name));
    if (pair) $('#audio_out').value = pair.name + '|' + pair.api;
  } else $('#audio_out').value = '';
  audioHint();
});
$('#audio_out').addEventListener('change', audioHint);

// ------------------------------------------------------------ сохранение
function collect() {
  const [audio_in_name, api_in] = $('#audio_in').value.split('|');
  const [audio_out_name, api_out] = $('#audio_out').value.split('|');
  return {
    cat_port: $('#cat_port').value, cat_baud: +$('#cat_baud').value,
    audio_in_name, audio_out_name, audio_api: api_in || api_out || '', audio_in_index: null, audio_out_index: null,
    ptt_method: $('#ptt_method').value, ptt_port: $('#ptt_port').value,
    simulate: $('#simulate').checked, password: $('#password').value,
    auto_connect: $('#auto_connect').checked, open_panel: $('#open_panel').checked, open_pult: $('#open_pult').checked,
    max_tx_seconds: Math.max(10, Math.min(1800, +$('#max_tx_seconds').value || 180)),
    http_port: +$('#http_port').value || 8080, https_port: +$('#https_port').value || 8443,
  };
}
function save() { clearTimeout(saveT); saveT = setTimeout(() => send({ t: 'admin_save', cfg: collect() }), 350); }
function flashSaved() { const s = $('#saved'); s.classList.add('show'); clearTimeout(s.t); s.t = setTimeout(() => s.classList.remove('show'), 1500); }
document.querySelectorAll('select, input').forEach(el => {
  if (el.id === 'autostart') return;
  el.addEventListener(el.matches('input[type=text], input[type=password], input[type=number]') ? 'input' : 'change', () => {
    pttPortVisibility(); pwHint(); save();
  });
});
function pttPortVisibility() { $('#pttPortField').classList.toggle('hidden', !/RTS|DTR/.test($('#ptt_method').value)); }
function pwHint() {
  const pw = $('#password').value;
  $('#pwHint').textContent = !pw ? '⚠ Пароль не задан — с телефона войти нельзя (это безопасно, но неудобно).'
    : pw.length < 6 ? '⚠ Короткий пароль — лучше 6+ символов.' : '';
}
$('#pwShow').onclick = () => { const p = $('#password'); p.type = p.type === 'password' ? 'text' : 'password'; };
$('#pwGen').onclick = () => {
  const a = 'abcdefghjkmnpqrstuvwxyz23456789', r = crypto.getRandomValues(new Uint8Array(8));
  $('#password').value = [...r].map(x => a[x % a.length]).join('');
  $('#password').type = 'text'; pwHint(); save();
};
$('#autostart').onchange = e => send({ t: 'admin_autostart', on: e.target.checked });
$('#btnRefresh').onclick = () => { send({ t: 'admin_refresh' }); toast('Обновляю список устройств…', 1200); };
$('#btnDetect').onclick = () => {
  send({ t: 'admin_refresh' });
  const h = e => { const m = JSON.parse(e.data); if (m.t === 'admin_devices') { ws.removeEventListener('message', h); applyDetect(m.detect, false); } };
  ws.addEventListener('message', h);
};
$('#btnRun').onclick = () => {
  if (lastStatus.running) send({ t: 'admin_stop' });
  else { clearTimeout(saveT); send({ t: 'admin_save', cfg: collect() }); send({ t: 'admin_start' }); $('#wizard').classList.add('hidden'); }
};
$('#btnPult').onclick = () => send({ t: 'admin_open_pult' });
$('#btnFolder').onclick = () => send({ t: 'admin_open_folder' });
$('#btnQuit').onclick = () => {
  if (!confirm('Выйти из программы? Связь с трансивером будет остановлена, пульты отключатся.')) return;
  quitting = true; send({ t: 'admin_quit' });
};

// ------------------------------------------------------------ состояние
function setLamp(cls, title, sub) {
  $('#lamp').className = 'lamp ' + cls; $('#stTitle').textContent = title; $('#stSub').textContent = sub || '';
}
const fmtF = f => { const s = String(Math.round(f)).padStart(7, '0'); return `${s.slice(0, -6)}.${s.slice(-6, -3)}.${s.slice(-3)}`; };
function renderStatus(s) {
  lastStatus = s;
  const b = $('#btnRun');
  b.disabled = s.busy;
  b.className = 'btn ' + (s.running ? 'stop' : 'go');
  b.textContent = s.busy ? '… Подождите' : s.running ? '■ Остановить' : '▶ Запустить';
  if (s.busy) setLamp('warn', 'Подключаюсь…', '');
  else if (!s.running) setLamp(s.error ? 'bad' : '', s.error ? 'Связь не установлена' : 'Связь с трансивером остановлена',
    s.error ? '' : 'Нажмите «Запустить»');
  else if (s.tx) setLamp('tx', 'ПЕРЕДАЧА', s.ptt_owner ? 'с ' + s.ptt_owner : '');
  else if (s.connected) setLamp('ok', s.sim ? 'Работает (симулятор)' : 'Трансивер на связи',
    [s.radio_id === '0840' ? 'FTX-1' : '', s.head === 2 ? 'Optima/SPA-1' : s.head === 1 ? 'Field' : '', s.fw ? 'прошивка ' + s.fw : ''].filter(Boolean).join(' · '));
  else setLamp('warn', 'Трансивер не отвечает', 'Включён? Тот ли COM-порт и скорость CAT?');
  $('#stFreq').textContent = s.running && s.connected && s.freq ? `${fmtF(s.freq)}  ${s.mode || ''}` : '';
  const err = $('#stErr');
  err.textContent = s.error || (s.running && s.audio_err ? 'Звук: ' + s.audio_err : '');
  err.classList.toggle('hidden', !err.textContent);
  $('#cCat').className = s.running ? (s.connected ? 'ok' : 'warn') : '';
  $('#cAudio').className = s.running ? (s.audio_ok ? 'ok' : 'bad') : '';
  $('#cTx').className = s.tx ? 'bad' : (s.running ? 'ok' : '');
  $('#cTxT').textContent = s.tx ? 'Передача' : 'Приём';
  $('#cCl').className = s.clients.length ? 'ok' : '';
  $('#cClT').textContent = 'Пультов: ' + s.clients.length;
  $('#clCount').textContent = s.clients.length ? `(${s.clients.length})` : '';
  $('#clients').innerHTML = s.clients.length ? s.clients.map(c =>
    `<div class="cl">${c.tx ? '🔴' : c.local ? '🖥' : '📱'} <span>${esc(c.name.split(':')[0])}</span>
     <span class="muted small">${c.local ? 'этот ПК' : 'по сети'}${c.audio ? ' · звук' : ''}${c.tx ? ' · ПЕРЕДАЁТ' : ''}</span></div>`).join('')
    : '<span class="muted small">Никого</span>';
  document.title = (s.tx ? '🔴 ' : '') + 'FTX-1 Пульт — сервер';
}
function renderUrls(urls) {
  $('#urls').innerHTML = urls.map(u => `<div class="url"><span title="${u.kind === 'local' ? 'на этом ПК' : 'для телефона и других ПК'}">${u.kind === 'local' ? '🖥' : '📱'}</span>
    <code>${esc(u.url)}</code><button class="btn small" data-copy="${esc(u.url)}">Копировать</button></div>`).join('');
  document.querySelectorAll('[data-copy]').forEach(b => b.onclick = () => {
    navigator.clipboard.writeText(b.dataset.copy).then(() => toast('Скопировано'), () => toast(b.dataset.copy));
  });
  const lan = urls.find(u => u.kind === 'lan');
  $('#qrbox').classList.toggle('hidden', !lan || typeof qrcode !== 'function');
  if (lan && typeof qrcode === 'function') {
    const q = qrcode(0, 'M'); q.addData(lan.url); q.make();
    $('#qr').innerHTML = q.createSvgTag(4, 0);
  }
}
function addLog(l) {
  const el = $('#log'), atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
  const d = document.createElement('div'); d.className = l.lvl; d.textContent = l.t;
  el.appendChild(d);
  while (el.childNodes.length > 400) el.removeChild(el.firstChild);
  if (atEnd) el.scrollTop = el.scrollHeight;
}
connect();
