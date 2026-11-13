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
    setLamp('bad', I18n.t('Нет связи с сервером'), I18n.t('Программа закрыта? Запустите FTX-1 Пульт снова.'));
    setTimeout(connect, 2000);
  };
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    switch (m.t) {
      case 'language': I18n.sync(m.language); break;
      case 'language_error': $('#language').value = m.language; toast(m.msg, 5000); break;
      case 'admin_denied':
        setLamp('bad', I18n.t('Панель доступна только на ПК с трансивером'), ''); break;
      case 'admin_init': init(m); break;
      case 'admin_devices': ports = m.ports; audio = m.audio; fillDevices(); renderUrls(m.urls); applyDetectIfEmpty(m.detect); break;
      case 'admin_status': renderStatus(m.s); break;
      case 'admin_log': addLog(m.line); break;
      case 'admin_saved':
        cfg = m.cfg; flashSaved();
        if (m.port_change) toast(I18n.t('Новые порты заработают после перезапуска программы'), 5000);
        break;
      case 'admin_autostart': $('#autostart').checked = !!m.on; break;
      case 'admin_ai': {
        const sel = $('#ai_model'), cur = cfg.ai_model || '';
        sel.innerHTML = I18n.t('<option value="">Любая загруженная</option>') + (m.models || []).map(x => `<option>${esc(x)}</option>`).join('');
        if (cur && !(m.models || []).includes(cur)) sel.add(new Option(cur + I18n.t(' (не загружена)'), cur));
        sel.value = cur;
        $('#aiHint').textContent = m.ok ? (m.models.length ? I18n.t`✓ LM Studio отвечает, моделей: ${m.models.length}` : I18n.t('⚠ LM Studio отвечает, но модель не загружена')) : '⚠ ' + m.msg;
        $('#aiHint').style.color = m.ok && m.models.length ? 'var(--ok)' : 'var(--warn)';
        break;
      }
    }
  };
}

// ------------------------------------------------------------ инициализация
function init(m) {
  I18n.sync(m.language);
  cfg = m.cfg; ports = m.ports; audio = m.audio;
  $('#ver').textContent = I18n.t`версия ${m.version}`;
  fillDevices();
  for (const k of ['cat_baud', 'ptt_method', 'max_tx_seconds', 'http_port', 'https_port']) $('#' + k).value = cfg[k];
  for (const k of ['simulate', 'auto_connect', 'open_panel', 'open_pult', 'ai_enabled', 'ai_auto_analyze']) $('#' + k).checked = !!cfg[k];
  $('#ai_url').value = cfg.ai_url || 'http://localhost:1234/v1';
  $('#cw_method').value = cfg.cw_method || 'cat';
  send({ t: 'admin_ai' });
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
  if (!d || !Object.keys(d).length) { if (!quiet) toast(I18n.t('FTX-1 не найден: проверьте кабель, питание и драйвер CP210x'), 5000); return; }
  if (d.cat_port) $('#cat_port').value = d.cat_port;
  if (d.ptt_port) $('#ptt_port').value = d.ptt_port;
  if (d.audio_in_name) {
    cfg.audio_in_name = d.audio_in_name; cfg.audio_out_name = d.audio_out_name; cfg.audio_api = d.audio_api;
    fillAudio();
  }
  save();
  if (!quiet) toast(I18n.t('Найдено: ') + [d.cat_port && 'CAT ' + d.cat_port, d.audio_in_name && I18n.t('звук ') + d.audio_in_name].filter(Boolean).join(', '));
}
function applyDetectIfEmpty(d) {
  if (d && ((!cfg.cat_port && d.cat_port) || (!cfg.audio_in_name && d.audio_in_name))) applyDetect(d, true);
}

