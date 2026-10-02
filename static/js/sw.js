/* DIAL service worker.
 *
 * Served by apps.core.views.service_worker at /sw.js; the two __DIAL_*__ placeholders are filled in there
 * (the raw file under /static/js/ is not meant to be registered directly).
 *
 * Strategy
 *   navigations   network-first (4 s timeout) -> cached copy -> /offline/; only a small allowlist of
 *                 HTML pages is ever stored, never redirects, non-200s or Cache-Control: no-store
 *   /static/      cache-first (URLs are versioned with ?v=ASSET_VERSION)
 *   anything else network only (API, forms, admin, provisioning, downloads ...)
 * A POST to /accounts/logout/ purges the page cache so a cached dashboard never outlives the session.
 */
"use strict";

const VERSION = "__DIAL_ASSET_VERSION__";
const STATIC_CACHE = "dial-static-" + VERSION;
const PAGES_CACHE = "dial-pages-" + VERSION;
const PRECACHE = __DIAL_PRECACHE__;
const OFFLINE_URL = "/offline/";
const NAV_TIMEOUT_MS = 4000;

// HTML pages that may be cached for offline use (pathname only, no query string).
const PAGE_ALLOW = [
  /^\/$/,                                   // home
  /^\/offline\/$/,
  /^\/docs\/(?:[a-z0-9-]+\/)?$/,            // docs index + pages
  /^\/e\/[a-z0-9_-]+\/$/,                   // event dashboard
  /^\/e\/[a-z0-9_-]+\/phonebook\/$/,        // phonebook index (bare list)
];
// Never cached, never served from cache - checked before the allowlist.
const PAGE_DENY = [
  /^\/accounts\//, /^\/admin\//, /^\/api\//, /^\/prov\//, /^\/switch\//,
  /^\/e\/[^/]+\/orga\//, /^\/e\/[^/]+\/pbx\//,
  /qr\.png$/, /\.vcf$/, /\.csv$/, /\.pdf$/, /\.ldif$/, /\.xml$/, /\.json$/,
];

function isCacheablePage(url) {
  if (url.origin !== self.location.origin || url.search) return false;
  const p = url.pathname;
  if (PAGE_DENY.some(function (re) { return re.test(p); })) return false;
  return PAGE_ALLOW.some(function (re) { return re.test(p); });
}

function isStatic(url) {
  return url.origin === self.location.origin && url.pathname.startsWith("/static/");
}

function storable(response) {
  if (!response || response.status !== 200 || response.type !== "basic" || response.redirected) return false;
  const cc = (response.headers.get("Cache-Control") || "").toLowerCase();
  return !cc.includes("no-store");
}

function fetchWithTimeout(request, ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(function () { ctrl.abort(); }, ms);
  return fetch(request, { signal: ctrl.signal }).finally(function () { clearTimeout(timer); });
}

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(function (cache) {
      // one failing asset must not block installation
      return Promise.allSettled(PRECACHE.map(function (u) {
        return cache.add(new Request(u, { credentials: "same-origin", cache: "reload" }));
      }));
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) {
        return k.startsWith("dial-") && k !== STATIC_CACHE && k !== PAGES_CACHE;
      }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("message", function (event) {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

async function handleNavigation(request, url) {
  const cacheable = isCacheablePage(url);
  try {
    const response = await fetchWithTimeout(request, NAV_TIMEOUT_MS);
    if (cacheable && storable(response)) {
      const copy = response.clone();
      caches.open(PAGES_CACHE).then(function (c) { return c.put(url.pathname, copy); }).catch(function () {});
    }
    return response;
  } catch (err) {
    if (cacheable) {
      const cached = await caches.match(url.pathname, { cacheName: PAGES_CACHE, ignoreVary: true });
      if (cached) return cached;
    }
    const offline = await caches.match(OFFLINE_URL, { ignoreVary: true });
    if (offline) return offline;
    throw err;
  }
}

async function handleStatic(request) {
  const cached = await caches.match(request, { ignoreVary: true, ignoreSearch: false });
  if (cached) return cached;
  const response = await fetch(request);
  if (storable(response)) {
    const copy = response.clone();
    caches.open(STATIC_CACHE).then(function (c) { return c.put(request, copy); }).catch(function () {});
  }
  return response;
}

self.addEventListener("fetch", function (event) {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method === "POST" && url.pathname.startsWith("/accounts/logout/")) {
    event.waitUntil(caches.delete(PAGES_CACHE));
    return; // network as usual
  }
  if (request.method !== "GET") return;

  if (request.mode === "navigate") {
    event.respondWith(handleNavigation(request, url));
  } else if (isStatic(url)) {
    event.respondWith(handleStatic(request));
  }
  // everything else: let the browser talk to the network directly
});
