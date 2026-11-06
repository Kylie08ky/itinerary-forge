import { useEffect, useRef, useState } from "react";
import { DayMap, matchDayPoints } from "./components/DayMap";
import { WeatherChip } from "./components/WeatherChip";
import { IconChevron } from "./icons";
import type { DiffDay } from "./diff";
import { api, mediaSrc } from "./api";
import { ErrorPopup } from "./confirm";
import { COMMUTE_REASON, CommuteLine } from "./components/CommuteLine";
import { legUrl } from "./nav";
import { Paperclip } from "./components/FilesTab";
import { Stories } from "./components/Stories";
import { ReservationControls, type ReservationPatch } from "./components/ReservationControls";
import type {
  Alternative,
  AttractionRef,
  CommuteLeg,
  CultureStory,
  Day,
  Itinerary,
  ItineraryItem,
  ReservationItem,
  ValidationReport,
  WeatherDay,
} from "./types";

export const TYPE_ZH: Record<string, string> = {
  sight: "观景",
  museum: "博物馆",
  food: "餐饮",
  meal: "用餐",
  logistics: "交通",
  activity: "活动",
  rest: "休息",
};
export const TYPE_ICON: Record<string, string> = {
  sight: "🌤️",
  museum: "🏛️",
  food: "🍽️",
  meal: "🥖",
  logistics: "🚕",
  activity: "🎫",
  rest: "☕",
};
export const STYLE_ZH: Record<string, string> = {
  relaxed: "轻松节奏",
  balanced: "均衡节奏",
  intense: "紧凑节奏",
};
export const BUDGET_ZH: Record<string, string> = {
  low: "低预算",
  medium: "中等预算",
  high: "高预算",
};
const WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"];

export function weekday(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  return WEEKDAY[(d.getDay() + 6) % 7];
}

