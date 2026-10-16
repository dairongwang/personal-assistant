/* Service worker: installable app + offline read access.
 *
 * - App shell (HTML/CSS/JS/icons): stale-while-revalidate. Served instantly from cache,
 *   refreshed in the background. Bump SHELL_VERSION on releases to drop old caches.
 * - API GET requests: network-first, falling back to the last cached response when
 *   offline, so a phone without signal still shows its tasks.
 * - Everything else (writes, /docs): network only.
 */
const SHELL_VERSION = 'v0.2.0';
const SHELL_CACHE = `pa-shell-${SHELL_VERSION}`;
const API_CACHE = 'pa-api-v1';
const SHELL_ASSETS = [
  '/',
  '/index.html',
  '/css/app.css',
  '/js/app.js',
  '/manifest.webmanifest',
  '/icons/favicon.svg',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable-512.png',
  '/icons/apple-touch-icon.png',
];
const API_PATHS = ['/tasks', '/events', '/today', '/health'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k.startsWith('pa-shell-') && k !== SHELL_CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});

function isApi(url) {
  return API_PATHS.some((p) => url.pathname === p || url.pathname.startsWith(`${p}/`));
}

function isShell(url, request) {
  return request.mode === 'navigate' || SHELL_ASSETS.includes(url.pathname);
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return; // writes always go to the network
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (isApi(url)) {
    event.respondWith(networkFirst(request));
  } else if (isShell(url, request)) {
    event.respondWith(staleWhileRevalidate(request));
  }
});

async function networkFirst(request) {
  const cache = await caches.open(API_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch {
    const cached = await cache.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({ detail: 'You are offline and this data has not been cached yet.', offline: true }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(SHELL_CACHE);
  // Navigations always resolve to the app shell (single page).
  const key = request.mode === 'navigate' ? '/' : request;
  const cached = await cache.match(key);
  const network = fetch(request).then((response) => {
    if (response.ok) cache.put(key, response.clone());
    return response;
  }).catch(() => cached);
  return cached || network;
}
