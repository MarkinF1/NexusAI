// Service worker NexusAI: только уведомления Web Push «Ответ готов». Страницы не кэширует.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch { data = { body: e.data?.text() }; }
  const url = data.url || "/";
  e.waitUntil(self.registration.showNotification(data.title || "NexusAI", {
    body: data.body || "",
    icon: "/icons/app-192.png",
    badge: "/icons/app-192.png",
    tag: url,  // повторное уведомление по той же беседе заменяет прежнее
    data: { url },
  }));
});

// Нажали на уведомление: открытое приложение переходит к беседе, иначе открываем новое окно.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const win = wins.find((w) => new URL(w.url).origin === self.location.origin);
    if (win) {
      await win.focus();
      win.postMessage({ open: new URL(url).hash.slice(1) });
      return;
    }
    await self.clients.openWindow(url);
  })());
});
