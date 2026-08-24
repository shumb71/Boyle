// sw.js — Entreno Boyle · network-first para el HTML (siempre la última versión si hay red)
// Sube el número de CACHE en cada despliegue para purgar lo viejo.
const CACHE = 'entreno-boyle-v162';

// Al instalar: activar de inmediato (sin esperar a que se cierren pestañas)
self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(['./', './index.html']).catch(() => {}))
  );
});

// Al activar: borrar cachés antiguas y tomar control de las páginas abiertas
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const esDoc = req.mode === 'navigate' || req.destination === 'document'
    || url.pathname.endsWith('/') || url.pathname.endsWith('index.html');

  if (esDoc) {
    // NETWORK-FIRST: si hay red, siempre la última versión; si no, la cacheada.
    e.respondWith(
      fetch(req)
        .then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); return res; })
        .catch(() => caches.match(req).then((r) => r || caches.match('./index.html')))
    );
    return;
  }

  // RESTO (CDNs, fuentes, libs): cache-first con relleno en segundo plano.
  e.respondWith(
    caches.match(req).then((r) => r || fetch(req).then((res) => {
      if (res && res.status === 200) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return res;
    }).catch(() => r))
  );
});

// Permite forzar la activación desde la página si hiciera falta
self.addEventListener('message', (e) => { if (e.data === 'skipWaiting') self.skipWaiting(); });
