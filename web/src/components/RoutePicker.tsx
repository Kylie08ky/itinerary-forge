import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { RouteCard, RouteDetail } from "../types";

const TRACK_ORDER: RouteCard["track"][] = ["history", "folk", "food", "other"]; // 兴趣线的先后：历史、民俗体验、美食、其他

/** 「2026 年 11 月 26 日 – 12 月 27 日」：同一年只写一次年份。 */
function seasonText(s: { start: string; end: string }): string {
  const [sy, sm, sd] = s.start.split("-").map(Number);
  const [ey, em, ed] = s.end.split("-").map(Number);
  const head = `${sy} 年 ${sm} 月 ${sd} 日`;
  return sy === ey ? `${head} – ${em} 月 ${ed} 日` : `${head} – ${ey} 年 ${em} 月 ${ed} 日`;
}

/**
 * 新建页的「深度路线」（规格 09 第五节）：和主题二选一。每条路线一张卡（主线、建议天数、章数、站数、几个站名），
 * 「看看这条路线讲什么」展开成章节和每章的站、延伸阅读，生成之前就知道它在讲什么。
 */
export function RoutePicker({
  city,
  routes,
  selected,
  days,
  stops,
  startDate,
  endDate,
  onSelect,
  onStops,
  onSuggestDays,
}: {
  city?: string; // 路线在哪座城市；不传就是默认城市
  routes: RouteCard[];
  selected: string | null;
  startDate: string; // 表单里选的日期：限定路线用它提示「当前日期没有这个活动」
  endDate: string;
  days: number;
  stops: string[] | null; // 你勾选要去的站（null = 全部）
  onSelect: (id: string | null) => void;
  onStops: (ids: string[] | null) => void;
  onSuggestDays: (n: number) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, RouteDetail>>({});
  const [err, setErr] = useState<string | null>(null);

  const want = open ?? selected;
  useEffect(() => {
    if (!want || detail[want]) return;
    api
      .deepRoute(want, city)
      .then((d) => setDetail((cur) => ({ ...cur, [want]: d })))
      .catch((e) => setErr(e instanceof ApiError ? e.message : String(e)));
  }, [want, detail]);

  if (routes.length === 0) return null;
  return (
    <section className="route-picker" aria-label="深度路线" data-testid="route-picker">
      <h3>深度路线</h3>
      {TRACK_ORDER.map((track) => {
        const inTrack = routes.filter((r) => r.track === track);
        if (inTrack.length === 0) return null;
        return (
          <div className="route-track" key={track} data-testid={`route-track-${track}`}>
            <h4 className="route-track-title">{inTrack[0].track_zh}</h4>
            <div className="route-cards">
        {inTrack.map((r) => {
          const on = selected === r.id;
          const few = on && days > 0 && days < r.days.min;
          const many = on && days > r.days.max;
          const d = detail[r.id];
          // 限定路线：你选的日期和举办期没有重合就提示（只是提示，不拦）
          const outOfSeason = !!r.season && !!startDate && !!endDate && (endDate < r.season.start || startDate > r.season.end);
          const all = d ? d.chapters.flatMap((c) => c.stops.map((s) => s.place_id)) : [];
          const core = d ? d.chapters.flatMap((c) => c.stops.filter((s) => s.priority === "core").map((s) => s.place_id)) : [];
          const picked = new Set(stops ?? all);
          const setPicked = (ids: string[]) => onStops(ids.length === all.length ? null : ids);
          const toggle = (id: string) => {
            const next = picked.has(id) ? all.filter((x) => x !== id && picked.has(x)) : all.filter((x) => x === id || picked.has(x));
            if (next.length > 0) setPicked(next); // 至少留一站
          };
          return (
            <article className={`route-card${on ? " on" : ""}`} key={r.id} data-testid={`route-card-${r.id}`} data-selected={on}>
              <header>
                <div>
                  <h4>
                    {r.name_zh}
                    {r.season && (
                      <span className="tag limited" data-testid={`route-limited-${r.id}`}>
                        限定
                      </span>
                    )}
                  </h4>
                  <p className="route-tagline">{r.tagline}</p>
                </div>
                <button
                  type="button"
                  className={on ? "tool" : "primary"}
                  aria-pressed={on}
                  data-testid={`route-select-${r.id}`}
                  onClick={() => onSelect(on ? null : r.id)}
                >
                  {on ? "取消选择" : "选这条路线"}
                </button>
              </header>
              <p className="route-meta">
                {r.season && <span data-testid={`route-season-${r.id}`}>{seasonText(r.season)} · </span>}
                推荐最少 {r.days.min} 天 · 建议 {r.days.suggested} 天 · {r.chapter_count} 章 · {r.stop_count} 站
              </p>
              {outOfSeason && r.season && (
                <p className="route-warn" role="status" data-testid={`route-season-warn-${r.id}`}>
                  当前日期没有这个活动：举办期是 {seasonText(r.season)}。
                </p>
              )}
              <ul className="route-chips" aria-label="例如">
                {r.examples.map((x) => (
                  <li key={x}>{x}</li>
                ))}
              </ul>
              {few && (
                <p className="route-warn" role="status" data-testid="route-days-warn">
                  你选了 {days} 天，少于推荐的 {r.days.min} 天：会按优先级取舍，先拿掉可选站（没排进来的站写在生成说明里）。
                  <button type="button" className="tool" data-testid="route-suggest-days" onClick={() => onSuggestDays(r.days.suggested)}>
                    改成建议的 {r.days.suggested} 天
                  </button>
                </p>
              )}
              {many && (
                <p className="route-warn" role="status" data-testid="route-days-warn">
                  你选了 {days} 天，这条路线的内容大约够 {r.days.max} 天，多出来的 {days - r.days.max} 天用路线对应的主题补充。
                </p>
              )}
              {on && d && (
                <div className="route-stops" data-testid="route-stops">
                  <div className="route-stops-head">
                    <b data-testid="route-stops-count">
                      要去的站：已选 {picked.size} / {all.length}
                    </b>
                    <button type="button" className="tool" data-testid="route-stops-all" onClick={() => onStops(null)}>
                      全选
                    </button>
                    <button type="button" className="tool" data-testid="route-stops-core" onClick={() => setPicked(core)}>
                      只留核心站
                    </button>
                  </div>
                  {d.chapters.map((c, i) => (
                    <fieldset key={c.id} className="route-stops-chapter">
                      <legend>
                        第 {i + 1} 章 · {c.title}
                      </legend>
                      {c.stops.map((s) => (
                        <label key={s.place_id} className="route-stop">
                          <input
                            type="checkbox"
                            checked={picked.has(s.place_id)}
                            data-testid={`route-stop-${s.place_id}`}
                            onChange={() => toggle(s.place_id)}
                          />
                          <span>
                            {s.name}
                            {s.priority === "optional" && <span className="tag">可选</span>}
                          </span>
                        </label>
                      ))}
                    </fieldset>
                  ))}
                </div>
              )}
              <button
                type="button"
                className="route-more"
                aria-expanded={open === r.id}
                data-testid={`route-more-${r.id}`}
                onClick={() => setOpen(open === r.id ? null : r.id)}
              >
                {open === r.id ? "收起" : "看看这条路线讲什么"}
              </button>
              {open === r.id && (
                <div className="route-detail" data-testid={`route-detail-${r.id}`}>
                  <p className="route-thesis">{r.thesis}</p>
                  {!d && !err && <p className="muted">读取中…</p>}
                  {err && <p className="muted">{err}</p>}
                  {d && (
                    <>
                      {d.chapters.map((c, i) => (
                        <section key={c.id} className="route-chapter">
                          <h5>
                            第 {i + 1} 章 · {c.title}
                          </h5>
                          <ol>
                            {c.stops.map((s) => (
                              <li key={s.place_id}>
                                <b>{s.name}</b>
                                {s.priority === "optional" && <span className="tag">可选</span>}
                                {s.why_here && <span className="muted"> · {s.why_here}</span>}
                              </li>
                            ))}
                          </ol>
                        </section>
                      ))}
                      {d.reading.length > 0 && (
                        <section className="route-reading">
                          <h5>延伸阅读 / 观影</h5>
                          <ul>
                            {d.reading.map((x) => (
                              <li key={x.title}>
                                <a href={x.source.url} target="_blank" rel="noreferrer">
                                  {x.title}
                                </a>{" "}
                                · {x.creator}，{x.year}
                                <span className="muted"> · {x.note}</span>
                              </li>
                            ))}
                          </ul>
                        </section>
                      )}
                      {d.notes.length > 0 && (
                        <ul className="route-notes muted">
                          {d.notes.map((n) => (
                            <li key={n}>{n}</li>
                          ))}
                        </ul>
                      )}
                    </>
                  )}
                </div>
              )}
            </article>
          );
        })}
            </div>
          </div>
        );
      })}
    </section>
  );
}
