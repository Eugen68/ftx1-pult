// Сервис-воркер: нужен, чтобы пульт ставился на Android как приложение.
// Файлы берём из сети (свежие), а из кэша — только если сервер недоступен.
const CACHE = 'ftx1-v1';
const FILES = ['./', 'index.html', 'app.css', 'app.js', 'worklets.js', 'manifest.webmanifest', 'icon.svg', 'icon-192.png', 'icon-512.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET' || new URL(e.request.url).pathname === '/ws') return;
  e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(CACHE).then(x => x.put(e.request, c)); return r; })
    .catch(() => caches.match(e.request)));
});
