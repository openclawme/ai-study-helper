/*
 * 我的AI学习助手 — PWA Service Worker
 *
 * 只缓存「页面壳」和 CDN 静态资源。
 * /api/、/media/、/anim/、/admin/、/paper/ 等动态或个人数据一律不缓存：
 *   - 避免换账号后看到别人的照片/对话
 *   - 避免你更新服务器后，用户被旧页面锁死
 *
 * 更新策略：页面导航网络优先；静态资源 stale-while-revalidate。
 * 新版本由 /pwa.js 提示用户点「刷新」，不静默替换正在用的页面。
 */
const SHELL_CACHE = 'gcl-shell-v1';
const RUNTIME_CACHE = 'gcl-runtime-v1';

const SHELL_ASSETS = [
  '/offline.html',
  '/manifest.webmanifest',
  '/pwa.js',
  '/favicon.svg',
  '/favicon.ico',
  '/favicon-192.png',
  '/pwa-icon-192.png',
  '/pwa-icon-512.png',
  '/pwa-icon-maskable-512.png',
  '/apple-touch-icon.png'
];

const CDN_HOSTS = new Set([
  'cdn.tailwindcss.com',
  'cdn.jsdelivr.net'
]);

const PRIVATE_PREFIXES = [
  '/api/',
  '/media/',
  '/anim/',
  '/admin',
  '/paper',
  '/ppt',
  '/board'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  // 不在这里 skipWaiting：让用户决定什么时候刷新，避免把正在填的表单冲掉。
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys
        .filter((key) => key !== SHELL_CACHE && key !== RUNTIME_CACHE)
        .map((key) => caches.delete(key))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

function isPrivatePath(pathname) {
  return PRIVATE_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function isShellAsset(pathname) {
  return pathname === '/manifest.webmanifest'
    || pathname === '/pwa.js'
    || pathname === '/favicon.ico'
    || pathname === '/favicon.svg'
    || pathname.startsWith('/favicon-')
    || pathname.startsWith('/pwa-icon-')
    || pathname.startsWith('/apple-touch-icon');
}

async function networkFirst(request) {
  try {
    // no-store：不要让浏览器把上次的登录页/旧 HTML 从 HTTP 缓存里拿出来，
    // 否则断网时会显示一个过期的页面，而不是明确的离线页。
    return await fetch(request, { cache: 'no-store' });
  } catch (err) {
    const offline = await caches.match('/offline.html');
    if (offline) return offline;
    return new Response(
      '<!doctype html><meta charset="utf-8"><title>暂时离线</title>'
      + '<body style="font-family:sans-serif;padding:40px;text-align:center">'
      + '<h1>暂时离线</h1><p>网络恢复后刷新即可。</p></body>',
      { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
    );
  }
}

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const network = fetch(request).then((response) => {
    // opaque 是跨域 CDN 的 normal 响应（status 0），也可以缓存
    if (response && (response.ok || response.type === 'opaque')) {
      cache.put(request, response.clone());
    }
    return response;
  }).catch(() => null);
  return cached || (await network) || Response.error();
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  let url;
  try {
    url = new URL(request.url);
  } catch (err) {
    return;
  }

  // 个人数据、动态接口、付费/管理页面：完全不拦截，避免缓存泄露。
  if (url.origin === self.location.origin && isPrivatePath(url.pathname)) {
    return;
  }

  // 页面导航：网络优先；断网时给离线页。
  // 不缓存导航响应，确保每次打开都拿到最新 HTML。
  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request));
    return;
  }

  if (url.origin === self.location.origin) {
    if (isShellAsset(url.pathname)) {
      event.respondWith(staleWhileRevalidate(request, SHELL_CACHE));
    }
    return;
  }

  if (CDN_HOSTS.has(url.hostname)) {
    event.respondWith(staleWhileRevalidate(request, RUNTIME_CACHE));
  }
});
