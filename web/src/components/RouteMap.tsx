import L from "leaflet";
import "leaflet.markercluster";
import { useEffect, useRef, useState } from "react";
import { FALLBACK_NOTE, inChina, loadAmap, type MapFallback } from "../mapkit";
import type { RouteData } from "../types";
import { drawAmap, type AmapLine, type AmapPin } from "./amapDraw";
import { bubble, keepTooltipInView } from "./DayMap";

function pin(n: number, color: string): L.DivIcon {
  return L.divIcon({
    className: "",
    html: `<div class="map-pin" style="background:${color}">${n}</div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

/**
 * 全程地图（规格 M05 S3 / S6）：每天一种颜色，图例列出每天的直线距离，点图例只看某一天；
 * 缩小时标记聚合成带数字的圆圈，放大后展开。点标记打开地点详情。
 */
export function RouteMap({
  route,
  onOpenPlace,
  photos,
}: {
  route: RouteData;
  onOpenPlace?: (pid: string) => void;
  photos?: Record<string, string>; // 地点编号 -> 照片路径；给了才在悬停气泡里放缩略图（公开分享页不给）
}) {
  const elRef = useRef<HTMLDivElement>(null);
  const [only, setOnly] = useState<number | null>(null); // 只看第几天（0 起），null = 全部

  const [fallback, setFallback] = useState<MapFallback | null>(null); // 境内的路线本该用高德却退回了基础地图

  useEffect(() => {
    if (!elRef.current) return;
    const el = elRef.current;
    const shown = route.days.map((d, i) => ({ d, i })).filter(({ i }) => only === null || only === i);
    const tipOf = (d: (typeof route.days)[number], i: number, k: number) => () => {
      const s = d.stops[k];
      return bubble(`第 ${i + 1} 天 · ${k + 1}. ${s.name}（${s.time}）`, photos?.[s.place_id], s.name_local);
    };
    let dispose = () => {};
    let cancelled = false;

    const drawLeaflet = () => {
      const map = L.map(el, { scrollWheelZoom: false });
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      }).addTo(map);

      const cluster = L.markerClusterGroup({ showCoverageOnHover: false, maxClusterRadius: 30, disableClusteringAtZoom: 13 });
      const bounds: L.LatLngTuple[] = [];
      shown.forEach(({ d, i }) => {
        const latlngs: L.LatLngTuple[] = d.stops.map((s) => [s.lat, s.lng]);
        if (latlngs.length > 1) L.polyline(latlngs, { color: d.color, weight: 4, opacity: 0.8 }).addTo(map);
        d.stops.forEach((s, k) => {
          const m = L.marker([s.lat, s.lng], { icon: pin(k + 1, d.color) }).bindTooltip(tipOf(d, i, k)(), {
            direction: "top",
            offset: [0, -14],
          });
          keepTooltipInView(map, m);
          if (onOpenPlace) m.on("click", () => onOpenPlace(s.place_id));
          cluster.addLayer(m);
          bounds.push([s.lat, s.lng]);
        });
      });
      map.addLayer(cluster);
      if (bounds.length) map.fitBounds(L.latLngBounds(bounds), { padding: [30, 30], maxZoom: 16 });
      else map.setView([48.8566, 2.3522], 12);
      return () => map.remove();
    };

    const stops = shown.flatMap(({ d }) => d.stops);
    if (stops.length > 0 && stops.every((s) => inChina(s.lat, s.lng))) {
      loadAmap().then((r) => {
        if (cancelled) return;
        if ("AMap" in r) {
          setFallback(null);
          const pins: AmapPin[] = shown.flatMap(({ d, i }) =>
            d.stops.map((s, k) => ({
              lat: s.lat,
              lng: s.lng,
              n: k + 1,
              color: d.color,
              tip: tipOf(d, i, k),
              onClick: onOpenPlace ? () => onOpenPlace(s.place_id) : undefined,
            })),
          );
          const lines: AmapLine[] = shown.map(({ d }) => ({
            color: d.color,
            weight: 4,
            opacity: 0.8,
            points: d.stops.map((s) => [s.lat, s.lng]),
          }));
          dispose = drawAmap(r.AMap, el, pins, lines, { maxZoom: 16, padding: 30, cluster: true });
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
  }, [route, only, onOpenPlace, photos]);

  return (
    <div className="routemap-wrap">
      <div className="fullmap" ref={elRef} data-testid="full-map" />
      {fallback && <p className="map-fallback-note">{FALLBACK_NOTE[fallback]}</p>}
      <div className="route-legend">
        {route.days.map((d, i) => (
          <button
            type="button"
            key={d.date}
            className={`lg${only === i ? " on" : ""}`}
            aria-pressed={only === i}
            onClick={() => setOnly(only === i ? null : i)}
            title="点一下只看这一天，再点一下看全部"
          >
            <span className="sw" style={{ background: d.color }} />第 {i + 1} 天 · {d.distance_km} 公里
          </button>
        ))}
        <span className="muted lg-note">距离是按顺序经过各点的直线距离累计，实际步行 / 通勤更长</span>
      </div>
    </div>
  );
}