function opt(v, t, sel) { return `<option value="${esc(v)}"${sel ? ' selected' : ''}>${esc(t)}</option>`; }
function fillDevices() {
  const portOpts = (cur, prefer) => {
    let h = '';
    if (!ports.length) h += opt('', I18n.t('— COM-порты не найдены —'), !cur);
    else if (!cur) h += opt('', I18n.t('— выберите —'), true);
    for (const p of ports) {
      const star = p.kind === prefer ? '★ ' : '';
      h += opt(p.device, `${star}${p.device} — ${p.desc}`, p.device === cur);
    }
    if (cur && !ports.some(p => p.device === cur)) h += opt(cur, I18n.t`${cur} (сейчас не подключён)`, true);
    return h;
  };
  $('#cat_port').innerHTML = portOpts(cfg.cat_port, 'enhanced');
  $('#ptt_port').innerHTML = portOpts(cfg.ptt_port, 'standard');
  $('#cw_port').innerHTML = portOpts(cfg.cw_port, 'standard');
  fillAudio();
}
// Звук: одна звуковая карта трансивера (вход + выход), драйвер — отдельно (по умолчанию авто).
const isCodec = n => /usb audio (codec|device)/i.test(n);
const coreName = n => { const m = /\(([^()]*)\)?\s*$/.exec(n); return ((m && m[1]) || n).replace(/^\s*\d+\s*-\s*/, '').trim(); };
const MAPPER = /переназначение|mapper|основной драйвер|primary sound/i;
function fillAudio() {
  // одно физическое устройство = одна строка, сколько бы драйверов Windows его ни показывали
  const cores = new Map();
  for (const d of audio) {
    if (MAPPER.test(d.name)) continue;
    const c = coreName(d.name), k = c.toLowerCase();
    const e = cores.get(k) || { name: c, in: false, out: false };
    e.in = e.in || d.in > 0; e.out = e.out || d.out > 0;
    cores.set(k, e);
  }
  const both = [...cores.values()].filter(e => e.in && e.out);
  both.sort((a, b) => isCodec(b.name) - isCodec(a.name));
  const cur = cfg.audio_in_name ? coreName(cfg.audio_in_name) : '';
  let h = opt('', I18n.t('Автоматически (USB Audio CODEC/Device)'), !cur), found = !cur;
  for (const e of both) {
    const sel = !!cur && e.name.toLowerCase() === cur.toLowerCase();
    if (sel) found = true;
    h += opt(e.name, (isCodec(e.name) ? '★ ' : '') + e.name + (isCodec(e.name) ? I18n.t(' — трансивер') : ''), sel);
  }
  if (!found) h += opt(cur, cur + I18n.t(' (не найдено)'), true);
  $('#audio_dev').innerHTML = h;
  $('#audio_api').value = cfg.audio_api || '';
  if ($('#audio_api').value !== (cfg.audio_api || '')) $('#audio_api').value = '';
  audioHint();
}
function audioHint(st) {
  const d = $('#audio_dev').value;
  let t = '';
  if (d && !isCodec(d)) t = I18n.t('⚠ Выбрано не USB-устройство трансивера.');
  else if (!audio.some(x => isCodec(x.name))) t = I18n.t('⚠ USB-звук трансивера не виден: проверьте кабель и нажмите ↻.');
  else if (st && st.audio_api_used) t = I18n.t('Сейчас звук идёт через ') + st.audio_api_used.replace('Windows ', '') + '.';
  $('#audioHint').textContent = t;
}
$('#audio_dev').addEventListener('change', () => audioHint());

