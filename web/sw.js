/* ============================================================
   SWITCHBOARD LAB — SERVICE WORKER
   Makes the lab installable + offline-capable.

   Strategy:
   - precache the app shell on install (lab pages, shared css/js, icons, root pages)
   - navigations: network-first, fall back to cache, then to the offline shell
   - same-origin static assets: stale-while-revalidate
   - cross-origin (fonts etc.): pass through, opportunistically cache
   Bump CACHE_VERSION to invalidate old caches on deploy.
   ============================================================ */

const CACHE_VERSION = "switchboard-lab-v1";
const RUNTIME = "switchboard-runtime-v1";

// Resolve against the SW scope so it works under any base path
// (e.g. project Pages at /switchboard/web/).
const SHELL = [
  "./",
  "./manifest.json",
  "./icon.svg",
  "./icon-maskable.svg",
  // root pages
  "./index.html",
  "./agents-demo.html",
  "./simulator.html",
  "./docs.html",
  // multi-page lab
  "./lab/index.html",
  "./lab/shared.css",
  "./lab/shared.js",
  "./lab/x402.html",
  "./lab/escrow.html",
  "./lab/streaming.html",
  "./lab/auction.html",
  "./lab/swap.html",
  "./lab/taxi.html",
  "./lab/cafe.html",
  "./lab/delivery.html",
  "./lab/trip.html",
  "./lab/multichain.html",
  "./lab/pq.html",
  "./lab/rails.html",
  "./lab/tools.html",
];

const OFFLINE_FALLBACK = "./lab/index.html";

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_VERSION);
      // addAll is atomic; if any single asset 404s the whole install fails,
      // so add resiliently (the shell list may drift from disk over time).
      await Promise.all(
        SHELL.map((url) =>
          cache.add(new Request(url, { cache: "reload" })).catch(() => {})
        )
      );
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => k !== CACHE_VERSION && k !== RUNTIME)
          .map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

// Allow the page to trigger an immediate update.
self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;

  // 1) Navigations -> network-first, fall back to cache, then offline shell.
  if (req.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          const fresh = await fetch(req);
          const cache = await caches.open(RUNTIME);
          cache.put(req, fresh.clone());
          return fresh;
        } catch (e) {
          const cached = await caches.match(req);
          if (cached) return cached;
          return (
            (await caches.match(OFFLINE_FALLBACK)) ||
            new Response("Offline", { status: 503, statusText: "Offline" })
          );
        }
      })()
    );
    return;
  }

  // 2) Same-origin static assets -> stale-while-revalidate.
  if (sameOrigin) {
    event.respondWith(
      (async () => {
        const cached = await caches.match(req);
        const network = fetch(req)
          .then((resp) => {
            if (resp && resp.ok) {
              caches.open(CACHE_VERSION).then((c) => c.put(req, resp.clone()));
            }
            return resp;
          })
          .catch(() => null);
        return cached || (await network) || new Response("", { status: 504 });
      })()
    );
    return;
  }

  // 3) Cross-origin (Google Fonts, etc.) -> cache-first, opportunistic fill.
  event.respondWith(
    (async () => {
      const cached = await caches.match(req);
      if (cached) return cached;
      try {
        const resp = await fetch(req);
        // Opaque responses are fine to cache for offline font rendering.
        const cache = await caches.open(RUNTIME);
        cache.put(req, resp.clone());
        return resp;
      } catch (e) {
        return cached || new Response("", { status: 504 });
      }
    })()
  );
});
