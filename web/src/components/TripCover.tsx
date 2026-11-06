import { useEffect, useRef, useState } from "react";
import { mediaSrc } from "../api";
import { FitTitle } from "./FitTitle";
import { ticketTotal } from "../money";
import { HeroCredit, type HeroPick, useHeroStatus } from "../hero";
import { coverStyle } from "../themes";
import type { AttractionRef, DayTripInfo, Itinerary, NoteSource, PlaceImageInfo, RouteData } from "../types";

/** 封面舞台的滚动长度（规格 06 S16）：封面在页面顶部「钉住」这么多像素，期间播放动画。 */
export const COVER_SCROLL = 1200;
const STORY_MIN_WIDTH = 700;
const STORY_MIN_HEIGHT = 616; // 钉住的舞台最矮 560px + 顶栏 56px；窗口再矮，卡片会掉到屏幕下面、进场动画也走不完，改用静态版
const DRAG_START = 6; // 拖动超过这么多像素才算拖（之内是点击）

export interface Highlight {
  placeId: string;
  day: number;
  name: string;
  area: string;
  blurb: string;
  thumb: PlaceImageInfo | null;
  fromNote: boolean;
  city?: string; // 多城市行程：这个点在哪座城市
}

const CITY_ZH: Record<string, string> = { paris: "巴黎" };
const NOT_PLACES = new Set(["meal", "logistics", "hotel", "transit"]);

/** 行程亮点（规格 06 S16）：每天一个主要点，最多 5 张；有「来自笔记」的点优先。 */
export function buildHighlights(
  it: Itinerary,
  places: AttractionRef[],
  notes: Record<string, NoteSource>,
  cityNames?: Record<string, string>, // 多城市行程：城市 id → 中文名；每座城市都要有亮点，不能全是第一座的
): Highlight[] {
  const byId = new Map(places.map((p) => [p.place_id as string, p]));
  const seen = new Set<string>();
  const picks: { placeId: string; day: number; city?: string }[] = [];
  // 只有一天（周边一日游）：每天一张就只有一张卡，改成这一天的前几个地点；深度路线：路线上排进来的站都放出来（可左右划）
  const routeMode = !!it.generation?.route;
  const oneDay = it.days.length === 1 || routeMode;
  it.days.forEach((d, i) => {
    for (const item of d.items) {
      if (item.place_id && !NOT_PLACES.has(item.type) && !seen.has(item.place_id)) {
        seen.add(item.place_id);
        picks.push({ placeId: item.place_id, day: i + 1, city: item.city_id ?? d.city_id ?? undefined });
        if (!oneDay) break;
      }
    }
  });
  // 来自笔记的点：不在上面的也补进来，并排在最前面
  for (const [i, d] of it.days.entries()) {
    for (const item of d.items) {
      if (item.place_id && notes[item.place_id] && !seen.has(item.place_id)) {
        seen.add(item.place_id);
        picks.push({ placeId: item.place_id, day: i + 1 });
      }
    }
  }
  picks.sort((a, b) => Number(!!notes[b.placeId]) - Number(!!notes[a.placeId]) || a.day - b.day);
  const cityIds = cityNames ? Object.keys(cityNames) : [];
  let chosen = picks.slice(0, routeMode ? 16 : 5);
  if (cityIds.length > 1 && !routeMode) {
    // 每座城市分到差不多的名额（总共最多 6 张），再按天数排回去
    const each = Math.max(2, Math.floor(6 / cityIds.length));
    const byCity = cityIds.flatMap((c) => picks.filter((p) => p.city === c).slice(0, each));
    chosen = [...byCity].sort((a, b) => a.day - b.day);
  }
  return chosen.map(({ placeId, day, city }) => {
    const p = byId.get(placeId);
    const intro = p?.media?.intro?.text.split(/[。！？]/)[0] ?? "";
    const blurb = p?.highlights?.slice(0, 2).join("、") || (intro.length > 0 && intro.length <= 36 ? intro : p?.note?.slice(0, 36)) || "";
    return {
      placeId,
      day,
      name: p?.name_zh ?? placeId,
      area: p?.area ?? "",
      blurb,
      thumb: p?.media?.image ?? null,
      fromNote: !!notes[placeId],
      city: city && cityNames ? cityNames[city] : undefined,
    };
  });
}

