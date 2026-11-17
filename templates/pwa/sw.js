{% autoescape off %}// Service worker of the installed panel (apps/core/views_pwa.py). It caches only the offline page
// and the files it needs; the panel's own pages hold keys and personal data and always come from
// the network. A page that cannot be loaded is replaced by the offline page. It also shows the
// notifications the panel pushes (apps/notifications/services/browser.py); a click opens the page
// the notification is about.
const CACHE = "lamnezia-{{ version }}";
const OFFLINE = "{{ offline|escapejs }}";
const ICON = "{{ icon|escapejs }}";
const NAME = "{{ site_name|escapejs }}";
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

// ---- Notifications ---------------------------------------------------------------------------

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    data = {};
  }
  event.waitUntil(
    self.registration.showNotification(data.title || NAME, {
      body: data.body || "",
      tag: data.tag,
      icon: ICON,
      badge: ICON,
      data: { url: data.url || "/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Only pages of the panel itself: the address comes in the push.
  const target = new URL(event.notification.data?.url || "/", self.location.origin);
  const url = target.origin === self.location.origin ? target.href : `${self.location.origin}/`;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows.find((client) => new URL(client.url).origin === self.location.origin);
      if (!open) return self.clients.openWindow(url);
      return open
        .focus()
        .then((client) => client.navigate(url))
        .catch(() => self.clients.openWindow(url));
    }),
  );
});
{% endautoescape %}
