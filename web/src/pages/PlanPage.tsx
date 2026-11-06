import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { diffItineraries, diffStats } from "../diff";
import type {
  AttractionRef,
  BulkConfirm,
  AttachmentsData,
  Day,
  Itinerary,
  NoteSource,
  PlanDetail,
  PlanTransfer,
  ReservationItem,
  RevisionChange,
  RouteData,
  Stay,
  WeatherData,
} from "../types";
import {
  BUDGET_ZH,
  CompareView,
  BookingAndRisks,
  DayTimeline,
  DayTools,
  Skeleton,
  STYLE_ZH,
  Toast,
  weekday,
  type DayEdit,
} from "../ui";
import { ManageBar } from "../components/ManageBar";
import { BudgetCard } from "../components/BudgetCard";
import { NotePendingCard } from "../components/NotePendingCard";
import { buildHighlights, TripCover } from "../components/TripCover";
import { pickCityHeroes, pickTripHero } from "../hero";
import { ticketTotal } from "../money";
import { CompanionCard } from "../components/CompanionCard";
import { DishesCard } from "../components/DishesCard";
import { type FileFilter, fileCountsOf } from "../components/FilesTab";
import { SectionNav, type NavItem } from "../components/SectionNav";
import { StaysCard } from "../components/StaysCard";
import { TodayCard } from "../components/TodayCard";
import { ErrorPopup, useConfirm } from "../confirm";
import { PlaceCard } from "../components/PlaceCard";
import { TransferDialog } from "../components/TransferDialog";
import type { ReservationPatch } from "../components/ReservationControls";
import { MapSheet } from "../components/MapSheet";
import { RouteMap } from "../components/RouteMap";
import { SharePanel } from "../components/SharePanel";
import { GenerationDetails } from "../components/GenerationDetails";
import { IconCalendar, IconCheck, IconDownload, IconEdit, IconImage, IconInfo, IconLink, IconMap, IconShare } from "../icons";
import { useReadOnly } from "../offline";
import { applyCity, coverStyle, DEFAULT_CITY, themeLook } from "../themes";
import { useMedia } from "../useMedia";
import { tzFor, zonedNow } from "../today";
import { useDragAutoScroll } from "../useDragAutoScroll";
import { useCollapsedDays } from "../useCollapsedDays";
import { useEditSession } from "../useEditSession";

/** 版本是怎么产生的（后端 VersionRecord.trigger）。 */
const TRIGGER_ZH: Record<string, string> = {
  initial: "初版",
  "user-feedback": "一句话修改",
  "manual-save": "手动保存",
  rollback: "回滚",
  "auto-repair": "自动修复",
  "date-shift": "平移日期",
  "stay-update": "住宿更新",
  "booking-lock": "预订锁定",
  "booking-unlock": "取消预订",
};

/** 这一天顶部的住宿提示：当晚住哪家 / 退房当天提示刚退的那家（规格 M06 S5）。 */
const MODE_ZH: Record<string, string> = { flight: "飞机", high_speed_rail: "高铁", train: "火车", coach: "大巴", drive: "自驾", other: "交通" };

/** 多城市行程：这一天是下一座城市开始的那天，就在它前面放上换城市的交通。 */
function transferBefore(detail: PlanDetail, date: string): PlanTransfer[] {
  const cities = detail.cities;
  if (!cities || !detail.transfers) return [];
  return detail.transfers.filter((t) => cities[t.position + 1]?.start_date === date);
}

function PlanTransferCard({ t, onEdit }: { t: PlanTransfer; onEdit?: () => void }) {
  return (
    <div className="trip-transfer" data-testid={`plan-transfer-${t.position}`}>
      <span aria-hidden>↓</span>
      <div>
        <b>
          {t.from_name} → {t.to_name}
        </b>{" "}
        · {MODE_ZH[t.mode] ?? "交通"}
        {t.number ? ` ${t.number}` : ""} · {t.depart_at.slice(5, 10).replace("-", "/")} {t.depart_at.slice(11, 16)} →{" "}
        {t.arrive_at.slice(11, 16)}
        {t.estimated && <span className="role-chip pending">估算</span>}
      </div>
      {onEdit && (
        <button type="button" className="tool" data-testid={`plan-transfer-edit-${t.position}`} onClick={onEdit}>
          改交通
        </button>
      )}
    </div>
  );
}

function hotelNote(stays: Stay[], date: string): string | null {
  const night = stays.find((s) => s.check_in <= date && date < s.check_out);
  if (night) return `今晚住：${night.name}${night.area ? `（${night.area}）` : ""}`;
  const out = stays.find((s) => s.check_out === date);
  return out ? `退房：${out.name}` : null;
}

