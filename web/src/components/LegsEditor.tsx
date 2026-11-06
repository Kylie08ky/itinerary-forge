import type { CityInfo, TransportMode } from "../types";
import { blankChoices, LegOptions, type LegChoices } from "./LegOptions";
import { staysError } from "./StayDraftEditor";
import { MODE_OPTIONS } from "./TransportEditor";

/** 多城市旅行（规格 11）：第一座城市以外的每一段，各自的目的地 + 开始 / 离开日期。theme 空 = 均衡。 */
export interface LegDraft extends LegChoices {
  destination: string;
  start: string; // YYYY-MM-DD：到达这座城市那天
  end: string; // 离开这座城市那天，可以和开始同一天
  endOk?: string; // 最近一次填完整的离开日期：手敲日期时中间状态是空的，平移要对着它算
}

/** 换城市的交通：两个钟点都填了才算「自己填」，否则后端按距离估算。日期不用填：出发在前一座的离开日，到达在后一座的开始日。 */
export interface TransferDraft {
  mode: TransportMode;
  depart: string; // HH:MM
  arrive: string;
}

export const MAX_LEGS = 8; // 整趟旅行最多几座城市
export const MAX_PICK = 5; // 国家弹窗里一次最多选几座（更多的用「添加下一座城市」手动加）
export const MAX_LEG_DAYS = 14;

export const blankTransfer = (): TransferDraft => ({ mode: "high_speed_rail", depart: "", arrive: "" });

export function addDays(date: string, n: number): string {
  if (date.length !== 10) return date;
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function dayDelta(a: string, b: string): number {
  if (a.length !== 10 || b.length !== 10) return 0;
  return Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000);
}

/** 新加的一座城市：当天换城市，先玩三天。 */
export function newLeg(prevEnd: string): LegDraft {
  const end = addDays(prevEnd, 2);
  return { ...blankChoices(), destination: "", start: prevEnd, end, endOk: end };
}

export interface LegDates {
  firstEnd: string;
  firstOk: string; // 第一座城市最近一次填完整的离开日期
  legs: LegDraft[];
}

/** 第一座城市的离开日期改了：后面的城市整体平移同样的天数（间隔和每座城市玩几天都不变）。 */
export function moveFirstEnd(s: LegDates, value: string): LegDates {
  if (value.length !== 10) return { ...s, firstEnd: value };      // 还没敲完：只记下，不平移
  const d = dayDelta(s.firstOk || value, value);
  return { ...s, firstEnd: value, firstOk: value, legs: d ? s.legs.map((l) => shiftLeg(l, d)) : s.legs };
}

/** 整趟旅行的开始日期改了：每一段一起平移。 */
export function moveAll(s: LegDates, oldStart: string, newStart: string): LegDates {
  const d = dayDelta(oldStart, newStart);
  if (!d) return s;
  return { firstEnd: addDays(s.firstEnd, d), firstOk: addDays(s.firstOk, d), legs: s.legs.map((l) => shiftLeg(l, d)) };
}

function shiftLeg(l: LegDraft, d: number): LegDraft {
  const end = addDays(l.end, d);
  return { ...l, start: addDays(l.start, d), end, endOk: end };
}

/** 后续第 i 座城市（从 0 数起）的开始日期改了：只动它自己，离开日期不早于开始。 */
export function moveLegStart(s: LegDates, i: number, value: string): LegDates {
  return {
    ...s,
    legs: s.legs.map((l, k) => {
      if (k !== i) return l;
      if (value.length !== 10 || l.end >= value) return { ...l, start: value };
      return { ...l, start: value, end: value, endOk: value };
    }),
  };
}

/** 后续第 i 座城市的离开日期改了：再后面的城市整体平移。 */
export function moveLegEnd(s: LegDates, i: number, value: string): LegDates {
  if (value.length !== 10) return { ...s, legs: s.legs.map((l, k) => (k === i ? { ...l, end: value } : l)) };
  const d = dayDelta(s.legs[i].endOk || value, value);
  return {
    ...s,
    legs: s.legs.map((l, k) => (k === i ? { ...l, end: value, endOk: value } : k > i && d ? shiftLeg(l, d) : l)),
  };
}

