{% autoescape off %}// Service worker of the installed panel (apps/core/views_pwa.py). It caches only the offline page
// and the files it needs; the panel's own pages hold keys and personal data and always come from
// the network. A page that cannot be loaded is replaced by the offline page.
const CACHE = "lamnezia-{{ version }}";
const OFFLINE = "{{ offline|escapejs }}";
const ASSETS = [{% for asset in assets %}"{{ asset|escapejs }}", {% endfor %}OFFLINE];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE)));
    return;
  }
  const path = new URL(request.url).pathname;
  if (ASSETS.includes(path)) {
    event.respondWith(fetch(request).catch(() => caches.match(path)));
  }
});
{% endautoescape %}