export function fmtDuration(min: number): string {
  if (min < 60) return `${min} 分钟`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${h} 小时${m ? ` ${m} 分钟` : ""}`;
}

export function QaBadge({ report }: { report: ValidationReport }) {
  const errors = report.findings.filter((f) => f.severity === "error");
  const warnings = report.findings.filter((f) => f.severity === "warning");
  const clean = errors.length === 0;
  return (
    <div>
      <span className={`qa ${clean ? "clean" : "dirty"}`}>
        质检：{errors.length} error / {warnings.length} warning
        {clean ? " · 可交付" : " · 待人工处理"}
      </span>
      {report.findings.length > 0 && (
        <ul className="findings">
          {report.findings.map((f, i) => (
            <li key={i}>
              <span className={`sev ${f.severity}`}>{f.severity === "error" ? "✗" : "!"}</span>
              {f.day ? `${f.day} ` : ""}
              {f.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Toast({ message }: { message: string }) {
  return (
    <div className="toast" role="status">
      {message}
    </div>
  );
}

export function Skeleton() {
  return (
    <div className="wrap" aria-busy="true" aria-label="载入中">
      <div className="sk sk-card" style={{ height: 150 }} />
      <div className="sk sk-card" style={{ height: 220 }} />
      <div className="sk sk-card" style={{ height: 220 }} />
    </div>
  );
}

const STATUS_MARK: Record<string, string> = { added: "＋", removed: "－", same: "" };

export function CompareView({ days, fromV, toV }: { days: DiffDay[]; fromV: number; toV: number }) {
  return (
    <div>
      <p className="muted" style={{ margin: "0 4px 10px" }}>
        v{fromV} → v{toV}：<span className="diff-add">＋新增</span> ·{" "}
        <span className="diff-del">－移除</span> · 其余未变
      </p>
      {days.map((d, i) => (
        <div className={`day${d.changed ? " changed" : ""}`} key={d.date}>
          <div className="day-head">
            <span className="dnum">第 {i + 1} 天</span>
            <span className="date">
              {d.date}（周{weekday(d.date)}）
            </span>
            <h3>{d.theme}</h3>
          </div>
          <div className="timeline">
            {d.rows.map((r, j) => (
              <div className={`item type-${r.item.type} diff-${r.status}`} key={j}>
                <div className="rail">
                  <span className="dot">{STATUS_MARK[r.status] || TYPE_ICON[r.item.type] || "•"}</span>
                </div>
                <div className="t">{r.item.time}</div>
                <div>
                  <h4>{r.item.title}</h4>
                  <div className="meta">
                    <b>{r.item.location}</b> · {fmtDuration(r.item.duration_minutes)}
                    {r.item.transport ? ` · ${r.item.transport}` : ""}
                  </div>
                  <div className="tags">
                    <span className="tag">{TYPE_ZH[r.item.type] ?? r.item.type}</span>
                    {r.item.booking_required && <span className="tag book">需预约</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export function matchFor(item: Pick<ItineraryItem, "place_id" | "title">, catalog: AttractionRef[]): AttractionRef | null {
  if (item.place_id) {
    const byId = catalog.find((x) => x.place_id === item.place_id);
    if (byId) return byId;
  }
  const clean = item.title.replace(/[（(].*/, "").trim();
  if (!clean) return null;
  return (
    catalog.find((x) => x.name_zh === clean || x.name_zh.includes(clean) || clean.includes(x.name_zh)) ?? null
  );
}

export function VerifiedNote({ status, at }: { status?: string | null; at?: string | null }) {
  if (!status || !at) return null;
  return (
    <span className="verified" title="由程序自动请求官网并核对">
      {status === "ok" ? `官网链接已于 ${at} 自动核对` : `官网有访问限制，程序没能自动打开核对（${at}），出发前请自己打开确认`}
    </span>
  );
}

/** 编辑模式下一天时间轴要用的数据和回调（规格 M04）。不传就是只读。 */
export interface DayEdit {
  days: { date: string; label: string }[]; // 「移到…」的目标
  daysFor?: (pid: string) => { date: string; label: string }[]; // 多城市行程：这个地点只能去它所在城市的日子
  addable: AttractionRef[]; // 地点库里行程中还没有的点
  addableFor?: (day: Day) => AttractionRef[]; // 多城市行程：这一天所在城市（换城市那天两座）的地点
  busy: boolean;
  onMove: (pid: string, toDate: string) => void;
  onReorder: (date: string, pids: string[]) => void;
  onLock: (pid: string, locked: boolean) => void;
  onRemove: (pid: string) => void;
  onAdd: (pid: string, date: string) => void;
  loadAlternatives: (pid: string, indoorOnly?: boolean) => Promise<Alternative[]>;
  onSwap: (pid: string, newPid: string) => void;
}

/** 日程上方：天气数据来源说明 + 「全部展开 / 全部收起」（规格 06-ui-design S9）。 */
export function DayTools({
  note,
  onExpandAll,
  onCollapseAll,
}: {
  note?: string;
  onExpandAll: () => void;
  onCollapseAll: () => void;
}) {
  return (
    <div className="day-tools">
      <span className="muted">{note ?? ""}</span>
      <span className="spacer" />
      <button type="button" className="tool" onClick={onExpandAll}>
        全部展开
      </button>
      <button type="button" className="tool" onClick={onCollapseAll}>
        全部收起
      </button>
    </div>
  );
}

/** 一条行程安排的备注对象（和后端 companion.note_ref 一致）：地点库的点按地点记，其余按「日期|名称」。 */
export function noteRef(date: string, it: { place_id?: string | null; title: string }): string {
  return it.place_id || `${date}|${it.title}`;
}

/**
 * 一条安排的备注（规格 M10 S4）。有 onSave：没有备注时只显示「📝 备注」按钮，点开写；有备注就直接显示输入框；
 * 离开输入框自动保存，清空 = 删除。没有 onSave（分享页 / 离线）：只读显示。
 */
function ItemNote({
  label,
  value,
  onSave,
  hint = "写点什么：想吃的店、要带的东西、提醒自己的事…",
}: {
  label: string;
  value: string;
  onSave?: (text: string) => Promise<void>;
  hint?: string;
}) {
  const [text, setText] = useState(value);
  const [editing, setEditing] = useState(false);
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => setText(value), [value]);
  useEffect(() => {
    if (editing) box.current?.focus();
  }, [editing]);

  if (!onSave) {
    return value ? (
      <div className="item-note-view" data-testid="item-note">
        📝 {value}
      </div>
    ) : null;
  }

  async function commit() {
    if (text.trim() === value.trim()) {
      if (!text.trim() && state !== "saved") setEditing(false); // 点开又什么都没写：收回去
      return;
    }
    setState("saving");
    try {
      await onSave!(text);
      setState("saved");
    } catch {
      setState("failed");
    }
  }

  if (!editing && !value && state === "idle") {
    return (
      <button type="button" className="tool note-add" aria-label={`给「${label}」写备注`} onClick={() => setEditing(true)}>
        📝 备注
      </button>
    );
  }
  return (
    <div className="item-note-box" data-testid="item-note">
      <textarea
        ref={box}
        rows={2}
        maxLength={2000}
        value={text}
        placeholder={hint}
        aria-label={`备注：${label}`}
        onChange={(e) => {
          setText(e.target.value);
          setState("idle");
        }}
        onBlur={() => void commit()}
      />
      <span className={`note-state ${state}`} data-testid="note-state" role="status">
        {state === "saving" ? "保存中…" : state === "saved" ? "已保存" : state === "failed" ? "保存失败，再试一次" : ""}
      </span>
    </div>
  );
}

/** 一天顶部的小标题只写「第 1 章」：章节名已经是这一天的标题，不重复；自由日整行就是标题，不再画一遍。 */
export function chapterOnly(chapter: string | null | undefined, theme: string): string {
  if (!chapter || chapter === theme) return "";
  return chapter.match(/^第 [\d–-]+ 章/)?.[0] ?? chapter;
}

export function DayTimeline({
  day,
  index,
  changed,
  attractions = [],
  reasons = [],
  edit,
  onOpenPlace,
  navUrls,
  amapUrls,
  commuteCity,
  hotelNote,
  color,
  weather,
  onStartEdit,
  collapsed = false,
  onToggle,
  notes,
  onSaveNote,
  isToday = false,
  photos = true,
  noteSources,
  fileCount = 0,
  onOpenFiles,
  stories,
  cityNames,
}: {
  day: Day;
  index: number;
  changed: boolean;
  attractions?: AttractionRef[];
  reasons?: string[];
  edit?: DayEdit;
  onOpenPlace?: (pid: string) => void;
  navUrls?: string[]; // 这一天的 Google 地图路线链接（后端按核对过的坐标生成；一天点太多时拆成几段）
  amapUrls?: string[]; // 境内的一天：高德里看当天所有地点的链接（高德网页链接不支持多点步行路线）
  commuteCity?: string; // 境内的一天：城市 id；传了就有「查看真实通勤时间」（高德，点了才查）
  hotelNote?: string | null; // 「今晚住：xx」/「退房：xx」（规格 M06 S5）
  color?: string; // 这一天在全程地图上的颜色：「第 N 天」徽标同色，一眼对得上（规格 06-ui-design）
  weather?: WeatherDay; // 这一天的天气；预报降雨概率 ≥ 60% 时出雨天提示和室外点标签（规格 M10 S1c）
  onStartEdit?: () => void; // 雨天提示里的「编辑行程，换成室内备选」
  collapsed?: boolean; // 这一天收起：只剩标题行（含天气）和地点数
  onToggle?: () => void;
  notes?: Record<string, string>; // 行程备注 {备注对象: 文本}（规格 M10 S4）；undefined = 不显示备注（对比 / 编辑中）
  onSaveNote?: (ref: string, text: string) => Promise<void>; // 不传 = 只读显示（分享页 / 离线）
  isToday?: boolean; // 目的地的今天就是这一天（规格 M15 S2f）
  photos?: boolean; // 景点缩略图（规格 M02 S7b-3）；公开分享页不放
  noteSources?: Record<string, unknown>; // 来自笔记的地点 id（规格 M12 S3）；undefined = 不显示（分享页 / 对比）
  fileCount?: number; // 挂在这一天的文件数（规格 M10 S5）：回形针
  onOpenFiles?: () => void;
  stories?: Record<string, CultureStory[]>; // 地点 → 它出现在哪些作品里（规格 09 十五）；不传 = 不显示
  cityNames?: Record<string, string>; // 多城市行程：城市 id → 中文名；传了就在每天标出在哪座城市
}) {
  const cityTag = cityNames
    ? Object.keys(day.theme_by_city ?? {}).length > 0
      ? Object.keys(day.theme_by_city ?? {}).map((c) => cityNames[c] ?? c).join(" → ")
      : cityNames[day.city_id ?? ""]
    : undefined;
  const headTheme = day.theme_by_city && Object.keys(day.theme_by_city).length > 0
    ? [...new Set(Object.values(day.theme_by_city))].join(" · ")
    : day.theme;
  const legacyNote = notes?.[`day|${day.date}`] ?? "";
  const chapterEyebrow = chapterOnly(day.chapter, day.theme);
  const points = matchDayPoints(day.items, attractions);
  const placeIds = day.items.filter((i) => i.place_id).map((i) => i.place_id as string);
  const [swap, setSwap] = useState<{ pid: string; alts: Alternative[] | null; indoor: boolean } | null>(null);
  const rainy = !!weather?.rainy;
  const outdoorIds = new Set(weather?.outdoor_places.map((p) => p.place_id) ?? []);
  const mixedIds = new Set(weather?.mixed_places.map((p) => p.place_id) ?? []);
  const rainOutdoor = (day.items.filter((i) => i.place_id && outdoorIds.has(i.place_id)));
  const [dragging, setDragging] = useState<string | null>(null);
  let prevStop: AttractionRef | null = null;
  // 相邻两个有坐标的点：和下面渲染时的「上一站」判断一致
  const legPairs: { j: number; from: AttractionRef; to: AttractionRef }[] = [];
  let lastStop: AttractionRef | null = null;
  day.items.forEach((it, j) => {
    const m = attractions.length ? matchFor(it, attractions) : null;
    if (!m) return;
    if (lastStop && lastStop !== m) legPairs.push({ j, from: lastStop, to: m });
    lastStop = m;
  });
  const [commute, setCommute] = useState<{ legs: Record<number, CommuteLeg | null>; reason?: string; busy: boolean }>({
    legs: {},
    busy: false,
  });
  const [commuteError, setCommuteError] = useState<string | null>(null);

  async function loadCommute() {
    setCommute((c) => ({ ...c, busy: true }));
    try {
      const got = await api.commute(
        commuteCity!,
        legPairs.map((p) => ({ from_lat: p.from.lat, from_lng: p.from.lng, to_lat: p.to.lat, to_lng: p.to.lng })),
        day.items.some((it) => it.transport?.includes("自驾")),
      );
      setCommute({ legs: Object.fromEntries(legPairs.map((p, k) => [p.j, got.legs[k] ?? null])), reason: got.reason, busy: false });
    } catch (e) {
      setCommute((c) => ({ ...c, busy: false }));
      setCommuteError(e instanceof Error ? e.message : "查询通勤时间失败");
    }
  }

  async function openSwap(pid: string, indoor = false) {
    if (swap?.pid === pid) {
      setSwap(null);
      return;
    }
    setSwap({ pid, alts: null, indoor });
    const alts = await edit!.loadAlternatives(pid, indoor);
    setSwap((cur) => (cur?.pid === pid ? { pid, alts, indoor } : cur));
  }

  /** 拖拽落下：从另一天来的 = 移到这天（后端重排）；本天内的 = 按落点位置重排（顺序按你拖的）。 */
  function handleDrop(e: React.DragEvent) {
    if (!edit) return;
    e.preventDefault();
    e.currentTarget.classList.remove("dropzone");
    document.body.classList.remove("is-dragging");
    setDragging(null);
    let data: { pid?: string; date?: string };
    try {
      data = JSON.parse(e.dataTransfer.getData("text/plain"));
    } catch {
      return;
    }
    if (!data.pid) return;
    if (data.date !== day.date) {
      edit.onMove(data.pid, day.date);
      return;
    }
    const target = (e.target as HTMLElement).closest<HTMLElement>("[data-pid]");
    const without = placeIds.filter((p) => p !== data.pid);
    let at = without.length;
    if (target?.dataset.pid && target.dataset.pid !== data.pid) {
      const idx = without.indexOf(target.dataset.pid);
      const r = target.getBoundingClientRect();
      at = e.clientY < r.top + r.height / 2 ? idx : idx + 1;
    }
    const next = [...without.slice(0, at), data.pid, ...without.slice(at)];
    if (next.join() !== placeIds.join()) edit.onReorder(day.date, next);
  }

  return (
    <div
      id={`day-${day.date}`}
      className={`day${changed ? " changed" : ""}`}
      onDragEnter={edit ? (e) => e.preventDefault() : undefined}
      onDragOver={
        edit
          ? (e) => {
              e.preventDefault(); // dragenter 和 dragover 都要声明「这里可以放」，浏览器才会发 drop
              e.dataTransfer.dropEffect = "move";
              e.currentTarget.classList.add("dropzone"); // 直接改 class：拖拽过程中不触发 React 重渲染，免得打断浏览器的拖拽
            }
          : undefined
      }
      onDragLeave={
        edit
          ? (e) => {
              if (!e.currentTarget.contains(e.relatedTarget as Node)) e.currentTarget.classList.remove("dropzone");
            }
          : undefined
      }
      onDrop={edit ? handleDrop : undefined}
    >
      <div
        className="day-head"
        onClick={(e) => {
          if (onToggle && !(e.target as HTMLElement).closest("button, a, input, select")) onToggle();
        }}
      >
        {onToggle && (
          <button
            type="button"
            className={`day-toggle${collapsed ? " collapsed" : ""}`}
            aria-expanded={!collapsed}
            aria-label={`${collapsed ? "展开" : "收起"}第 ${index + 1} 天`}
            onClick={onToggle}
          >
            <IconChevron size={16} />
          </button>
        )}
        <span className="dnum" style={color ? { background: color, color: "#fff" } : undefined}>
          第 {index + 1} 天
        </span>
        <span className="date">
          {day.date}（周{weekday(day.date)}）
        </span>
        {isToday && (
          <span className="today-chip" data-testid="today-chip">
            今天
          </span>
        )}
        {cityTag && (
          <span className="day-city" data-testid="day-city">
            {cityTag}
          </span>
        )}
        <h3>{headTheme}</h3>
        {collapsed && <span className="day-count muted">{placeIds.length} 个地点</span>}
        {weather && <WeatherChip day={weather} />}
        {onOpenFiles && <Paperclip count={fileCount} onClick={onOpenFiles} label={`第 ${index + 1} 天的文件`} />}
      </div>
      {!collapsed && (
        <>
        {legacyNote && (
          <ItemNote
            label="整天笔记"
            value={legacyNote}
            hint="以前写的整天笔记，可以改，清空就删除"
            onSave={onSaveNote ? (t) => onSaveNote(`day|${day.date}`, t) : undefined}
          />
        )}
        {rainy && weather && (
          <div className="rain-banner" data-testid="rain-banner" role="status">
            <span aria-hidden>🌧️</span>
            <div>
              <b>预报降雨概率 {weather.rain_prob}%</b>
              {rainOutdoor.length > 0 ? (
                <>
                  ，当天有室外点：{rainOutdoor.map((i) => i.title).join("、")}。
                  {edit ? "下面带标签的点可以点「换成室内备选」。" : ""}
                </>
              ) : (
                "，当天的点基本都在室内，问题不大。"
              )}
              {!edit && onStartEdit && rainOutdoor.length > 0 && (
                <button type="button" className="tool rain-edit" onClick={onStartEdit}>
                  编辑行程，换成室内备选
                </button>
              )}
            </div>
          </div>
        )}
        {chapterEyebrow && (
          <div className="day-chapter" data-testid="day-chapter">
            {chapterEyebrow}
          </div>
        )}
        {day.guide && (
          <div className="day-guide" data-testid="day-guide">
            {day.guide.split("\n").map((p, k) => (
              <p key={k}>{p}</p>
            ))}
          </div>
        )}
        {hotelNote && (
          <div className="hotel-note" data-testid="hotel-note">
            🏨 {hotelNote}
          </div>
        )}
        <DayMap points={points} photos={photos} />
        {amapUrls && amapUrls.length > 0 && (
          <div className="day-nav">
            {amapUrls.map((u, k) => (
              <a key={u} className="tool nav-day" href={u} target="_blank" rel="noreferrer">
                🧭 在高德地图中查看当天地点{amapUrls.length > 1 ? `（第 ${k + 1} 段）` : ""} ↗
              </a>
            ))}
          </div>
        )}
        {commuteCity && amapUrls && amapUrls.length > 0 && !edit && legPairs.length > 0 && (
          <div className="day-nav">
            <button type="button" className="tool" data-testid="commute-load" disabled={commute.busy} onClick={loadCommute}>
              {commute.busy ? "正在查…" : Object.keys(commute.legs).length ? "🚶 重新查询通勤时间" : "🚶 查看真实通勤时间"}
            </button>
            {commute.reason && (
              <span className="muted commute-reason" role="status" data-testid="commute-reason">
                {COMMUTE_REASON[commute.reason] ?? commute.reason}
              </span>
            )}
          </div>
        )}
        <ErrorPopup message={commuteError} onClose={() => setCommuteError(null)} />
        {navUrls && navUrls.length > 0 && (
          <div className="day-nav">
            {navUrls.map((u, k) => (
              <a key={u} className="tool nav-day" href={u} target="_blank" rel="noreferrer">
                🧭 在 Google 地图中打开当天路线{navUrls.length > 1 ? `（第 ${k + 1} 段）` : ""} ↗
              </a>
            ))}
          </div>
        )}
        {reasons.length > 0 && (
          <details className="why">
            <summary>为什么这样排</summary>
            <ul>
              {reasons.map((r, k) => (
                <li key={k}>{r}</li>
              ))}
            </ul>
          </details>
        )}
        <div className="timeline">
          {day.items.map((it, j) => {
            const match = attractions.length ? matchFor(it, attractions) : null;
            const prev = prevStop; // 上一个有坐标的点：两点之间给一个「导航」链接
            if (match) prevStop = match;
            const pid = it.place_id ?? null;
            const editable = !!edit && !!pid;
            return (
              <div
                className={`item type-${it.type}${dragging && dragging === pid ? " dragging" : ""}${it.locked ? " locked" : ""}`}
                key={j}
                data-pid={pid ?? undefined}
                draggable={editable && !it.locked}
                onDragStart={
                  editable && !it.locked
                    ? (e) => {
                        e.dataTransfer.setData("text/plain", JSON.stringify({ pid, date: day.date }));
                        e.dataTransfer.effectAllowed = "move";
                        document.body.classList.add("is-dragging"); // 拖拽期间让按钮 / 下拉框穿透，落在哪儿都算落在这一天
                        setDragging(pid);
                      }
                    : undefined
                }
                onDragEnd={
                  editable
                    ? () => {
                        document.body.classList.remove("is-dragging");
                        setDragging(null);
                      }
                    : undefined
                }
              >
                <div className="rail">
                  <span className="dot">{TYPE_ICON[it.type] ?? "•"}</span>
                </div>
                <div className="t">{it.time}</div>
                <div>
                  {photos && pid && onOpenPlace && match?.media?.image && (
                    <button
                      type="button"
                      className="item-thumb"
                      data-testid="item-thumb"
                      aria-label={`查看${it.title}的详情和照片`}
                      title={`摄影：${match.media.image.author} · ${match.media.image.license}`}
                      onClick={() => onOpenPlace(pid)}
                    >
                      <img src={mediaSrc(match.media.image.url)} alt="" crossOrigin="anonymous" loading="lazy" onError={(e) => ((e.currentTarget.parentElement as HTMLElement).hidden = true)} />
                    </button>
                  )}
                  <div className="item-head">
                    <h4>
                      {pid && onOpenPlace ? (
                        <button type="button" className="linklike title-link" onClick={() => onOpenPlace(pid)} title="查看地点详情">
                          {it.title}
                        </button>
                      ) : (
                        it.title
                      )}
                      {it.locked && <span className="lock-badge" title="已锁定：重新排程时位置和时间不变"> 🔒</span>}
                    </h4>
                    {pid && onOpenPlace && (
                      <button
                        type="button"
                        className="detail-tag"
                        data-testid="item-detail"
                        aria-label={`查看${it.title}的详情`}
                        title="地址、交通、票价、闭馆日、参观重点、购票说明……"
                        onClick={() => onOpenPlace(pid)}
                      >
                        <span>详情 ›</span>
                      </button>
                    )}
                  </div>
                  <div className="meta">
                    <b>{it.location}</b> · {fmtDuration(it.duration_minutes)}
                    {it.transport ? ` · ${it.transport}` : ""}
                    {prev && match && prev !== match && (
                      <>
                        {" · "}
                        <a className="leg-nav" href={legUrl(prev, match)} target="_blank" rel="noreferrer">
                          🧭 从「{prev.name_zh}」导航 ↗
                        </a>
                      </>
                    )}
                  </div>
                  {commute.legs[j] && <CommuteLine leg={commute.legs[j] as CommuteLeg} transport={it.transport} />}
                  {it.notes && <div className="note">{it.notes}</div>}
                  {it.why_here && (
                    <div className="why-here" data-testid="why-here">
                      {it.why_here}
                    </div>
                  )}
                  {!!it.look_for?.length && (
                    <ul className="look-for" data-testid="look-for" aria-label="这一站看什么">
                      {it.look_for.map((x, k) => (
                        <li key={k}>{x}</li>
                      ))}
                    </ul>
                  )}
                  {pid && stories?.[pid] && <Stories stories={stories[pid]} limit={2} />}
                  {match?.long_term_closed && (
                    <div className="tip warn">
                      <span aria-hidden>⚠️</span> 长期闭馆/整修中，出发前务必查官网确认
                    </div>
                  )}
                  <div className="tags">
                    {pid && noteSources?.[pid] != null && onOpenPlace && (
                      <button
                        type="button"
                        className="tag note-badge"
                        data-testid="item-note-badge"
                        title="这个地点来自你导入的笔记，点开看原文片段"
                        onClick={() => onOpenPlace(pid)}
                      >
                        📝 来自笔记
                      </button>
                    )}
                    {rainy && pid && outdoorIds.has(pid) && <span className="tag rain">室外 · 可能淋雨</span>}
                    {rainy && pid && mixedIds.has(pid) && <span className="tag rain">半室外 · 可能淋雨</span>}
                    {match?.ticketing === "online_required" && <span className="tag book">必须提前线上购票</span>}
                    {match?.ticketing === "online_or_walkin" && (
                      <span className="tag walkin">建议线上购票 · 现场也可排队</span>
                    )}
                    {!match && it.booking_required && <span className="tag book">需预约</span>}
                    {match?.url && (
                      <a className="tag link" href={match.url} target="_blank" rel="noreferrer">
                        官网购票 ↗
                      </a>
                    )}
                  </div>
                  {notes && (
                    <ItemNote
                      label={it.title}
                      value={notes[noteRef(day.date, it)] ?? ""}
                      onSave={onSaveNote ? (t) => onSaveNote(noteRef(day.date, it), t) : undefined}
                    />
                  )}
                  {editable && edit && pid && (
                    <>
                      <div className="edit-tools">
                        <span className="drag-handle" title="拖到别的地点前后可调顺序，拖到另一天可移过去" aria-hidden>
                          ⠿
                        </span>
                        <button
                          type="button"
                          className={`tool${it.locked ? " on" : ""}`}
                          disabled={edit.busy}
                          onClick={() => edit.onLock(pid, !it.locked)}
                          title={it.locked ? "解锁后才能移动、删除或换掉" : "锁定后，重新排程时位置和时间不变"}
                        >
                          {it.locked ? "🔒 已锁定" : "🔓 锁定"}
                        </button>
                        <button
                          type="button"
                          className="tool"
                          disabled={edit.busy || it.locked}
                          onClick={() => void openSwap(pid, rainy && outdoorIds.has(pid))}
                        >
                          {rainy && outdoorIds.has(pid) ? "换成室内备选" : "换一个"}
                        </button>
                        <select
                          className="tool"
                          aria-label={`把${it.title}移到另一天`}
                          value=""
                          disabled={edit.busy || it.locked}
                          onChange={(e) => e.target.value && edit.onMove(pid, e.target.value)}
                        >
                          <option value="">移到…</option>
                          {(edit.daysFor ? edit.daysFor(pid) : edit.days)
                            .filter((d) => d.date !== day.date)
                            .map((d) => (
                              <option key={d.date} value={d.date}>
                                {d.label}
                              </option>
                            ))}
                        </select>
                        <button
                          type="button"
                          className="tool danger"
                          disabled={edit.busy || it.locked}
                          onClick={() => edit.onRemove(pid)}
                        >
                          删除
                        </button>
                      </div>
                      {swap?.pid === pid && (
                        <div className="swap-panel">
                          {swap.alts === null && <span className="muted">正在找备选…</span>}
                          {swap.alts?.length === 0 && (
                            <span className="muted">
                              没有合适的备选（{swap.indoor ? "室内、" : ""}同主题、当天开门、行程里还没有的点）
                            </span>
                          )}
                          {swap.alts?.map((a) => (
                            <div className="alt" key={a.place_id}>
                              <div>
                                <b>{a.name_zh}</b>
                                <span className="muted"> · {a.area} · 约 {fmtDuration(a.duration_min)}</span>
                                <div className="muted">{a.reason}</div>
                              </div>
                              <button
                                type="button"
                                className="tool"
                                disabled={edit.busy}
                                onClick={() => {
                                  setSwap(null);
                                  edit.onSwap(pid, a.place_id);
                                }}
                              >
                                换成它
                              </button>
                            </div>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        {edit && (
          <div className="add-row">
            <select
              aria-label={`给第 ${index + 1} 天加入地点`}
              value=""
              disabled={edit.busy}
              onChange={(e) => e.target.value && edit.onAdd(e.target.value, day.date)}
            >
              <option value="">＋ 给这一天加入地点…</option>
              {(edit.addableFor ? edit.addableFor(day) : edit.addable).map((a) => (
                <option key={a.place_id} value={a.place_id}>
                  {a.name_zh}
                </option>
              ))}
            </select>
          </div>
        )}
        </>
      )}
    </div>
  );
}

/** 购票清单和风险提醒：行程页和公开分享页共用。 */
export function BookingAndRisks({
  it,
  attractions,
  reservations,
  onSaveReservation,
  reservationsDisabled = false,
  reservationFiles,
  onOpenFiles,
}: {
  it: Itinerary;
  attractions: AttractionRef[];
  reservations?: ReservationItem[]; // 只有行程页（自己的行程）传；公开分享页不传，也就看不到确认码
  onSaveReservation?: (placeId: string, patch: ReservationPatch) => Promise<void>;
  reservationsDisabled?: boolean;
  reservationFiles?: Record<string, number>; // 挂在预订上的文件数（规格 M10 S5）：回形针
  onOpenFiles?: (placeId: string) => void;
}) {
  const resOf = (pid?: string | null) => (pid ? reservations?.find((r) => r.place_id === pid) : undefined);
  const orphans = (reservations ?? []).filter((r) => !r.in_itinerary);
  return (
    <>
      {it.booking_checklist.length > 0 && (
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>购票清单</h2>
          {[
            { title: "必须提前线上购票", must: true },
            { title: "建议线上购票（现场也可排队购买）", must: false },
          ].map((g) => {
            const rows = it.booking_checklist.filter((b) => (b.must_online ?? true) === g.must);
            if (rows.length === 0) return null;
            return (
              <div className="check-group" key={g.title}>
                <h3>{g.title}</h3>
                <div className="check">
                  {rows.map((b, i) => {
                    const addr = attractions.find((a) => a.place_id === b.place_id)?.address;
                    return (
                      <div className="rowc" key={i}>
                        {resOf(b.place_id) && onSaveReservation ? (
                          <input
                            type="checkbox"
                            className="res-check"
                            aria-label={`${b.title}已订好了`}
                            title="订好了就勾上"
                            checked={resOf(b.place_id)!.status === "booked"}
                            disabled={reservationsDisabled}
                            onChange={(e) =>
                              void onSaveReservation(b.place_id!, { status: e.target.checked ? "booked" : "todo" })
                            }
                          />
                        ) : (
                          <div className="box" />
                        )}
                        <div>
                          <b>{b.title}</b>
                          {b.place_id && onOpenFiles && (
                            <Paperclip
                              count={reservationFiles?.[b.place_id] ?? 0}
                              onClick={() => onOpenFiles(b.place_id!)}
                              label={`${b.title}的预订文件`}
                            />
                          )}{" "}
                          — {b.reason}
                          {b.deadline_hint ? `（${b.deadline_hint}）` : ""}
                          {b.url && (
                            <>
                              {" · "}
                              <a href={b.url} target="_blank" rel="noreferrer">
                                官网购票 ↗
                              </a>
                              <VerifiedNote status={b.url_status} at={b.verified_at} />
                            </>
                          )}
                          {!b.url && b.place_id && <span className="muted"> · 暂无已核实的官网链接，请自行搜索官网</span>}
                          {addr && <span className="muted"> · 地址：{addr}</span>}
                          {b.note && <div className="ticket-note">购票提示：{b.note}</div>}
                          {resOf(b.place_id) && onSaveReservation && (
                            <ReservationControls
                              item={resOf(b.place_id)!}
                              disabled={reservationsDisabled}
                              onSave={onSaveReservation}
                            />
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {orphans.length > 0 && onSaveReservation && (
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>不在当前行程里的预订记录</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            这些地点已经从行程里拿掉了，但你记过预订，确认码先留着。
          </p>
          {orphans.map((r) => (
            <div key={r.place_id} className="orphan-res">
              <b>{r.title}</b>
              <ReservationControls item={r} disabled={reservationsDisabled} onSave={onSaveReservation} />
            </div>
          ))}
        </div>
      )}

      {it.risk_notes.length > 0 && (
        <div className="card">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>风险提醒</h2>
          <ul className="risks">
            {it.risk_notes.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}
