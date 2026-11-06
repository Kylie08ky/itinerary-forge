import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import { ErrorPopup, useConfirm } from "../confirm";
import { IconArchive, IconPlus, IconSearch, IconTrash } from "../icons";
import { useReadOnly } from "../offline";
import { GeneratingTile } from "../components/GeneratingTile";
import { PlanTile } from "../components/PlanTile";
import { refreshUnfinishedTasks, useUnfinishedTasks } from "../useUnfinishedTasks";
import { groupTrips } from "../tripPhase";
import type { PlanCard, TaskCard } from "../types";

function stamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} 生成`;
}

export function PlansPage() {
  const [active, setActive] = useState<PlanCard[] | null>(null);
  const [archived, setArchived] = useState<PlanCard[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { readOnly } = useReadOnly();
  const confirm = useConfirm();

  function load() {
    Promise.all([api.planCards(false), api.planCards(true)])
      .then(([a, b]) => {
        setActive(a);
        setArchived(b);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
  }
  useEffect(load, []);

  // 「正在生成」：有任务生成完了（从列表里消失），就重新读行程列表，它会变成正常的行程卡
  const tasks = useUnfinishedTasks();
  const seenRunning = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (tasks === null) return;
    const now = new Set(tasks.filter((t) => t.status !== "failed").map((t) => t.id));
    if ([...seenRunning.current].some((id) => !tasks.some((t) => t.id === id))) load();
    seenRunning.current = now;
  }, [tasks]);

  async function removeTask(c: TaskCard) {
    try {
      await api.removeTask(c.id);
      refreshUnfinishedTasks();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function toggleArchive(e: React.MouseEvent, c: PlanCard) {
    e.preventDefault(); // 按钮在整张卡片的链接里，不要触发跳转
    e.stopPropagation();
    try {
      await api.archivePlan(c.plan_id, !c.archived);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function remove(e: React.MouseEvent, c: PlanCard) {
    e.preventDefault();
    e.stopPropagation();
    const yes = await confirm({
      title: "永久删除这份行程？",
      message: `「${c.title}」（${c.start_date}）会连同所有版本和分享链接一起删掉，不能恢复。`,
      confirmLabel: "永久删除",
      danger: true,
    });
    if (!yes) return;
    try {
      await api.deletePlan(c.plan_id);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  const all = showArchived ? archived : active;
  const q = query.trim().toLowerCase();
  const cards = all && q ? all.filter((c) => `${c.title} ${c.start_date} ${c.end_date} ${c.theme_name ?? ""}`.toLowerCase().includes(q)) : all;
  const groups = groupTrips(cards ?? []);
  const PAST_SHOWN = 6; // 已结束的默认只展开最近这么多条（规格 07 P05）

  const tile = (c: PlanCard) => (
    <PlanTile
      key={c.plan_id}
      c={c}
      footer={
        <div className="card-foot">
          <span className="muted">{stamp(c.created_at)}</span>
          <span className="card-actions">
            <button className="tool" disabled={readOnly} onClick={(e) => void toggleArchive(e, c)}>
              <IconArchive size={14} />
              {c.archived ? "恢复" : "归档"}
            </button>
            <button className="tool danger" disabled={readOnly} onClick={(e) => void remove(e, c)}>
              <IconTrash size={14} />
              删除
            </button>
          </span>
        </div>
      }
    />
  );

  return (
    <div className="wrap wide plans-page">
      <ErrorPopup message={error} onClose={() => setError(null)} />
      <div className="page-head">
        <h1>我的行程</h1>
        <span className="spacer" />
        <label className="plans-search">
          <IconSearch size={18} />
          <input
            type="search"
            data-testid="plans-search"
            aria-label="搜索行程"
            placeholder="搜索"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <div className="seg-switch" role="group" aria-label="列表范围">
          <button className={`vbtn${!showArchived ? " active" : ""}`} onClick={() => setShowArchived(false)}>
            进行中 ({active?.length ?? 0})
          </button>
          <button className={`vbtn${showArchived ? " active" : ""}`} onClick={() => setShowArchived(true)}>
            已归档 ({archived.length})
          </button>
        </div>
        <Link to="/new" className="btn-link primary">
          <IconPlus size={16} />
          新建
        </Link>
      </div>

      {!showArchived && tasks && tasks.length > 0 && (
        <section className="trip-group" data-testid="group-generating">
          <h2 className="group-title">
            正在生成 <span className="muted">{tasks.length}</span>
          </h2>
          <div className="gen-grid">
            {tasks.map((t) => (
              <GeneratingTile key={t.id} c={t} onRemove={(x) => void removeTask(x)} disabled={readOnly} />
            ))}
          </div>
        </section>
      )}

      {cards && cards.length === 0 && q && <div className="card muted" data-testid="plans-no-match">没有匹配的行程</div>}
      {cards && cards.length === 0 && !q && showArchived && <div className="card muted">没有归档的行程。</div>}
      {cards && cards.length === 0 && !q && !showArchived && !(tasks && tasks.length > 0) && (
        <div className="card empty-state">
          <div className="empty-icon">🧳</div>
          <h2>还没有行程</h2>
          <Link to="/new" className="btn-link primary">
            去生成一份 →
          </Link>
        </div>
      )}

      {groups.upcoming.length > 0 && (
        <section className="trip-group" data-testid="group-upcoming">
          <h2 className="group-title">
            即将到来 <span className="muted">{groups.upcoming.length}</span>
          </h2>
          <div className="plan-grid">{groups.upcoming.map(tile)}</div>
        </section>
      )}
      {groups.past.length > 0 && (
        <section className="trip-group" data-testid="group-past">
          <h2 className="group-title">
            已结束 <span className="muted">{groups.past.length}</span>
          </h2>
          <div className="plan-grid">{groups.past.slice(0, PAST_SHOWN).map(tile)}</div>
          {groups.past.length > PAST_SHOWN && (
            <details className="past-more">
              <summary>更多已结束的行程（{groups.past.length - PAST_SHOWN}）</summary>
              <div className="plan-grid">{groups.past.slice(PAST_SHOWN).map(tile)}</div>
            </details>
          )}
        </section>
      )}
    </div>
  );
}
