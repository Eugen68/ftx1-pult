const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const acorn = require('acorn');
const { JSDOM, ResourceLoader, VirtualConsole } = require('jsdom');
const web = path.resolve(__dirname, '../../web');
const catalogue = JSON.parse(fs.readFileSync(path.join(web, 'locales/en.json'), 'utf8'));
class LocalResources extends ResourceLoader {
  fetch(url) { return Promise.resolve(fs.readFileSync(path.join(web, new URL(url).pathname))); }
}
async function open(file, language, blockedStorage = false, offline = false, setup = () => {}) {
  const errors = [], sent = [], sockets = [];
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(error.message));
  const dom = new JSDOM(fs.readFileSync(path.join(web, file), 'utf8'), {
    url: 'http://localhost/' + file, runScripts: 'dangerously', resources: new LocalResources(),
    virtualConsole: console, pretendToBeVisual: true,
    beforeParse(w) {
      if (blockedStorage) Object.defineProperty(w, 'localStorage', { get() { throw Error('blocked'); } });
      w.fetch = async url => {
        if (url === 'api/language' && offline) throw Error('offline');
        return { ok: true, json: async () => url === 'api/language' ? { language } : catalogue };
      };
      w.ResizeObserver = class { observe() {} disconnect() {} };
      w.HTMLCanvasElement.prototype.getContext = () => new Proxy({
        measureText: s => ({ width: String(s).length * 6 }),
        createImageData: (x, y) => ({ data: new Uint8ClampedArray(x * y * 4), width: x, height: y }),
        createLinearGradient: () => ({ addColorStop() {} }), createRadialGradient: () => ({ addColorStop() {} }),
      }, { get: (t, k) => k in t ? t[k] : () => {}, set: (t, k, v) => (t[k] = v, true) });
      w.WebSocket = class {
        constructor() { this.readyState = 1; sockets.push(this); setTimeout(() => this.onopen?.(), 0); }
        send(s) { sent.push(JSON.parse(s)); }
        addEventListener() {}
        close() { this.readyState = 3; }
      };
      w.matchMedia = () => ({ matches: false, addEventListener() {} });
      setup(w);
    },
  });
  // Wait for the real bootstrap to finish loading all application scripts.
  for (let n = 0; n < 200 && !sent.length; n++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(sent.length, 'application must connect after localization');
  return { dom, errors, sent, sockets, message: m => sockets[0].onmessage({ data: JSON.stringify(m) }) };
}
function russianText(document) {
  const found = [], walker = document.createTreeWalker(document, 4);
  let node;
  while ((node = walker.nextNode())) {
    if (node.parentElement?.closest('script,style,option[value="ru"]')) continue;
    if (/[А-Яа-яЁё]/.test(node.textContent)) found.push(node.textContent.trim());
  }
  for (const el of document.querySelectorAll('*'))
    for (const attr of ['title', 'placeholder', 'aria-label', 'alt', 'data-label', 'data-opts'])
      if (/[А-Яа-яЁё]/.test(el.getAttribute(attr) || '')) found.push(el.getAttribute(attr));
  return found;
}
for (const file of ['index.html', 'panel.html']) for (const language of ['en', 'ru']) {
  test(`${file}: boots in ${language} with translated controls and no script errors`, async () => {
    const { dom, errors, sent, message } = await open(file, language);
    try {
      const document = dom.window.document;
      assert.equal(document.documentElement.lang, language);
      assert.equal(document.querySelector('#language').value, language);
      assert.equal(document.documentElement.classList.contains('i18n-loading'), false);
      if (file === 'index.html') {
        message({ t: 'hello_ok', language, local: true, version: 'test', ptt_method: 'TX1', ctcss: [67, 88.5], max_tx: 180 });
        // The language control sends a dedicated setting message, without radio commands.
        const select = document.querySelector('#language');
        select.value = language === 'en' ? 'ru' : 'en';
        select.dispatchEvent(new dom.window.Event('change'));
        assert.equal(sent.at(-1).t, 'set_language');
      } else {
        message({ t: 'admin_init', language, cfg: { language, cat_baud: 38400, ptt_method: 'TX1', max_tx_seconds: 180,
          http_port: 8080, https_port: 8443 }, ports: [], audio: [], urls: [], log: [], detect: {}, win: false });
        message({ t: 'admin_status', s: { running: true, connected: true, freq: 14074000, mode: 'DATA-U', clients: [] } });
      }
      if (language === 'en') assert.deepEqual(russianText(document), []);
      else assert.match(document.body.textContent, /Настройки|Трансивер/);
      assert.deepEqual(errors, []);
    } finally { dom.window.close(); }
  });
}
test('every localized JavaScript literal and template fragment has an English translation', async () => {
  const { dom } = await open('index.html', 'en');
  try {
    dom.window.document.querySelector('#loginPw').value = 'Москва UA3ABC';
    assert.equal(dom.window.eval('I18n.t`Частота ${"Москва UA3ABC"}`'), 'Frequency Москва UA3ABC');
    assert.equal(dom.window.document.querySelector('#loginPw').value, 'Москва UA3ABC');
    assert.equal(dom.window.eval('I18n.t("CQ DE UA3ABC K")'), 'CQ DE UA3ABC K');
    for (const file of ['app.js', 'mem.js', 'panel.js']) {
      const ast = acorn.parse(fs.readFileSync(path.join(web, file), 'utf8'), { ecmaVersion: 'latest' });
      function walk(node) {
        if (!node || typeof node !== 'object') return;
        const localized = n => n?.type === 'MemberExpression' && n.object.name === 'I18n' && n.property.name === 't';
        const strings = node.type === 'TaggedTemplateExpression' && localized(node.tag) ? node.quasi.quasis.map(q => q.value.cooked)
          : node.type === 'CallExpression' && localized(node.callee) && node.arguments[0]?.type === 'Literal' ? [node.arguments[0].value] : [];
        for (const s of strings) assert.doesNotMatch(dom.window.eval(`I18n.t(${JSON.stringify(s)})`), /[А-Яа-яЁё]/, file + ': ' + s);
        for (const value of Object.values(node)) {
          if (Array.isArray(value)) value.forEach(walk); else if (value && typeof value === 'object') walk(value);
        }
      }
      walk(ast);
    }
  } finally { dom.window.close(); }
});
test('blocked browser storage does not prevent startup', async () => {
  const { dom, errors } = await open('index.html', 'en', true);
  try { assert.equal(dom.window.document.documentElement.lang, 'en'); assert.deepEqual(errors, []); }
  finally { dom.window.close(); }
});
test('unavailable language API falls back to Russian and still starts', async () => {
  const { dom, errors } = await open('index.html', 'ru', false, true);
  try { assert.equal(dom.window.document.documentElement.lang, 'ru'); assert.deepEqual(errors, []); }
  finally { dom.window.close(); }
});
test('top Exit saves current controls and recovers if the server cannot save', async () => {
  const { dom, sent, message } = await open('panel.html', 'en');
  try {
    dom.window.confirm = () => true;
    message({ t: 'admin_init', language: 'en', cfg: { language: 'en', cat_baud: 38400, ptt_method: 'TX1', max_tx_seconds: 180,
      http_port: 8080, https_port: 8443 }, ports: [], audio: [], urls: [], log: [], detect: {}, win: false });
    const document = dom.window.document;
    document.querySelector('#max_tx_seconds').value = '181';
    document.querySelector('#btnQuitTop').click();
    assert.equal(sent.at(-1).t, 'admin_quit');
    assert.equal(sent.at(-1).cfg.max_tx_seconds, 181);
    assert.equal(document.querySelector('#btnQuitTop').disabled, true);
    message({ t: 'admin_quit_failed', msg: 'Disk full' });
    assert.equal(document.querySelector('#btnQuitTop').disabled, false);
    assert.equal(dom.window.eval('quitting'), false);
  } finally { dom.window.close(); }
});
test('shutdown from tray stops panel reconnect attempts', async () => {
  const { dom, message } = await open('panel.html', 'en');
  try {
    message({ t: 'server_stopping' });
    assert.equal(dom.window.eval('quitting'), true);
    assert.equal(dom.window.document.querySelector('#gone').classList.contains('hidden'), false);
  } finally { dom.window.close(); }
});