// --------------------------------------------------------------------------- #
const clamp = (v: number, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const smoothstep = (e0: number, e1: number, v: number) => {
  const x = clamp((v - e0) / (e1 - e0));
  return x * x * (3 - 2 * x);
};
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

function useReducedMotion(): boolean {
  const [on, setOn] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const h = () => setOn(mq.matches);
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, []);
  return on;
}

function useWide(): boolean {
  const query = `(min-width: ${STORY_MIN_WIDTH}px) and (min-height: ${STORY_MIN_HEIGHT}px)`;
  const [wide, setWide] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const h = () => setWide(mq.matches);
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, []);
  return wide;
}

/**
 * 封面滚动动画（规格 06 S16）：所有值只由滚动位置算出（带一点惯性），写成这个元素上的 CSS 变量，样式里用变量。
 * 节拍（px，相对封面顶部）：0–260 标题上浮淡出；0–700 照片拉近 + 模糊 + 压暗 + 城市色罩；360–760 统计面板；
 * 700–1000 行程亮点卡片从右侧飞入；900–1050 左右按钮出现；1000–1200 内容轻轻上移，接上正常页面。
 */
function useCoverProgress(wrap: React.RefObject<HTMLDivElement>, enabled: boolean, reduced: boolean) {
  useEffect(() => {
    const el = wrap.current;
    if (!el || !enabled) return;
    let smooth = 0;
    let started = false;
    let mx = 0,
      my = 0,
      tmx = 0,
      tmy = 0;
    let pending = false;

    const set = (k: string, v: string | number) => el.style.setProperty(k, String(v));
    const tick = () => {
      pending = false;
      const stage = el.firstElementChild as HTMLElement | null;
      const range = Math.max(1, el.offsetHeight - (stage?.offsetHeight ?? 0));
      const navH = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--nav-h")) || 0;
      const target = clamp(navH - el.getBoundingClientRect().top, 0, range);
      smooth = !started || reduced ? target : lerp(smooth, target, 0.14);
      started = true;
      if (Math.abs(smooth - target) < 0.08) smooth = target;
      mx = lerp(mx, tmx, 0.12);
      my = lerp(my, tmy, 0.12);

      const s = smooth;
      const out = smoothstep(0, 260, s);
      const p = smoothstep(0, 700, s);
      const stats = smoothstep(360, 760, s);
      const cards = Math.pow(smoothstep(700, 1000, s), 1.55);
      const ctl = smoothstep(900, 1050, s);
      const exit = smoothstep(1000, 1200, s);
      set("--title-y", `${(-120 * out).toFixed(2)}px`);
      set("--title-o", (1 - out).toFixed(4));
      set("--photo-scale", (1 + 0.18 * p).toFixed(4));
      set("--photo-blur", `${(12 * p).toFixed(2)}px`);
      set("--photo-bright", (1 - 0.22 * p).toFixed(4));
      set("--tint-a", (0.45 * p).toFixed(4));
      set("--stats-o", stats.toFixed(4));
      set("--stats-y", `${((1 - stats) * 40).toFixed(2)}px`);
      set("--cards-x", `${((1 - cards) * 110).toFixed(3)}vw`);
      set("--cards-vis", cards > 0.01 ? "visible" : "hidden");
      set("--ctl-o", ctl.toFixed(4));
      set("--inner-y", `${(-60 * exit).toFixed(2)}px`);
      set("--mx", `${(mx * -12).toFixed(2)}px`);
      set("--my", `${(my * -6).toFixed(2)}px`);
      el.classList.toggle("ctl-ready", ctl > 0.98);
      if (Math.abs(smooth - target) > 0.08 || Math.abs(mx - tmx) > 0.001 || Math.abs(my - tmy) > 0.001) request();
    };
    const request = () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(tick);
    };
    const onMove = (e: PointerEvent) => {
      if (reduced) return;
      tmx = e.clientX / window.innerWidth - 0.5;
      tmy = e.clientY / window.innerHeight - 0.5;
      request();
    };
    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", request);
    window.addEventListener("pointermove", onMove, { passive: true });
    request();
    return () => {
      window.removeEventListener("scroll", request);
      window.removeEventListener("resize", request);
      window.removeEventListener("pointermove", onMove);
    };
  }, [wrap, enabled, reduced]);
}

