const CACHE = 'boyle-v2-104';
const ASSETS = ['./'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});
self.addEventListener('fetch', e => {
  const url = e.request.url;
  if (url.includes('firebasejs') || url.includes('googleapis') ||
      url.includes('accounts.google') || url.includes('firebaseio') ||
      url.includes('garmin_historial') || url.includes('tabler-icons')) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
