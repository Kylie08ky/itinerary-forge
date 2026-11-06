import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { IconChevron } from "../icons";
import { FALLBACK_NOTE, inChina, loadAmap, type MapFallback } from "../mapkit";
import type { Itinerary, ItineraryItem, RouteData, RouteStop } from "../types";
import { amapEngine, leafletEngine, type SheetMapEngine } from "./sheetMap";

/** 半屏时抽屉露出的高度（px）；展开时顶部停在距屏幕上沿 TOP_GAP 的位置。 */
export const PEEK_PX = 360;
export const TOP_GAP = 132;
const HEAD_PX = 108; // 抽屉顶部（把手 + 标题行）的高度
const DRAG_STEP = 40; // 位移超过这么多才换停靠位

type Snap = "peek" | "full";

interface Entry {
  item: ItineraryItem;
  stop: RouteStop | null; // 有坐标的地点对应的路线点
  n: number | null; // 地图上的序号（1 起）
}

function clean(title: string): string {
  return title.replace(/[（(].*/, "").trim();
}

/** 把当天的每一项和路线里带坐标的点对上：先按地点编号，再按名字。没对上的（午餐、住宿）不上图。 */
export function entriesOf(items: ItineraryItem[], stops: RouteStop[]): Entry[] {
  const used = new Set<number>();
  return items.map((item) => {
    let k = item.place_id ? stops.findIndex((s, i) => !used.has(i) && s.place_id === item.place_id) : -1;
    if (k < 0) {
      const c = clean(item.title);
      if (c) k = stops.findIndex((s, i) => !used.has(i) && (s.name === c || s.name.includes(c) || c.includes(s.name)));
    }
    if (k < 0) return { item, stop: null, n: null };
    used.add(k);
    return { item, stop: stops[k], n: k + 1 };
  });
}

function km(a: RouteStop, b: RouteStop): number {
  const r = (d: number) => (d * Math.PI) / 180;
  const h =
    Math.sin(r(b.lat - a.lat) / 2) ** 2 + Math.cos(r(a.lat)) * Math.cos(r(b.lat)) * Math.sin(r(b.lng - a.lng) / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

const md = (d: string) => `${Number(d.slice(5, 7))}.${Number(d.slice(8, 10))}`;

/**
 * 手机行程页的「地图模式」（规格 06 S31）：全屏地图 + 顶部按天切换 + 可拖动的底部抽屉（半屏 / 展开两个停靠位）。
 * 抽屉里是当天的行程时间轴；点一项（或地图上的圆点）选中，地图平移到它并避开抽屉。
 */
export function MapSheet({
  it,
  route,
  startDay,
  onClose,
  onOpenPlace,
}: {
  it: Itinerary;
  route: RouteData;
  startDay: number;
  onClose: () => void;
  onOpenPlace: (placeId: string) => void;
}) {
  const [day, setDay] = useState(() => Math.min(Math.max(startDay, 0), Math.max(route.days.length - 1, 0)));
  const [snap, setSnap] = useState<Snap>("peek");
  const [sel, setSel] = useState<number | null>(null); // 当天列表里选中的第几项
  const [drag, setDrag] = useState<number | null>(null); // 拖动中的位移（px），null = 没在拖
  const [panelH, setPanelH] = useState(0);

  const mapEl = useRef<HTMLDivElement>(null);
  const engine = useRef<SheetMapEngine | null>(null);
  const [mapReady, setMapReady] = useState(0); // 地图引擎建好（或换了）就 +1，触发重画
  const [fallback, setFallback] = useState<MapFallback | null>(null); // 境内的路线本该用高德却退回了基础地图
  const [kind, setKind] = useState<"amap" | "osm" | null>(null);
  const panel = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const gesture = useRef<{ y: number; off: number; moved: boolean } | null>(null);
  const snapRef = useRef<Snap>(snap);
  snapRef.current = snap;

  const d = it.days[day];
  const rd = route.days[day];
  const entries = useMemo(() => (d && rd ? entriesOf(d.items, rd.stops) : []), [d, rd]);
  const color = rd?.color ?? "#3b6ea5";
  const booking = entries.filter((e) => e.item.booking_required).length;

  // ---- 抽屉的位置 ----
  useLayoutEffect(() => {
    const measure = () => setPanelH(panel.current?.offsetHeight ?? 0);
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);
  const peekOff = Math.max(0, panelH - PEEK_PX);
  const baseOff = snap === "full" ? 0 : peekOff;
  const offset = drag ?? baseOff;

  function onDown(e: React.PointerEvent<HTMLDivElement>) {
    e.currentTarget.setPointerCapture(e.pointerId);
    gesture.current = { y: e.clientY, off: baseOff, moved: false };
    setDrag(baseOff);
  }
  function onMove(e: React.PointerEvent<HTMLDivElement>) {
    const g = gesture.current;
    if (!g) return;
    const dy = e.clientY - g.y;
    if (Math.abs(dy) > 4) g.moved = true;
    setDrag(Math.min(Math.max(g.off + dy, 0), peekOff));
  }
  function onUp(e: React.PointerEvent<HTMLDivElement>) {
    const g = gesture.current;
    gesture.current = null;
    setDrag(null);
    if (!g) return;
    const dy = e.clientY - g.y;
    if (!g.moved) {
      toggle(); // 点一下：指针被捕获后 click 不会落到按钮上，所以在松手时直接切换
      return;
    }
    if (dy <= -DRAG_STEP) setSnap("full");
    else if (dy >= DRAG_STEP) setSnap("peek");
  }
  function toggle() {
    setSnap((s) => (s === "peek" ? "full" : "peek"));
  }

  // ---- 地图：整条路线都在大陆 → 高德；否则 / 高德用不了 → 基础地图（OpenStreetMap） ----
  // 用「是不是全在大陆」这个布尔值做依赖：route 对象每次渲染可能是新的，不能拿它当依赖（否则地图会反复重建）
  const mainland = useMemo(() => {
    const all = route.days.flatMap((x) => x.stops);
    return all.length > 0 && all.every((s) => inChina(s.lat, s.lng));
  }, [route]);
  useEffect(() => {
    const el = mapEl.current;
    if (!el) return;
    let cancelled = false;
    const pad = { top: 120, bottom: PEEK_PX + 24, side: 28 };
    const use = (e: SheetMapEngine, k: "amap" | "osm") => {
      engine.current = e;
      setKind(k);
      setMapReady((n) => n + 1);
    };
    if (mainland) {
      loadAmap().then((r) => {
        if (cancelled) return;
        if ("AMap" in r) {
          setFallback(null);
          use(amapEngine(r.AMap, el, pad), "amap");
        } else {
          setFallback(r.fallback);
          use(leafletEngine(el, pad), "osm");
        }
      });
    } else {
      use(leafletEngine(el, pad), "osm");
    }
    return () => {
      cancelled = true;
      engine.current?.destroy();
      engine.current = null;
    };
  }, [mainland]);

  // 切换天：重画圆点和虚线，缩放到这一天（依赖这一天的点串，而不是 rd 对象：重渲染时不重画、不丢用户拖动过的视野）
  const stopsKey = rd ? rd.stops.map((s) => `${s.place_id}@${s.lat},${s.lng}`).join("|") : "";
  useEffect(() => {
    const e = engine.current;
    if (!e || !rd) return;
    e.draw(rd.stops, rd.color, (n) => {
      const at = entries.findIndex((x) => x.n === n);
      if (at >= 0) select(at, false);
    });
    e.highlight(sel !== null ? (entries[sel]?.n ?? null) : null, rd.color);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [day, stopsKey, mapReady]);

  // 选中的圆点变大加圈
  useEffect(() => {
    engine.current?.highlight(sel !== null ? (entries[sel]?.n ?? null) : null, color);
  }, [sel, color, entries, day, mapReady]);

  function select(at: number, fromList: boolean) {
    setSel(at);
    const e = entries[at];
    const map = engine.current;
    if (e?.stop && map) {
      // 让圆点落在「顶部条」和「抽屉」之间可见地图的中央
      const vh = map.height();
      const visibleMid = (TOP_GAP - 12 + (snapRef.current === "peek" ? vh - PEEK_PX : TOP_GAP)) / 2;
      map.focus(e.stop, vh / 2 - visibleMid);
    }
    if (!fromList) {
      requestAnimationFrame(() =>
        body.current?.querySelector<HTMLElement>(`[data-i="${at}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" }),
      );
    }
  }

  function pickDay(i: number) {
    setDay(i);
    setSel(null);
    body.current?.scrollTo({ top: 0 });
  }

  // Esc 关闭；打开期间锁住底下页面的滚动
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  if (!d || !rd) return null;
  return (
    <div className="map-sheet" role="dialog" aria-modal="true" aria-label="地图与行程" data-testid="map-sheet">
      <div className="sheet-map" ref={mapEl} data-testid="sheet-map" data-engine={kind ?? undefined} />

      <header className="sheet-top">
        <button type="button" className="sheet-back" aria-label="返回" data-testid="map-sheet-close" onClick={onClose}>
          <span style={{ display: "inline-flex", transform: "rotate(180deg)" }}>
            <IconChevron size={22} />
          </span>
        </button>
        <div className="sheet-days" role="tablist" aria-label="按天切换">
          {route.days.map((x, i) => (
            <button
              key={x.date}
              type="button"
              role="tab"
              aria-selected={i === day}
              data-testid="sheet-day"
              className={i === day ? "on" : undefined}
              style={i === day ? { background: x.color, borderColor: x.color } : undefined}
              onClick={() => pickDay(i)}
            >
              第 {i + 1} 天
            </button>
          ))}
        </div>
      </header>
      {fallback && (
        <p className="sheet-note" data-testid="sheet-map-note">
          {FALLBACK_NOTE[fallback]}
        </p>
      )}

      <div
        className={`sheet-panel${drag !== null || panelH === 0 ? " dragging" : ""}`}
        ref={panel}
        data-testid="sheet-panel"
        data-snap={snap}
        style={{ transform: `translateY(${offset}px)` }}
      >
        <div className="sheet-handle" onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp}>
          <button
            type="button"
            className="sheet-grip"
            data-testid="sheet-grip"
            aria-label={snap === "peek" ? "展开行程" : "收起行程"}
            aria-expanded={snap === "full"}
            onClick={(e) => {
              if (e.detail === 0) toggle(); // 键盘（回车 / 空格）；鼠标和手指走 onUp
            }}
          >
            <span aria-hidden />
          </button>
          <div className="sheet-title">
            <h2>
              第 {day + 1} 天 · {md(d.date)}
            </h2>
            <span>{entries.length} 站</span>
          </div>
        </div>

        <div className="sheet-body" ref={body} style={snap === "peek" ? { maxHeight: PEEK_PX - HEAD_PX } : undefined}>
          {booking > 0 && (
            <div className="sheet-warn" data-testid="sheet-warn">
              {booking} 个地点需提前订票
            </div>
          )}
          <ol className="sheet-list">
            {entries.map((e, i) => {
              const prev = i > 0 ? entries[i - 1] : null;
              const leg =
                e.item.transport ??
                (prev?.stop && e.stop ? `直线约 ${km(prev.stop, e.stop).toFixed(1)} 公里` : null);
              const open = sel === i;
              return (
                <li key={`${e.item.time}-${i}`} data-i={i}>
                  {leg && i > 0 && <div className="sheet-leg">{leg}</div>}
                  <div className={`sheet-stop${open ? " open" : ""}`} data-testid="sheet-stop" data-selected={open}>
                    <button type="button" className="sheet-stop-main" aria-pressed={open} onClick={() => select(i, true)}>
                      {e.n !== null ? (
                        <span className="sheet-no" style={{ background: color }}>
                          {e.n}
                        </span>
                      ) : (
                        <span className="sheet-dot" aria-hidden />
                      )}
                      <span className="sheet-stop-text">
                        <span className="sheet-stop-row">
                          <strong>{e.item.title}</strong>
                          <time>{e.item.time}</time>
                        </span>
                        {(e.stop?.name_local || e.item.location) && (
                          <span className="sheet-sub">{e.stop?.name_local ?? e.item.location}</span>
                        )}
                        {e.item.booking_required && <span className="sheet-tag">须订票</span>}
                      </span>
                    </button>
                    {open && e.stop && (
                      <button type="button" className="sheet-detail" onClick={() => onOpenPlace(e.stop!.place_id)}>
                        详情 ›
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      </div>
    </div>
  );
}
