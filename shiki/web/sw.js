const CACHE = 'shiki-shell-v1';
const SHELL = ['/', '/index.html', '/style.css', '/app.js', '/manifest.webmanifest', '/icon.svg'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('shiki-shell-') && k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  // API, credentials, cross-origin apps, and arbitrary URLs never enter this cache.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/') || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) {const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));}
    return response;
  }).catch(() => caches.match(event.request).then(cached => cached || (event.request.mode === 'navigate' ? caches.match('/') : Response.error()))));
});