function mockAudio(w, { rejectPlay = false, stream = true } = {}) {
  const connections = [], packets = [], actions = {};
  let plays = 0, pauses = 0;
  w.AudioContext = class {
    constructor() {
      this.state = 'running'; this.sampleRate = 48000; this.destination = { speaker: true };
      this.audioWorklet = { addModule: async () => {} };
      if (stream) this.createMediaStreamDestination = () => ({ stream: { receiver: true } });
    }
    async resume() { this.state = 'running'; }
  };
  w.AudioWorkletNode = class {
    constructor() { this.port = { postMessage: m => packets.push(m) }; }
    connect(node) { connections.push(node); }
  };
  w.HTMLMediaElement.prototype.play = async function () {
    plays++;
    if (rejectPlay) throw Error('Playback blocked');
  };
  w.HTMLMediaElement.prototype.pause = function () { pauses++; };
  Object.defineProperty(w.navigator, 'mediaSession', { value: {
    setActionHandler: (name, handler) => actions[name] = handler,
  } });
  return { connections, packets, actions, get plays() { return plays; }, get pauses() { return pauses; } };
}
for (const stream of [true, false]) test(`RX media controls and safe screen locking (stream=${stream})`, async () => {
  let audio;
  const { dom, sent } = await open('index.html', 'en', false, false, w => { audio = mockAudio(w, { stream }); });
  try {
    const w = dom.window;
    await w.eval('setReceiveAudio(true)');
    assert.equal(w.eval('C.audioOn'), true);
    assert.equal(audio.connections.length, 1, 'exactly one output path');
    assert.equal(!!audio.connections[0].stream, stream);
    assert.equal(audio.plays, stream ? 1 : 0);
    w.eval('C.pttPressed = true');
    Object.defineProperty(w.document, 'hidden', { configurable: true, value: true });
    w.document.dispatchEvent(new w.Event('visibilitychange'));
    assert.equal(w.eval('C.audioOn'), true, 'screen locking must not disable reception');
    assert.equal(w.eval('C.pttPressed'), false);
    assert.ok(sent.some(m => m.t === 'ptt' && m.on === false), 'locking releases PTT');
    await audio.actions.pause();
    assert.equal(w.eval('C.audioOn'), false);
    assert.equal(sent.at(-1).on, false);
    await audio.actions.play();
    assert.equal(w.eval('C.audioOn'), true);
    await audio.actions.stop();
    assert.equal(w.eval('C.audioOn'), false);
  } finally { dom.window.close(); }
});
test('blocked playback leaves RX off; Stop wins over an unfinished start', async () => {
  const { dom, sent } = await open('index.html', 'en', false, false, w => mockAudio(w, { rejectPlay: true }));
  try {
    await dom.window.eval('setReceiveAudio(true)');
    assert.equal(dom.window.eval('C.audioOn'), false);
    assert.equal(sent.at(-1).on, false);
    await dom.window.eval('Promise.all([setReceiveAudio(true), setReceiveAudio(false)])');
    assert.equal(dom.window.eval('C.audioOn'), false);
  } finally { dom.window.close(); }
});

