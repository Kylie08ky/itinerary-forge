import L from "leaflet";
import { toGcj, type AMapNS } from "../mapkit";
import type { RouteStop } from "../types";

/** 抽屉里的地图（规格 06 S31）：Leaflet（基础地图）和高德两套，对外是同一个接口。 */
export interface SheetMapEngine {
  /** 画这一天：编号圆点 + 虚线，缩放到刚好装下（避开顶部条和半屏抽屉）。 */
  draw(stops: RouteStop[], color: string, onPin: (n: number) => void): void;
  /** 选中第 n 个圆点（1 起，null = 都不选）：变大加圈。 */
  highlight(n: number | null, color: string): void;
  /** 平移到这一点；dy = 地图中心比「想让圆点落的位置」低多少 px。 */
  focus(stop: RouteStop, dy: number): void;
  /** 地图容器的高度（px）。 */
  height(): number;
  destroy(): void;
}

export interface FitPad {
  top: number;
  bottom: number;
  side: number;
}

const pinSize = (selected: boolean) => (selected ? 36 : 28);
// 显式给宽高：高德的标记容器不像 Leaflet 那样有固定尺寸，百分比会被压成椭圆
const pinHtml = (n: number, color: string, selected: boolean) =>
  `<div class="sheet-pin${selected ? " sel" : ""}" style="background:${color};width:${pinSize(selected)}px;height:${pinSize(selected)}px">${n}</div>`;

export function leafletEngine(el: HTMLElement, pad: FitPad): SheetMapEngine {
  const map = L.map(el, { zoomControl: false, attributionControl: true });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  let layer: L.LayerGroup | null = null;
  const markers = new Map<number, L.Marker>();
  const icon = (n: number, color: string, selected: boolean) => {
    const size = pinSize(selected);
    return L.divIcon({ className: "", html: pinHtml(n, color, selected), iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
  };
  return {
    draw(stops, color, onPin) {
      layer?.remove();
      layer = L.layerGroup().addTo(map);
      markers.clear();
      const pts: L.LatLngTuple[] = stops.map((s) => [s.lat, s.lng]);
      if (pts.length > 1) L.polyline(pts, { color, weight: 4, opacity: 0.85, dashArray: "1 9", lineCap: "round" }).addTo(layer);
      stops.forEach((s, k) => {
        const m = L.marker([s.lat, s.lng], { icon: icon(k + 1, color, false), keyboard: false }).addTo(layer!);
        m.on("click", () => onPin(k + 1));
        markers.set(k + 1, m);
      });
      if (pts.length) {
        map.fitBounds(L.latLngBounds(pts), {
          paddingTopLeft: [pad.side, pad.top],
          paddingBottomRight: [pad.side, pad.bottom],
          maxZoom: 16,
          animate: false,
        });
      } else {
        map.setView([48.8566, 2.3522], 12);
      }
    },
    highlight(n, color) {
      markers.forEach((m, k) => m.setIcon(icon(k, color, n === k)));
    },
    focus(stop, dy) {
      const zoom = Math.max(map.getZoom(), 15);
      const target = map.project([stop.lat, stop.lng], zoom).add([0, dy]);
      map.flyTo(map.unproject(target, zoom), zoom, { duration: 0.5 });
    },
    height: () => map.getSize().y,
    destroy() {
      map.remove();
    },
  };
}

export function amapEngine(AMap: AMapNS, el: HTMLElement, pad: FitPad): SheetMapEngine {
  const map = new AMap.Map(el, { zoom: 12, resizeEnable: true, viewMode: "2D", zooms: [3, 18] });
  const markers = new Map<number, AMapNS>();
  const content = (n: number, color: string, selected: boolean) => pinHtml(n, color, selected);
  const offset = (selected: boolean) => {
    const h = pinSize(selected) / 2;
    return new AMap.Pixel(-h, -h);
  };
  return {
    draw(stops, color, onPin) {
      map.clearMap();
      markers.clear();
      const overlays: AMapNS[] = [];
      if (stops.length > 1) {
        const line = new AMap.Polyline({
          path: stops.map((s) => toGcj(s.lat, s.lng)),
          strokeColor: color,
          strokeWeight: 4,
          strokeOpacity: 0.85,
          strokeStyle: "dashed",
          strokeDasharray: [1, 9],
          lineCap: "round",
        });
        map.add(line);
        overlays.push(line);
      }
      stops.forEach((s, k) => {
        const marker = new AMap.Marker({ position: toGcj(s.lat, s.lng), content: content(k + 1, color, false), offset: offset(false) });
        marker.on("click", () => onPin(k + 1));
        map.add(marker);
        overlays.push(marker);
        markers.set(k + 1, marker);
      });
      // avoid：[上, 下, 左, 右]
      if (overlays.length) map.setFitView(overlays, true, [pad.top, pad.bottom, pad.side, pad.side], 16);
    },
    highlight(n, color) {
      markers.forEach((m, k) => {
        const sel = n === k;
        m.setContent(content(k, color, sel));
        m.setOffset(offset(sel));
        m.setzIndex(sel ? 200 : 100);
      });
    },
    focus(stop, dy) {
      const pos = toGcj(stop.lat, stop.lng);
      const zoom = Math.max(map.getZoom(), 15);
      map.setZoomAndCenter(zoom, pos, true);
      // 先放到该缩放级别的中心，再把中心往下挪 dy，圆点就落在可见区域的中央
      const c = map.lngLatToContainer(pos);
      map.setCenter(map.containerToLngLat(new AMap.Pixel(c.x, c.y + dy)), false, 500);
    },
    height: () => el.clientHeight,
    destroy() {
      map.destroy();
    },
  };
}
