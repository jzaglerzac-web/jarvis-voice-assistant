// ZAC Zentrale als App: alles kommt live vom PC, nichts wird zwischengespeichert.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<body style="background:#141413;color:#eee;font:16px sans-serif;padding:32px">' +
    '<h2>ZAC Zentrale nicht erreichbar</h2><p>Der PC ist aus oder Jarvis läuft nicht. Bitte am PC die ZAC Zentrale starten.</p>',
    { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});
