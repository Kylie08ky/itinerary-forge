/* AI 旅行规划 · Service Worker（规格 M15 S3 / S4）
 *
 * 目标：先联网打开过的行程，没网时仍能只读查看。手写、不依赖任何第三方库。
 *
 *   页面外壳（index.html 和构建出的 /assets/*） —— 安装时预缓存；之后 cache-first
 *   页面导航                                  —— 先走网络，断网退回缓存的外壳（单页应用自己路由）
 *   后端的只读接口（行程、路线、地点、分享…）  —— 先走网络并更新缓存，断网退回缓存，
 *                                               并加响应头 x-from-cache: 1，页面据此提示「只读」
 *   地图瓦片（OpenStreetMap）                  —— cache-first，最多留 400 张；没缓存到的缺图
 *   地点照片 / 封面大图（/media/places/*、/media/hero/*）—— cache-first
 *   写操作（POST / PUT / DELETE）、导出下载、任务进度、模型用量 —— 一律不碰
 */
const VERSION = "v1";
const SHELL = `atp-shell-${VERSION}`;
const DATA = `atp-data-${VERSION}`;
const TILES = `atp-tiles-${VERSION}`;
const MEDIA = `atp-media-${VERSION}`;
const KEEP = new Set([SHELL, DATA, TILES, MEDIA]);
const MAX_TILES = 400;

// 只缓存这些只读接口；导出文件（.pdf/.ics/.kml）、/tasks、/llm、搜索都不在其中
const API_READ = /^\/(plans(\/[^/]+(\/(route|trace|share|reservations|attachments))?)?|plan-cards|places(\/(?!search)[^/]+)?|themes(\/[^/]+)?|share\/[^/]+|attractions)$/;

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL);
      const res = await fetch("/", { cache: "reload" });
      const html = await res.clone().text();
      await cache.put("/", res);
      // 把 index.html 里引用的构建产物一并缓存，首次联网就能离线启动
      const assets = [...new Set(html.match(/\/assets\/[^"')\s]+/g) ?? [])];
      const statics = ["/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"];
      await Promise.all(
        [...assets, ...statics].map((u) => cache.add(u).catch(() => undefined)),
      );
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const k of await caches.keys()) if (!KEEP.has(k)) await caches.delete(k);
      await self.clients.claim();
    })(),
  );
});

function fromCache(res) {
  const headers = new Headers(res.headers);
  headers.set("x-from-cache", "1");
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}

async function networkFirstApi(request) {
  const cache = await caches.open(DATA);
  try {
    const res = await fetch(request);
    if (res.ok) await cache.put(request, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(request, { ignoreVary: true });
    if (hit) return fromCache(hit);
    throw err;
  }
}

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) return hit;
  const res = await fetch(request);
  if (res.ok || res.type === "opaque") await cache.put(request, res.clone());
  return res;
}

async function tile(request) {
  const cache = await caches.open(TILES);
  const hit = await cache.match(request);
  if (hit) return hit;
  try {
    const res = await fetch(request);
    if (res.ok || res.type === "opaque") {
      await cache.put(request, res.clone());
      const keys = await cache.keys();
      if (keys.length > MAX_TILES) await Promise.all(keys.slice(0, keys.length - MAX_TILES).map((k) => cache.delete(k)));
    }
    return res;
  } catch {
    return Response.error(); // 没缓存到的瓦片：缺图，页面本身不受影响
  }
}

async function navigation(request) {
  const cache = await caches.open(SHELL);
  try {
    const res = await fetch(request);
    if (res.ok) await cache.put("/", res.clone());
    return res;
  } catch {
    return (await cache.match("/")) ?? Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (/^\/media\/(places|hero)\//.test(url.pathname)) {
    event.respondWith(cacheFirst(req, MEDIA)); // 地点照片：先联网看过的，离线也有（规格 M02 S7b-5）
    return;
  }
  if (/(^|\.)tile\.openstreetmap\.org$/.test(url.hostname)) {
    event.respondWith(tile(req));
    return;
  }
  if (url.origin === self.location.origin) {
    if (req.mode === "navigate") {
      event.respondWith(navigation(req));
    } else if (url.pathname.startsWith("/assets/") || /\.(png|svg|webmanifest|ico)$/.test(url.pathname)) {
      event.respondWith(cacheFirst(req, SHELL));
    }
    return;
  }
  if (API_READ.test(url.pathname) && !/\.(pdf|ics|kml|md|html)$/.test(url.pathname)) {
    event.respondWith(networkFirstApi(req));
  }
});
