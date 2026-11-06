/**
 * 国内地图走高德（规格 M05 S7）：加载 JS API 2.0（官方脚本，不引第三方包）、WGS-84 → GCJ-02。
 * 没配 Key / 额度用完 / 脚本加载失败：返回原因，调用方退回 OpenStreetMap，不报错。
 */
import { api } from "./api";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type AMapNS = any;
export type MapFallback = "no_key" | "quota" | "load_failed";
export type AmapResult = { AMap: AMapNS } | { fallback: MapFallback };

const PI = Math.PI;
const A = 6378245.0;
const EE = 0.00669342162296594323;

/** 大陆范围（高德有细节的地方）；用矩形再挖掉朝鲜半岛 / 日本一侧。和后端 app/maps/coords.py 同一套。 */
export function inChina(lat: number, lng: number): boolean {
  if (!(73.66 < lng && lng < 135.05 && 3.86 < lat && lat < 53.55)) return false;
  return !(lng > 123 && lat < 41);
}

function dlat(x: number, y: number): number {
  let r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  r += ((20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2) / 3;
  r += ((20 * Math.sin(y * PI) + 40 * Math.sin((y / 3) * PI)) * 2) / 3;
  r += ((160 * Math.sin((y / 12) * PI) + 320 * Math.sin((y * PI) / 30)) * 2) / 3;
  return r;
}
function dlng(x: number, y: number): number {
  let r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  r += ((20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2) / 3;
  r += ((20 * Math.sin(x * PI) + 40 * Math.sin((x / 3) * PI)) * 2) / 3;
  r += ((150 * Math.sin((x / 12) * PI) + 300 * Math.sin((x / 30) * PI)) * 2) / 3;
  return r;
}

/** 返回 [经度, 纬度]（高德的顺序）。库里的坐标是 WGS-84，只在画图时换。 */
export function toGcj(lat: number, lng: number): [number, number] {
  if (!inChina(lat, lng)) return [lng, lat];
  let a = dlat(lng - 105, lat - 35);
  let b = dlng(lng - 105, lat - 35);
  const rad = (lat / 180) * PI;
  const magic = 1 - EE * Math.sin(rad) ** 2;
  const sq = Math.sqrt(magic);
  a = (a * 180) / (((A * (1 - EE)) / (magic * sq)) * PI);
  b = (b * 180) / ((A / sq) * Math.cos(rad) * PI);
  return [lng + b, lat + a];
}

let loading: Promise<AmapResult> | null = null;

function inject(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("高德脚本加载失败"));
    document.head.appendChild(s);
  });
}

/** 整个页面只问一次后端、只加载一次脚本（后端把这一次算作一次地图加载，规格 M05 S7c）。 */
export function loadAmap(): Promise<AmapResult> {
  if (loading) return loading;
  loading = (async (): Promise<AmapResult> => {
    let cfg: { provider: string; reason?: string; js_key?: string; security_code?: string };
    try {
      cfg = await api.mapConfig();
    } catch {
      return { fallback: "load_failed" };
    }
    if (cfg.provider !== "amap" || !cfg.js_key || !cfg.security_code) {
      return { fallback: cfg.reason === "quota" ? "quota" : "no_key" };
    }
    try {
      (window as unknown as { _AMapSecurityConfig: unknown })._AMapSecurityConfig = { securityJsCode: cfg.security_code };
      await inject(`https://webapi.amap.com/maps?v=2.0&plugin=AMap.MarkerCluster&key=${encodeURIComponent(cfg.js_key)}`);
      const AMap = (window as unknown as { AMap?: AMapNS }).AMap;
      return AMap ? { AMap } : { fallback: "load_failed" };
    } catch {
      return { fallback: "load_failed" };
    }
  })();
  loading.then((r) => {
    if ("fallback" in r && r.fallback === "load_failed") loading = null; // 网络问题下次还能试；没 Key / 额度用完不重复问
  });
  return loading;
}

export const FALLBACK_NOTE: Record<MapFallback, string> = {
  no_key: "当前使用基础地图（没配高德 Key）",
  quota: "当前使用基础地图（本月高德额度已到自留上限）",
  load_failed: "当前使用基础地图（高德地图没加载出来）",
};
