const CACHE = 'shiki-shell-v9';
const SHELL = ['/', '/index.html', '/style.css', '/theme.js', '/app.js', '/manifest.webmanifest', '/icon.svg', '/icon-192.png', '/icon-512.png', '/icon-maskable-512.png'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('shiki-shell-') && k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  // API, credentials, cross-origin apps, and arbitrary URLs never enter this cache.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/') || !SHELL.includes(url.pathname)) return;
  const cachedShell = () => caches.match(event.request).then(cached => cached || (event.request.mode === 'navigate' ? caches.match('/') : undefined));
  event.respondWith(fetch(event.request).then(async response => {
    if (response.ok) {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE).then(cache => cache.put(event.request, copy)));
    } else if (response.status >= 500) {
      // A reachable reverse proxy can return 502 while the receiver is down.
      // Keep the cached UI available so it can contact the independent backup.
      return (await cachedShell()) || response;
    }
    return response;
  }).catch(async () => (await cachedShell()) || Response.error()));
});
