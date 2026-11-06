import { useEffect, useRef, useState } from "react";
import { PartyEditor, partyError, toTravelers } from "../components/PartyEditor";
import { DestinationFlow } from "../components/DestinationFlow";
import { DurationAdvice } from "../components/DurationAdvice";
import { blankChoices } from "../components/LegOptions";
import { LegThemes } from "../components/LegThemes";
import {
  blankTransfer,
  lastDay,
  LegsEditor,
  legsError,
  MAX_LEGS,
  MAX_PICK,
  moveAll,
  moveFirstEnd,
  newLeg,
  type LegDates,
  type LegDraft,
  type TransferDraft,
} from "../components/LegsEditor";
import { StayDraftEditor, staysError, toStays, type StayDraft } from "../components/StayDraftEditor";
import {
  TransportEditor,
  defaultTransport,
  moveLegDate,
  toTransport,
  transportErrors,
  type TransportDraft,
} from "../components/TransportEditor";
import { useLocation, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import { CandidatePool, prefsToPayload, type PoolPrefs } from "../components/CandidatePool";
import type { ImportAccepted } from "../components/NoteImport";
import { PlaceCard } from "../components/PlaceCard";
import { ErrorPopup } from "../confirm";
import { NlForm } from "../components/HomeHero";
import { NlSummary } from "../components/NlSummary";
import { IconCalendar, IconCheck, IconSpark } from "../icons";
import { RoutePicker } from "../components/RoutePicker";
import { coverStyle, themeLook } from "../themes";
import { useReadOnly } from "../offline";
import type { AttractionRef, CityInfo, Currency, IntakeGap, NlIntent, NoteSource, PendingPlace, RouteCard, ThemeInfo, TravelerDraft } from "../types";

const SPLIT = /[,，、;；\s]+/;

const BUSY_STEPS = [
  "读取地点库…",
  "按主题选点、避开闭馆日、排路线…",
  "跑质检规则…",
  "生成说明文字（模型不可用时用模板）…",
];

function useRotating(active: boolean, steps: string[], intervalMs = 2600): string {
  const [i, setI] = useState(0);
  useEffect(() => {
    if (!active) {
      setI(0);
      return;
    }
    const t = setInterval(() => setI((n) => Math.min(n + 1, steps.length - 1)), intervalMs);
    return () => clearInterval(t);
  }, [active, steps.length, intervalMs]);
  return steps[i];
}

function list(v: string): string[] {
  return v.split(SPLIT).map((s) => s.trim()).filter(Boolean);
}

function dayCount(start: string, end: string): number {
  const ms = new Date(end + "T00:00:00").getTime() - new Date(start + "T00:00:00").getTime();
  return Math.round(ms / 86_400_000) + 1;
}

/** 日期只填一次（规格 M01 S4a / 07 P06）：开始 / 离开日期 → 天数；到达 / 离开的具体时间在「来回交通」里填。 */
function tripSpan(f: { start_date: string; end_date: string }) {
  const ok = f.start_date.length === 10 && f.end_date.length === 10;
  return { start: f.start_date, end: f.end_date, days: ok ? dayCount(f.start_date, f.end_date) : 0 };
}

/** 两个日期（YYYY-MM-DD）差几天；任何一个不完整 → 0。 */
function dayDelta(a: string, b: string): number {
  if (a.length !== 10 || b.length !== 10) return 0;
  return Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000);
}

/** 「YYYY-MM-DDTHH:MM」平移若干天；空的 / 不是这个格式的原样返回。 */
function shiftLeg(value: string, days: number): string {
  if (!days || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value)) return value;
  const d = new Date(Date.parse(value.slice(0, 10)) + days * 86_400_000);
  return `${d.toISOString().slice(0, 10)}${value.slice(10)}`;
}

/** 超过这么多天算长行程：节奏默认「轻松」（规格 07 Q5）。 */
const LONG_TRIP_DAYS = 7;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** 默认值：从今天开始、四天的行程（今天之前的日期没意义；要提前订的行程，自己改日期）。 */
function defaultDates(): { start_date: string; end_date: string } {
  const at = (days: number) => {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  };
  return { start_date: at(0), end_date: at(3) };
}

/** 日期字符串 + n 天（只算日历，不碰时区）。 */
function addDays(date: string, n: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

/** 目的地文字 → 城市配置（中文名 / 英文名 / id 均可）；认不出就是 null，页面用通用建议。 */
function resolveCity(cities: CityInfo[], destination: string): CityInfo | null {
  const d = destination.trim().toLowerCase();
  const short = (zh: string) => (zh.endsWith("市") && zh.length >= 3 ? zh.slice(0, -1) : zh); // 「重庆」也认得出是「重庆市」
  return cities.find((c) => [c.id, c.name_zh, short(c.name_zh), c.name_en].some((n) => n.toLowerCase() === d)) ?? null;
}

/** 一句话读出的几座城市 → 每座城市的起止日期（日期读到了用读到的；没读到按天数从开始日期顺排，换城市在第二天；没写几天先按 3 天）。 */
function stopRows(
  stops: { destination: string; days?: number; start_date?: string; end_date?: string }[],
  base: string,
): { destination: string; start: string; end: string }[] {
  let cursor = base;
  return stops.map((s) => {
    const start = s.start_date ?? cursor;
    const end = s.end_date ?? addDays(start, (s.days ?? 3) - 1);
    cursor = addDays(end, 1);
    return { destination: s.destination, start, end };
  });
}

/** 多城市里每座城市自己的选项（和后端的 LEG_OPTION_KEYS 一致）。 */
const LEG_OPTION_KEYS = ["route", "route_stops", "must_go_ids", "avoid_ids", "place_weights", "stays"];

/** 去掉空数组 / 空对象的项。 */
function nonEmpty<T extends object>(o: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(o).filter(([, v]) => (Array.isArray(v) ? v.length : Object.keys(v as object).length)),
  ) as Partial<T>;
}

