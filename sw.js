const CACHE_NAME = 'entreno-boyle-v85';
const ASSETS = [
  './',
  './index.html',
];

self.addEventListener('install', function(e) {
  e.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(ASSETS);
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(e) {
  e.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(
        keys.filter(function(k) { return k !== CACHE_NAME; })
            .map(function(k) { return caches.delete(k); })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function(e) {
  // index.html: network first — siempre intenta cargar la versión más reciente
  if(e.request.url.endsWith('/') || e.request.url.includes('index.html')) {
    e.respondWith(
      fetch(e.request).then(function(res) {
        var clone = res.clone();
        caches.open(CACHE_NAME).then(function(c) { c.put(e.request, clone); });
        return res;
      }).catch(function() {
        return caches.match(e.request);
      })
    );
    return;
  }
  // garmin_historial.json: network first
  if(e.request.url.includes('garmin_historial.json')) {
    e.respondWith(
      fetch(e.request).then(function(res) {
        var clone = res.clone();
        caches.open(CACHE_NAME).then(function(c) { c.put(e.request, clone); });
        return res;
      }).catch(function() {
        return caches.match(e.request);
      })
    );
    return;
  }
  // Resto: cache first
  e.respondWith(
    caches.match(e.request).then(function(cached) {
      return cached || fetch(e.request).then(function(res) {
        if(res && res.status === 200 && res.type === 'basic') {
          var clone = res.clone();
          caches.open(CACHE_NAME).then(function(c) { c.put(e.request, clone); });
        }
        return res;
      });
    })
  );
});
