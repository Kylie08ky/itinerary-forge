import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { mapName, navHere, searchNearby } from "../nav";
import { buildToday, fmtMinutes, tzFor, useActiveTrip, useNowMs } from "../today";
import type { AttractionRef, CultureStory, Itinerary, PlanDetail } from "../types";
import { Stories } from "./Stories";
import { matchFor, noteRef, weekday } from "../ui";

/** 午餐 / 晚餐 / 餐饮占位：没有坐标，导航改成「搜附近餐厅」。 */
const isMeal = (type: string) => type === "meal" || type === "food";

const STATE_ZH: Record<string, string> = { done: "已结束", current: "进行中", next: "下一站", later: "" };

/**
 * 「今天」卡片（规格 M15 S2b）：按目的地时区的现在，显示正在进行 / 下一站（含导航）/ 今天全部安排的小时间轴。
 * 每 30 秒自己刷新状态。今天不在这份行程里就什么都不画。
 */
export function TodayCard({
  itinerary,
  attractions,
  planId,
  big = false,
  notes,
  stories,
}: {
  itinerary: Itinerary;
  attractions: AttractionRef[];
  planId?: string; // 给了就在右上角放「查看行程」链接（首页用）
  big?: boolean; // /today 整页：字更大，并显示每条安排的地点和备注
  notes?: Record<string, string>;
  stories?: Record<string, CultureStory[]>; // 地点 → 作品里的故事（规格 09 十五）：现在 / 下一站有就多一行
}) {
  const now = useNowMs(30000);
  const v = buildToday(itinerary, tzFor(itinerary.trip.destination), now);
  if (v.status !== "during") return null;

  const nextMatch = v.next && attractions.length ? matchFor({ place_id: v.next.placeId, title: v.next.title }, attractions) : null;
  const currentMatch =
    v.current && attractions.length ? matchFor({ place_id: v.current.placeId, title: v.current.title }, attractions) : null;
  const near = currentMatch ?? nextMatch ?? attractions[0]; // 今天在哪：用来选高德 / Google
  const noneStarted = v.items.every((x) => x.state !== "done" && x.state !== "current");
  const dayDate = itinerary.days[v.dayIndex].date;

  return (
    <section className={`card today-card${big ? " big" : ""}`} data-testid="today-card" aria-label="今天">
      <header className="today-head">
        <h2>今天</h2>
        <span className="today-meta" data-testid="today-meta">
          第 {v.dayIndex + 1} 天 / 共 {v.dayCount} 天 · {v.date}（周{weekday(v.date)}）
        </span>
        {planId && (
          <Link to={`/plans/${planId}`} className="next-link">
            查看行程
          </Link>
        )}
      </header>

      {v.current && (
        <div className="today-now" data-testid="today-now">
          <span className="today-label now">现在</span>
          <b>{v.current.title}</b>
          <span className="muted">
            （到 {v.current.end} 结束，还有 {fmtMinutes(v.minutesToEnd ?? 0)}）
          </span>
          {isMeal(v.current.type) && !currentMatch && (
            <a className="btn-ghost today-nav" href={searchNearby("餐厅 restaurant", near)} target="_blank" rel="noreferrer">
              🍽 搜附近餐厅 ↗
            </a>
          )}
          {v.current.placeId && stories?.[v.current.placeId] && <Stories stories={stories[v.current.placeId]} limit={1} />}
        </div>
      )}
      {v.next ? (
        <div className="today-next" data-testid="today-next">
          <span className="today-label next">{noneStarted ? "今天第一站" : "下一站"}</span>
          <b>{v.next.title}</b>
          <span className="muted">
            · {v.next.time} 开始，还有 {fmtMinutes(v.minutesToNext ?? 0)}
            {v.next.location ? ` · ${v.next.location}` : ""}
          </span>
          {nextMatch ? (
            <a className="btn-ghost today-nav" href={navHere(nextMatch)} target="_blank" rel="noreferrer">
              🧭 导航 ↗
            </a>
          ) : (
            isMeal(v.next.type) && (
              <a className="btn-ghost today-nav" href={searchNearby("餐厅 restaurant", near)} target="_blank" rel="noreferrer">
                🍽 搜附近餐厅 ↗
              </a>
            )
          )}
          {v.next.placeId && stories?.[v.next.placeId] && <Stories stories={stories[v.next.placeId]} limit={1} />}
        </div>
      ) : (
        !v.current && (
          <div className="today-done" data-testid="today-done">
            今天的安排都完成了。
            {v.tomorrowFirst && (
              <span className="muted">
                {" "}
                明天第一站：{v.tomorrowFirst.title}（{v.tomorrowFirst.time}）
              </span>
            )}
          </div>
        )
      )}
      <div className="muted today-remaining" data-testid="today-remaining">
        今天还剩 {v.remaining} 个安排
      </div>

      <ol className="today-list" aria-label="今天的安排">
        {v.items.map((x) => (
          <li key={x.index} className={`today-item ${x.state}`} data-state={x.state} data-testid="today-item">
            <span className="t">{x.time}</span>
            <span className="title">
              {x.title}
              {STATE_ZH[x.state] && <span className={`today-label ${x.state}`}>{STATE_ZH[x.state]}</span>}
            </span>
            {(() => {
              const m = attractions.length ? matchFor({ place_id: x.placeId, title: x.title }, attractions) : null;
              if (m) {
                return (
                  <a
                    className="today-row-nav"
                    href={navHere(m)}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`导航到${x.title}`}
                    title="从你现在的位置导航到这里"
                  >
                    🧭 导航
                  </a>
                );
              }
              return isMeal(x.type) && x.state !== "done" ? (
                <a
                  className="today-row-nav"
                  href={searchNearby("餐厅 restaurant", near)}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`搜${x.title}附近的餐厅`}
                  title={`在${mapName(near)}里搜你附近的餐厅`}
                >
                  🍽 附近餐厅
                </a>
              ) : null;
            })()}
            {big && x.location && <span className="muted loc">{x.location}</span>}
            {big && notes?.[noteRef(dayDate, { place_id: x.placeId, title: x.title })] && (
              <span className="today-note">
                📝 {notes[noteRef(dayDate, { place_id: x.placeId, title: x.title })]}
              </span>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * 找到正在进行的行程、取回行程和地点库，画「今天」卡片。首页和 /today 页用；
 * 没有正在进行的行程：big 时显示一句说明，否则什么都不画。
 */
export function ActiveToday({ big = false }: { big?: boolean }) {
  const { loading, card } = useActiveTrip();
  const [data, setData] = useState<{ plan: PlanDetail; attractions: AttractionRef[]; notes: Record<string, string> } | null>(
    null,
  );
  const planId = card?.plan_id ?? null;
  const version = card?.latest_version;

  useEffect(() => {
    if (!planId) {
      setData(null);
      return;
    }
    let live = true;
    Promise.all([
      api.getPlan(planId),
      api.attractions().catch(() => [] as AttractionRef[]),
      big ? api.notes(planId).catch(() => ({}) as Record<string, string>) : Promise.resolve({} as Record<string, string>),
    ])
      .then(([plan, attractions, notes]) => live && setData({ plan, attractions, notes }))
      .catch(() => live && setData(null));
    return () => {
      live = false;
    };
  }, [planId, version, big]);

  if (loading) return big ? <p className="muted center">加载中…</p> : null;
  if (!card || !data) {
    return big ? (
      <div className="card center today-empty" data-testid="today-empty">
        <h2>今天不在任何行程里</h2>
        <p className="muted">行程进行中，这里会显示今天的安排、下一站和导航。</p>
        <Link to="/plans" className="primary-link">
          我的行程
        </Link>
      </div>
    ) : null;
  }
  return (
    <TodayCard
      itinerary={data.plan.itinerary}
      attractions={data.attractions}
      planId={big ? undefined : card.plan_id}
      big={big}
      notes={data.notes}
      stories={data.plan.stories}
    />
  );
}
