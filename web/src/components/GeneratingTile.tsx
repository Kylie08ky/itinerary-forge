import { Link } from "react-router-dom";
import { STEP_LABEL } from "../taskSteps";
import type { TaskCard } from "../types";

/**
 * 「我的行程」里「正在生成」区的一张卡（规格 07 P19）：目的地 · 主题 · 日期、第几步 / 共几步、进度条；
 * 点开回到进度页。失败的（含服务重启被中断的）写明原因，可以重新生成或移除。
 */
export function GeneratingTile({
  c,
  onRemove,
  disabled,
}: {
  c: TaskCard;
  onRemove: (c: TaskCard) => void;
  disabled?: boolean;
}) {
  const failed = c.status === "failed";
  const stepTitle = c.current_step ? STEP_LABEL[c.current_step]?.title ?? c.current_step : "";
  const pct = Math.round((c.steps_done / Math.max(1, c.steps_total)) * 100);
  const head = (
    <div className="gen-head">
      <b>
        {c.destination} · {c.theme_name}
      </b>
      <span className="muted">
        {c.start_date} → {c.end_date} · {c.days} 天
      </span>
    </div>
  );
  const bar = (
    <div className="gen-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <span style={{ width: `${Math.max(pct, 6)}%` }} />
    </div>
  );

  if (failed) {
    return (
      <div className="card gen-tile failed" data-testid="gen-tile" data-status="failed">
        {head}
        <div className="gen-status err">生成没有完成</div>
        {bar}
        <p className="muted gen-error">{c.error}</p>
        <div className="gen-actions">
          <Link to="/new" className="btn-link primary">
            重新生成
          </Link>
          <Link to={`/tasks/${c.id}`} className="tool">
            查看详情
          </Link>
          <button type="button" className="tool danger" disabled={disabled} onClick={() => onRemove(c)}>
            移除
          </button>
        </div>
      </div>
    );
  }
  return (
    <Link to={`/tasks/${c.id}`} className="card gen-tile running" data-testid="gen-tile" data-status={c.status}>
      {head}
      <div className="gen-status">
        <span className="spin" aria-hidden />
        {c.status === "queued"
          ? "排队中，马上开始…"
          : `第 ${Math.min(c.steps_done + 1, c.steps_total)} / ${c.steps_total} 步 · ${stepTitle}…`}
      </div>
      {bar}
      <span className="muted gen-hint">点开看每一步的进度；可以放心离开，它会在后台继续</span>
    </Link>
  );
}
