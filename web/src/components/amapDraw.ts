import { toGcj, type AMapNS } from "../mapkit";

export interface AmapPin {
  lat: number;
  lng: number;
  n: number;
  color: string;
  tip: () => HTMLElement;
  onClick?: () => void;
}
export interface AmapLine {
  color: string;
  weight: number;
  opacity: number;
  dashed?: boolean;
  points: [number, number][]; // [纬度, 经度]，WGS-84
}

const GAP = 14; // 气泡和标记中心的距离（和 Leaflet 版一致）

/**
 * 用高德 JS API 2.0 画编号标记 + 折线 + 悬停气泡（行为对齐 Leaflet 版）。
 * 坐标进来是 WGS-84，这里换成 GCJ-02 再画。返回清理函数。
 */
export function drawAmap(
  AMap: AMapNS,
  el: HTMLElement,
  pins: AmapPin[],
  lines: AmapLine[],
  opts: { maxZoom: number; padding: number; cluster?: boolean }, // cluster：缩小时标记聚合成带数字的圆圈（全程地图）
): () => void {
  const map = new AMap.Map(el, { zoom: 12, resizeEnable: true, scrollWheel: false, viewMode: "2D" });
  const tip = document.createElement("div");
  tip.className = "amap-tip";
  tip.style.display = "none";
  el.appendChild(tip);

  const hide = () => {
    tip.style.display = "none";
  };
  const place = (pos: [number, number]) => {
    const px = map.lngLatToContainer(pos);
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const W = el.clientWidth;
    const pad = 6;
    const above = px.y - GAP - h;
    const y = above >= pad ? above : px.y + GAP; // 上面放不下就放下面
    const x = Math.min(Math.max(px.x - w / 2, pad), Math.max(pad, W - w - pad));
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  };
  const show = (pin: AmapPin, pos: [number, number]) => {
    tip.replaceChildren(pin.tip());
    tip.style.display = "block";
    place(pos);
    tip.querySelectorAll("img").forEach((img) => img.addEventListener("load", () => place(pos), { once: true }));
  };

  const overlays: unknown[] = [];
  for (const l of lines) {
    if (l.points.length < 2) continue;
    const line = new AMap.Polyline({
      path: l.points.map(([lat, lng]) => toGcj(lat, lng)),
      strokeColor: l.color,
      strokeWeight: l.weight,
      strokeOpacity: l.opacity,
      strokeStyle: l.dashed ? "dashed" : "solid",
      strokeDasharray: l.dashed ? [1, 8] : undefined,
      lineCap: "round",
    });
    map.add(line);
    overlays.push(line);
  }
  const pinHtml = (pin: AmapPin) => `<div class="map-pin" style="background:${pin.color}">${pin.n}</div>`;
  const bind = (marker: AMapNS, pin: AmapPin) => {
    if (marker.__bound === pin) return; // 聚合会复用标记对象，别重复绑事件
    marker.__bound = pin;
    marker.off("mouseover");
    marker.off("mouseout");
    marker.off("click");
    marker.on("mouseover", () => show(pin, toGcj(pin.lat, pin.lng)));
    marker.on("mouseout", hide);
    if (pin.onClick) marker.on("click", pin.onClick);
  };
  const pad = opts.padding;
  if (opts.cluster) {
    const pts = pins.map((p) => toGcj(p.lat, p.lng)); // 自己留一份：插件会改写传给它的数据（lnglat 变成它自己的对象）
    const data = pts.map((lnglat, i) => ({ lnglat, i }));
    const dist = (p: [number, number], q: [number, number]) => (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2;
    // 点圆圈：放大到刚好装下这一簇的所有点（和 Leaflet 版一样）；点挨得太近放不大就至少放大两级
    const zoomInto = (group: [number, number][]) => {
      const lngs = group.map((q) => q[0]);
      const lats = group.map((q) => q[1]);
      const before = map.getZoom();
      hide();
      map.setBounds(new AMap.Bounds([Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]), true, [pad, pad, pad, pad]);
      if (map.getZoom() <= before) map.setZoom(before + 2);
    };
    new AMap.MarkerCluster(map, data, {
      gridSize: 40,
      maxZoom: 13, // 放大到 13 级及以上不再聚合（和 Leaflet 版一致）
      renderMarker: (ctx: { data: { i: number }[]; marker: AMapNS }) => {
        const pin = pins[ctx.data[0].i];
        ctx.marker.setContent(pinHtml(pin));
        ctx.marker.setOffset(new AMap.Pixel(-13, -13));
        bind(ctx.marker, pin);
      },
      renderClusterMarker: (ctx: { count: number; marker: AMapNS; clusterData?: { i: number }[] }) => {
        // 圆圈是自己的 DOM：点击直接绑在上面（高德的聚合对象不给圆圈的点击事件）
        const el = document.createElement("div");
        el.className = "map-cluster";
        el.title = "点一下放大";
        el.textContent = String(ctx.count);
        // 插件只告诉我们这一簇里的一两个点和总数：以它们为锚，取离得最近的 count 个点当作这一簇
        const anchorIdx = ctx.clusterData?.[0]?.i;
        const group =
          anchorIdx === undefined
            ? []
            : [...pts].sort((p, q) => dist(p, pts[anchorIdx]) - dist(q, pts[anchorIdx])).slice(0, ctx.count);
        el.addEventListener("click", (ev) => {
          ev.stopPropagation();
          if (group.length) zoomInto(group);
        });
        ctx.marker.setContent(el);
        ctx.marker.setOffset(new AMap.Pixel(-15, -15));
      },
    });
    if (pts.length) {
      const lngs = pts.map((q) => q[0]);
      const lats = pts.map((q) => q[1]);
      const bounds = new AMap.Bounds([Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]);
      map.setBounds(bounds, true, [pad, pad, pad, pad]);
      if (map.getZoom() > opts.maxZoom) map.setZoom(opts.maxZoom);
    }
  } else {
    for (const pin of pins) {
      const pos = toGcj(pin.lat, pin.lng);
      const marker = new AMap.Marker({ position: pos, content: pinHtml(pin), offset: new AMap.Pixel(-13, -13) });
      bind(marker, pin);
      map.add(marker);
      overlays.push(marker);
    }
    if (overlays.length) map.setFitView(overlays, true, [pad, pad, pad, pad], opts.maxZoom);
  }

  return () => {
    tip.remove();
    map.destroy();
  };
}