/** 整趟旅行的最后一天。 */
export function lastDay(firstEnd: string, legs: LegDraft[]): string {
  return legs.length > 0 ? legs[legs.length - 1].end : firstEnd;
}

function cityOf(cities: CityInfo[], text: string): CityInfo | null {
  const d = text.trim().toLowerCase();
  return cities.find((c) => [c.id, c.name_zh, c.name_en].some((n) => n.toLowerCase() === d)) ?? null;
}

/** 提交前的检查：返回第一处问题（人话），没问题是 null。 */
export function legsError(
  first: { destination: string; start: string; end: string },
  legs: LegDraft[],
  cities: CityInfo[],
): string | null {
  const all = [
    { destination: first.destination, start: first.start, end: first.end },
    ...legs.map((l) => ({ destination: l.destination, start: l.start, end: l.end })),
  ];
  for (let i = 0; i < all.length; i++) {
    const who = `第 ${i + 1} 座城市`;
    const l = all[i];
    if (!l.destination.trim()) return `${who}还没填`;
    const c = cityOf(cities, l.destination);
    if (!c) return `${who}「${l.destination.trim()}」还没有数据`;
    if (l.start.length !== 10 || l.end.length !== 10) return `${who}的日期还没填`;
    if (l.end < l.start) return `${who}的离开日期不能早于开始日期`;
    if (dayDelta(l.start, l.end) + 1 > MAX_LEG_DAYS) return `${who}最多玩 ${MAX_LEG_DAYS} 天`;
    if (i > 0) {
      const gap = dayDelta(all[i - 1].end, l.start);
      if (gap !== 0 && gap !== 1) return `${who}要在上一座城市离开的当天或第二天开始`;
      if (c.id === cityOf(cities, all[i - 1].destination)?.id) return "相邻的两座城市不能是同一座";
    }
    const stays = i > 0 ? legs[i - 1].stays : [];
    const se = staysError(stays, l.start, l.end);
    if (se) return `${who}的住宿：${se}`;
  }
  if (dayDelta(all[0].start, all[all.length - 1].end) < 1) return "整趟旅行至少要 2 天";
  return null;
}

function shortDay(date: string): string {
  return date.length === 10 ? date.slice(5) : "";
}

