// Minimal service worker — just enough to make Hermes installable as an app.
// We intentionally do NOT cache API calls (they must always hit the live brain).
// The app shell is network-first: always the latest version when the brain is reachable,
// the cached copy only when offline (cache-first would pin phones to an old app forever).
const CACHE = "hermes-shell-v2";
const SHELL = ["./", "./index.html", "./icon.svg", "./manifest.webmanifest"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // Never cache the API — always go to network.
  if (e.request.method !== "GET" || url.pathname.startsWith("/spotify/") ||
      ["/chat", "/talk", "/voice", "/health"].some(p => url.pathname.endsWith(p))) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request)));
});
