/* Applai Dashboard Service Worker — Web Push 通知 */
self.addEventListener('push', (event) => {
  let data = { title: 'Applai', body: '有新消息' };
  try {
    if (event.data) data = Object.assign(data, event.data.json());
  } catch (e) {}
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/dashboard/icon.svg',
      badge: '/dashboard/icon.svg',
      tag: 'applai-notify',
      renotify: true,
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if (c.url.includes('/dashboard')) return c.focus();
      }
      return clients.openWindow('/dashboard');
    })
  );
});