function HighlightCard({ h, onOpen }: { h: Highlight; onOpen: () => void }) {
  return (
    <article
      className="cover-card"
      tabIndex={0}
      role="button"
      aria-label={`查看${h.name}的详情`}
      data-testid="cover-card"
      data-pid={h.placeId}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      <span className="cover-card-kicker">
        第 {h.day} 天{h.city ? ` · ${h.city}` : ""}{h.area ? ` · ${h.area}` : ""}
        {h.fromNote && <span className="cover-card-note"> · 来自笔记</span>}
      </span>
      {h.thumb && <img className="cover-card-thumb" src={mediaSrc(h.thumb.url)} alt="" loading="lazy" crossOrigin="anonymous" onError={(e) => ((e.currentTarget as HTMLElement).hidden = true)} />}
      <h3>{h.name}</h3>
      {h.blurb && <p>{h.blurb}</p>}
    </article>
  );
}

/**
 * 横向滚动的一排卡片：就是一个原生的横向滚动容器，所以手指左右划、触控板双指左右划、Shift + 滚轮都直接能用；
 * 鼠标按住左右拖也行（拖动不算点击）。不循环、不复制：每张卡只出现一次。
 */
function useDragScroll() {
  const ref = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x0: number; left0: number; moved: boolean } | null>(null);
  const justDragged = useRef(false);
  const stop = () => {
    drag.current = null;
    ref.current?.classList.remove("dragging");
  };
  const handlers = {
    onPointerDown(e: React.PointerEvent) {
      if (e.pointerType === "mouse" && e.button === 0 && ref.current) {
        drag.current = { x0: e.clientX, left0: ref.current.scrollLeft, moved: false };
      }
    },
    onPointerMove(e: React.PointerEvent) {
      const d = drag.current;
      if (!d || !ref.current) return;
      const dx = e.clientX - d.x0;
      if (!d.moved) {
        if (Math.abs(dx) < DRAG_START) return;
        d.moved = true;
        ref.current.classList.add("dragging");
        e.currentTarget.setPointerCapture(e.pointerId); // 过了门槛才抓住指针，单纯点一下仍然是点击
      }
      ref.current.scrollLeft = d.left0 - dx;
    },
    onPointerUp() {
      if (drag.current?.moved) {
        justDragged.current = true;
        setTimeout(() => (justDragged.current = false), 0);
      }
      stop();
    },
    onPointerCancel: stop,
    onClickCapture(e: React.MouseEvent) {
      if (justDragged.current) {
        e.stopPropagation();
        e.preventDefault();
      }
    },
  };
  return { ref, handlers };
}

/**
 * 在 host 上左右划都能带动横向卡片行 track（不只是手指刚好按在卡片上）：
 * - 触控板双指左右划总带一点上下分量，浏览器会把整个手势交给页面纵向滚动，所以主方向偏横就由我们接手；
 * - 触屏：Chrome 把电脑触屏上的双指当成「捏合」手势，只会滚根页面，横向的双指划根本到不了卡片行
 *   （Claude 自带浏览器没有这个行为，所以那边能划）。所以 host 上设了 touch-action: pan-y（见 theme.css）：
 *   上下划仍由浏览器滚页面，左右划浏览器不接管，用指针事件自己带动卡片——单指、双指、落在哪里都一样，松手后带一点惯性。
 * shown 为 false 时（卡片还没显示出来）什么都不做。返回清理函数。
 */