test('Stop cancels a pending media play and foreground recovery keeps RX opt-in', async () => {
  const { dom } = await open('index.html', 'en', false, false, w => mockAudio(w));
  try {
    const w = dom.window;
    await w.eval('ensureAudioContext()');
    let release;
    w.HTMLMediaElement.prototype.play = () => new Promise(resolve => { release = resolve; });
    const starting = w.eval('setReceiveAudio(true)');
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(typeof release, 'function');
    await w.eval('setReceiveAudio(false)');
    release(); await starting;
    assert.equal(w.eval('C.audioOn'), false);
    Object.defineProperty(w.document, 'hidden', { configurable: true, value: false });
    w.document.dispatchEvent(new w.Event('visibilitychange'));
    assert.equal(w.eval('C.audioOn'), false);
    w.HTMLMediaElement.prototype.play = async () => {};
    await w.eval('setReceiveAudio(true)');
    w.eval("C.ac.state = 'interrupted'");
    w.document.dispatchEvent(new w.Event('visibilitychange'));
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(w.eval('C.ac.state'), 'running');
    assert.equal(w.eval('C.audioOn'), true);
  } finally { dom.window.close(); }
});

test('standby recovery replaces stale sockets and ignores their late events', async () => {
  const { dom, sockets, sent } = await open('index.html', 'en', false, false, w => mockAudio(w));
  try {
    const w = dom.window, old = sockets[0];
    w.eval('C.audioOn = true; C.lastMessage = Date.now() - 25000');
    Object.defineProperty(w.document, 'hidden', { configurable: true, value: false });
    w.document.dispatchEvent(new w.Event('visibilitychange'));
    assert.equal(sockets.length, 2);
    assert.equal(old.readyState, 3);
    assert.equal(w.eval('C.ws'), sockets[1]);
    old.onclose();
    old.onmessage({ data: JSON.stringify({ t: 'server_stopping' }) });
    assert.equal(w.eval('C.shuttingDown'), undefined);
    sockets[1].onmessage({ data: JSON.stringify({ t: 'hello_ok', language: 'en', local: false,
      version: 'test', ptt_method: 'TX1', ctcss: [67], max_tx: 180 }) });
    assert.equal(w.eval('C.authed'), true);
    assert.ok(sent.some(m => m.t === 'audio' && m.on === true), 'resubscribe to enabled RX');
    assert.equal(w.document.documentElement.lang, 'en');
    await new Promise(resolve => setTimeout(resolve, 0));
  } finally { dom.window.close(); }
});
test('unresponsive OPEN sockets are probed and replaced; shutdown stays stopped', async () => {
  const { dom, sockets, message } = await open('index.html', 'en');
  try {
    const w = dom.window;
    const schedule = w.setTimeout;
    let timeout;
    w.setTimeout = (callback, ms, ...args) => {
      if (ms === 4000) { timeout = callback; return 9999; }
      return schedule(callback, ms, ...args);
    };
    w.eval('checkConnection(true)');
    const stamp = w.eval('C.probeStamp');
    message({ t: 'pong', ts: stamp });
    timeout();
    assert.equal(sockets.length, 1, 'matching pong confirms the socket');
    w.eval('checkConnection(true)');
    timeout();
    assert.equal(sockets.length, 2, 'missing pong replaces even an OPEN socket');
    sockets[1].onmessage({ data: JSON.stringify({ t: 'server_stopping' }) });
    w.dispatchEvent(new w.Event('online'));
    w.dispatchEvent(new w.Event('pageshow'));
    assert.equal(sockets.length, 2, 'explicit shutdown must not reconnect');
  } finally { dom.window.close(); }
});
test('offline startup preserves saved English using the cached translation catalogue', async () => {
  const { dom, errors } = await open('index.html', 'en', false, false, w => {
    w.localStorage.setItem('ftx1.language', 'en');
    w.localStorage.setItem('ftx1.english', JSON.stringify(catalogue));
    w.fetch = async () => { throw Error('network unavailable after standby'); };
  });
  try {
    assert.equal(dom.window.document.documentElement.lang, 'en');
    assert.deepEqual(russianText(dom.window.document), []);
    assert.deepEqual(errors, []);
    assert.equal(dom.window.localStorage.getItem('ftx1.language'), 'en');
  } finally { dom.window.close(); }
});
test('language API returning no valid language preserves the saved choice', async () => {
  const { dom } = await open('index.html', 'invalid', false, false, w => {
    w.localStorage.setItem('ftx1.language', 'en');
  });
  try { assert.equal(dom.window.document.documentElement.lang, 'en'); }
  finally { dom.window.close(); }
});

test('receive buffer selection is sent to the player and saved on this device', async () => {
  let audio;
  const { dom } = await open('index.html', 'en', false, false, w => { audio = mockAudio(w); });
  try {
    const w = dom.window;
    await w.eval('setReceiveAudio(true)');
    assert.ok(audio.packets.some(m => m.bufferMs === 240));
    const select = w.document.querySelector('#optRxBuffer');
    select.value = '400';
    select.dispatchEvent(new w.Event('change'));
    assert.equal(audio.packets.at(-1).bufferMs, 400);
    assert.equal(JSON.parse(w.localStorage.getItem('ftx1.opts')).rxBuffer, 400);
  } finally { dom.window.close(); }
});
