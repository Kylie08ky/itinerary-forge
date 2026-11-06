import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { PlaceCard } from "../components/PlaceCard";
import { RouteMap } from "../components/RouteMap";
import { SharedCompanion } from "../components/SharedCompanion";
import type { AttractionRef, SharedPlan } from "../types";
import { ticketTotal } from "../money";
import { applyCity, DEFAULT_CITY } from "../themes";
import { useCollapsedDays } from "../useCollapsedDays";
import { useMedia } from "../useMedia";
import { BookingAndRisks, BUDGET_ZH, DayTimeline, DayTools, Skeleton, STYLE_ZH } from "../ui";

/** 公开只读页（规格 M08 S5）：没有账号的朋友打开的行程；没有编辑、版本、分享入口。 */
export function SharePage() {
  const { token = "" } = useParams();
  const [data, setData] = useState<SharedPlan | null>(null);
  const [gone, setGone] = useState<"expired" | "missing" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attractions, setAttractions] = useState<AttractionRef[]>([]);
  const [cardId, setCardId] = useState<string | null>(null);
  const days = useCollapsedDays(`s:${token}`);
  const wide = useMedia("(min-width: 1080px)");

  useEffect(() => {
    api
      .readShare(token)
      .then((d) => {
        setData(d);
        api.places(d.city_id).then(setAttractions).catch(() => setAttractions([]));
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 410) setGone("expired");
        else if (e instanceof ApiError && e.status === 404) setGone("missing");
        else setError(e instanceof Error ? e.message : String(e));
      });
  }, [token]);

  // 套用行程所在城市的配色（和行程页一致）；离开时还原
  const destination = data?.itinerary.trip.destination;
  useEffect(() => {
    if (destination) applyCity(destination);
    return () => applyCity(DEFAULT_CITY);
  }, [destination]);

  // 地点编号 -> 照片路径：全程地图悬停气泡用（分享页也带照片，规格 M02 S7b-3）
  const photoMap = useMemo(
    () =>
      Object.fromEntries(
        attractions.filter((a) => a.place_id && a.media?.image).map((a) => [a.place_id as string, a.media!.image!.url]),
      ),
    [attractions],
  );

  if (gone) {
    return (
      <div className="wrap">
        <div className="card center share-gone">
          <h1>{gone === "expired" ? "链接已失效" : "没有这个分享链接"}</h1>
          <p className="muted">
            {gone === "expired" ? "分享的人已经关闭了这个链接。如果还想看，请让 TA 重新发一个。" : "请确认链接是否完整。"}
          </p>
        </div>
      </div>
    );
  }
  if (error) {
    return (
      <div className="wrap">
        <div className="banner err">{error}</div>
      </div>
    );
  }
  if (!data) return <Skeleton />;

  const it = data.itinerary;
  const weather = data.weather?.available ? data.weather : null;
  const hasMap = data.route.days.some((d) => d.stops.length > 0);
  const map = (
    <div className={wide ? "card aside-card" : "card"}>
      {wide ? (
        <div className="aside-head">
          <h2>全程地图</h2>
          <span className="muted">
            {data.route.place_count} 个地点 · {data.route.total_km} 公里（直线累计）
          </span>
        </div>
      ) : (
        <h2 style={{ marginTop: 0, fontSize: 16 }}>
          全程地图 <span className="muted">· {data.route.place_count} 个地点 · {data.route.total_km} 公里（直线累计）</span>
        </h2>
      )}
      <RouteMap route={data.route} onOpenPlace={setCardId} photos={photoMap} />
    </div>
  );
  return (
    <>
      <div className="topbar">
        <span className="brand">AI 旅行规划</span>
        <span className="muted">只读分享 · v{data.version}</span>
      </div>
      <div className="wrap wide">
        <div className={`plan-layout${wide && hasMap ? " with-aside" : ""}`}>
          <div className="plan-main">
            <section className="trip-head plan-hero share-hero-card" id="sec-top">
              <div className="plan-title-compact" data-testid="share-hero">
                <h2>
                  {it.trip.destination} · {it.days.length} 天行程
                </h2>
                <div className="sub">
                  {it.trip.date_range.start} → {it.trip.date_range.end}
                </div>
                <div className="chips on-cover">
                  <span className="chip">{STYLE_ZH[it.trip.traveler_profile.style] ?? it.trip.traveler_profile.style}</span>
                  <span className="chip">{BUDGET_ZH[it.trip.traveler_profile.budget] ?? it.trip.traveler_profile.budget}</span>
                  {it.trip.traveler_profile.interests.map((x) => (
                    <span className="chip" key={x}>
                      {x}
                    </span>
                  ))}
                </div>
                <p className="share-note">这是朋友分享给你的只读行程，不能修改。出发前请以各景点官网信息为准。</p>
              </div>
              <div className="stat-tiles" data-testid="share-stats">
                <div className="stat-tile">
                  <b>{it.days.length}</b>
                  <span>天</span>
                </div>
                <div className="stat-tile">
                  <b>{data.route.place_count}</b>
                  <span>个地点</span>
                </div>
                <div className="stat-tile">
                  <b>{data.route.total_km}</b>
                  <span>公里（直线累计）</span>
                </div>
                <div className="stat-tile">
                  <b>{ticketTotal(data.route.ticket_eur, data.route.ticket_currency)}</b>
                  <span>门票合计</span>
                </div>
              </div>
            </section>
            {!wide && hasMap && map}

        <DayTools
          note={weather?.note}
          onExpandAll={days.expandAll}
          onCollapseAll={() => days.collapseAll(it.days.map((x) => x.date))}
        />
        {it.days.map((d, i) => (
          <DayTimeline
            key={d.date}
            day={d}
            index={i}
            changed={false}
            attractions={attractions}
            reasons={it.generation?.day_reasons[d.date] ?? []}
            onOpenPlace={setCardId}
            navUrls={data.route.days[i]?.google_urls}
            amapUrls={data.route.days[i]?.amap_urls}
            weather={weather?.days.find((w) => w.date === d.date)}
            collapsed={days.collapsed.has(d.date)}
            onToggle={() => days.toggle(d.date)}
            notes={data.notes}
            stories={data.stories}
          />
        ))}
        <BookingAndRisks it={it} attractions={attractions} />
        <SharedCompanion data={data} />
          </div>
          {wide && hasMap && <aside className="plan-aside" aria-label="全程地图">{map}</aside>}
        </div>
      </div>
      {cardId && <PlaceCard placeId={cardId} city={data?.city_id} onClose={() => setCardId(null)} />}
    </>
  );
}
