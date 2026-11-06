import { useEffect, useState } from "react";
import { api } from "./api";
import type { Itinerary, PlanCard } from "./types";

/**
 * 今日视图的纯逻辑（规格 M15 S2）：全部在浏览器里按「目的地时区」算，所以离线也能用，
 * 也不受手机 / 电脑自己时区设置的影响。时区数据用浏览器自带的（含夏令时），不引依赖。
 */

/** 目的地 -> 时区。目前只有巴黎；没登记的城市先按巴黎算。 */
export const CITY_TZ: Record<string, string> = { paris: "Europe/Paris" };
export const DEFAULT_TZ = "Europe/Paris";

export function tzFor(destination?: string | null): string {
  return CITY_TZ[(destination ?? "").trim().toLowerCase()] ?? DEFAULT_TZ;
}

/** 这一刻在目的地是几号、几点（minutes = 从 0 点起的分钟数）。 */
export function zonedNow(nowMs: number, tz: string): { date: string; minutes: number } {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(nowMs));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "00";
  return { date: `${get("year")}-${get("month")}-${get("day")}`, minutes: Number(get("hour")) * 60 + Number(get("minute")) };
}

export type ItemState = "done" | "current" | "next" | "later";

export interface TodayItem {
  index: number;
  time: string; // "HH:MM"
  end: string; // "HH:MM"
  start: number; // 分钟
  endMin: number;
  title: string;
  location: string;
  type: string;
  placeId: string | null;
  transport: string | null;
  state: ItemState;
}

export interface TodayView {
  status: "before" | "during" | "after";
  date: string; // 目的地的「今天」
  dayIndex: number; // 第几天（从 0 数）；不在行程里是 -1
  dayCount: number;
  items: TodayItem[];
  current: TodayItem | null; // 正在进行的
  next: TodayItem | null; // 下一个还没开始的
  minutesToNext: number | null;
  minutesToEnd: number | null; // 正在进行的还有多久结束
  remaining: number; // 还没结束的安排数（进行中 + 没开始）
  tomorrowFirst: { time: string; title: string } | null;
}

function toMin(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
}

function fromMin(n: number): string {
  const x = ((n % 1440) + 1440) % 1440;
  return `${String(Math.floor(x / 60)).padStart(2, "0")}:${String(x % 60).padStart(2, "0")}`;
}

/** 把行程按「现在」切成：已结束 / 正在进行 / 下一站 / 还没到。 */
export function buildToday(it: Itinerary, tz: string, nowMs: number): TodayView {
  const { date, minutes } = zonedNow(nowMs, tz);
  const dayCount = it.days.length;
  const idx = it.days.findIndex((d) => d.date === date);
  const empty = { dayIndex: -1, dayCount, items: [], current: null, next: null, minutesToNext: null, minutesToEnd: null, remaining: 0, tomorrowFirst: null };
  if (idx < 0) {
    const first = it.days[0]?.date;
    return { status: first && date < first ? "before" : "after", date, ...empty };
  }
  const day = it.days[idx];
  const items: TodayItem[] = day.items.map((x, i) => {
    const start = toMin(x.time);
    const endMin = start + x.duration_minutes;
    return {
      index: i,
      time: x.time,
      end: fromMin(endMin),
      start,
      endMin,
      title: x.title,
      location: x.location,
      type: x.type,
      placeId: x.place_id ?? null,
      transport: x.transport ?? null,
      state: minutes >= endMin ? "done" : minutes >= start ? "current" : "later",
    };
  });
  const current = [...items].reverse().find((x) => x.state === "current") ?? null;
  const next = items.find((x) => x.state === "later") ?? null;
  if (next) next.state = "next";
  const tomorrow = it.days[idx + 1]?.items[0];
  return {
    status: "during",
    date,
    dayIndex: idx,
    dayCount,
    items,
    current,
    next,
    minutesToNext: next ? next.start - minutes : null,
    minutesToEnd: current ? current.endMin - minutes : null,
    remaining: items.filter((x) => x.state !== "done").length,
    tomorrowFirst: tomorrow ? { time: tomorrow.time, title: tomorrow.title } : null,
  };
}

/** 「25 分钟」/「1 小时 5 分钟」/「2 小时」。 */
export function fmtMinutes(n: number): string {
  const m = Math.max(0, Math.round(n));
  if (m < 60) return `${m} 分钟`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} 小时 ${m % 60} 分钟` : `${h} 小时`;
}

/** 今天落在哪份行程里：未归档、日期范围包含今天；几份重叠取最近创建的。 */
export function pickActive(cards: PlanCard[], nowMs: number): PlanCard | null {
  const { date } = zonedNow(nowMs, DEFAULT_TZ);
  const hit = cards
    .filter((c) => !c.archived && c.start_date <= date && date <= c.end_date)
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  return hit[0] ?? null;
}

/** 当前时间（毫秒），每隔 everyMs 刷新一次；用的是 Date.now()，测试里可以用假时钟拨动。 */
export function useNowMs(everyMs = 30000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(t);
  }, [everyMs]);
  return now;
}

/** 有没有正在进行的行程（底栏的「今天」、首页的今日卡、/today 页都用）。失败就当没有，不弹错。 */
export function useActiveTrip(refreshKey: string = ""): { loading: boolean; card: PlanCard | null } {
  const [cards, setCards] = useState<PlanCard[] | null>(null);
  const now = useNowMs(30000);
  useEffect(() => {
    let live = true;
    api
      .planCards(false)
      .then((c) => live && setCards(c))
      .catch(() => live && setCards([]));
    return () => {
      live = false;
    };
  }, [refreshKey]);
  return { loading: cards === null, card: cards ? pickActive(cards, now) : null };
}