export function PlanPage() {
  const { planId = "" } = useParams();
  const [detail, setDetail] = useState<PlanDetail | null>(null);
  const [version, setVersion] = useState<number | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState("");
  const [revising, setRevising] = useState(false);
  const [lastChange, setLastChange] = useState<RevisionChange | null>(null);
  const [attractions, setAttractions] = useState<AttractionRef[]>([]);
  const [attractionsLoaded, setAttractionsLoaded] = useState(false); // 封面要等地点库读完才知道用哪张大图
  const [toast, setToast] = useState<string | null>(null);

  const [compare, setCompare] = useState<{ from: number; to: number } | null>(null);
  const [cmpData, setCmpData] = useState<{ from: Itinerary; to: Itinerary } | null>(null);

  // 手动编辑（规格 M04）：编辑中的行程在 session 里，保存成版本前不写库
  const session = useEditSession(planId, setError);
  const [saveOpen, setSaveOpen] = useState(false);
  const [versionName, setVersionName] = useState("");
  const [editingTransfer, setEditingTransfer] = useState<number | null>(null); // 多城市：正在改第几次换城市的交通
  const [bulk, setBulk] = useState<BulkConfirm | null>(null);
  const [cardId, setCardId] = useState<string | null>(null);
  const { readOnly } = useReadOnly();
  const confirm = useConfirm();
  const wide = useMedia("(min-width: 1080px)");
  const rail = useMedia("(min-width: 1280px)"); // 目录放页面最左侧的竖栏（规格 07 P11）；窄一点就收成顶部横栏
  const [route, setRoute] = useState<RouteData | null>(null);
  const [weather, setWeather] = useState<WeatherData | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  // 文件附件（规格 M10 S5）：回形针要用数量，所以列表在页面这一层取；null = 没取到（后端没这个接口 / 断网），不显示相关入口
  const [attachments, setAttachments] = useState<AttachmentsData | null>(null);
  const [fileFilter, setFileFilter] = useState<FileFilter | null>(null);
  const [filesSignal, setFilesSignal] = useState(0);
  const [pdfBusy, setPdfBusy] = useState(false);
  const [imgBusy, setImgBusy] = useState(false);
  // 预算卡里「编辑行程，换成它」：先进入编辑，等编辑会话就绪后再把这一换应用上去（还没保存，可以放弃）
  const [pendingSwap, setPendingSwap] = useState<{ pid: string; alt: string } | null>(null);
  const days = useCollapsedDays(planId); // 收起的天：记在这台浏览器里，每份行程各记各的
  const [reservations, setReservations] = useState<ReservationItem[]>([]);
  const [mapOpen, setMapOpen] = useState(() => window.matchMedia?.("(max-width: 720px)").matches ?? false); // 手机上默认展开地图（规格 06 S26）
  const [shareOpen, setShareOpen] = useState(false);
  // 手机的「地图模式」底部抽屉（规格 06 S31 / S31b）：开关由地址里的 #map 决定，所以系统返回键只关抽屉
  const loc = useLocation();
  const navigate = useNavigate();
  const phone = useMedia("(max-width: 720px)");
  const sheetOpen = phone && loc.hash === "#map";
  const openSheet = () => navigate({ pathname: loc.pathname, search: loc.search, hash: "#map" }, { state: { sheet: true } });
  const wasSheet = useRef(false);
  useEffect(() => {
    if (wasSheet.current && !sheetOpen) setCardId(null); // 返回键关掉地图模式时，从抽屉里打开的地点卡一起关
    wasSheet.current = sheetOpen;
  }, [sheetOpen]);
  const closeSheet = () => {
    if ((loc.state as { sheet?: boolean } | null)?.sheet) navigate(-1); // 是我们推进去的那一条：等于按了一次后退
    else navigate({ pathname: loc.pathname, search: loc.search, hash: "" }, { replace: true }); // 直接打开的 #map：只去掉它
  };
  const [genOpen, setGenOpen] = useState(false); // 「生成详情」弹窗（质检、生成过程、说明）
  const [pagePerDay, setPagePerDay] = useState(true);

  useEffect(() => {
    if (pendingSwap && session.active && session.current) {
      void session.apply({ op: "swap", place_id: pendingSwap.pid, new_place_id: pendingSwap.alt });
      setPendingSwap(null);
    }
  }, [pendingSwap, session]);

  useEffect(() => {
    if (session.notes.length > 0) setToast(session.notes[0]);
  }, [session.notes]);

  // 拖拽时靠边自动滚动；并在拖拽结束时兜底清理
  useDragAutoScroll(session.active);

  // 地点库跟着这份行程的城市走（周边一日游的目的地不是巴黎）。行程还没读回来时，先按行程号里的城市名猜
  // （「heidelberg-20261212-ab12cd」→ heidelberg），免得多等一轮请求；读回来以后以后端给的 city_id 为准。
  const guessed = planId?.replace(/-\d{8}-[0-9a-f]+$/, "");
  const cityId = detail?.city_id ?? (guessed && /^[a-z][a-z-]*$/.test(guessed) ? guessed : undefined);
  // 一份多城市行程：把每座城市的地点并在一起，并记下每个地点在哪座城市（点开地点卡要按城市查）
  const cityKey = detail?.cities ? detail.cities.map((c) => c.city_id).join(",") : "";
  const [placeCity, setPlaceCity] = useState<Record<string, string>>({});
  const cityNames = useMemo(
    () => (detail?.cities ? Object.fromEntries(detail.cities.map((c) => [c.city_id, c.name_zh])) : undefined),
    [detail?.cities],
  );
  // 编辑中顶着导航条的那张编辑条有一定高度：点目录跳到某一天时，要多留出这块，不然那一天的标题会被它盖住
  const editingNow = session.active && session.current !== null;
  useEffect(() => {
    if (!editingNow) return;
    const root = document.documentElement;
    const before = root.style.scrollPaddingTop;
    const bar = document.querySelector<HTMLElement>(".edit-bar");
    const set = () => root.style.setProperty("scroll-padding-top", `calc(var(--nav-h) + ${(bar?.offsetHeight ?? 0) + 24}px)`);
    set();
    window.addEventListener("resize", set);
    return () => {
      window.removeEventListener("resize", set);
      root.style.scrollPaddingTop = before;
    };
  }, [editingNow]);
  useEffect(() => {
    if (cityKey) {
      Promise.all(cityKey.split(",").map((c) => api.places(c).then((ps) => ({ c, ps }))))
        .then((all) => {
          setAttractions(all.flatMap((x) => x.ps));
          setPlaceCity(Object.fromEntries(all.flatMap((x) => x.ps.map((p) => [p.place_id as string, x.c]))));
        })
        .catch(() => setAttractions([]))
        .finally(() => setAttractionsLoaded(true));
      return;
    }
    api
      .places(cityId)
      .then(setAttractions)
      .catch(() => setAttractions([]))
      .finally(() => setAttractionsLoaded(true));
  }, [cityId, cityKey]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(t);
  }, [toast]);

  const load = useCallback(
    async (v?: number) => {
      setError(null);
      try {
        setDetail(await api.getPlan(planId, v));
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      }
    },
    [planId],
  );

  useEffect(() => {
    void load(version);
  }, [load, version]);

  const shownVersion = detail?.version;
  useEffect(() => {
    if (shownVersion === undefined) return;
    let live = true;
    api
      .route(planId, shownVersion)
      .then((r) => live && setRoute(r))
      .catch(() => live && setRoute(null));
    return () => {
      live = false;
    };
  }, [planId, shownVersion]);

  // 天气：失败就当没有（不显示天气卡，也不报错）
  useEffect(() => {
    if (shownVersion === undefined) return;
    let live = true;
    api
      .weather(planId, shownVersion)
      .then((w) => live && setWeather(w.available ? w : null))
      .catch(() => live && setWeather(null));
    return () => {
      live = false;
    };
  }, [planId, shownVersion]);

  // 每日笔记：读不到就当没有（笔记框照常显示，空的）
  useEffect(() => {
    let live = true;
    api
      .notes(planId)
      .then((n) => live && setNotes(n))
      .catch(() => live && setNotes({}));
    return () => {
      live = false;
    };
  }, [planId, shownVersion]);

  useEffect(() => {
    let live = true;
    api
      .attachments(planId)
      .then((d) => live && setAttachments(d))
      .catch(() => live && setAttachments(null));
    return () => {
      live = false;
    };
  }, [planId, detail?.latest_version]); // 平移日期会把挂在某一天的文件跟着换日期，所以版本变了要重取

  // 预订状态：跟着当前显示的版本的购票清单走
  useEffect(() => {
    if (shownVersion === undefined) return;
    let live = true;
    api
      .reservations(planId, shownVersion)
      .then((r) => live && setReservations(r))
      .catch(() => live && setReservations([]));
    return () => {
      live = false;
    };
  }, [planId, shownVersion]);

  async function saveReservation(placeId: string, patch: ReservationPatch) {
    const before = reservations;
    // 勾选 / 取消勾选立刻有反应，不等服务器；失败了再退回去
    setReservations((cur) => cur.map((r) => (r.place_id === placeId ? { ...r, status: patch.status } : r)));
    try {
      const { version: newVersion, notes, ...row } = await api.saveReservation(planId, placeId, patch);
      setReservations((cur) =>
        cur.some((r) => r.place_id === placeId) ? cur.map((r) => (r.place_id === placeId ? row : r)) : [...cur, row],
      );
      if (newVersion !== null) {
        // 锁定 / 解锁了行程：切到新版本（页面和预订清单都会重新加载）
        setLastChange(null);
        setCompare(null);
        setVersion(newVersion);
        setToast(
          notes.length > 0
            ? notes.join(" ")
            : row.locked && row.entry_from
              ? `已锁定在 ${row.entry_from}，新建 v${newVersion}`
              : `已解锁，新建 v${newVersion}`,
        );
      } else {
        setToast(patch.status === "booked" ? "已记为已订" : "已更新预订记录");
      }
    } catch (err) {
      setReservations(before);
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  useEffect(() => {
    if (!compare) {
      setCmpData(null);
      return;
    }
    let live = true;
    Promise.all([api.getPlan(planId, compare.from), api.getPlan(planId, compare.to)])
      .then(([a, b]) => live && setCmpData({ from: a.itinerary, to: b.itinerary }))
      .catch((err) => live && setError(err instanceof ApiError ? err.message : String(err)));
    return () => {
      live = false;
    };
  }, [planId, compare]);

  async function submitRevision(e: React.FormEvent) {
    e.preventDefault();
    if (!feedback.trim()) return;
    setRevising(true);
    setError(null);
    try {
      const res = await api.revise(planId, feedback.trim());
      finishRevision(res);
    } catch (err) {
      // 一句话要删 3 处及以上：后端先不执行，让用户确认
      const body = err instanceof ApiError ? (err.detail as { detail?: BulkConfirm } | null) : null;
      if (err instanceof ApiError && err.status === 409 && body?.detail?.needs_confirm) {
        setBulk(body.detail);
      } else {
        setError(err instanceof ApiError ? err.message : String(err));
      }
    } finally {
      setRevising(false);
    }
  }

  function finishRevision(res: { version: number; change: RevisionChange | null }) {
    setFeedback("");
    setBulk(null);
    setLastChange(res.change);
    setCompare(null);
    setVersion(res.version);
    setToast(`已生成 v${res.version}`);
  }

  async function confirmBulk() {
    if (!bulk) return;
    setRevising(true);
    setError(null);
    try {
      finishRevision(await api.revise(planId, feedback.trim(), bulk.ops));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setRevising(false);
    }
  }

  async function rollback(n: number) {
    const yes = await confirm({
      title: `回滚到 v${n}？`,
      message: `以 v${n} 的内容新建一个版本。v${n} 和之后的版本都会保留，随时可以再切回去。`,
      confirmLabel: "回滚",
    });
    if (!yes) return;
    try {
      const res = await api.restoreVersion(planId, n);
      setLastChange(null);
      setCompare(null);
      setVersion(res.version);
      setToast(`已回滚：新建 v${res.version}（内容同 v${n}）`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function exitEdit() {
    if (session.dirty) {
      const yes = await confirm({
        title: "退出编辑？",
        message: "有未保存的修改，退出后会丢掉。",
        confirmLabel: "退出并丢掉修改",
        danger: true,
      });
      if (!yes) return;
    }
    session.exit();
    setSaveOpen(false);
  }

  async function doSave(e: React.FormEvent) {
    e.preventDefault();
    const res = await session.save(versionName.trim(), "");
    if (!res) return;
    session.exit();
    setSaveOpen(false);
    setVersionName("");
    setLastChange(null);
    setCompare(null);
    setVersion(res.version);
    setToast(`已保存为 v${res.version}`);
  }

  const cmpDays = useMemo(
    () => (cmpData ? diffItineraries(cmpData.from, cmpData.to) : []),
    [cmpData],
  );

  const destination = detail?.itinerary.trip.destination;
  useEffect(() => {
    if (destination) applyCity(destination);
    return () => applyCity(DEFAULT_CITY);
  }, [destination]);

  // 地点编号 -> 照片路径：全程地图悬停气泡用（规格 M02 S7b-4）；useMemo 保证引用稳定，地图不会无故重画
  const photoMap = useMemo(
    () =>
      Object.fromEntries(
        attractions.filter((a) => a.place_id && a.media?.image).map((a) => [a.place_id as string, a.media!.image!.url]),
      ),
    [attractions],
  );

  if (error && !detail) {
    return (
      <div className="wrap">
        <div className="banner err">{error}</div>
        <Link to="/">← 回到需求表单</Link>
      </div>
    );
  }
  if (!detail) return <Skeleton />;

  const { history, latest_version } = detail;
  const dayTrip = detail.day_trip_info ?? null; // 周边一日游：当天往返，没有住宿、预算、随行清单，标题只写城市和一个日期
  const filesOn = dayTrip ? null : attachments;
  const editing = session.active && session.current !== null;
  const it = editing ? session.current!.itinerary : detail.itinerary;
  const report = editing ? session.current!.report : detail.report;
  const shown = detail.version;
  const multi = !!detail.cities; // 一份多城市行程：编辑时每个操作只动被改的那座城市
  const canEdit = shown === latest_version && !compare && !!detail.itinerary.generation && !readOnly;
  const changedDays = new Set(
    editing
      ? session.touched
      : lastChange && shown === latest_version && !compare
        ? lastChange.changed_days
        : [],
  );
  const versions = Array.from({ length: latest_version }, (_, i) => i + 1);

  const inTrip = new Set(it.days.flatMap((d) => d.items.map((i) => i.place_id).filter(Boolean)));
  const dayEdit: DayEdit | undefined = editing
    ? {
        days: it.days.map((d, i) => ({
          date: d.date,
          label: `第 ${i + 1} 天 ${d.date.slice(5)}（周${weekday(d.date)}）`,
        })),
        addable: attractions.filter((a) => a.place_id && !inTrip.has(a.place_id)),
        ...(detail.cities
          ? {
              // 多城市：地点只能去它所在城市的日子；加地点只列这一天所在城市的
              daysFor: (pid: string) => {
                const city = detail.cities?.find((c) => c.city_id === placeCity[pid]);
                return it.days
                  .map((d, i) => ({
                    date: d.date,
                    label: `第 ${i + 1} 天 ${d.date.slice(5)}（周${weekday(d.date)}）`,
                  }))
                  .filter((d) => !city || (d.date >= city.start_date && d.date <= city.end_date));
              },
              addableFor: (day: Day) => {
                const here = Object.keys(day.theme_by_city ?? {}).length > 0 ? Object.keys(day.theme_by_city ?? {}) : [day.city_id];
                return attractions.filter(
                  (a) => a.place_id && !inTrip.has(a.place_id) && here.includes(placeCity[a.place_id as string]),
                );
              },
            }
          : {}),
        busy: session.busy,
        onMove: (pid, to) => void session.apply({ op: "move", place_id: pid, to_date: to }),
        onReorder: (date, pids) => void session.apply({ op: "reorder", date, place_ids: pids }),
        onLock: (pid, locked) => void session.apply({ op: "lock", place_id: pid, locked }),
        onRemove: (pid) => void session.apply({ op: "remove", place_id: pid }),
        onAdd: (pid, date) => void session.apply({ op: "add", place_id: pid, date }),
        loadAlternatives: session.alternatives,
        onSwap: (pid, newPid) => void session.apply({ op: "swap", place_id: pid, new_place_id: newPid }),
      }
    : undefined;

  /** 下载 PDF：先显示「生成中…」，生成好再下载；失败弹窗（规格 M08 S6b）。 */
  async function downloadPdf() {
    setPdfBusy(true);
    try {
      await api.downloadPdf(planId, shown, pagePerDay);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setPdfBusy(false);
    }
  }

  const todayDate = zonedNow(Date.now(), tzFor(it.trip.destination)).date; // 目的地的今天

  /** 导出长图（PNG）：菜单里的按钮显示「生成长图中…」，生成好自动下载（规格 M08 S6）。 */
  async function downloadImage() {
    setImgBusy(true);
    try {
      await api.downloadImage(planId, shown);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setImgBusy(false);
    }
  }

  const fileCounts = fileCountsOf(attachments);
  function openFiles(kind: FileFilter["kind"], ref: string | null) {
    setFileFilter({ kind, ref });
    setFilesSignal((n) => n + 1);
  }
  const fileDays = it.days.map((d, i) => ({ date: d.date, label: `第 ${i + 1} 天 · ${d.date.slice(5)}` }));
  const filePlaces = [...new Map(
    it.days.flatMap((d) => d.items).filter((i) => i.place_id).map((i) => [i.place_id as string, i.title]),
  ).entries()].map(([id, name]) => ({ id, name }));
  const fileReservations = reservations.map((r) => ({ id: r.place_id, name: r.title }));

  // 笔记导入（规格 M12 S3）：哪些地点来自笔记；这个版本的需求自带，所以回滚 / 复制 / 平移都在
  const noteSourceMap: Record<string, NoteSource> = Object.fromEntries(detail.note_sources.map((s) => [s.place_id, s]));

  // 行程页封面（规格 06 S15–S20）：只在「最新版本、不在编辑、不在对比」时出现；历史版本 / 编辑 / 对比仍用原来的封面卡
  const coverOn = !compare && !editing && shown === latest_version;
  const coverHero = coverOn ? pickTripHero(it, attractions) : null;
  const coverHighlights = coverOn ? buildHighlights(it, attractions, noteSourceMap, cityNames) : [];
  const coverHeroes = coverOn && cityNames ? pickCityHeroes(it, attractions, Object.keys(cityNames)) : [];

  // 质检结果不再展示给用户（规格 07 P10）：没问题时什么都不显示；有问题（错误 / 警告）才变成一条「提醒」，完整质检在「更多 → 生成详情」
  const reminders = (report?.findings ?? []).filter((f) => f.severity === "error" || f.severity === "warning");

  // 多城市：每座城市占第几天到第几天（换城市那天两座城市都算）；天归属于它的 city_id（换城市那天 = 出发的城市）
  const cityRanges = detail.cities
    ? detail.cities
        .map((c) => {
          const idx = it.days.flatMap((d, i) =>
            d.city_id === c.city_id || c.city_id in (d.theme_by_city ?? {}) ? [i] : [],
          );
          return idx.length
            ? { ...c, first: idx[0], last: idx[idx.length - 1], firstDate: it.days[idx[0]].date, own: it.days.flatMap((d, i) => (d.city_id === c.city_id ? [i] : [])) }
            : null;
        })
        .filter((c): c is NonNullable<typeof c> => !!c)
    : [];
  const md = (iso: string) => iso.slice(5).replace("-", "/"); // 2026-12-24 → 12/24
  const rangeText = (c: { first: number; last: number }) =>
    c.first === c.last ? `第 ${c.first + 1} 天` : `第 ${c.first + 1}–${c.last + 1} 天`;
  const jumpToDay = (date: string) => {
    days.expand(date);
    setTimeout(() => document.getElementById(`day-${date}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  };
  const dayNavItems: NavItem[] =
    cityRanges.length > 1
      ? cityRanges.flatMap((c) => [
          { id: `city-${c.city_id}`, label: `${c.name_zh} ${md(it.days[c.first].date)}–${md(it.days[c.last].date)}`, group: c.city_id, target: `day-${c.firstDate}`, onJump: () => days.expand(c.firstDate) },
          ...c.own.map((i) => {
            const d = it.days[i];
            const next = Object.keys(d.theme_by_city ?? {}).find((k) => k !== c.city_id);
            return {
              id: `day-${d.date}`,
              label: `第 ${i + 1} 天 · ${md(d.date)}${next && cityNames?.[next] ? ` → ${cityNames[next]}` : ""}`,
              onJump: () => days.expand(d.date),
            };
          }),
        ])
      : it.days.map((d, i) => ({ id: `day-${d.date}`, label: `第 ${i + 1} 天 · ${md(d.date)}`, onJump: () => days.expand(d.date) }));

  // 本页目录（规格 06-ui-design S10 / 07 P11 / P13）：列出这一页的栏目；编辑中没有住宿 / 预算 / 随行
  const navItems: NavItem[] = [
    { id: "sec-top", label: "概览" },
    { id: "sec-today", label: "今天" }, // 行程进行中才有（没有这张卡时目录自动不列）
    ...dayNavItems,
    { id: "sec-pending", label: "待核实" }, // 笔记里提到、地点库里没有的地点（没有时目录自动不列）
    { id: "sec-booking", label: "购票与风险" },
    ...(editing
      ? []
      : [
          ...(detail.day_trip_from ? [] : [{ id: "sec-stays", label: "住宿" }]), // 一日游当天往返，没有住宿
          { id: "sec-budget", label: "预算" },
          ...(dayTrip ? [] : [{ id: "sec-companion", label: "随行" }]),
        ]),
    ...(it.generation?.route ? [{ id: "sec-route", label: "路线" }] : []),
    ...(!compare && !editing && detail.culture?.length ? [{ id: "sec-culture", label: "作品" }] : []),
    { id: "sec-feedback", label: "修改意见" },
    { id: "sec-history", label: "版本历史" },
  ];
  const showAside = wide && !compare && !editing && !!route && route.days.some((d) => d.stops.length > 0);
  const aside =
    showAside && route ? (
      <aside className="plan-aside" aria-label="全程地图">
        <div className="card aside-card">
          <div className="aside-head">
            <h2>全程地图</h2>
            <span className="muted">
              {route.place_count} 个地点 · {route.total_km} 公里（直线累计）
            </span>
          </div>
          <RouteMap route={route} onOpenPlace={setCardId} photos={photoMap} />
        </div>
      </aside>
    ) : null;

  return (
    <>
      {coverOn && <TripCover
          it={it}
          hero={coverHero}
          heroes={coverHeroes}
          highlights={coverHighlights}
          route={route}
          loading={!attractionsLoaded}
          dayTrip={dayTrip}
          onOpenPlace={setCardId}
        />}
      <div className={`wrap wide${rail && !compare ? " has-toc" : ""}`}>
        <ErrorPopup message={error} onClose={() => setError(null)} />
        <div className={`plan-layout${aside ? " with-aside" : ""}${rail && !compare ? " with-toc" : ""}`}>
        {!compare && rail && <SectionNav items={navItems} variant="rail" />}
        <div className="plan-main">
        <section className="trip-head plan-hero" id="sec-top">
          {coverOn && (
            <div className="plan-title-compact" data-testid="plan-title">
              <Link to="/plans" className="crumb">
                ← 我的行程
              </Link>
              <h2>{dayTrip ? `${dayTrip.city} · 一日游` : `${it.trip.destination} · ${it.days.length} 天行程`}</h2>
              <div className="sub">
                {dayTrip
                  ? `${it.trip.date_range.start} · 从${dayTrip.origin}出发`
                  : `${it.trip.date_range.start} → ${it.trip.date_range.end}`}
              </div>
            </div>
          )}
          <div className="hero-cover" hidden={coverOn} style={coverStyle(it.generation?.theme_id)}>
            <span className="hero-cover-icon" aria-hidden>
              {themeLook(it.generation?.theme_id).icon}
            </span>
            <div className="hero-cover-body">
              <Link to="/plans" className="crumb">
                ← 我的行程
              </Link>
              <h1>{dayTrip ? `${dayTrip.city} · 一日游` : `${it.trip.destination} · ${it.days.length} 天行程`}</h1>
              <div className="sub">
                {dayTrip
                  ? `${it.trip.date_range.start} · 从${dayTrip.origin}出发`
                  : `${it.trip.date_range.start} → ${it.trip.date_range.end}`}
              </div>
              <div className="hero-dates" aria-hidden>
                <span className="big-dates">
                  {dayTrip
                    ? it.trip.date_range.start.slice(5).replace("-", ".")
                    : `${it.trip.date_range.start.slice(5).replace("-", ".")}—${it.trip.date_range.end.slice(5).replace("-", ".")}`}
                </span>
                {!dayTrip && (
                  <span className="night-count">
                    {it.days.length} 天 {Math.max(it.days.length - 1, 0)} 晚
                  </span>
                )}
              </div>
              <div className="chips on-cover">
                <span className="chip">{STYLE_ZH[it.trip.traveler_profile.style] ?? it.trip.traveler_profile.style}</span>
                {!dayTrip && <span className="chip">{BUDGET_ZH[it.trip.traveler_profile.budget] ?? it.trip.traveler_profile.budget}</span>}
                {!dayTrip &&
                  it.trip.traveler_profile.interests.map((x, _i, all) => (
                    <span className="chip" key={x}>
                      {all.length === 1 && it.generation?.theme_name ? it.generation.theme_name : x}
                    </span>
                  ))}
              </div>
            </div>
          </div>
          {cityRanges.length > 1 && !compare && (
            <div className="city-strip" data-testid="city-strip">
              {cityRanges.map((c) => (
                <button key={c.city_id} type="button" data-testid="city-strip-item" onClick={() => jumpToDay(c.firstDate)}>
                  <b>{c.name_zh}</b>
                  <span>
                    {rangeText(c)} · {c.start_date.slice(5).replace("-", "/")}–{c.end_date.slice(5).replace("-", "/")}
                  </span>
                </button>
              ))}
            </div>
          )}
          {!compare && (
            <div className="stat-tiles" data-testid="stat-tiles">
              <div className="stat-tile">
                <b>{it.days.length}</b>
                <span>天</span>
              </div>
              <div className="stat-tile">
                <b>{route ? route.place_count : "—"}</b>
                <span>个地点</span>
              </div>
              <div className="stat-tile">
                <b>{route ? route.total_km : "—"}</b>
                <span>公里（直线累计）</span>
              </div>
              <div className="stat-tile">
                <b>{route ? ticketTotal(route.ticket_eur, route.ticket_currency) : "—"}</b>
                <span>门票合计</span>
              </div>
            </div>
          )}
          <div className="toolbar hero-actions">
            {versions.map((v) => (
              <button
                key={v}
                className={`vbtn${v === shown && !compare ? " active" : ""}`}
                disabled={editing}
                onClick={() => {
                  setCompare(null);
                  setVersion(v);
                }}
              >
                v{v}
              </button>
            ))}
            {latest_version >= 2 && (
              <button
                className={`vbtn${compare ? " active" : ""}`}
                disabled={editing}
                onClick={() =>
                  setCompare(compare ? null : { from: latest_version - 1, to: latest_version })
                }
              >
                对比
              </button>
            )}
            <span className="spacer" />
            {canEdit && !editing && (
              <button
                className="btn-primary edit-btn"
                onClick={() => session.start({ itinerary: detail.itinerary, report: detail.report })}
              >
                <IconEdit size={16} />
                编辑行程
              </button>
            )}
            {!canEdit && detail.itinerary.generation && !compare && (
              <span className="muted">历史版本只读，切到最新版才能编辑</span>
            )}
            <button
              className="btn-ghost share-btn"
              onClick={() => setShareOpen((v) => !v)}
              aria-expanded={shareOpen}
              disabled={readOnly}
              title={readOnly ? "离线时不能分享" : undefined}
            >
              <IconShare size={16} />
              分享
            </button>
            <button className="btn-ghost pdf-link" type="button" disabled={pdfBusy} aria-busy={pdfBusy} onClick={() => void downloadPdf()}>
              <IconDownload size={16} />
              {pdfBusy ? "生成中…" : "下载 PDF"}
            </button>
            {!compare && (
              <ManageBar
                planId={planId}
                startDate={it.trip.date_range.start}
                endDate={it.trip.date_range.end}
                canShift={shown === latest_version}
                disabled={editing || readOnly}
                archived={detail.archived}
                extra={
                  <>
                    <button
                      className="menu-item"
                      onClick={() => {
                        navigator.clipboard?.writeText(location.href).then(() => setToast("链接已复制"));
                      }}
                    >
                      <IconLink size={16} />
                      复制链接
                    </button>
                    <a className="menu-item" href={api.icsUrl(planId, shown)}>
                      <IconCalendar size={16} />
                      导出日历
                    </a>
                    <button className="menu-item" type="button" disabled={imgBusy} aria-busy={imgBusy} onClick={() => void downloadImage()}>
                      <IconImage size={16} />
                      {imgBusy ? "生成长图中…" : "导出长图"}
                    </button>
                    <a className="menu-item" href={api.kmlUrl(planId, shown)}>
                      <IconMap size={16} />
                      导出 KML
                    </a>
                    <button className="menu-item" type="button" data-testid="open-gen-details" onClick={() => setGenOpen(true)}>
                      <IconInfo size={16} />
                      生成详情
                    </button>
                    <label className="menu-item check" title="关掉后各天紧接着排，省纸">
                      <input type="checkbox" checked={pagePerDay} onChange={(e) => setPagePerDay(e.target.checked)} />
                      PDF 每天一页
                      {pagePerDay && <IconCheck size={14} />}
                    </label>
                  </>
                }
                onVersion={(v) => {
                  setCompare(null);
                  setLastChange(null);
                  setVersion(v);
                }}
                onReload={() => void load(version)}
                onToast={setToast}
                onError={setError}
              />
            )}
          </div>
          <div className="hero-info">
          {detail.copied_from && !compare && (
            <div className="muted" style={{ marginTop: 8 }}>
              副本 · 复制自 <Link to={`/plans/${detail.copied_from}`}>{detail.copied_from}</Link>
            </div>
          )}
          {!compare && detail.route_status?.deviated && (
            <div className="banner warn" data-testid="route-deviated" role="status">
              你改过行程，已经偏离了路线「{detail.route_status.name_zh}」：每天的章节标题和导览是按原来的安排写的，可能和现在的顺序对不上。
            </div>
          )}
          {!compare && reminders.length > 0 && (
            <details className="plan-reminders" data-testid="plan-reminders">
              <summary>有 {reminders.length} 条提醒</summary>
              <ul>
                {reminders.map((f, i) => (
                  <li key={i}>
                    {f.day ? `${f.day.slice(5)} ` : ""}
                    {f.message}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {compare && (
            <div className="cmp-pick" style={{ marginTop: 14 }}>
              <label>
                从{" "}
                <select
                  value={compare.from}
                  onChange={(e) => setCompare({ ...compare, from: Number(e.target.value) })}
                >
                  {versions.map((v) => (
                    <option key={v} value={v}>
                      v{v}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                到{" "}
                <select
                  value={compare.to}
                  onChange={(e) => setCompare({ ...compare, to: Number(e.target.value) })}
                >
                  {versions.map((v) => (
                    <option key={v} value={v}>
                      v{v}
                    </option>
                  ))}
                </select>
              </label>
              {cmpData && (
                <span className="muted">
                  {(() => {
                    const s = diffStats(cmpDays);
                    return `${s.changedDays} 天变化 · ＋${s.added} －${s.removed}`;
                  })()}
                </span>
              )}
            </div>
          )}
          </div>
        </section>

        {detail.archived && (
          <div className="banner warn">这份行程已归档（不在「我的行程」默认列表里）。内容都在，可以在右上角「更多」菜单里点「恢复」。</div>
        )}
        {!compare && !editing && shown === latest_version && !detail.archived && (
          <div id="sec-today">
            <TodayCard itinerary={it} attractions={attractions} stories={detail.stories} />
          </div>
        )}
        {!compare && !rail && <SectionNav items={navItems} />}
        {shareOpen && !readOnly && <SharePanel planId={planId} dayTrip={!!detail?.day_trip_info} onToast={setToast} onClose={() => setShareOpen(false)} />}

        {!wide && !compare && !editing && route && route.days.some((d) => d.stops.length > 0) && (
          <details className="card routecard" open={mapOpen} onToggle={(e) => setMapOpen(e.currentTarget.open)}>
            <summary>
              全程地图 <span className="muted">· {route.place_count} 个地点 · {route.total_km} 公里（直线累计）</span>
            </summary>
            {mapOpen && <RouteMap route={route} onOpenPlace={setCardId} photos={photoMap} />}
          </details>
        )}

        {editing && (
          <div className="card edit-bar">
            <div className="edit-row">
              <b>编辑中</b>
              <span className="muted">
                {session.dirty ? `${session.editCount} 处未保存的修改` : "还没有修改"}
              </span>
              <span className="spacer" />
              <button className="tool" disabled={!session.canUndo || session.busy} onClick={session.undo}>
                ↶ 撤销
              </button>
              <button className="tool" disabled={!session.canRedo || session.busy} onClick={session.redo}>
                ↷ 重做
              </button>
              <button
                className="tool"
                disabled={session.busy}
                onClick={async () => {
                  const yes = await confirm({
                    title: "重新排程？",
                    message: "重新排程会重新选点、重新排顺序，只有已锁定的点保持原来的日期、位置和时间。",
                    confirmLabel: "重新排程",
                  });
                  if (yes) void session.apply({ op: "reschedule" });
                }}
              >
                重新排程（保留锁定）
              </button>
              <button
                className="primary"
                disabled={!session.dirty || session.busy}
                onClick={() => setSaveOpen((v) => !v)}
              >
                保存为新版本…
              </button>
              <button className="tool" onClick={exitEdit}>
                退出编辑
              </button>
            </div>
            {saveOpen && (
              <form className="save-form" onSubmit={doSave}>
                <input
                  value={versionName}
                  onChange={(e) => setVersionName(e.target.value)}
                  maxLength={60}
                  placeholder="给这个版本起个名字（选填），比如：朋友确认版"
                  aria-label="版本名字"
                />
                <button className="primary" type="submit" disabled={session.busy}>
                  {session.busy ? "保存中…" : "保存"}
                </button>
                <button className="tool" type="button" onClick={() => setSaveOpen(false)}>
                  取消
                </button>
              </form>
            )}
            <div className="muted edit-hint">
              拖动地点可以调顺序，拖到另一天就移过去；每个地点下面也有「移到…」「换一个」「锁定」「删除」。
              改完点「保存为新版本」才会成为一个版本——刷新页面会丢掉没保存的修改。
            </div>
          </div>
        )}

        {compare ? (
          compare.from === compare.to ? (
            <p className="center">选两个不同的版本来对比</p>
          ) : cmpData ? (
            <CompareView days={cmpDays} fromV={compare.from} toV={compare.to} />
          ) : (
            <Skeleton />
          )
        ) : (
          <>
            <DayTools
              note={weather?.note}
              onExpandAll={days.expandAll}
              onCollapseAll={() => days.collapseAll(it.days.map((x) => x.date))}
            />
            {it.days.map((d, i) => (
              <Fragment key={d.date}>
              {transferBefore(detail, d.date).map((t) => (
                <PlanTransferCard
                  key={t.position}
                  t={t}
                  onEdit={canEdit && !editing && !compare ? () => setEditingTransfer(t.position) : undefined}
                />
              ))}
              <DayTimeline
                day={d}
                index={i}
                changed={changedDays.has(d.date)}
                attractions={attractions}
                reasons={it.generation?.day_reasons[d.date] ?? []}
                edit={dayEdit}
                onOpenPlace={setCardId}
                navUrls={editing ? undefined : route?.days[i]?.google_urls}
                amapUrls={editing ? undefined : route?.days[i]?.amap_urls}
                commuteCity={compare || readOnly ? undefined : (d.city_id ?? cityId)}
                hotelNote={hotelNote(detail.stays, d.date)}
                color={route?.days[i]?.color}
                weather={compare ? undefined : weather?.days.find((w) => w.date === d.date)}
                onStartEdit={
                  canEdit ? () => session.start({ itinerary: detail.itinerary, report: detail.report }) : undefined
                }
                collapsed={days.collapsed.has(d.date)}
                onToggle={() => days.toggle(d.date)}
                isToday={!compare && d.date === todayDate}
                fileCount={compare || editing ? 0 : (fileCounts.day[d.date] ?? 0)}
                stories={compare ? undefined : detail.stories}
                cityNames={cityNames}
                onOpenFiles={compare || editing || !filesOn ? undefined : () => openFiles("day", d.date)}
                noteSources={compare ? undefined : noteSourceMap}
                notes={compare || editing ? undefined : notes}
                onSaveNote={readOnly ? undefined : async (ref, text) => setNotes(await api.putNote(planId, ref, text))}
              />
              </Fragment>
            ))}

            {!compare && !editing && detail.note_pending.length > 0 && (
              <div id="sec-pending">
                <NotePendingCard pending={detail.note_pending} />
              </div>
            )}

            <div id="sec-booking">
              <BookingAndRisks
                it={it}
                attractions={attractions}
                reservations={reservations}
                reservationFiles={filesOn ? fileCounts.reservation : undefined}
                onOpenFiles={filesOn ? (placeId) => openFiles("reservation", placeId) : undefined}
                onSaveReservation={saveReservation}
                reservationsDisabled={readOnly || editing}
              />
            </div>
          </>
        )}

        {!compare && !editing && !detail.day_trip_from && (
          <div id="sec-stays">
          <StaysCard
            planId={planId}
            stays={detail.stays}
            tripStart={it.trip.date_range.start}
            tripEnd={it.trip.date_range.end}
            disabled={readOnly || shown !== latest_version}
            onSaved={(res) => {
              setLastChange(null);
              setVersion(res.version);
              setToast(res.notes.length > 0 ? res.notes.join(" ") : `住宿已保存，新建 v${res.version}`);
            }}
            onError={setError}
          />
          </div>
        )}
        {!compare && !editing && (
          <div id="sec-budget">
          <BudgetCard
            planId={planId}
            version={shownVersion}
            dayTrip={!!detail.day_trip_from}
            disabled={readOnly}
            onSwap={(pid, alt) => {
              if (!canEdit) {
                setToast("历史版本只读，切到最新版才能换");
                return;
              }
              setPendingSwap({ pid, alt });
              session.start({ itinerary: detail.itinerary, report: detail.report });
            }}
            onError={setError}
          />
          </div>
        )}
        {!compare && !editing && !dayTrip && (
          <div id="sec-dishes">
            <DishesCard cityIds={detail.cities ? detail.cities.map((c) => c.city_id) : cityId ? [cityId] : []} />
          </div>
        )}
        {!compare && !editing && !dayTrip && (
          <div id="sec-companion">
            <CompanionCard
              planId={planId}
              disabled={readOnly}
              onError={setError}
              files={attachments}
              onFiles={setAttachments}
              fileDays={fileDays}
              filePlaces={filePlaces}
              fileReservations={fileReservations}
              fileFilter={fileFilter}
              onFileFilter={setFileFilter}
              filesSignal={filesSignal}
            />
          </div>
        )}

        {!compare && !editing && it.generation?.route && (
          <section className="card route-reading-card" id="sec-route" data-testid="route-reading">
            <h2>路线：{it.generation.route.name_zh}</h2>
            <p className="muted">{it.generation.route.thesis}</p>
            {it.generation.route.reading.length > 0 && (
              <>
                <h3 style={{ fontSize: 14, margin: "10px 0 4px" }}>延伸阅读 / 观影</h3>
                <ul>
                  {it.generation.route.reading.map((r) => (
                    <li key={r.title}>
                      <a href={r.source_url} target="_blank" rel="noreferrer">
                        {r.title}
                      </a>{" "}
                      · {r.creator}，{r.year}
                      <span className="muted"> · {r.note}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {it.generation.route.notes.length > 0 && (
              <ul className="muted" style={{ fontSize: 12.5 }}>
                {it.generation.route.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            )}
          </section>
        )}

        {!compare && !editing && !!detail.culture?.length && (
          <details className="card culture-recap" id="sec-culture" data-testid="culture-recap">
            <summary>
              <h2>这趟旅行里的作品 <span className="muted">· {detail.culture.length} 部</span></h2>
            </summary>
            <ul className="recap-list">
              {detail.culture.map((w) => (
                <li key={w.id} data-testid={`recap-work-${w.id}`}>
                  <b>
                    {w.kind_zh}《{w.title_zh ?? w.title}》
                  </b>
                  {w.title_zh && <span className="muted"> {w.title}</span>}
                  <span className="muted">
                    {" "}
                    · {w.creator}，{w.year}
                  </span>
                  {w.summary && <div>{w.summary}</div>}
                  <div className="recap-visited">
                    你会经过：{w.visited.map((v) => v.name_zh).join("、")}
                    <a href={w.source.url} target="_blank" rel="noreferrer" className="muted">
                      {" "}
                      · 来源 ↗
                    </a>
                  </div>
                </li>
              ))}
            </ul>
            <Link to="/culture" data-testid="culture-more">
              更多作品 ›
            </Link>
          </details>
        )}

        <div className="card" id="sec-feedback">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>提修改意见</h2>
          {bulk && (
            <div className="banner warn bulk-confirm">
              <b>{bulk.message}</b>
              <ul>
                {bulk.preview.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
              <div style={{ display: "flex", gap: 10 }}>
                <button className="primary" type="button" disabled={revising} onClick={() => void confirmBulk()}>
                  {revising ? "执行中…" : "确认执行"}
                </button>
                <button className="tool" type="button" onClick={() => setBulk(null)}>
                  取消
                </button>
              </div>
            </div>
          )}
          {lastChange && (
            <div className="banner warn">
              上次改动：{lastChange.summary}
              {(lastChange.skipped ?? []).map((s, i) => (
                <div key={`s${i}`} className="muted">
                  没做成：{s}
                </div>
              ))}
              {lastChange.added.map((a, i) => (
                <div key={`a${i}`}>＋ {a}</div>
              ))}
              {lastChange.removed.map((a, i) => (
                <div key={`r${i}`}>－ {a}</div>
              ))}
            </div>
          )}
          {readOnly && <p className="muted">离线或连不上后端时只能查看，不能修改。</p>}
          {multi && <p className="muted">多城市行程的一句话修改还没做；可以在上面直接编辑，或改住宿和交通。</p>}
          <form onSubmit={submitRevision} hidden={readOnly || multi}>
            <textarea
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
              placeholder="例：不想早起，第 2 天上午加一个咖啡馆；把卢浮宫挪到下午"
            />
            <div style={{ marginTop: 10, display: "flex", gap: 10, alignItems: "center" }}>
              <button className="primary" type="submit" disabled={revising || !feedback.trim()}>
                {revising ? "修订中…" : "生成新版本"}
              </button>
              {revising && <span className="muted">本地模型改一版约 10–40 秒</span>}
            </div>
          </form>
        </div>

        <div className="card history" id="sec-history">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>版本历史</h2>
          {history.map((h) => (
            <div className="hrow" key={h.version}>
              <div className="hline">
                <span className="vtag">v{h.version}</span>
                <span>{TRIGGER_ZH[h.trigger] ?? h.trigger}</span>
                {h.name && <span className="vname">{h.name}</span>}
                {h.trigger !== "initial" && h.note && <span className="note">“{h.note}”</span>}
                <span className="spacer" />
                {h.trigger !== "initial" && (
                  <span className="delta">
                    error {h.errors_before}→{h.errors_after}
                  </span>
                )}
                {h.version !== latest_version && !editing && !readOnly && (
                  <button className="tool" type="button" onClick={() => void rollback(h.version)}>
                    回滚到这版
                  </button>
                )}
              </div>
              {h.change_summary && (
                <div className="hsummary" data-testid="history-summary">
                  {h.change_summary.split("\n").map((line, i) => (
                    <div key={i}>{i === 0 ? `改动：${line}` : line}</div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
        </div>
        {aside}
        </div>
      </div>
      {genOpen && <GenerationDetails planId={planId} it={it} report={report} onClose={() => setGenOpen(false)} />}
      {dayEdit && (
        // 拖拽时右侧浮出一排「第 N 天」：直接拖到那一天的名字上就移过去，不用滚动页面
        <div className="drag-dock" aria-hidden>
          <div className="dock-title">拖到这里，移到那一天</div>
          {it.days.map((d, i) => (
            <div
              key={d.date}
              className="dock-day"
              data-date={d.date}
              onDragEnter={(e) => e.preventDefault()}
              onDragOver={(e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                e.currentTarget.classList.add("hot");
              }}
              onDragLeave={(e) => e.currentTarget.classList.remove("hot")}
              onDrop={(e) => {
                e.preventDefault();
                e.currentTarget.classList.remove("hot");
                document.body.classList.remove("is-dragging");
                try {
                  const data = JSON.parse(e.dataTransfer.getData("text/plain")) as { pid?: string; date?: string };
                  if (data.pid && data.date !== d.date) dayEdit.onMove(data.pid, d.date);
                } catch {
                  /* 不是我们的拖拽数据，忽略 */
                }
              }}
            >
              第 {i + 1} 天 · {d.date.slice(5)}
            </div>
          ))}
        </div>
      )}
      {toast && <Toast message={toast} />}
      {editingTransfer !== null && detail.cities && detail.transfers?.find((x) => x.position === editingTransfer) && (
        <TransferDialog
          value={detail.transfers.find((x) => x.position === editingTransfer)!}
          from={detail.cities[editingTransfer].name_zh}
          to={detail.cities[editingTransfer + 1].name_zh}
          fromDate={detail.cities[editingTransfer].end_date}
          toDate={detail.cities[editingTransfer + 1].start_date}
          onClose={() => setEditingTransfer(null)}
          onSave={async (body) => {
            try {
              const res = await api.savePlanTransfer(planId, editingTransfer, body);
              setEditingTransfer(null);
              setLastChange(null);
              setVersion(res.version);
              setToast(`交通已保存，按新时间重新排程，新建 v${res.version}`);
            } catch (e) {
              setError(e instanceof ApiError ? e.message : String(e));
            }
          }}
        />
      )}
      {!compare && !editing && route && route.days.some((x) => x.stops.length > 0) && (
        <>
          {!sheetOpen && (
            <button type="button" className="sheet-fab" data-testid="open-map-sheet" aria-label="地图与行程" onClick={openSheet}>
              <IconMap size={20} />
              地图
            </button>
          )}
          {sheetOpen && (
            <MapSheet
              it={it}
              route={route}
              startDay={Math.max(0, it.days.findIndex((x) => x.date === todayDate))}
              onClose={closeSheet}
              onOpenPlace={setCardId}
            />
          )}
        </>
      )}
      {cardId && (
        <PlaceCard
          placeId={cardId}
          city={placeCity[cardId] ?? detail?.city_id}
          note={noteSourceMap[cardId]}
          files={fileCounts.place[cardId] ?? 0}
          onOpenFiles={
            filesOn
              ? () => {
                  setCardId(null);
                  openFiles("place", cardId);
                }
              : undefined
          }
          onClose={() => setCardId(null)}
        />
      )}
    </>
  );
}
