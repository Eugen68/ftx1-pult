// Сервис-воркер: нужен, чтобы пульт ставился на Android как приложение.
// Только статика пульта: свежая из сети, из кэша — лишь когда сервер недоступен.
// Всё остальное (WebSocket, записи, панель, API) идёт мимо него.
const CACHE = 'ftx1-v34-background-audio';
const FILES = ['./', 'index.html', 'app.css', 'app.js', 'mem.js', 'worklets.js', 'i18n.js', 'locales/en.json', 'manifest.webmanifest', 'icon.svg', 'icon-192.png', 'icon-512.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
const OWN = new Set(['/', '/index.html', '/app.css', '/app.js', '/mem.js', '/worklets.js', '/i18n.js', '/locales/en.json', '/manifest.webmanifest', '/icon.svg', '/icon-192.png', '/icon-512.png']);
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin || !OWN.has(u.pathname)) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const c = r.clone(); caches.open(CACHE).then(x => x.put(e.request, c)); }
    return r;
  }).catch(async () => (await caches.match(e.request, { ignoreSearch: true })) ||
    new Response('<meta charset="utf-8"><h3>FTX-1 Pult: server unavailable / сервер недоступен</h3><p>Check that the application is running on the PC and your phone is on the same network, then reload.</p><p>Проверьте, что программа на ПК запущена и телефон в той же сети, затем обновите страницу.</p>',
      { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
});