function attachTouchDrag(track: HTMLElement, host: HTMLElement, shown: () => boolean): () => void {
  let wheelIdle = 0;
  const canMove = (d: number) => (d < 0 ? track.scrollLeft > 1 : track.scrollLeft + track.clientWidth < track.scrollWidth - 1);
  const onWheel = (e: WheelEvent) => {
    if (!shown()) return;
    const dx = e.shiftKey ? e.deltaY : e.deltaX;
    const horizontalIntent = e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY) * 0.6;
    if (!horizontalIntent || dx === 0 || !canMove(dx)) return;
    e.preventDefault();
    // 触控板一划是几十个几像素的小滚轮：吸附开着，每个小增量都会被拉回原位，累计为 0——划的过程中先关掉吸附，停下 150ms 再恢复
    track.classList.add("dragging");
    clearTimeout(wheelIdle);
    wheelIdle = window.setTimeout(() => track.classList.remove("dragging"), 150);
    track.scrollLeft += dx;
  };

  const fingers = new Map<number, { x: number; y: number }>();
  let g: { x: number; y: number; left: number; dir: "" | "h" | "v"; lastX: number; lastT: number; v: number } | null = null;
  let momentum = 0;
  const centre = () => {
    const pts = [...fingers.values()];
    return { x: pts.reduce((a, q) => a + q.x, 0) / pts.length, y: pts.reduce((a, q) => a + q.y, 0) / pts.length };
  };
  const stopMomentum = () => {
    if (momentum) cancelAnimationFrame(momentum);
    momentum = 0;
  };
  const swallowNextClick = () => {
    const swallow = (e: Event) => {
      e.stopPropagation();
      e.preventDefault();
    };
    host.addEventListener("click", swallow, { capture: true, once: true });
    setTimeout(() => host.removeEventListener("click", swallow, true), 350);
  };
  const begin = (e: PointerEvent) => {
    const c = centre();
    g = { x: c.x, y: c.y, left: track.scrollLeft, dir: "", lastX: c.x, lastT: e.timeStamp, v: 0 };
  };
  const onDown = (e: PointerEvent) => {
    if (e.pointerType !== "touch" || !shown()) return;
    stopMomentum();
    fingers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    begin(e);                                              // 多一根手指落下：以两指中心为新的起点
  };
  const onMove = (e: PointerEvent) => {
    if (!fingers.has(e.pointerId) || !g) return;
    fingers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const c = centre();
    const dx = c.x - g.x;
    const dy = c.y - g.y;
    if (!g.dir) {
      if (Math.hypot(dx, dy) < 8) return;
      g.dir = Math.abs(dx) > Math.abs(dy) * 1.2 ? "h" : "v";
      if (g.dir === "h") track.classList.add("dragging");     // 拖的时候关掉吸附
    }
    if (g.dir !== "h") return;
    track.scrollLeft = g.left - dx;
    const dt = e.timeStamp - g.lastT;
    if (dt > 0) g.v = 0.8 * g.v + 0.2 * ((c.x - g.lastX) / dt);
    g.lastX = c.x;
    g.lastT = e.timeStamp;
  };
  const finish = (e: PointerEvent) => {
    if (!fingers.delete(e.pointerId)) return;
    const was = g;
    if (fingers.size > 0) {
      begin(e);                                              // 还剩一根手指：接着拖
      return;
    }
    g = null;
    if (!was || was.dir !== "h") {
      track.classList.remove("dragging");
      return;
    }
    swallowNextClick();                                      // 拖完不算点击，不会误开地点详情
    let vel = -was.v * 16;                                   // 每帧滚多少像素（松手速度，px/ms × 一帧约 16ms）
    const coast = () => {
      track.scrollLeft += vel;
      vel *= 0.94;
      if (Math.abs(vel) > 0.5 && canMove(vel)) momentum = requestAnimationFrame(coast);
      else {
        momentum = 0;
        track.classList.remove("dragging");
      }
    };
    if (Math.abs(vel) > 2) momentum = requestAnimationFrame(coast);
    else track.classList.remove("dragging");
  };
  host.addEventListener("wheel", onWheel, { passive: false });
  host.addEventListener("pointerdown", onDown);
  host.addEventListener("pointermove", onMove);
  host.addEventListener("pointerup", finish);
  host.addEventListener("pointercancel", finish);
  return () => {
    stopMomentum();
    clearTimeout(wheelIdle);
    host.removeEventListener("wheel", onWheel);
    host.removeEventListener("pointerdown", onDown);
    host.removeEventListener("pointermove", onMove);
    host.removeEventListener("pointerup", finish);
    host.removeEventListener("pointercancel", finish);
  };
}

