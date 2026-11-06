import type { Itinerary, ItineraryItem } from "./types";

export type ItemStatus = "same" | "added" | "removed";

export interface DiffItem {
  item: ItineraryItem;
  status: ItemStatus;
}

export interface DiffDay {
  date: string;
  theme: string;
  themeChanged: boolean;
  rows: DiffItem[];
  changed: boolean;
}

function key(it: ItineraryItem): string {
  return `${it.time}|${it.title.trim()}`;
}

/** 逐天做并集 diff：以 (time, title) 为标识，标 same / added / removed。 */
export function diffItineraries(oldIt: Itinerary, newIt: Itinerary): DiffDay[] {
  const oldDays = new Map(oldIt.days.map((d) => [d.date, d]));
  const newDays = new Map(newIt.days.map((d) => [d.date, d]));
  const dates = [...new Set([...oldDays.keys(), ...newDays.keys()])].sort();

  return dates.map((date) => {
    const o = oldDays.get(date);
    const n = newDays.get(date);
    const oldByKey = new Map((o?.items ?? []).map((i) => [key(i), i]));

    const rows: DiffItem[] = [];
    const seen = new Set<string>();
    for (const i of n?.items ?? []) {
      const k = key(i);
      seen.add(k);
      rows.push({ item: i, status: oldByKey.has(k) ? "same" : "added" });
    }
    for (const i of o?.items ?? []) {
      if (!seen.has(key(i))) rows.push({ item: i, status: "removed" });
    }
    rows.sort((a, b) => a.item.time.localeCompare(b.item.time));

    const changed = rows.some((r) => r.status !== "same") || (o?.theme ?? "") !== (n?.theme ?? "");
    return {
      date,
      theme: n?.theme ?? o?.theme ?? "",
      themeChanged: !!o && !!n && o.theme !== n.theme,
      rows,
      changed,
    } satisfies DiffDay;
  });
}

export function diffStats(days: DiffDay[]): { added: number; removed: number; changedDays: number } {
  let added = 0;
  let removed = 0;
  let changedDays = 0;
  for (const d of days) {
    if (d.changed) changedDays++;
    for (const r of d.rows) {
      if (r.status === "added") added++;
      if (r.status === "removed") removed++;
    }
  }
  return { added, removed, changedDays };
}
