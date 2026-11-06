import type { PlanCard } from "./types";

/** 这趟行程相对「今天」的阶段：还没出发 / 进行中 / 已结束（规格 07 P03 / P05）。日期都按本机日历算，不碰时区。 */
export type Phase = {
  kind: "upcoming" | "current" | "past";
  /** 封面上的大字：「还有 17 天」「明天出发」「今天出发」「进行中 · 第 2 天」「已结束 3 天前」 */
  label: string;
  /** 距出发 / 已结束的天数（进行中 = 第几天） */
  n: number;
};

function ymdNumber(s: string): number {
  const [y, m, d] = s.slice(0, 10).split("-").map(Number);
  return Date.UTC(y, m - 1, d) / 86_400_000;
}

export function todayString(now: Date = new Date()): string {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

export function tripPhase(startDate: string, endDate: string, now: Date = new Date()): Phase {
  const today = ymdNumber(todayString(now));
  const start = ymdNumber(startDate);
  const end = ymdNumber(endDate);
  if (today < start) {
    const n = start - today;
    return { kind: "upcoming", n, label: n === 1 ? "明天出发" : `还有 ${n} 天` };
  }
  if (today <= end) {
    const n = today - start + 1;
    return { kind: "current", n, label: n === 1 ? "今天出发" : `进行中 · 第 ${n} 天` };
  }
  const n = today - end;
  return { kind: "past", n, label: n === 1 ? "昨天结束" : `已结束 ${n} 天前` };
}

/**
 * 「我的行程」分组（规格 07 P05）：
 * 即将到来（进行中的排最前，其余按出发日期从近到远）；已结束按出发日期从新到旧。
 */
export function groupTrips(cards: PlanCard[], now: Date = new Date()): { upcoming: PlanCard[]; past: PlanCard[] } {
  const withPhase = cards.map((c) => ({ c, p: tripPhase(c.start_date, c.end_date, now) }));
  const live = withPhase.filter((x) => x.p.kind !== "past");
  live.sort((a, b) => {
    if ((a.p.kind === "current") !== (b.p.kind === "current")) return a.p.kind === "current" ? -1 : 1;
    return a.c.start_date.localeCompare(b.c.start_date);
  });
  const past = withPhase.filter((x) => x.p.kind === "past").sort((a, b) => b.c.start_date.localeCompare(a.c.start_date));
  return { upcoming: live.map((x) => x.c), past: past.map((x) => x.c) };
}

/** 首页的「下一站」：进行中的优先，否则最近的还没出发的；都没有 = null。 */
export function nextOrCurrent(cards: PlanCard[], now: Date = new Date()): PlanCard | null {
  return groupTrips(cards, now).upcoming[0] ?? null;
}
