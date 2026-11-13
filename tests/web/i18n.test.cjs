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
async function open(file, language, blockedStorage = false, offline = false) {
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
