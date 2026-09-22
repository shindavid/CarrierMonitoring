// Service worker for the Carrier Control home-screen app.
//
// It does NOT cache pages — the control page is live data and must never be served
// stale — so there is no fetch handler. Its job is to make the page installable and
// to receive Web Push notifications (iOS 16.4+, only for a page added to the home
// screen). The push/notificationclick handlers are inert until a subscription exists.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = { body: event.data && event.data.text() }; }
  const title = data.title || 'Carrier Control';
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: data.tag || 'carrier-control',
    renotify: true,
    data: { url: data.url || '/control' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/control';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    for (const w of wins) {
      if (w.url.includes(url) && 'focus' in w) return w.focus();
    }
    return self.clients.openWindow ? self.clients.openWindow(url) : undefined;
  }));
});
