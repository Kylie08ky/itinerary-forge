import L from "leaflet";
import { useEffect, useRef, useState } from "react";
import { mediaSrc } from "../api";
import { FALLBACK_NOTE, inChina, loadAmap, type MapFallback } from "../mapkit";
import { localLabel } from "../names";
import type { AttractionRef, ItineraryItem } from "../types";
import { drawAmap } from "./amapDraw";

function matchAttraction(item: ItineraryItem, catalog: AttractionRef[]): AttractionRef | undefined {
  // 行程条目带 place_id（来自地点库）时直接按 id 对上，不靠标题模糊匹配
  if (item.place_id) {
    const byId = catalog.find((a) => a.place_id === item.place_id);
    if (byId) return byId;
  }
  const title = item.title;
  const clean = title.replace(/[（(].*/, "").trim();
  if (!clean) return undefined;
  return catalog.find((a) => a.name_zh === clean || a.name_zh.includes(clean) || clean.includes(a.name_zh));
}

export interface MapPoint {
  item: ItineraryItem;
  attraction: AttractionRef;
}

export function matchDayPoints(items: ItineraryItem[], catalog: AttractionRef[]): MapPoint[] {
  const points: MapPoint[] = [];
  for (const item of items) {
    const a = matchAttraction(item, catalog);
    if (a) points.push({ item, attraction: a });
  }
  return points;
}

/** 地图标记的悬停气泡：有照片就带一张小缩略图（规格 M02 S7b-4）；用 DOM 构造，名字当文本，不拼 HTML。 */
export function bubble(text: string, imagePath?: string, sub?: string | null): HTMLElement {
  const box = document.createElement("div");
  box.className = "map-bubble";
  if (imagePath) {
    const img = document.createElement("img");
    img.src = mediaSrc(imagePath);
    img.alt = "";
    img.crossOrigin = "anonymous";
    img.onerror = () => img.remove();
    box.appendChild(img);
  }
  const label = document.createElement("span");
  label.textContent = text;
  if (sub) {
    const small = document.createElement("small"); // 当地名放在第二行小字
    small.className = "bubble-local";
    small.textContent = sub;
    label.appendChild(small);
  }
  box.appendChild(label);
  return box;
}

const GAP = 14; // 气泡和标记中心的距离（标记直径 26）
const OFFSETS: Record<string, [number, number]> = { top: [0, -GAP], bottom: [0, GAP], right: [GAP, 0], left: [-GAP, 0] };

/**
 * 悬停气泡要完整显示（规格 M02 S7b-4）：标记靠近地图边缘时，气泡会被地图容器裁掉一截。
 * 气泡打开后量一下：放得下就不动；放不下就换一个方向（上 / 下 / 右 / 左，第一个放得下的）；
 * 四个方向都放不下（地图比气泡还小）才选超出最少的，并把地图平移刚好够的距离。
 * 不靠「先平移地图」：标记会从鼠标下面移走，气泡就消失了。照片加载完气泡变大了会再量一次。
 */
export function keepTooltipInView(map: L.Map, marker: L.Marker): void {
  let busy = false;
  const place = () => {
    const tip = marker.getTooltip();
    const el = tip?.getElement();
    if (!tip || !el || busy) return;
    const size = map.getSize();
    const pt = map.latLngToContainerPoint(marker.getLatLng());
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const pad = 6;
    const spots: Record<string, [number, number]> = {
      top: [pt.x - w / 2, pt.y - GAP - h],
      bottom: [pt.x - w / 2, pt.y + GAP],
      right: [pt.x + GAP, pt.y - h / 2],
      left: [pt.x - GAP - w, pt.y - h / 2],
    };
    const over = ([x, y]: [number, number]) =>
      Math.max(0, pad - x) + Math.max(0, pad - y) + Math.max(0, x + w - (size.x - pad)) + Math.max(0, y + h - (size.y - pad));
    const order = ["top", "bottom", "right", "left"];
    const current = String(tip.options.direction ?? "top");
    const best = [current, ...order.filter((d) => d !== current)].reduce((a, b) => (over(spots[b]) < over(spots[a]) ? b : a));
    if (over(spots[current]) === 0) return; // 现在的方向就放得下
    if (best !== current) {
      busy = true; // 重新打开会再触发一次 tooltipopen，这里不要递归
      tip.options.direction = best as L.Direction;
      tip.options.offset = L.point(OFFSETS[best]);
      marker.closeTooltip();
      marker.openTooltip();
      busy = false;
    }
    const [x, y] = spots[best];
    const dx = x < pad ? x - pad : x + w > size.x - pad ? x + w - (size.x - pad) : 0;
    const dy = y < pad ? y - pad : y + h > size.y - pad ? y + h - (size.y - pad) : 0;
    if (dx || dy) map.panBy([dx, dy], { animate: true, duration: 0.25 }); // 四个方向都放不下时的最后手段
  };
  marker.on("tooltipopen", (e) => {
    requestAnimationFrame(place);
    (e as L.TooltipEvent).tooltip
      .getElement()
      ?.querySelectorAll("img")
      .forEach((img) => img.addEventListener("load", place, { once: true }));
  });
}

function numberIcon(n: number, accent: string): L.DivIcon {
  return L.divIcon({
    className: "",
    html: `<div class="map-pin" style="background:${accent}">${n}</div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

/** 一天的迷你地图：按行程顺序给每个点编号并连线（地点库里的点都会画）；一个点时只标出位置。境内的点走高德，其余走 OpenStreetMap。 */
export function DayMap({ points, photos = true }: { points: MapPoint[]; photos?: boolean }) {
  const elRef = useRef<HTMLDivElement>(null);
  const [fallback, setFallback] = useState<MapFallback | null>(null); // 境内的点本该用高德却退回了基础地图

  useEffect(() => {
    if (!elRef.current || points.length < 1) return;
    const el = elRef.current;
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#b3541e";
    const tipOf = (p: MapPoint, i: number) => () =>
      bubble(`${i + 1}. ${p.item.title}`, photos ? p.attraction.media?.image?.url : undefined, localLabel(p.attraction.name_zh, p.attraction.name_local));
    let dispose = () => {};
    let cancelled = false;

    const drawLeaflet = () => {
      const map = L.map(el, { zoomControl: false, attributionControl: true, scrollWheelZoom: false });
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      }).addTo(map);

      const latlngs: [number, number][] = points.map((p) => [p.attraction.lat, p.attraction.lng]);
      points.forEach((p, i) => {
        const marker = L.marker([p.attraction.lat, p.attraction.lng], { icon: numberIcon(i + 1, accent) })
          .addTo(map)
          .bindTooltip(tipOf(p, i)(), { direction: "top", offset: [0, -14] });
        keepTooltipInView(map, marker);
      });
      if (latlngs.length > 1) {
        L.polyline(latlngs, { color: accent, weight: 3, opacity: 0.6, dashArray: "1 8", lineCap: "round" }).addTo(map);
      }
      map.fitBounds(L.latLngBounds(latlngs), { padding: [28, 28], maxZoom: 16 });
      return () => map.remove();
    };

    if (points.every((p) => inChina(p.attraction.lat, p.attraction.lng))) {
      loadAmap().then((r) => {
        if (cancelled) return;
        if ("AMap" in r) {
          setFallback(null);
          dispose = drawAmap(
            r.AMap,
            el,
            points.map((p, i) => ({ lat: p.attraction.lat, lng: p.attraction.lng, n: i + 1, color: accent, tip: tipOf(p, i) })),
            [{ color: accent, weight: 3, opacity: 0.6, dashed: true, points: points.map((p) => [p.attraction.lat, p.attraction.lng]) }],
            { maxZoom: 16, padding: 28 },
          );
        } else {
          setFallback(r.fallback);
          dispose = drawLeaflet();
        }
      });
    } else {
      dispose = drawLeaflet();
    }

    return () => {
      cancelled = true;
      dispose();
    };
  }, [points, photos]);

  if (points.length < 1) return null;
  return (
    <>
      <div className="daymap" ref={elRef} />
      {fallback && <p className="map-fallback-note">{FALLBACK_NOTE[fallback]}</p>}
    </>
  );
}