// ------------------------------------------------------------ сохранение
function collect() {
  const dev = $('#audio_dev').value;
  return {
    cat_port: $('#cat_port').value, cat_baud: +$('#cat_baud').value,
    audio_in_name: dev, audio_out_name: dev, audio_api: $('#audio_api').value, audio_in_index: null, audio_out_index: null,
    ptt_method: $('#ptt_method').value, ptt_port: $('#ptt_port').value,
    simulate: $('#simulate').checked, password: $('#password').value,
    auto_connect: $('#auto_connect').checked, open_panel: $('#open_panel').checked, open_pult: $('#open_pult').checked,
    max_tx_seconds: Math.max(10, Math.min(1800, +$('#max_tx_seconds').value || 180)),
    http_port: +$('#http_port').value || 8080, https_port: +$('#https_port').value || 8443,
    ai_enabled: $('#ai_enabled').checked, ai_auto_analyze: $('#ai_auto_analyze').checked,
    ai_url: $('#ai_url').value.trim() || 'http://localhost:1234/v1', ai_model: $('#ai_model').value,
    cw_method: $('#cw_method').value, cw_port: $('#cw_port').value,
  };
}
function save() { clearTimeout(saveT); saveT = setTimeout(() => send({ t: 'admin_save', cfg: collect() }), 350); }
function flashSaved() { const s = $('#saved'); s.classList.add('show'); clearTimeout(s.t); s.t = setTimeout(() => s.classList.remove('show'), 1500); }
document.querySelectorAll('select, input').forEach(el => {
  if (['autostart', 'language'].includes(el.id)) return;
  el.addEventListener(el.matches('input[type=text], input[type=password], input[type=number]') ? 'input' : 'change', () => {
    pttPortVisibility(); pwHint(); save();
  });
});
function pttPortVisibility() {
  $('#pttPortField').classList.toggle('hidden', !/RTS|DTR/.test($('#ptt_method').value));
  $('#cwPortField').classList.toggle('hidden', $('#cw_method').value === 'cat');
}
$('#aiTest').onclick = () => { clearTimeout(saveT); send({ t: 'admin_save', cfg: collect() }); setTimeout(() => send({ t: 'admin_ai' }), 300); $('#aiHint').textContent = I18n.t('Проверяю…'); };
function pwHint() {
  const pw = $('#password').value;
  $('#pwHint').textContent = !pw ? I18n.t('⚠ Пароль не задан — с телефона войти нельзя (это безопасно, но неудобно).')
    : pw.length < 6 ? I18n.t('⚠ Короткий пароль — лучше 6+ символов.') : '';
}
$('#pwShow').onclick = () => { const p = $('#password'); p.type = p.type === 'password' ? 'text' : 'password'; };
$('#pwGen').onclick = () => {
  const a = 'abcdefghjkmnpqrstuvwxyz23456789', r = crypto.getRandomValues(new Uint8Array(8));
  $('#password').value = [...r].map(x => a[x % a.length]).join('');
  $('#password').type = 'text'; pwHint(); save();
};
$('#language').onchange = e => {
  if (!ws || ws.readyState !== 1) { e.target.value = I18n.language; return; }
  clearTimeout(saveT);
  send({ t: 'admin_save', cfg: collect() });
  send({ t: 'set_language', language: e.target.value });
};
$('#autostart').onchange = e => send({ t: 'admin_autostart', on: e.target.checked });
$('#btnRefresh').onclick = () => { send({ t: 'admin_refresh' }); toast(I18n.t('Обновляю список устройств…'), 1200); };
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
  if (!confirm(I18n.t('Выйти из программы? Связь с трансивером будет остановлена, пульты отключатся.'))) return;
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
  b.textContent = s.busy ? I18n.t('… Подождите') : s.running ? I18n.t('■ Остановить') : I18n.t('▶ Запустить');
  if (s.busy) setLamp('warn', I18n.t('Подключаюсь…'), '');
  else if (!s.running) setLamp(s.error ? 'bad' : '', s.error ? I18n.t('Связь не установлена') : I18n.t('Связь с трансивером остановлена'),
    s.error ? '' : I18n.t('Нажмите «Запустить»'));
  else if (s.tx) setLamp('tx', I18n.t('ПЕРЕДАЧА'), s.ptt_owner ? I18n.t('с ') + s.ptt_owner : '');
  else if (s.connected) setLamp('ok', s.sim ? I18n.t('Работает (симулятор)') : I18n.t('Трансивер на связи'),
    [s.radio_id === '0840' ? 'FTX-1' : '', s.head === 2 ? 'Optima/SPA-1' : s.head === 1 ? 'Field' : '', s.fw ? I18n.t('прошивка ') + s.fw : ''].filter(Boolean).join(' · '));
  else setLamp('warn', I18n.t('Трансивер не отвечает'), I18n.t('Включён? Тот ли COM-порт и скорость CAT?'));
  $('#stFreq').textContent = s.running && s.connected && s.freq ? `${fmtF(s.freq)}  ${s.mode || ''}` : '';
  const err = $('#stErr');
  err.textContent = s.error || (s.running && s.audio_err ? I18n.t('Звук: ') + s.audio_err : '');
  err.classList.toggle('hidden', !err.textContent);
  $('#cCat').className = s.running ? (s.connected ? 'ok' : 'warn') : '';
  $('#cAudio').className = s.running ? (s.audio_ok ? 'ok' : 'bad') : '';
  if (s.audio_api_used !== undefined && document.activeElement !== $('#audio_dev')) audioHint(s);
  $('#cTx').className = s.tx ? 'bad' : (s.running ? 'ok' : '');
  $('#cTxT').textContent = s.tx ? I18n.t('Передача') : I18n.t('Приём');
  $('#cCl').className = s.clients.length ? 'ok' : '';
  $('#cClT').textContent = I18n.t('Пультов: ') + s.clients.length;
  $('#clCount').textContent = s.clients.length ? `(${s.clients.length})` : '';
  $('#clients').innerHTML = s.clients.length ? s.clients.map(c =>
    `<div class="cl">${c.tx ? '🔴' : c.local ? '🖥' : '📱'} <span>${esc(c.name.split(':')[0])}</span>
     <span class="muted small">${c.local ? I18n.t('этот ПК') : I18n.t('по сети')}${c.audio ? I18n.t(' · звук') : ''}${c.tx ? I18n.t(' · ПЕРЕДАЁТ') : ''}</span></div>`).join('')
    : I18n.t('<span class="muted small">Никого</span>');
  document.title = (s.tx ? '🔴 ' : '') + I18n.t('FTX-1 Пульт — сервер');
}
function renderUrls(urls) {
  $('#urls').innerHTML = urls.map(u => I18n.t`<div class="url"><span title="${u.kind === 'local' ? I18n.t('на этом ПК') : I18n.t('для телефона и других ПК')}">${u.kind === 'local' ? '🖥' : '📱'}</span>
    <code>${esc(u.url)}</code><button class="btn small" data-copy="${esc(u.url)}">Копировать</button></div>`).join('');
  document.querySelectorAll('[data-copy]').forEach(b => b.onclick = () => {
    navigator.clipboard.writeText(b.dataset.copy).then(() => toast(I18n.t('Скопировано')), () => toast(b.dataset.copy));
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
// Масштаб окна: «авто» — под размер экрана (макет рассчитан на ~1080×860)
const scaleOpt = (() => { try { return localStorage.getItem('ftx1.panelScale') || 'auto'; } catch { return 'auto'; } })();
$('#scale').value = scaleOpt;
function applyScale() {
  let z = parseFloat($('#scale').value);
  const auto = !(z > 0), w = innerWidth, h = innerHeight;
  if (auto) z = w < 700 ? 1 : Math.max(0.6, Math.min(2.2, Math.min(w / 1080, h / 860)));
  document.documentElement.style.zoom = z;
  if (auto && w >= 700) {
    // наибольший масштаб, при котором панель целиком влезает в окно (число колонок
    // зависит от масштаба, поэтому подбираем делением пополам)
    const fits = zz => { document.documentElement.style.zoom = zz; return $('.wrap').getBoundingClientRect().height <= h - 3; };
    if (!fits(z)) {
      let lo = 0.45, hi = z;
      for (let i = 0; i < 8; i++) { const mid = (lo + hi) / 2; if (fits(mid)) lo = mid; else hi = mid; }
      fits(lo);
    }
  }
}
$('#scale').onchange = () => { try { localStorage.setItem('ftx1.panelScale', $('#scale').value); } catch { } applyScale(); };
addEventListener('resize', () => { if ($('#scale').value === 'auto') applyScale(); });
// содержимое панели меняется (данные, QR-код, список клиентов) — пересчитать масштаб
let lastH = 0, roT;
new ResizeObserver(() => {
  const hh = $('.wrap').offsetHeight;
  if (Math.abs(hh - lastH) < 3) return;
  lastH = hh; clearTimeout(roT); roT = setTimeout(applyScale, 80);
}).observe($('.wrap'));
applyScale();
connect();
