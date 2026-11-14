'use strict';

// Only source literals and the initial static DOM are translated. Never walk
// live decoder output, user inputs, imported logs or CAT protocol messages.
const I18n = (() => {
  let language = 'ru', english = {};
  const scripts = document.currentScript.dataset.scripts.split(',');
  const attrs = ['title', 'placeholder', 'aria-label', 'alt', 'data-label', 'data-opts'];
  function text(source) {
    if (language !== 'en') return source;
    const key = source.trim();
    if (!Object.hasOwn(english, key)) return source;
    const start = source.length - source.trimStart().length;
    return source.slice(0, start) + english[key] + source.slice(start + key.length);
  }
  function fragment(source) {
    const direct = text(source);
    if (direct !== source || !/<[a-z!/]/i.test(source)) return direct;
    return source.split(/(<[^>]*>)/g).map(part => {
      if (!part.startsWith('<')) return text(part);
      if (part.startsWith('<!--')) return part;
      return part.replace(/\b(title|placeholder|aria-label|alt)\s*=\s*(["'])(.*?)\2/gs,
        (all, attr, quote, value) => `${attr}=${quote}${text(value)}${quote}`);
    }).join('');
  }
  function t(source, ...values) {
    if (Array.isArray(source) && Object.hasOwn(source, 'raw'))
      return source.reduce((out, part, i) => out + fragment(part) + (i < values.length ? values[i] : ''), '');
    return fragment(source);
  }
  function localize() {
    document.documentElement.lang = language;
    const walker = document.createTreeWalker(document, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.parentElement?.closest('script,style,textarea')) continue;
      node.textContent = text(node.textContent);
    }
    document.querySelectorAll('*').forEach(el => {
      for (const attr of attrs) if (el.hasAttribute(attr)) el.setAttribute(attr, text(el.getAttribute(attr)));
    });
    document.querySelectorAll('[data-language]').forEach(select => { select.value = language; });
  }
  function remember(value) {
    try { localStorage.setItem('ftx1.language', value); } catch { /* storage may be disabled */ }
  }
  function sync(value) {
    if (!['ru', 'en'].includes(value)) return;
    remember(value);
    if (value !== language) location.reload();
  }
  function savedLanguage() {
    try {
      const value = localStorage.getItem('ftx1.language');
      return ['ru', 'en'].includes(value) ? value : 'ru';
    } catch { return 'ru'; }
  }
  async function fetchJson(url, options = {}) {
    const controller = new AbortController();
    let timer;
    try {
      // Race as well as abort: startup must not depend on an unresponsive network.
      return await Promise.race([
        fetch(url, { ...options, signal: controller.signal }).then(r => {
          if (!r.ok) throw Error('Interface resource unavailable');
          return r.json();
        }),
        new Promise((_, reject) => { timer = setTimeout(() => {
          controller.abort(); reject(Error('Interface request timed out'));
        }, 4000); }),
      ]);
    } finally { clearTimeout(timer); }
  }
  async function start() {
    try {
      const [catalogue, settings] = await Promise.all([
        fetchJson('locales/en.json').then(value => {
          try { localStorage.setItem('ftx1.english', JSON.stringify(value)); } catch {}
          return value;
        }).catch(error => {
          try {
            const cached = JSON.parse(localStorage.getItem('ftx1.english'));
            if (cached && typeof cached === 'object' && !Array.isArray(cached)) return cached;
          } catch {}
          throw error;
        }),
        fetchJson('api/language', { cache: 'no-store' }).catch(() => ({ language: savedLanguage() })),
      ]);
      english = catalogue;
      language = ['ru', 'en'].includes(settings.language) ? settings.language : savedLanguage();
      remember(language);
      localize();
      document.documentElement.classList.remove('i18n-loading');
      for (const src of scripts) await new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = src; script.onload = resolve; script.onerror = reject;
        document.body.append(script);
      });
    } catch (error) {
      document.documentElement.classList.remove('i18n-loading');
      const notice = document.createElement('p');
      notice.textContent = 'Could not load the interface. Reload the page. / Не удалось загрузить интерфейс. Обновите страницу.';
      // Do not expose the untranslated source UI when English assets fail to load.
      const retry = document.createElement('button');
      retry.textContent = 'Retry / Повторить';
      retry.onclick = () => location.reload();
      document.body.replaceChildren(notice, retry);
      console.error(error);
    }
  }
  // Defer initialization until the const binding is available to loaded scripts.
  queueMicrotask(start);
  return { t, sync, get language() { return language; } };
})();
