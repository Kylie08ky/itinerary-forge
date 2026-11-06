import { inChina, toGcj } from "./mapkit";
import type { AttractionRef } from "./types";

type Pt = Pick<AttractionRef, "lat" | "lng"> & { name_zh?: string };

/** 境内（高德有细节的地方）的点用高德网页链接，其余用 Google 地图（规格 M05 S7e）。网页链接都不要 Key、不耗高德配额。 */
const onAmap = (p?: Pt | null): boolean => !!p && inChina(p.lat, p.lng);
const AMAP = "https://uri.amap.com";
const SRC = "src=ai-travel-planner";

/** `经度,纬度,名字`：带上名字，高德页面才显示「从哪里到哪里」。高德用 GCJ-02；库里是 WGS-84。 */
function lnglat(p: Pt): string {
  const [lng, lat] = toGcj(p.lat, p.lng);
  const name = p.name_zh ? `,${encodeURIComponent(p.name_zh.replace(/[|,]/g, " "))}` : "";
  return `${lng.toFixed(6)},${lat.toFixed(6)}${name}`;
}

/** 这一处该叫哪个地图：用在按钮 / 提示文字里。 */
export function mapName(p?: Pt | null): string {
  return onAmap(p) ? "高德地图" : "Google 地图";
}

/** 两点之间的路线链接（公共交通）。只用核对过的坐标。 */
export function legUrl(a: Pt, b: Pt): string {
  if (onAmap(a) && onAmap(b)) return `${AMAP}/navigation?from=${lnglat(a)}&to=${lnglat(b)}&mode=bus&coordinate=gaode&${SRC}`;
  const pt = (p: Pt) => `${p.lat.toFixed(6)},${p.lng.toFixed(6)}`;
  return `https://www.google.com/maps/dir/?api=1&origin=${pt(a)}&destination=${pt(b)}&travelmode=transit`;
}

/** 从「你现在的位置」到这个点的路线（公共交通）：不写起点，地图会用手机当前位置（高德的网页链接这一项只在手机上有效）。 */
export function navHere(b: Pt): string {
  if (onAmap(b)) return `${AMAP}/navigation?to=${lnglat(b)}&mode=bus&coordinate=gaode&${SRC}`;
  return `https://www.google.com/maps/dir/?api=1&destination=${b.lat.toFixed(6)},${b.lng.toFixed(6)}&travelmode=transit`;
}

export interface DishSearch {
  key: "dianping" | "xhs" | "google";
  label: string;
  href: string;
}

/**
 * 名菜旁边的「去哪吃」入口（规格 10 H22）：不存餐厅，点了才去别处现查。
 * 国内：大众点评、小红书（都是网页搜索链接，用「城市名 + 菜名」）；
 * 国外：Google 地图，用当地语言搜「当地菜名 + 当地城市名」，直接搜到当地餐厅；没有当地语言名时退回中文名。
 */
export function dishSearches(
  d: { name_zh: string; name_local?: string; city_zh?: string; city_local?: string },
  near?: Pt | null,
): DishSearch[] {
  const city = d.city_zh ?? "";
  if (onAmap(near)) {
    const short = city.length > 2 ? city.replace(/市$/, "") : city; // 「泉州市米香」不如「泉州米香」好搜
    const q = encodeURIComponent(`${short}${d.name_zh}`);
    return [
      { key: "dianping", label: "在大众点评里搜", href: `https://www.dianping.com/search/keyword/0/0_${q}` },
      { key: "xhs", label: "在小红书里搜", href: `https://www.xiaohongshu.com/search_result?keyword=${q}` },
    ];
  }
  const q = d.name_local ? `${d.name_local} ${d.city_local || city}`.trim() : `${d.name_zh} ${city}`.trim();
  return [{ key: "google", label: "在 Google 地图里搜", href: `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(q)}` }];
}

/** 搜「你附近」的某类地方（不写位置，地图会用手机当前位置）。near = 这趟行程所在的位置，用来选地图。 */
export function searchNearby(query: string, near?: Pt | null): string {
  if (onAmap(near)) return `${AMAP}/search?keyword=${encodeURIComponent(query.split(" ")[0])}&view=map&${SRC}`;
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`;
}
