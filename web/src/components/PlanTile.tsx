import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { mediaSrc } from "../api";
import { useHeroStatus } from "../hero";
import { coverStyle, themeLook } from "../themes";
import { tripPhase } from "../tripPhase";
import type { PlanCard } from "../types";

/**
 * 一张行程卡（规格 07 P03）：封面是这趟行程里标志性地点的大图，压暗后叠上倒计时（还有 N 天 / 进行中 · 第 N 天 /
 * 已结束 N 天前）；没有大图退回城市主色渐变 + 主题图标。首页「即将出发」和「我的行程」共用。
 * footer 由用的人传（归档 / 删除按钮）。
 */
const TILE_SLIDE_MS = 4000;

/** 多城市行程的卡片封面：各座城市的图轮流切换（只有一张或减少动态效果时不动）。 */
function useTileHero(c: PlanCard): PlanCard["hero"] {
  const heroes = (c.cities ?? []).flatMap((x) => (x.hero ? [x.hero] : []));
  const [at, setAt] = useState(0);
  const count = heroes.length;
  useEffect(() => {
    if (count < 2 || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const t = setInterval(() => setAt((i) => (i + 1) % count), TILE_SLIDE_MS);
    return () => clearInterval(t);
  }, [count]);
  return count > 0 ? heroes[at % count] : c.hero;
}

export function PlanTile({ c, footer, now }: { c: PlanCard; footer?: React.ReactNode; now?: Date }) {
  const hero = useTileHero(c);
  const status = useHeroStatus(hero);
  const photo = status === "ready" && hero;
  const phase = tripPhase(c.start_date, c.end_date, now);
  const look = themeLook(c.theme_id);
  const problems = c.errors + c.warnings;
  return (
    <Link to={`/plans/${c.plan_id}`} className="plan-card" data-testid="plan-card" data-phase={phase.kind}>
      <div
        className={`plan-cover${photo ? " has-photo" : ""}`}
        style={
          photo
            ? { backgroundImage: `linear-gradient(180deg, rgba(8,14,26,0.28), rgba(8,14,26,0.62)), url("${mediaSrc(hero.url)}")` }
            : coverStyle(c.theme_id)
        }
      >
        {!photo && (
          <span className="cover-icon" aria-hidden>
            {look.icon}
          </span>
        )}
        <span className="cover-countdown" data-testid="plan-countdown">
          {phase.label}
        </span>
        <div className="cover-main">
          <h2>{c.title}</h2>
          <span className="cover-dates">
            {c.start_date.slice(5).replace("-", ".")} — {c.end_date.slice(5).replace("-", ".")}
          </span>
        </div>
      </div>
      <div className="plan-body">
        <div className="chips">
          <span className="chip days">{c.days} 天</span>
          {c.theme_name && <span className="chip">{c.theme_name}主题</span>}
          <span className="chip">v{c.latest_version}</span>
          {problems > 0 && <span className={`chip ${c.errors > 0 ? "bad" : "warn"}`}>{problems} 条提醒</span>}
          {c.copied_from && <span className="chip">副本</span>}
        </div>
        {footer}
      </div>
    </Link>
  );
}