function validate(f: Record<string, string>): Record<string, string> {
  const e: Record<string, string> = {};
  if (!f.destination.trim()) e.destination = "先填目的地";
  if (!f.start_date) e.start_date = "必填";
  if (!f.end_date) e.end_date = "必填";
  if (f.start_date && f.end_date) {
    const { days } = tripSpan({ start_date: f.start_date, end_date: f.end_date });
    if (days < 2) e.end_date = "离开日期要晚于开始日期（至少 2 天）";
  }
  if (list(f.interests).length === 0) e.interests = "至少写一个兴趣";
  return e;
}

/** 首页跳过来时带的东西（规格 07 P04）：一句话 / 要打开的导入弹窗。 */
interface NewPlanState {
  nl?: string;
  importNotes?: boolean;
}

export function NewPlanPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const fromHome = useRef<NewPlanState | null>((location.state as NewPlanState | null) ?? null);
  const [busy, setBusy] = useState(false);
  const [gap, setGap] = useState<IntakeGap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [repair, setRepair] = useState(true);
  const [paperTickets, setPaperTickets] = useState(false);
  const [party, setParty] = useState<TravelerDraft[]>([{ age: 30, residence: "none", student: false }]);
  const [tr, setTr] = useState<TransportDraft>(() => {
    const d = defaultDates();
    return defaultTransport(d.start_date, d.end_date);
  });
  const [stays, setStays] = useState<StayDraft[]>([]);
  const [legs, setLegs] = useState<LegDraft[]>([]); // 多城市旅行（规格 11）：第一座城市之外的每一段
  const [transfers, setTransfers] = useState<TransferDraft[]>([]);
  const [firstEnd, setFirstEnd] = useState(""); // 多城市时第一座城市的离开日期（f.end_date 那时是整趟的最后一天）
  const [firstOk, setFirstOk] = useState(""); // 它最近一次填完整的值（手敲日期时中间状态是空的）
  const startOk = useRef(""); // 开始日期同理
  const [flowLeg, setFlowLeg] = useState<number | null>(null); // 正在认 / 建的是第几个后续城市；null = 主目的地
  // 目的地填的是国家时最多能选几座城市（只有「最后一座」才能往后接，中间的只能换成一座）
  const pickSlots = Math.min(
    MAX_PICK,
    flowLeg === null ? (legs.length === 0 ? MAX_LEGS : 1) : flowLeg === legs.length - 1 ? Math.max(MAX_LEGS - (1 + flowLeg), 1) : 1,
  );
  const multi = legs.length > 0;
  const [cities, setCities] = useState<CityInfo[]>([]);
  const [cap, setCap] = useState("");
  const [currency, setCurrency] = useState<Currency>("CNY");
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const [themes, setThemes] = useState<ThemeInfo[]>([]);
  const [routes, setRoutes] = useState<RouteCard[]>([]); // 深度游学路线（规格 09）：和主题二选一
  const [routeId, setRouteId] = useState<string | null>(null);
  const [routeStops, setRouteStops] = useState<string[] | null>(null); // 路线里你勾选要去的站；null = 全部
  // 默认「均衡」（没有特定倾向，让系统推荐）；点具体主题就改成混选：不限个数，每个有权重 1–5（默认 3）
  const [balanced, setBalanced] = useState(true);
  const [picked, setPicked] = useState<{ id: string; weight: number }[]>([]);
  const [prefs, setPrefs] = useState<PoolPrefs>({});
  const [places, setPlaces] = useState<AttractionRef[]>([]);
  const [cardId, setCardId] = useState<string | null>(null);
  const [noteSources, setNoteSources] = useState<Record<string, NoteSource>>({}); // 笔记导入（M12）
  const [notePending, setNotePending] = useState<PendingPlace[]>([]);
  const [nl, setNl] = useState<NlIntent | null>(null); // 一句话填表的结果（规格 M01 S10）
  const [nlBusy, setNlBusy] = useState(false);
  const [importSignal, setImportSignal] = useState(0);
  const { readOnly } = useReadOnly();
  const allThemes = themes.map((t) => ({ id: t.id, weight: 3 }));
  const effective = balanced ? allThemes : picked;
  const route = routes.find((r) => r.id === routeId) ?? null;
  const activeTheme = route ? route.base_theme : (effective[0]?.id ?? null);

  /** 笔记导入确认后（M12）：记下每个接受地点的原文片段；库外的进「待核实」（同名只留一份）。 */
  function onImported(a: ImportAccepted) {
    setNoteSources((cur) => {
      const next = { ...cur };
      for (const p of a.picks) {
        if (p.state === "must") next[p.place_id] = p.source;
        else delete next[p.place_id];
      }
      return next;
    });
    setNotePending((cur) => [...cur, ...a.pending.filter((p) => !cur.some((c) => c.name === p.name))]);
  }

  /** 加 / 去掉后续城市：第一次加时，第一座城市的离开日期取现在的离开日期。
   *  去掉最后一座回到单城市时，离开日期回到第一座城市的。 */
  function changeLegs(next: LegDraft[]) {
    if (legs.length === 0 && next.length > 0) {
      setFirstEnd(f.end_date);
      setFirstOk(f.end_date);
    }
    if (legs.length > 0 && next.length === 0) setF((prev) => ({ ...prev, end_date: firstEnd }));
    setLegs(next);
  }

  /** 日期变了（连带平移后面的城市）。 */
  function applyDates(next: LegDates) {
    setFirstEnd(next.firstEnd);
    setFirstOk(next.firstOk);
    setLegs(next.legs);
  }

  /** 一句话填表：后端读懂意图 → 把读到的项填进下面的表单（读不出来的不动）；**不会生成行程**，由用户确认（规格 M01 S10）。 */
  async function readIntent(text: string) {
    setNlBusy(true);
    setError(null);
    try {
      const res = await api.intakeNl(text);
      const fl = res.fields;
      const stops = fl.stops && fl.stops.length > 1 ? stopRows(fl.stops, fl.start_date ?? f.start_date) : null;
      if (stops) {
        // 多城市：第一座填进「目的地」，其余的填进「添加下一座城市」；换城市的交通先留空（按距离估算）
        const rest = stops.slice(1);
        setFirstEnd(stops[0].end);
        setFirstOk(stops[0].end);
        setLegs(rest.map((r) => ({ ...blankChoices(), destination: r.destination, start: r.start, end: r.end, endOk: r.end })));
        setTransfers(rest.map(() => blankTransfer()));
        startOk.current = stops[0].start;
      } else if (multi && fl.destination) {
        // 这句话说的是一座城市：不再是多城市，之前读到 / 填的后续城市去掉
        setLegs([]);
        setTransfers([]);
      } else if (multi && fl.start_date) {
        applyDates(moveAll({ firstEnd, firstOk, legs }, f.start_date, fl.start_date)); // 只说了日期：整趟一起平移
      }
      const keepMulti = multi && !stops && !fl.destination; // 多城市行程里只改了日期：结束日由各座城市决定，不按天数另算
      setF((prev) => {
        const next = { ...prev };
        if (fl.destination) next.destination = fl.destination;
        if (stops) {
          next.start_date = stops[0].start;
          next.end_date = stops[stops.length - 1].end;
        } else {
          if (fl.start_date) next.start_date = fl.start_date;
          if (!keepMulti) {
            if (fl.end_date) next.end_date = fl.end_date;
            else if (fl.days) next.end_date = addDays(fl.start_date ?? prev.start_date, fl.days - 1);
          }
        }
        if (fl.travel_style) {
          next.travel_style = fl.travel_style;
          styleTouched.current = true;
        }
        if (fl.restrictions?.length) next.restrictions = fl.restrictions.join("、");
        // 必去 / 想避开：写进「偏好」里的文字框（和已有的合并去重），用户一眼看得到、也能直接改
        if (fl.must_go?.length) next.must_go = [...new Set([...list(prev.must_go), ...fl.must_go])].join("、");
        if (fl.avoid?.length) next.avoid = [...new Set([...list(prev.avoid), ...fl.avoid])].join("、");
        return next;
      });
      // 不在库里的城市：马上去认 / 建；多座时一座建好再开下一座
      const names = stops ? stops.map((r) => r.destination) : fl.destination ? [fl.destination] : [];
      flowQueue.current = names.map((n, at) => ({ at: at === 0 ? null : at - 1, text: n })).filter((q) => !resolveCity(cities, q.text));
      openNextFlow(cities);
      const ids = fl.theme_ids ?? [];
      if (themes.length > 0) {
        const mine = ids.filter((id) => themes.some((t) => t.id === id));
        if (mine.length > 0) {
          setBalanced(false);
          setPicked(mine.map((id) => ({ id, weight: 3 })));
        }
      } else pendingThemes.current = ids; // 目的地还没认出来：等主题列表出来再对号入座
      if (fl.origin || fl.transport_out || fl.transport_back) {
        setTr((prev) => ({
          out: { ...prev.out, ...(fl.transport_out ? { mode: fl.transport_out } : {}), ...(fl.origin ? { place: fl.origin } : {}) },
          back: { ...prev.back, ...(fl.transport_back ? { mode: fl.transport_back } : {}), ...(fl.origin ? { place: fl.origin } : {}) },
        }));
      }
      if (fl.travelers?.length) setParty(fl.travelers.map((t) => ({ age: t.age, residence: t.residence as TravelerDraft["residence"], student: t.student })));
      if (fl.budget_cap !== undefined) setCap(String(fl.budget_cap));
      if (fl.currency) setCurrency(fl.currency);
      setNl(res);
      requestAnimationFrame(() => document.getElementById("nl-summary")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setNlBusy(false);
    }
  }

  // 从首页带来的一句话：主题列表读完再解析（要用主题 id 对号入座），解析完不自动生成
  useEffect(() => {
    const st = fromHome.current;
    if (!st) return;
    fromHome.current = null;
    window.history.replaceState({}, "");                       // 刷新 / 后退回来时不再重复解析
    if (st.nl) void readIntent(st.nl);
    if (st.importNotes) {
      // 导入笔记要对着一座城市的地点库：还没填目的地就先用默认的，等地点列表出来再弹出导入窗口
      setF((prev) => (prev.destination ? prev : { ...prev, destination: "Paris" }));
      setImportPending(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    api.cities().then(setCities).catch(() => setCities([]));
  }, []);

  const styleTouched = useRef(false); // 用户（或一句话填表）定过节奏之后，不再按天数自动换
  const [f, setF] = useState({
    destination: new URLSearchParams(location.search).get("destination") ?? "",
    ...defaultDates(),
    budget_level: "medium",
    travel_style: "balanced",
    interests: "艺术",
    must_go: "",
    avoid: "",
    restrictions: "",
  });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    if (k === "travel_style") styleTouched.current = true;
    setF({ ...f, [k]: e.target.value });
  };
  const busyStep = useRotating(busy, BUSY_STEPS);
  const span = tripSpan(f);
  const city = resolveCity(cities, f.destination);

  // 先填目的地，再选主题（规格 10）：主题、深度路线、候选地点都是这座城市的；还没认出城市就什么都不显示
  const pendingThemes = useRef<string[]>([]); // 一句话里读到的主题（Paris 库里的 id）：这座城市有同名主题才用
  useEffect(() => {
    setRouteId(null);
    setRouteStops(null);
    if (!city) {
      setThemes([]);
      setRoutes([]);
      setPlaces([]);
      return;
    }
    let alive = true;
    api.themes(city.id).then((t) => {
      if (!alive) return;
      setThemes(t);
      const ids = pendingThemes.current.filter((id) => t.some((x) => x.id === id));
      pendingThemes.current = [];
      if (ids.length > 0) {
        setRouteId(null);
        setBalanced(false);
        setPicked(ids.map((id) => ({ id, weight: 3 })));
      } else pickBalanced();
    }).catch(() => alive && setThemes([]));
    api.deepRoutes(city.id).then((r) => alive && setRoutes(r)).catch(() => alive && setRoutes([]));
    api.places(city.id).then((p) => alive && setPlaces(p)).catch(() => alive && setPlaces([]));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [city?.id]);
  const [flowText, setFlowText] = useState<string | null>(null);
  const flowQueue = useRef<{ at: number | null; text: string }[]>([]); // 一句话里还没建好数据的城市（at = 后续城市的序号，null = 主目的地）

  /** 队列里下一座还没数据的城市：弹出认 / 建的窗口（已经能认出来的跳过）。 */
  function openNextFlow(list: CityInfo[]) {
    while (flowQueue.current.length > 0) {
      const next = flowQueue.current.shift()!;
      if (resolveCity(list, next.text)) continue;
      setFlowLeg(next.at);
      setFlowText(next.text);
      return;
    }
  }
  const [importPending, setImportPending] = useState(false);
  useEffect(() => {
    if (importPending && places.length > 0 && themes.length > 0) {
      setImportPending(false);
      setImportSignal((n) => n + 1);
    }
  }, [importPending, places, themes]);

  /** 目的地填好了（失焦 / 回车）：库里没有的现在就去认、去建，不等到点「生成」。 */
  function settleDestination() {
    const text = f.destination.trim();
    if (text && !resolveCity(cities, text) && !flowText && cities.length > 0) setFlowText(text);
  }

  // 多城市：整趟的离开日期 = 最后一座城市的离开日期（回程交通、预算、天数都按它）
  useEffect(() => {
    if (!multi) return;
    const end = lastDay(firstEnd, legs);
    if (end.length === 10) setF((prev) => (prev.end_date === end ? prev : { ...prev, end_date: end }));
  }, [multi, legs, firstEnd]);

  // 行程比较长（超过一周）：节奏默认放宽到「轻松」，一天 2 个景点；用户自己选过节奏就不再动它
  useEffect(() => {
    if (styleTouched.current || span.days < 1) return;
    const want = span.days > LONG_TRIP_DAYS ? "relaxed" : "balanced";
    setF((prev) => (prev.travel_style === want ? prev : { ...prev, travel_style: want }));
  }, [span.days]);

  // 选的主题变了，「兴趣」跟着变（均衡就写「均衡」）；用户之后自己改兴趣文字不受影响
  useEffect(() => {
    const names = balanced ? "均衡" : picked.map((p) => themes.find((t) => t.id === p.id)?.name_zh).filter(Boolean).join("、");
    if (names) setF((prev) => ({ ...prev, interests: names }));
  }, [balanced, picked, themes]);

  // 开始 / 离开日期改了（一句话、行程信息、来回交通哪里改的都一样），其他地方跟着统一：
  // 到达 / 返程出发那天换成新日期；去程出发、返程到达按同样的天数平移（比如夜里的航班仍然是前一天出发）；
  // 住宿里入住 / 退房正好是原来开始 / 离开日的，跟着换
  const lastDates = useRef({ start: f.start_date, end: f.end_date });
  useEffect(() => {
    const prev = lastDates.current;
    lastDates.current = { start: f.start_date, end: f.end_date };
    const dStart = dayDelta(prev.start, f.start_date);
    const dEnd = dayDelta(prev.end, f.end_date);
    setTr((cur) => ({
      out: {
        ...cur.out,
        arrive_at: moveLegDate(cur.out.arrive_at, f.start_date),
        depart_at: shiftLeg(cur.out.depart_at, dStart),
      },
      back: {
        ...cur.back,
        depart_at: moveLegDate(cur.back.depart_at, f.end_date),
        arrive_at: shiftLeg(cur.back.arrive_at, dEnd),
      },
    }));
    setStays((cur) =>
      cur.map((st) => ({
        ...st,
        check_in: st.check_in === prev.start ? f.start_date : st.check_in,
        check_out: st.check_out === prev.end ? f.end_date : st.check_out,
      })),
    );
  }, [f.start_date, f.end_date]);

  /**
   * 交通时间改了，日期跟着变（日期只需要填一次，规格 M01 S4a）：去程到达那天 = 开始日期，返程出发那天 = 离开日期。
   * 反过来改日期，已填的时间也跟着换日期（上面的 effect）。
   */
  function changeTransport(next: TransportDraft) {
    setTr(next);
    const arrive = next.out.arrive_at.slice(0, 10);
    const leave = next.back.depart_at.slice(0, 10);
    setF((prev) => ({
      ...prev,
      start_date: arrive.length === 10 ? arrive : prev.start_date,
      end_date: leave.length === 10 && !multi ? leave : prev.end_date,
    }));
  }

  /** 点「均衡」：回到不限定主题；点具体主题：从均衡切到混选，再点已选的取消，全取消就回到均衡。 */
  function pickBalanced() {
    setRouteId(null);
    setBalanced(true);
    setPicked([]);
  }

  /** 选路线：和主题二选一，兴趣文字改成路线名；点主题 / 均衡会取消路线。 */
  function pickRoute(id: string | null) {
    setRouteId(id);
    setRouteStops(null);
    if (id) {
      const r = routes.find((x) => x.id === id);
      if (r) setF((prev) => ({ ...prev, interests: r.name_zh }));
    }
  }

  function togglePick(t: ThemeInfo) {
    if (routeId) setRouteId(null);
    if (balanced) {
      setBalanced(false);
      setPicked([{ id: t.id, weight: 3 }]);
      return;
    }
    const next = picked.some((p) => p.id === t.id) ? picked.filter((p) => p.id !== t.id) : [...picked, { id: t.id, weight: 3 }];
    if (next.length === 0) pickBalanced();
    else setPicked(next);
  }

  function setWeight(id: string, weight: number) {
    setPicked((cur) => cur.map((p) => (p.id === id ? { ...p, weight } : p)));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const fe = validate(f);
    if (cap.trim() !== "" && (!Number.isFinite(Number(cap)) || Number(cap) < 0)) fe.budget_cap = "预算上限要填不小于 0 的数字，不想设就留空";
    const pe = partyError(party);
    if (pe) fe.travelers = pe;
    Object.assign(fe, transportErrors(tr, f.start_date, f.end_date));
    const se = staysError(stays, f.start_date, multi ? firstEnd : f.end_date);
    if (se) fe.stays = se;
    if (multi) {
      const le = legsError({ destination: f.destination, start: f.start_date, end: firstEnd }, legs, cities);
      if (le) fe.legs = le;
    }
    if (!resolveCity(cities, f.destination) && f.destination.trim()) {
      setFieldErr(fe);
      setFlowText(f.destination.trim());
      return;
    }
    setFieldErr(fe);
    if (Object.keys(fe).length > 0) {
      // 把第一处错误滚到眼前（长表单里错误在屏幕外就等于没提示）
      requestAnimationFrame(() => document.querySelector(".home-form .ferr")?.scrollIntoView({ behavior: "smooth", block: "center" }));
      return;
    }
    setBusy(true);
    setGap(null);
    setError(null);
    const payload: Record<string, unknown> = {
      destination: f.destination,
      start_date: span.start,
      end_date: span.end,
      budget_level: f.budget_level,
      travel_style: f.travel_style,
      interests: list(f.interests),
      must_go: list(f.must_go),
      avoid: list(f.avoid),
      restrictions: list(f.restrictions),
    };
    payload.transport = toTransport(tr);
    if (stays.length > 0) payload.stays = toStays(stays);
    if (route) {
      payload.theme = route.base_theme; // 路线对应的主题：编辑重排、行李等沿用它
      payload.route = route.id;
      if (routeStops && routeStops.length > 0) payload.route_stops = routeStops;
    } else if (effective.length > 0) {
      const lead = [...effective].sort((a, b) => b.weight - a.weight)[0];
      payload.theme = lead.id;
      if (effective.length > 1) payload.themes = effective;
    }
    if (!route) {
      Object.assign(payload, nonEmpty(prefsToPayload(prefs)));
    }
    // 笔记来源只带还标着「必去」的地点；待核实的不进排程，只随行程保存用来显示
    const sources = Object.values(noteSources).filter((s) => prefs[s.place_id]?.state === "must");
    if (sources.length > 0) payload.note_sources = sources.slice(0, 60);
    if (notePending.length > 0) payload.note_pending = notePending.slice(0, 60);
    if (paperTickets) payload.paper_tickets = true;
    payload.travelers = toTravelers(party);
    payload.currency = currency;
    if (cap.trim() !== "") payload.budget_cap = Number(cap);

    try {
      if (multi) {
        const tp = toTransport(tr);
        const starts = [span.start, ...legs.map((l) => l.start)];
        const ends = [firstEnd, ...legs.map((l) => l.end)];
        const common: Record<string, unknown> = {
          budget_level: f.budget_level,
          travel_style: f.travel_style,
          travelers: payload.travelers,
          currency,
          restrictions: list(f.restrictions),
          must_go: list(f.must_go),
          avoid: list(f.avoid),
        };
        if (cap.trim() !== "") common.budget_cap = Number(cap);
        if (paperTickets) common.paper_tickets = true;
        const { task_id } = await api.createTask({
          legs: [
            {
              destination: f.destination,
              start_date: span.start,
              end_date: firstEnd,
              interests: payload.interests,
              ...(payload.theme ? { theme: payload.theme } : {}),
              ...(payload.themes ? { themes: payload.themes } : {}),
              options: Object.fromEntries(
                LEG_OPTION_KEYS.filter((k) => payload[k] !== undefined).map((k) => [k, payload[k]]),
              ),
            },
            ...legs.map((l) => {
              const options: Record<string, unknown> = {};
              let pick: { theme: string; interests: string[] } | null = null;
              if (l.routeId) {
                pick = { theme: l.routeTheme, interests: [l.routeName] };
                options.route = l.routeId;
                if (l.routeStops && l.routeStops.length > 0) options.route_stops = l.routeStops;
              } else {
                if (l.theme) pick = { theme: l.theme, interests: [l.themeName] };
                Object.assign(options, nonEmpty(prefsToPayload(l.prefs)));
              }
              if (l.stays.length > 0) options.stays = toStays(l.stays);
              return { destination: l.destination, start_date: l.start, end_date: l.end, ...(pick ?? {}), options };
            }),
          ],
          outbound: tp.outbound,
          inbound: tp.inbound,
          transfers: transfers.map((t, i) =>
            t.depart && t.arrive
              ? { mode: t.mode, depart_at: `${ends[i]}T${t.depart}:00`, arrive_at: `${starts[i + 1]}T${t.arrive}:00` }
              : {},
          ),
          common,
        });
        navigate(`/tasks/${task_id}`);
        return;
      }
      if (activeTheme) {
        // 主题生成走后台任务：立刻拿到任务号，在进度页看真实进度（刷新也不丢）
        const { task_id } = await api.createTask(payload);
        navigate(`/tasks/${task_id}`);
        return;
      }
      const res = await api.createPlan(payload, repair);
      navigate(`/plans/${res.plan_id}`);
    } catch (err) {
      if (err instanceof ApiError && err.intakeGap) setGap(err.intakeGap);
      else setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const pickedNames = balanced ? "均衡（系统推荐）" : picked.map((p) => themes.find((t) => t.id === p.id)?.name_zh).filter(Boolean).join(" + ");
  const errorList = Object.values(fieldErr);

  return (
    <>
      <div className="wrap new-page">
        <div className="page-head new-head">
          <div>
            <h1>新建行程</h1>
          </div>
        </div>
        <NlForm
          variant="plain"
          busy={nlBusy}
          disabled={readOnly}
          initial={fromHome.current?.nl ?? ""}
          onRead={(text) => void readIntent(text)}
          onImport={() => setImportSignal((n) => n + 1)}
        />
        <form className="home-form" onSubmit={submit} noValidate>
          {nl && <NlSummary result={nl} onClose={() => setNl(null)} />}
          <section className="card step-card">
            <header className="step-head">
              <span className="step-no">1</span>
              <div>
                <h2>行程信息</h2>
              </div>
            </header>
            <div className="field">
              <label htmlFor="destination">目的地</label>
              <input
                id="destination"
                value={f.destination}
                placeholder="想去哪里？城市、地区或国家都可以"
                onChange={set("destination")}
                onBlur={settleDestination}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    settleDestination();
                  }
                }}
              />
              {fieldErr.destination && <div className="ferr">{fieldErr.destination}</div>}
            </div>
            <div className="row2">
              <div className="field">
                <label htmlFor="start_date">开始日期</label>
                <input
                  id="start_date"
                  type="date"
                  value={f.start_date}
                  onChange={(e) => {
                    const v = e.target.value;
                    if (multi && v.length === 10) {
                      applyDates(moveAll({ firstEnd, firstOk, legs }, startOk.current || f.start_date, v));
                    }
                    if (v.length === 10) startOk.current = v;
                    set("start_date")(e);
                  }}
                  required
                />
                {fieldErr.start_date && <div className="ferr">{fieldErr.start_date}</div>}
              </div>
              <div className="field">
                <label htmlFor="end_date">离开日期</label>
                <input
                  id="end_date"
                  type="date"
                  value={multi ? firstEnd : f.end_date}
                  min={f.start_date || undefined}
                  onChange={(e) => (multi ? applyDates(moveFirstEnd({ firstEnd, firstOk, legs }, e.target.value)) : set("end_date")(e))}
                  required
                />
                {fieldErr.end_date && <div className="ferr">{fieldErr.end_date}</div>}
              </div>
            </div>
            {span.days > 0 && (
              <div className="trip-span" data-testid="trip-span">
                <IconCalendar size={15} />
                共 {span.days} 天（{span.start} → {span.end}）
              </div>
            )}
            {!multi && <DurationAdvice days={span.days} city={city} />}
            <LegsEditor
              first={{ destination: f.destination, start: f.start_date, end: multi ? firstEnd : f.end_date }}
              firstOk={firstOk}
              legs={legs}
              transfers={transfers}
              cities={cities}
              error={fieldErr.legs}
              onDates={applyDates}
              onLegs={changeLegs}
              onTransfers={setTransfers}
              onNeedCity={(i, text) => {
                if (flowText) return;
                setFlowLeg(i);
                setFlowText(text);
              }}
            />
            <div className="row2">
              <div className="field">
                <label htmlFor="budget_level">预算</label>
                <select id="budget_level" value={f.budget_level} onChange={set("budget_level")}>
                  <option value="low">低</option>
                  <option value="medium">中</option>
                  <option value="high">高</option>
                </select>
              </div>
            </div>
            <div className="row2">
              <div className="field">
                <label htmlFor="travel_style">节奏</label>
                <select id="travel_style" value={f.travel_style} onChange={set("travel_style")}>
                  <option value="relaxed">轻松</option>
                  <option value="balanced">均衡</option>
                  <option value="intense">紧凑</option>
                </select>
              </div>
            </div>
          </section>

          <section className="card step-card">
            <header className="step-head">
              <span className="step-no">2</span>
              <div>
                <h2>选主题</h2>
              </div>
            </header>
            {!city && (
              <p className="muted" data-testid="theme-need-destination">
                先在上面填目的地，这里会出现这座城市的主题和深度路线。
              </p>
            )}
            {city && (
              <>
            {multi && <h3 className="leg-themes-title">{city.name_zh}</h3>}
            <div className="theme-picker">
              {themes.length > 0 && (
                <button
                  type="button"
                  className={`theme-btn balanced${balanced ? " active" : ""}`}
                  aria-pressed={balanced}
                  data-testid="theme-balanced"
                  onClick={pickBalanced}
                >
                  <span className="theme-icon" aria-hidden>
                    <IconSpark size={18} />
                  </span>
                  <span className="theme-text">
                    <span className="theme-label">均衡 · 让我来推荐</span>
                    <span className="theme-hint">没有特定倾向，各类主题均匀搭配</span>
                  </span>
                  {balanced && (
                    <span className="theme-check" aria-hidden>
                      <IconCheck size={14} />
                    </span>
                  )}
                </button>
              )}
              {themes.map((t) => {
                const sel = picked.find((p) => p.id === t.id);
                const look = themeLook(t.id);
                return (
                  <button
                    type="button"
                    key={t.id}
                    className={`theme-btn${sel ? " active" : ""}`}
                    aria-pressed={!!sel}
                    onClick={() => togglePick(t)}
                  >
                    <span className="theme-icon" style={coverStyle(t.id)} aria-hidden>
                      {look.icon}
                    </span>
                    <span className="theme-text">
                      <span className="theme-label">{t.name_zh}</span>
                      <span className="theme-hint">{t.tagline}</span>
                    </span>
                    {sel && (
                      <span className="theme-check" aria-hidden>
                        <IconCheck size={14} />
                      </span>
                    )}
                  </button>
                );
              })}
              {themes.length === 0 && <span className="muted">主题加载中…（连不上后端时会走旧的自由生成）</span>}
            </div>
            {picked.length > 1 && (
              <div className="mix-weights" aria-label="主题权重">
                {picked.map((p) => (
                  <label key={p.id}>
                    {themes.find((t) => t.id === p.id)?.name_zh} 权重
                    <select
                      value={p.weight}
                      aria-label={`${themes.find((t) => t.id === p.id)?.name_zh}的主题权重`}
                      onChange={(e) => setWeight(p.id, Number(e.target.value))}
                    >
                      {[1, 2, 3, 4, 5].map((w) => (
                        <option key={w} value={w}>
                          {w}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
            )}
            <RoutePicker
              city={city.id}
              routes={routes}
              selected={routeId}
              days={multi ? dayCount(f.start_date, firstEnd) : span.days}
              startDate={f.start_date}
              endDate={multi ? firstEnd : f.end_date}
              stops={routeStops}
              onStops={setRouteStops}
              onSelect={pickRoute}
              onSuggestDays={(n) =>
                multi
                  ? applyDates(moveFirstEnd({ firstEnd, firstOk, legs }, addDays(f.start_date, n - 1)))
                  : setF((prev) => ({ ...prev, end_date: addDays(prev.start_date, n - 1) }))
              }
            />
            {effective.length > 0 && !route && (
              <CandidatePool
                key={city.id}
                city={city.id}
                themeIds={effective.map((p) => p.id)}
                places={places}
                prefs={prefs}
                onChange={setPrefs}
                onOpenPlace={setCardId}
                noteSources={noteSources}
                notePending={notePending}
                onImported={onImported}
                onRemovePending={(name) => setNotePending((cur) => cur.filter((p) => p.name !== name))}
                onPlaceAdded={(a) => setPlaces((cur) => (cur.some((p) => p.place_id === a.place_id) ? cur : [...cur, a]))}
                amapCity={city.map_provider === "amap"}
                importSignal={importSignal}
              />
            )}
              </>
            )}
            {multi &&
              legs.map((l, i) => (
                <LegThemes
                  key={i}
                  index={i}
                  name={l.destination.trim() || "下一座城市"}
                  city={resolveCity(cities, l.destination)}
                  value={l}
                  onChange={(p) => setLegs((cur) => cur.map((x, k) => (k === i ? { ...x, ...p } : x)))}
                />
              ))}
          </section>

          <ErrorPopup message={error} onClose={() => setError(null)} />
      {flowText && (
        <DestinationFlow
          text={flowText}
          maxPick={pickSlots}
          onClose={() => {
            flowQueue.current = [];
            setFlowText(null);
            setFlowLeg(null);
          }}
          onDone={(picked) => {
            setFlowText(null);
            const at = flowLeg;
            setFlowLeg(null);
            const [head, ...rest] = picked;
            void api.cities().then((list) => {
              setCities(list);
              if (at === null) setF((prev) => ({ ...prev, destination: head.name_zh }));
              else setLegs((cur) => cur.map((l, k) => (k === at ? { ...l, destination: head.name_zh } : l)));
              if (rest.length > 0) {
                // 国家里选了几座：按点选的顺序接在后面，日期一座接一座（用户再改）
                let prevEnd = at === null ? lastDay(f.end_date, legs) : legs[at].end;
                const added = rest.map((c) => {
                  const leg = { ...newLeg(prevEnd), destination: c.name_zh };
                  prevEnd = leg.end;
                  return leg;
                });
                if (legs.length === 0) {
                  setFirstEnd(f.end_date);
                  setFirstOk(f.end_date);
                }
                setLegs((cur) => [...cur, ...added]);
                setTransfers((cur) => [...cur, ...added.map(() => blankTransfer())]);
              }
              openNextFlow(list);
            });
          }}
        />
      )}
          {gap && (
            <div className="banner warn">
              还需要补充：
              <ul>
                {gap.questions.map((q, i) => (
                  <li key={i}>{q}</li>
                ))}
              </ul>
            </div>
          )}

          <section className="card step-card" data-testid="transport-step">
            <header className="step-head">
              <span className="step-no">3</span>
              <div>
                <h2>来回交通与住宿</h2>
                <p className="muted">
                  <b className="req">交通必填</b>
                </p>
              </div>
            </header>
            <TransportEditor
              value={tr}
              onChange={changeTransport}
              errors={{ out_depart: fieldErr.out_depart, out_arrive: fieldErr.out_arrive, back_depart: fieldErr.back_depart }}
            />
            <div className="party-block" data-testid="stay-block">
              <h3>{multi ? `${city?.name_zh ?? f.destination}的住宿` : "住宿"}</h3>
              <StayDraftEditor value={stays} onChange={setStays} start={f.start_date} end={multi ? firstEnd : f.end_date} />
              {fieldErr.stays && <div className="ferr">{fieldErr.stays}</div>}
            </div>
          </section>

          <section className="card step-card">
            <header className="step-head">
              <span className="step-no">4</span>
              <div>
                <h2>出行人与预算</h2>
              </div>
            </header>
            <div className="party-block" data-testid="party-block">
              <p className="muted">姓名只在你自己的行程里显示，不进分享页和导出。</p>
              <PartyEditor
                value={party}
                onChange={setParty}
                saveable
                showResidence={!city || city.residency_options.length > 0}
              />
              {fieldErr.travelers && <div className="ferr">{fieldErr.travelers}</div>}
              <div className="row2">
                <div className="field">
                  <label htmlFor="budget_cap">预算上限 <span className="hint">（选填，整个行程所有人合计）</span></label>
                  <input id="budget_cap" type="number" placeholder="不设就留空" value={cap} onChange={(e) => setCap(e.target.value)} />
                  {fieldErr.budget_cap && <div className="ferr">{fieldErr.budget_cap}</div>}
                </div>
                <div className="field">
                  <label htmlFor="currency">结算货币</label>
                  <select id="currency" value={currency} onChange={(e) => setCurrency(e.target.value as Currency)}>
                    <option value="CNY">人民币</option>
                    <option value="EUR">欧元（巴黎当地货币）</option>
                  </select>
                </div>
              </div>
            </div>
          </section>

          <section className="card step-card">
            <header className="step-head">
              <span className="step-no">5</span>
              <div>
                <h2>偏好</h2>
              </div>
            </header>
            <div className="field">
              <label>兴趣 <span className="hint">（逗号分隔，至少一个）</span></label>
              <input value={f.interests} onChange={set("interests")} />
              {fieldErr.interests && <div className="ferr">{fieldErr.interests}</div>}
            </div>
            <div className="row2">
              <div className="field">
                <label>必去</label>
                <input value={f.must_go} onChange={set("must_go")} />
              </div>
              <div className="field">
                <label>想避开</label>
                <input value={f.avoid} onChange={set("avoid")} />
              </div>
            </div>
            <div className="field">
              <label>其它限制</label>
              <input value={f.restrictions} onChange={set("restrictions")} placeholder="例：不想早起、素食" />
            </div>
            <label className="check-inline">
              <input type="checkbox" checked={paperTickets} onChange={(e) => setPaperTickets(e.target.checked)} />
              我想收集纸质票根（能现场买的景点，建议去现场窗口购票；不勾选则一律推荐线上购票）
            </label>
            {!activeTheme && (
              <label className="check-inline">
                <input type="checkbox" checked={repair} onChange={(e) => setRepair(e.target.checked)} />
                生成后自动跑质检修复循环（慢一点，质量更稳）
              </label>
            )}
          </section>

          <div className="submit-bar">
            {f.destination.trim() && (
              <div className="submit-head" data-testid="submit-head">
                <span className="submit-city">
                  {f.destination.trim()}
                  {span.days > 0 ? ` ${span.days} 天` : ""}
                </span>
                {span.days > 0 && (
                  <span className="submit-dates">
                    {f.start_date.slice(5).replace("-", ".")} — {f.end_date.slice(5).replace("-", ".")}
                  </span>
                )}
              </div>
            )}
            <div className="submit-summary">
              <b>{pickedNames || "还没选主题"}</b>
              <span className="muted">
                {span.days > 0 ? `共 ${span.days} 天` : ""}
                {f.travel_style === "relaxed" ? " · 轻松节奏" : f.travel_style === "intense" ? " · 紧凑节奏" : " · 均衡节奏"}
              </span>
              {errorList.length > 0 && (
                <span className="ferr" data-testid="submit-errors">
                  还有 {errorList.length} 项没填好：{errorList.slice(0, 3).join("；")}
                  {errorList.length > 3 ? "…" : ""}
                </span>
              )}
              {readOnly && <span className="muted">离线或连不上后端，暂时不能生成。已生成的行程可以在「我的行程」里查看。</span>}
              {busy && (
                <span className="muted busy-step">
                  <span className="spin" aria-hidden />
                  {busyStep}
                </span>
              )}
            </div>
            <button className="primary big" type="submit" disabled={busy || readOnly}>
              <IconSpark size={17} />
              {busy ? "生成中…" : "生成行程"}
            </button>
          </div>
        </form>
      </div>
      {cardId && <PlaceCard placeId={cardId} city={city?.id} note={noteSources[cardId]} onClose={() => setCardId(null)} />}
    </>
  );
}