export function LegsEditor({
  first,
  firstOk,
  legs,
  transfers,
  cities,
  error,
  onDates,
  onLegs,
  onTransfers,
  onNeedCity,
}: {
  first: { destination: string; start: string; end: string };
  firstOk: string;
  legs: LegDraft[];
  transfers: TransferDraft[];
  cities: CityInfo[];
  error?: string;
  onDates: (next: LegDates) => void; // 日期变动（会连带平移后面的城市）
  onLegs: (next: LegDraft[]) => void; // 其他变动：目的地、主题、增删
  onTransfers: (next: TransferDraft[]) => void;
  onNeedCity: (index: number, text: string) => void;
}) {
  const state: LegDates = { firstEnd: first.end, firstOk, legs };
  const patchLeg = (i: number, p: Partial<LegDraft>) => onLegs(legs.map((l, k) => (k === i ? { ...l, ...p } : l)));
  const patchTransfer = (i: number, p: Partial<TransferDraft>) =>
    onTransfers(transfers.map((t, k) => (k === i ? { ...t, ...p } : t)));
  const names = [first.destination.trim() || "第一座城市", ...legs.map((l) => l.destination.trim() || "下一座城市")];
  const ends = [first.end, ...legs.map((l) => l.end)];
  const starts = [first.start, ...legs.map((l) => l.start)];

  return (
    <div className="legs-editor" data-testid="legs-editor">
      {legs.length > 0 && (
        <div className="leg-row" data-testid="leg-0">
          <span className="leg-no">1</span>
          <span className="leg-name">{names[0]}</span>
          <span className="leg-range">
            {shortDay(first.start)} → {shortDay(first.end)}
          </span>
        </div>
      )}
      {legs.map((leg, i) => (
        <div key={i} className="leg-seg">
          <div className="transfer-row" data-testid={`transfer-${i}`}>
            <span className="transfer-title">
              {names[i]} → {names[i + 1]}
              <b>
                {" · "}
                {ends[i] === starts[i + 1]
                  ? `${shortDay(ends[i])} 当天换城市`
                  : `${shortDay(ends[i])} 出发，${shortDay(starts[i + 1])} 到达`}
              </b>
            </span>
            <select
              aria-label={`第 ${i + 1} 次换城市的交通方式`}
              value={transfers[i]?.mode ?? "high_speed_rail"}
              onChange={(e) => patchTransfer(i, { mode: e.target.value as TransportMode })}
            >
              {MODE_OPTIONS.map((o) => (
                <option key={o.v} value={o.v}>
                  {o.t}
                </option>
              ))}
            </select>
            <input
              type="time"
              aria-label={`第 ${i + 1} 次换城市的出发时间`}
              value={transfers[i]?.depart ?? ""}
              onChange={(e) => patchTransfer(i, { depart: e.target.value })}
            />
            <span aria-hidden>→</span>
            <input
              type="time"
              aria-label={`第 ${i + 1} 次换城市的到达时间`}
              value={transfers[i]?.arrive ?? ""}
              onChange={(e) => patchTransfer(i, { arrive: e.target.value })}
            />
            <span className="hint">不填就按距离估算</span>
          </div>
          <div className="leg-card" data-testid={`leg-${i + 1}`}>
            <div className="leg-row-top">
              <span className="leg-no">{i + 2}</span>
              <input
                className="leg-dest"
                aria-label={`第 ${i + 2} 座城市`}
                value={leg.destination}
                placeholder="目的地"
                onChange={(e) => patchLeg(i, { destination: e.target.value })}
                onBlur={() => {
                  const text = leg.destination.trim();
                  if (text && !cityOf(cities, text) && cities.length > 0) onNeedCity(i, text);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    (e.target as HTMLInputElement).blur();
                  }
                }}
              />
              <button
                type="button"
                className="tool"
                aria-label={`去掉第 ${i + 2} 座城市`}
                onClick={() => {
                  onLegs(legs.filter((_, k) => k !== i));
                  onTransfers(transfers.filter((_, k) => k !== i));
                }}
              >
                去掉
              </button>
            </div>
            <div className="row2">
              <div className="field">
                <label htmlFor={`leg-start-${i}`}>开始日期</label>
                <input
                  id={`leg-start-${i}`}
                  type="date"
                  aria-label={`第 ${i + 2} 座城市的开始日期`}
                  value={leg.start}
                  onChange={(e) => onDates(moveLegStart(state, i, e.target.value))}
                />
              </div>
              <div className="field">
                <label htmlFor={`leg-end-${i}`}>离开日期</label>
                <input
                  id={`leg-end-${i}`}
                  type="date"
                  aria-label={`第 ${i + 2} 座城市的离开日期`}
                  value={leg.end}
                  min={leg.start || undefined}
                  onChange={(e) => onDates(moveLegEnd(state, i, e.target.value))}
                />
              </div>
            </div>
            <LegOptions
              city={cityOf(cities, leg.destination)}
              start={leg.start}
              end={leg.end}
              value={leg}
              onChange={(p) => patchLeg(i, p)}
              onEnd={(end) => onDates(moveLegEnd(state, i, end))}
            />
          </div>
        </div>
      ))}
      {error && <div className="ferr">{error}</div>}
      {legs.length < MAX_LEGS - 1 && (
        <button
          type="button"
          className="tool add-leg"
          data-testid="add-leg"
          onClick={() => {
            onLegs([...legs, newLeg(lastDay(first.end, legs))]);
            onTransfers([...transfers, blankTransfer()]);
          }}
        >
          ＋ 添加下一座城市
        </button>
      )}
    </div>
  );
}
