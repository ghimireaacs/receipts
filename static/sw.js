// Keeps the app shell available when the server cannot be reached, so photos can still be taken.
// The API is never cached.
const CACHE = "receipts-v8";
const SHELL = ["./", "index.html", "app.css", "app.js", "manifest.webmanifest", "icon.svg", "icon-180.png", "icon-192.png", "icon-512.png"];

self.addEventListener("install", (e) => {
  // best effort: while signed out the files redirect to Authentik, and that must not block the update
  e.waitUntil(caches.open(CACHE).then((c) => Promise.allSettled(SHELL.map((u) =>
    fetch(u, { redirect: "manual" }).then((r) => (r.ok && r.type === "basic" ? c.put(u, r) : null))))).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.includes("/api/")) return;
  // network first, so a new version shows up straight away; the cache only covers being offline
  e.respondWith(
    fetch(e.request).then((r) => {
      // only keep real app files: a sign-in redirect (Authentik) must never be cached as the app
      if (r.ok && r.type === "basic" && !r.redirected) {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      }
      return r;
    }).catch(() => caches.match(e.request).then((r) => r || caches.match("index.html")))
  );
});
