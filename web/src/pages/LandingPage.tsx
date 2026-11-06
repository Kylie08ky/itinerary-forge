import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { HomeHero } from "../components/HomeHero";
import { PlanTile } from "../components/PlanTile";
import { ActiveToday } from "../components/TodayCard";
import { NextTripCard } from "../NextTripCard";
import { useReadOnly } from "../offline";
import { groupTrips, nextOrCurrent } from "../tripPhase";
import type { CityInfo, PlaceImageInfo, PlanCard } from "../types";

/** 没有行程时轮换各城市的大图：按日期轮（同一天内不变），现在只有巴黎所以先只会是巴黎。 */
function rotatingHero(cities: CityInfo[], now: Date = new Date()): PlaceImageInfo | null {
  const withHero = cities.filter((c) => c.hero);
  if (withHero.length === 0) return null;
  const dayNo = Math.floor(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 86_400_000);
  return withHero[dayNo % withHero.length].hero;
}

/**
 * 首页 `/`（规格 07 P01–P04）：整个产品的入口，不绑定任何城市。
 * 全屏英雄图（有下一站用那座城市的大图，没有就轮换各城市的）+ 「你想去哪里？」一句话输入；
 * 写完点「开始规划」跳到 /new，带着这句话自动解析、填好表单。英雄下面是下一站倒计时和即将出发的行程。
 */
export function LandingPage() {
  const navigate = useNavigate();
  const { readOnly } = useReadOnly();
  const [cities, setCities] = useState<CityInfo[]>([]);
  const [cards, setCards] = useState<PlanCard[] | null>(null);

  useEffect(() => {
    api.cities().then(setCities).catch(() => setCities([]));
    api.planCards(false).then(setCards).catch(() => setCards([]));
  }, []);

  const fullCities = cities.filter((c) => (c.tier ?? "full") === "full"); // 一日游目的地不当「去哪座城市」
  const next = cards ? nextOrCurrent(cards) : null;
  const hero = next?.hero ?? rotatingHero(cities);
  const upcoming = cards ? groupTrips(cards).upcoming.slice(0, 3) : [];

  return (
    <>
      <HomeHero
        hero={hero}
        busy={false}
        disabled={readOnly}
        onRead={(text) => navigate("/new", { state: { nl: text } })}
        onImport={() => navigate("/new", { state: { importNotes: true } })}
        next={<NextTripCard />}
      />
      {(next || fullCities.length > 1) && (
        <div className="wrap wide landing-below">
          <ActiveToday />
          {upcoming.length > 0 && (
            <section className="landing-section" data-testid="upcoming-trips">
              <div className="section-head">
                <h2>即将出发</h2>
                <Link to="/plans" className="next-link">
                  全部行程 →
                </Link>
              </div>
              <div className="plan-grid">
                {upcoming.map((c) => (
                  <PlanTile key={c.plan_id} c={c} />
                ))}
              </div>
            </section>
          )}
          {fullCities.length > 1 && (
            <section className="landing-section" data-testid="city-entries">
              <div className="section-head">
                <h2>去哪座城市</h2>
              </div>
              <div className="chips">
                {fullCities.map((c) => (
                  <Link key={c.id} to="/new" state={{ city: c.id }} className="chip city-chip">
                    {c.name_zh}
                  </Link>
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </>
  );
}
