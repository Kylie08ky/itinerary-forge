import { useState } from "react";
import { api, ApiError } from "../api";
import type { LlmCall, TaskTrace } from "../types";

export const PURPOSE_ZH: Record<string, string> = {
  narration: "写每天的说明",
  nl_edit: "理解一句话修改",
  freeform_generate: "自由生成（旧流程）",
  other: "其它",
};
export const STEP_ZH: Record<string, string> = {
  library: "读取地点库",
  schedule: "选点并排程",
  validate: "质检",
  narrate: "生成说明",
};

export function fmtMs(ms: number): string {
  return ms < 1000 ? `${ms} 毫秒` : `${(ms / 1000).toFixed(1)} 秒`;
}
export function fmtTokens(c: Pick<LlmCall, "prompt_tokens" | "completion_tokens">): string {
  return c.prompt_tokens == null ? "—" : `${c.prompt_tokens} → ${c.completion_tokens ?? 0}`;
}

/** 这份行程是怎么生成的：每一步耗时、模型调用（次数 / 耗时 / token / 成败）、是否降级（规格 M07 S6）。 */
export function TracePanel({ planId }: { planId: string }) {
  const [trace, setTrace] = useState<TaskTrace | null>(null);
  const [error, setError] = useState<string | null>(null);

  function onToggle(open: boolean) {
    if (!open || trace || error) return;
    api
      .planTrace(planId)
      .then(setTrace)
      .catch((e) =>
        setError(e instanceof ApiError && e.status === 404 ? "这份行程不是后台任务生成的，没有生成轨迹。" : String(e)),
      );
  }

  return (
    <details className="trace" onToggle={(e) => onToggle(e.currentTarget.open)}>
      <summary>生成过程（耗时与模型调用）</summary>
      {error && <div className="muted">{error}</div>}
      {!trace && !error && <div className="muted">读取中…</div>}
      {trace && (
        <>
          <ol className="trace-steps">
            {trace.steps.map((s) => (
              <li key={s.name}>
                <span>{STEP_ZH[s.name] ?? s.name}</span>
                <span className="muted">{s.seconds == null ? "—" : `${s.seconds} 秒`}</span>
              </li>
            ))}
          </ol>
          <div className="trace-sum">
            模型调用 <b>{trace.llm.totals.calls}</b> 次，失败 <b>{trace.llm.totals.failures}</b> 次
            {trace.llm.totals.prompt_tokens > 0 &&
              `，token ${trace.llm.totals.prompt_tokens} → ${trace.llm.totals.completion_tokens}`}
            ，费用 ${trace.llm.totals.cost_usd.toFixed(4)}
          </div>
          {trace.calls.map((c, i) => (
            <div className={`trace-call${c.ok ? "" : " bad"}`} key={i}>
              {PURPOSE_ZH[c.purpose] ?? c.purpose} · {c.model} · {fmtMs(c.latency_ms)} · {c.ok ? "成功" : `失败：${c.error}`}
            </div>
          ))}
          {trace.degraded && <div className="banner warn">已降级：{trace.degraded_reason}</div>}
        </>
      )}
    </details>
  );
}
