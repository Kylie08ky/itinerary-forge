import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import { ErrorPopup, useConfirm } from "../confirm";
import { fmtMs, fmtTokens, PURPOSE_ZH } from "../components/TracePanel";
import type { LlmUsage, MapUsageRow } from "../types";

/** 模型用量：调用次数、失败数、平均耗时、token、费用（本地模型费用是 0），按用途分组 + 最近的调用。 */
export function UsagePage() {
  const [u, setU] = useState<LlmUsage | null>(null);
  const [maps, setMaps] = useState<MapUsageRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const confirm = useConfirm();

  function load() {
    api
      .llmUsage()
      .then(setU)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
    api
      .mapUsage()
      .then((r) => setMaps(r.services))
      .catch(() => setMaps([]));
  }
  useEffect(load, []);

  async function reset() {
    const yes = await confirm({
      title: "清零模型用量？",
      message: "会删掉所有模型调用记录（次数、耗时、token、费用），不影响任何行程。",
      confirmLabel: "清零",
      danger: true,
    });
    if (!yes) return;
    try {
      await api.resetLlmUsage();
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <>
      <div className="wrap">
        <ErrorPopup message={error} onClose={() => setError(null)} />
        <h1 className="page-title">模型用量</h1>
        <Link to="/about" className="about-link" data-testid="usage-about-link">
          <span>版本足迹</span>
          <span aria-hidden="true">›</span>
        </Link>
        {u && (
          <>
            <div className="stats-grid">
              <div className="stat-card">
                <b>{u.totals.calls}</b>
                <span>模型调用</span>
              </div>
              <div className="stat-card">
                <b>{u.totals.failures}</b>
                <span>失败（已降级为模板）</span>
              </div>
              <div className="stat-card">
                <b>{u.totals.calls ? fmtMs(u.totals.avg_latency_ms) : "—"}</b>
                <span>平均耗时</span>
              </div>
              <div className="stat-card">
                <b>${u.totals.cost_usd.toFixed(4)}</b>
                <span>
                  费用（token {u.totals.prompt_tokens} → {u.totals.completion_tokens}）
                </span>
              </div>
            </div>
            <div className="toolbar">
              <button className="tool danger" onClick={() => void reset()} disabled={u.totals.calls === 0}>
                清零统计
              </button>
            </div>
            <p className="muted">
              只记次数、耗时、token 和成败，不保存你输入的原话。本地模型费用为 0；换成付费模型时在 .env 里填单价就会自动统计。
            </p>

            {u.by_purpose.length === 0 && <div className="card muted">还没有模型调用记录。生成一份行程后再来看。</div>}
            {u.by_purpose.length > 0 && (
              <div className="card">
                <h2 style={{ marginTop: 0, fontSize: 16 }}>按用途</h2>
                <table className="usage-table">
                  <thead>
                    <tr>
                      <th>用途</th>
                      <th>次数</th>
                      <th>失败</th>
                      <th>平均耗时</th>
                      <th>token（入 → 出）</th>
                    </tr>
                  </thead>
                  <tbody>
                    {u.by_purpose.map((p) => (
                      <tr key={p.purpose}>
                        <td>{PURPOSE_ZH[p.purpose] ?? p.purpose}</td>
                        <td data-label="次数">{p.calls}</td>
                        <td data-label="失败">{p.failures}</td>
                        <td data-label="平均">{fmtMs(p.avg_latency_ms)}</td>
                        <td data-label="token">{fmtTokens({ prompt_tokens: p.prompt_tokens || null, completion_tokens: p.completion_tokens })}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {maps.length > 0 && (
              <div className="card" data-testid="map-usage">
                <h2 style={{ marginTop: 0, fontSize: 16 }}>高德本月用量</h2>
                <table className="usage-table">
                  <thead>
                    <tr>
                      <th>服务</th>
                      <th>已用</th>
                      <th>自留上限</th>
                      <th>免费额度</th>
                    </tr>
                  </thead>
                  <tbody>
                    {maps.map((m) => (
                      <tr key={m.service}>
                        <td>{m.label}</td>
                        <td data-label="已用">
                          {m.used}
                          {m.blocked ? "（当月已停用高德）" : m.warn ? "（快到上限）" : ""}
                        </td>
                        <td data-label="上限">{m.cap}</td>
                        <td data-label="免费额度">{m.free_allowance}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {u.recent.length > 0 && (
              <div className="card">
                <h2 style={{ marginTop: 0, fontSize: 16 }}>最近的调用</h2>
                {u.recent.map((c, i) => (
                  <div className={`trace-call${c.ok ? "" : " bad"}`} key={i}>
                    {new Date(c.at).toLocaleString("zh-CN", { hour12: false })} · {PURPOSE_ZH[c.purpose] ?? c.purpose} ·{" "}
                    {c.model} · {fmtMs(c.latency_ms)} · {c.ok ? "成功" : `失败：${c.error}`}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}
