// Minimal service worker — just enough to make Hermes installable as an app.
// We intentionally do NOT cache API calls (they must always hit the live brain).
const SHELL = ["./", "./index.html", "./icon.svg", "./manifest.webmanifest"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open("hermes-shell-v1").then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", e => self.clients.claim());
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // Never cache the API — always go to network.
  if (["/chat", "/talk", "/voice", "/health"].some(p => url.pathname.endsWith(p))) return;
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
