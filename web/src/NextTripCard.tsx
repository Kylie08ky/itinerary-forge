import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import { IconChevron } from "./icons";
import type { PlanCard } from "./types";

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** 最近的、还没出发的行程（按开始日期 00:00 算）；没有就返回 null。 */
export function nextTrip(cards: PlanCard[], nowMs: number): PlanCard | null {
  const upcoming = cards
    .map((c) => ({ c, t: new Date(`${c.start_date}T00:00:00`).getTime() }))
    .filter((x) => x.t > nowMs)
    .sort((a, b) => a.t - b.t);
  return upcoming[0]?.c ?? null;
}

/**
 * 首页的「下一站」倒计时卡（规格 06-ui-design S5）：距离出发的天 / 时 / 分 / 秒，每秒走一格。
 * 没有即将出发的行程时不显示。
 */
export function NextTripCard() {
  const [plan, setPlan] = useState<PlanCard | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    api
      .planCards(false)
      .then((cards) => setPlan(nextTrip(cards, Date.now())))
      .catch(() => setPlan(null));
  }, []);

  useEffect(() => {
    if (!plan) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [plan]);

  if (!plan) return null;
  const left = Math.max(0, new Date(`${plan.start_date}T00:00:00`).getTime() - now);
  const s = Math.floor(left / 1000);
  const parts: [string, string][] = [
    [String(Math.floor(s / 86400)), "天"],
    [pad(Math.floor((s % 86400) / 3600)), "小时"],
    [pad(Math.floor((s % 3600) / 60)), "分"],
    [pad(s % 60), "秒"],
  ];
  const city = plan.title.split(" · ")[0];

  return (
    <section className="card next-trip" data-testid="next-trip" aria-label="下一站倒计时">
      <header>
        <h2>下一站，{city}</h2>
        <Link to={`/plans/${plan.plan_id}`} className="next-link">
          <span className="next-link-text">查看行程</span>
          <IconChevron size={15} />
        </Link>
      </header>
      <div className="muted">距离出发</div>
      <div className="countdown" role="timer">
        {parts.map(([n, label], i) => (
          <div className="cd-cell" key={label}>
            <b data-testid={i === 3 ? "cd-seconds" : undefined}>{n}</b>
            <span>{label}</span>
          </div>
        ))}
      </div>
      <div className="next-meta muted">
        {plan.start_date.slice(5).replace("-", ".")} — {plan.end_date.slice(5).replace("-", ".")} · {plan.days} 天 · {plan.title}
      </div>
    </section>
  );
}
