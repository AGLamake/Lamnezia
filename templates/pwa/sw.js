{% autoescape off %}// Service worker of the installed panel (apps/core/views_pwa.py). It caches only the offline page
// and the files it needs; the panel's own pages hold keys and personal data and always come from
// the network. A page that cannot be loaded is replaced by the offline page. It also shows the
// notifications the panel pushes (apps/notifications/services/browser.py); a click opens the page
// the notification is about. When the browser changes the subscription, the new one replaces the
// old one in the panel (apps/notifications/views_push.py).
const CACHE = "lamnezia-{{ version }}";
const OFFLINE = "{{ offline|escapejs }}";
const ICON = "{{ icon|escapejs }}";
const NAME = "{{ site_name|escapejs }}";
const PUSH_KEY = "{{ push_key|escapejs }}";
const RESUBSCRIBE = "{{ resubscribe|escapejs }}";
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

// The push service changed or dropped the subscription: subscribe again with the panel's key and
// send the new one in place of the old, so pushes go on without a visit to the profile. Without the
// old subscription there is nothing to replace: the profile subscribes this browser again.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(resubscribe(event.oldSubscription, event.newSubscription));
});

async function resubscribe(old, current) {
  if (!old) return;
  const subscription =
    current ||
    (await self.registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: fromB64url(PUSH_KEY),
    }));
  const json = subscription.toJSON();
  const body = new URLSearchParams({
    old_endpoint: old.endpoint,
    endpoint: json.endpoint || "",
    p256dh: json.keys?.p256dh || "",
    auth: json.keys?.auth || "",
  });
  await fetch(RESUBSCRIBE, { method: "POST", body, credentials: "same-origin" });
}

function fromB64url(text) {
  return Uint8Array.from(atob(text.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
}
{% endautoescape %}