/** 封面舞台上的亮点卡片：横向滚动的一排 + ← → 按钮（滚到头就变灰）。 */
function CardTrack({ cards, onOpen }: { cards: Highlight[]; onOpen: (pid: string) => void }) {
  const { ref, handlers } = useDragScroll();
  const [edge, setEdge] = useState({ start: true, end: false });
  const n = cards.length;

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setEdge({ start: el.scrollLeft <= 2, end: el.scrollLeft + el.clientWidth >= el.scrollWidth - 2 });
    measure();
    el.addEventListener("scroll", measure, { passive: true });
    window.addEventListener("resize", measure);
    return () => {
      el.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
    };
  }, [ref, n]);

  useEffect(() => {
    const track = ref.current;
    const stage = track?.closest<HTMLElement>(".cover-stage");
    const shell = track?.closest<HTMLElement>(".cover-cards");
    if (!track || !stage || !shell) return;
    return attachTouchDrag(track, stage, () => getComputedStyle(shell).visibility === "visible");
  }, [ref, n]);

  function step(d: number) {
    const el = ref.current;
    const first = el?.firstElementChild as HTMLElement | null;
    if (!el || !first) return;
    const gap = parseFloat(getComputedStyle(el).columnGap || "0") || 0;
    el.scrollBy({ left: d * (first.offsetWidth + gap), behavior: "smooth" });
  }

  if (n === 0) return null;
  return (
    <>
      <div className="cover-cards-viewport">
        <div
          ref={ref}
          className="cover-track"
          data-testid="cover-track"
          style={{ "--cards": Math.max(n, 3) } as React.CSSProperties}
          {...handlers}
        >
          {cards.map((h) => (
            <HighlightCard key={h.placeId} h={h} onOpen={() => onOpen(h.placeId)} />
          ))}
        </div>
      </div>
      {n > 1 && !(edge.start && edge.end) && (
        <div className="cover-controls" aria-label="行程亮点切换">
          <button type="button" className="cover-nav" data-testid="cover-prev" aria-label="上一个亮点" disabled={edge.start} onClick={() => step(-1)}>
            ←
          </button>
          <button type="button" className="cover-nav" data-testid="cover-next" aria-label="下一个亮点" disabled={edge.end} onClick={() => step(1)}>
            →
          </button>
        </div>
      )}
    </>
  );
}

