import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { STEP_LABEL } from "../taskSteps";
import type { Task, TaskStep } from "../types";

const POLL_MS = 700;

function seconds(step: TaskStep): string | null {
  if (!step.started_at || !step.finished_at) return null;
  const ms = new Date(step.finished_at).getTime() - new Date(step.started_at).getTime();
  return ms < 100 ? "<0.1 秒" : `${(ms / 1000).toFixed(1)} 秒`;
}

const MARK: Record<TaskStep["state"], string> = { pending: "○", running: "◔", done: "✓", failed: "✕" };

export function TaskPage() {
  const { taskId = "" } = useParams();
  const navigate = useNavigate();
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function tick() {
      try {
        const t = await api.getTask(taskId);
        if (stop) return;
        setTask(t);
        setError(null);
        if (t.status === "succeeded" && t.plan_id) {
          navigate(`/plans/${t.plan_id}`, { replace: true });
          return;
        }
        if (t.status === "failed") return;
      } catch (err) {
        if (stop) return;
        setError(err instanceof ApiError ? err.message : String(err));
        if (err instanceof ApiError && err.status === 404) return;
      }
      timer = setTimeout(tick, POLL_MS);
    }
    void tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [taskId, navigate]);

  const failed = task?.status === "failed";

  return (
    <>
      <div className="wrap">
        <div className="card">
          <h1 style={{ marginTop: 0, fontSize: 22 }}>{failed ? "生成没有完成" : "正在生成行程"}</h1>
          <p className="muted" style={{ marginTop: 0 }}>
            任务号 {taskId} · 可以刷新页面或稍后回来，进度不会丢。
          </p>
          {!failed && (
            <p className="muted" data-testid="task-leave-hint" style={{ marginTop: 0 }}>
              可以离开这个页面，生成会在后台继续；回到 <Link to="/plans">「我的行程」</Link>，顶部「正在生成」里能找到它。
            </p>
          )}

          {error && <div className="banner err">{error}</div>}

          {task && (
            <ol className="progress-steps">
              {task.steps.map((s) => (
                <li key={s.name} className={`pstep ${s.state}`}>
                  <span className="pmark" aria-hidden>
                    {MARK[s.state]}
                  </span>
                  <div>
                    <b>{STEP_LABEL[s.name]?.title ?? s.name}</b>
                    <div className="muted">{STEP_LABEL[s.name]?.hint}</div>
                  </div>
                  <span className="ptime">{s.state === "running" ? "进行中…" : seconds(s)}</span>
                </li>
              ))}
            </ol>
          )}
          {!task && !error && <div className="muted">读取任务状态…</div>}

          {failed && (
            <>
              <div className="banner err">{task?.error}</div>
              <Link to="/" className="primary-link">
                ← 回到表单重新生成
              </Link>
            </>
          )}
          {error && !task && (
            <Link to="/" className="primary-link">
              ← 回到表单
            </Link>
          )}
        </div>
      </div>
    </>
  );
}