/** 静态版（手机 / 减少动态效果 / 没有大图）的卡片横排：同样是原生横向滚动 + 鼠标可拖。 */
function CardStrip({ highlights, onOpen }: { highlights: Highlight[]; onOpen: (pid: string) => void }) {
  const { ref, handlers } = useDragScroll();
  useEffect(() => {
    const row = ref.current;
    return row ? attachTouchDrag(row, row, () => true) : undefined;
  }, [ref]);
  return (
    <div ref={ref} className="cover-strip-row" data-testid="cover-strip-row" {...handlers}>
      {highlights.map((h) => (
        <HighlightCard key={h.placeId} h={h} onOpen={() => onOpen(h.placeId)} />
      ))}
    </div>
  );
}

const SLIDE_MS = 5000;

/** 多城市封面：只留加载成功的图，每隔几秒换下一张（减少动态效果时不轮播）。 */
function useRotatingHeroes(heroes: HeroPick[]): { picks: HeroPick[]; at: number; loading: boolean } {
  const key = heroes.map((h) => h.image.url).join("|");
  const [ok, setOk] = useState<Set<string>>(new Set());
  const [done, setDone] = useState(heroes.length < 2);
  const [at, setAt] = useState(0);
  useEffect(() => {
    if (heroes.length < 2) {
      setDone(true);
      return;
    }
    setDone(false);
    let live = true;
    let left = heroes.length;
    for (const h of heroes) {
      const img = new Image();
      const end = () => {
        left -= 1;
        if (live && left === 0) setDone(true);
      };
      img.onload = () => {
        if (live) setOk((prev) => new Set(prev).add(h.image.url));
        end();
      };
      img.onerror = end;
      img.src = mediaSrc(h.image.url);
    }
    return () => {
      live = false;
    };
    // heroes 的内容由 key 代表
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const picks = heroes.length < 2 ? [] : heroes.filter((h) => ok.has(h.image.url));
  const count = picks.length;
  useEffect(() => {
    if (count < 2 || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const t = setInterval(() => setAt((i) => (i + 1) % count), SLIDE_MS);
    return () => clearInterval(t);
  }, [count]);
  return { picks: count >= 2 ? picks : [], at, loading: !done };
}

/**
 * 行程页封面（规格 06 S15–S20，修订 3：Mostar 结构，紧凑版）。
 * 桌面：全屏舞台钉在页面顶部，滚动约 1200px 播放动画后接上正常内容；
 * 手机 / 「减少动态效果」/ 没有大图：静态一屏（不钉住、无滚动动画），行程亮点变成一排可横滑的卡片。
 */
export function TripCover({
  it,
  hero,
  heroes = [],
  highlights,
  route,
  loading = false,
  dayTrip = null,
  onOpenPlace,
}: {
  it: Itinerary;
  heroes?: HeroPick[]; // 多城市行程：每座城市一张，封面轮流切换（不够两张时用 hero）
  dayTrip?: DayTripInfo | null; // 周边一日游：标题写「海德堡 Heidelberg」，只写一个日期，不写「几晚」
  hero: HeroPick | null;
  highlights: Highlight[];
  route: RouteData | null;
  loading?: boolean; // 地点库还没读完：先按故事版排好（渐变），读完有大图再换上，免得先矮后高地跳一下
  onOpenPlace: (pid: string) => void;
}) {
  const slides = useRotatingHeroes(heroes);
  const single = useHeroStatus(slides.picks.length > 0 ? null : hero?.image);
  const status = slides.picks.length > 0 ? "ready" : slides.loading ? "loading" : single;
  const ready = status === "ready";
  const current: HeroPick | null = slides.picks.length > 0 ? slides.picks[slides.at % slides.picks.length] : hero;
  const reduced = useReducedMotion();
  const wide = useWide();
  const story = (status === "ready" || status === "loading" || (loading && status === "none")) && wide && !reduced;
  const wrap = useRef<HTMLDivElement>(null);
  useCoverProgress(wrap, story, reduced);

  const theme = it.generation?.theme_id ?? null;
  const city = dayTrip?.city ?? CITY_ZH[it.trip.destination.trim().toLowerCase()] ?? it.trip.destination;
  const themeName = it.generation?.theme_name ?? it.trip.traveler_profile.interests.join(" + ");
  const nights = Math.max(it.days.length - 1, 0);
  const [from, to] = [it.trip.date_range.start, it.trip.date_range.end].map((d) => d.slice(5).replace("-", "."));

  const stats = [
    ...(dayTrip ? [] : [{ n: it.days.length, label: "天" }]),
    { n: route ? route.place_count : "—", label: "个地点" },
    // 一日游的票价没核对；现建的城市也没有票价：写「待补」，不写 €0
    ...(dayTrip ? [] : [{ n: route ? ticketTotal(route.ticket_eur, route.ticket_currency) : "—", label: "门票合计" }]),
    { n: route ? route.total_km : "—", label: "公里（直线累计）" },
  ];

  function skip() {
    document.getElementById("cover-end")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  return (
    <>
      <div className={`trip-cover ${story ? "story" : "static"}`} ref={wrap} data-testid="trip-cover" data-mode={story ? "story" : "static"}>
        <div className="cover-stage" style={ready ? undefined : coverStyle(theme)}>
          {ready && slides.picks.length > 0
            ? slides.picks.map((h, i) => (
                <div
                  key={h.image.url}
                  className="cover-photo cover-photo-layer"
                  data-testid="cover-photo"
                  data-city-slide={i}
                  style={{ backgroundImage: `url("${mediaSrc(h.image.url)}")`, opacity: i === slides.at % slides.picks.length ? 1 : 0 }}
                />
              ))
            : ready && hero && (
                <div className="cover-photo" data-testid="cover-photo" style={{ backgroundImage: `url("${mediaSrc(hero.image.url)}")` }} />
              )}
          <div className="cover-tint" />
          <div className="cover-inner">
            <div className="cover-title" data-testid="cover-title">
              <span className="cover-kicker">
                {dayTrip ? "DAY TRIP" : `${it.trip.traveler_profile.style === "relaxed" ? "RELAXED" : "ITINERARY"} · ${it.days.length} DAYS`}
              </span>
              <FitTitle text={city} />
              <p className="cover-theme">{dayTrip ? `从${dayTrip.origin}出发的一日游` : `${themeName}之旅`}</p>
              <div className="cover-dates" aria-label={`${it.trip.date_range.start} 到 ${it.trip.date_range.end}`}>
                <span className="big-dates">{dayTrip ? from : `${from}—${to}`}</span>
                {!dayTrip && (
                  <span className="night-count">
                    共 {it.days.length} 天 {nights} 晚
                  </span>
                )}
              </div>
              {!dayTrip && (
                <div className="cover-pills">
                  {it.trip.traveler_profile.interests.map((x) => (
                    <span key={x}>{x}</span>
                  ))}
                </div>
              )}
            </div>

            {story && (
              <div className="cover-stats" data-testid="cover-stats">
                {stats.map((s) => (
                  <div key={s.label}>
                    <b>{s.n}</b>
                    <span>{s.label}</span>
                  </div>
                ))}
              </div>
            )}

            {story && highlights.length > 0 && (
              <section className="cover-cards" aria-label="行程亮点" data-testid="cover-cards">
                <CardTrack cards={highlights} onOpen={onOpenPlace} />
              </section>
            )}
          </div>

          {story && (
            <button type="button" className="cover-skip" data-testid="cover-skip" onClick={skip}>
              跳过封面 ↓
            </button>
          )}
          {story && <span className="cover-hint">向下滚动 ↓</span>}
          {ready && current && <HeroCredit image={current.image} className="hero-credit cover-credit" />}
        </div>
      </div>

      {!story && highlights.length > 0 && (
        <section className="cover-strip" aria-label="行程亮点" data-testid="cover-strip">
          <CardStrip highlights={highlights} onOpen={onOpenPlace} />
        </section>
      )}
      <div id="cover-end" />
    </>
  );
}
