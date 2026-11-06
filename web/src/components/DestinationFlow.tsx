import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { DestinationChoice } from "../types";

export interface FlowCity {
  id: string;
  name_zh: string;
}

type Flow =
  | { kind: "resolving" }
  | { kind: "choices"; reason: string; choices: DestinationChoice[]; many: boolean; country: boolean }
  | { kind: "building"; name: string; message: string }
  | { kind: "ready"; cities: FlowCity[] }
  | { kind: "error"; message: string; title?: string };

/**
 * 目的地不在库里时的流程（规格 10）：认城市 → （国家 / 同名时选一个）→ 现建（后台几分钟，有进度）→ 准备好。
 * 全部在页面内的弹窗里完成，不用原生对话框；建好后点「继续」回到表单，由用户点「生成」。
 */
export function DestinationFlow({
  text,
  maxPick = 1,
  onDone,
  onClose,
}: {
  text: string;
  maxPick?: number; // 国家里的城市最多能选几个（还有几个空位）；1 = 只能选一个
  onDone: (cities: FlowCity[]) => void;
  onClose: () => void;
}) {
  const [flow, setFlow] = useState<Flow>({ kind: "resolving" });
  const [picks, setPicks] = useState<string[]>([]); // 选了哪些城市（qid），顺序就是走的顺序
  const alive = useRef(true);
  const started = useRef<string | null>(null); // 开发模式下 effect 会跑两遍：同一个目的地只开始一次，免得建两份

  useEffect(() => {
    alive.current = true;
    if (started.current !== text) {
      started.current = text;
      void start();
    }
    return () => {
      alive.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  const fail = (e: unknown) =>
    alive.current && setFlow({ kind: "error", title: "查询目的地出错", message: e instanceof ApiError ? e.message : String(e) });

  async function start() {
    setFlow({ kind: "resolving" });
    try {
      const r = await api.resolveDestination(text);
      if (!alive.current) return;
      if (r.status === "ready" && r.city_id) onDone([{ id: r.city_id, name_zh: r.name_zh ?? text }]);
      else if (r.status === "build" && r.choice) await buildAll([r.choice]);
      else if (r.status === "choices")
        setFlow({ kind: "choices", reason: r.reason ?? "请选一个", choices: r.choices ?? [], many: !!r.many && maxPick > 1, country: !!r.many });
      else setFlow({ kind: "error", message: r.reason ?? "没找到叫这个名字的目的地" });
    } catch (e) {
      fail(e);
    }
  }

  /** 逐座建好（已经建好的直接用）；全部好了才进「准备好了」。 */
  async function buildAll(chosen: DestinationChoice[], fromCountry = false) {
    const done: FlowCity[] = [];
    try {
      for (const [n, c] of chosen.entries()) {
        const tag = chosen.length > 1 ? `（${n + 1}/${chosen.length}）` : "";
        setFlow({ kind: "building", name: `${c.label}${tag}`, message: "准备中" });
        const b = await api.buildDestination(c.qid, fromCountry ? "" : text); // 从国家里挑的城市：国家名不能存成这座城市的别名
        if (b.status === "ready" && b.city_id) {
          done.push({ id: b.city_id, name_zh: b.name_zh ?? c.label });
          continue;
        }
        const jobId = b.job_id as string;
        for (;;) {
          await new Promise((r) => setTimeout(r, 2000));
          if (!alive.current) return;
          const job = await api.destinationJob(jobId);
          if (!alive.current) return;
          if (job.status === "done") {
            done.push({ id: job.city_id, name_zh: job.name_zh ?? c.label });
            break;
          }
          if (job.status === "failed") {
            setFlow({ kind: "error", title: "建立目的地数据失败", message: job.error || "建立目的地数据失败" });
            return;
          }
          setFlow({ kind: "building", name: `${c.label}${tag}`, message: job.message });
        }
      }
      if (alive.current) setFlow({ kind: "ready", cities: done });
    } catch (e) {
      fail(e);
    }
  }

  function toggle(qid: string) {
    setPicks((cur) => (cur.includes(qid) ? cur.filter((q) => q !== qid) : cur.length < maxPick ? [...cur, qid] : cur));
  }

  const busy = flow.kind === "resolving" || flow.kind === "building";
  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={busy ? undefined : onClose}>
      <div
        className="modal confirm-dialog destination-flow"
        role="alertdialog"
        aria-modal="true"
        aria-label="识别目的地"
        data-testid="destination-flow"
        onClick={(e) => e.stopPropagation()}
      >
        {flow.kind === "resolving" && (
          <>
            <h2>正在识别目的地</h2>
            <p className="confirm-msg" data-testid="destination-progress">
              查找「{text}」……
            </p>
          </>
        )}
        {flow.kind === "choices" && (
          <>
            <h2>{flow.many ? "选城市" : "请选一个"}</h2>
            <p className="confirm-msg">{flow.many ? `${flow.reason}（最多 ${maxPick} 个）` : flow.reason}</p>
            <ul className="destination-choices">
              {flow.choices.map((c) => {
                const order = picks.indexOf(c.qid) + 1;
                return (
                  <li key={c.qid}>
                    <button
                      type="button"
                      className={`tool${flow.many && order ? " picked" : ""}`}
                      data-testid={`destination-choice-${c.qid}`}
                      aria-pressed={flow.many ? order > 0 : undefined}
                      onClick={() => (flow.many ? toggle(c.qid) : void buildAll([c], flow.country))}
                    >
                      {flow.many && order > 0 ? <b className="pick-no">{order}</b> : null}
                      {c.label}
                      {c.country ? <span className="muted"> · {c.country}</span> : null}
                      {c.description ? <span className="muted"> · {c.description}</span> : null}
                    </button>
                  </li>
                );
              })}
            </ul>
            <div className="confirm-actions">
              <button className="tool" data-testid="destination-cancel" onClick={onClose}>
                取消
              </button>
              {flow.many && (
                <button
                  className="primary"
                  data-testid="destination-pick-ok"
                  disabled={picks.length === 0}
                  onClick={() => void buildAll(picks.flatMap((q) => flow.choices.filter((c) => c.qid === q)), flow.country)}
                >
                  {picks.length > 1 ? `按顺序添加这 ${picks.length} 座城市` : "添加"}
                </button>
              )}
            </div>
          </>
        )}
        {flow.kind === "building" && (
          <>
            <h2>正在建立「{flow.name}」的目的地数据</h2>
            <p className="confirm-msg" data-testid="destination-progress">
              {flow.message}
            </p>
            <p className="muted">自动查找景点并核对，大约需要 3–5 分钟；建好之后这座城市会保存下来，下次直接用。</p>
          </>
        )}
        {flow.kind === "ready" && (
          <>
            <h2>「{flow.cities.map((c) => c.name_zh).join("、")}」准备好了</h2>
            <p className="confirm-msg">景点是按名气自动选的，价格、预约和部分闭馆日没有逐条核对，行程里会写明。</p>
            <div className="confirm-actions">
              <button className="primary" data-testid="destination-ready-ok" onClick={() => onDone(flow.cities)}>
                继续
              </button>
            </div>
          </>
        )}
        {flow.kind === "error" && (
          <>
            <h2>{flow.title ?? "没找到这个目的地"}</h2>
            <p className="confirm-msg" data-testid="destination-error">
              {flow.message}
            </p>
            <div className="confirm-actions">
              <button className="primary" data-testid="destination-error-ok" onClick={onClose}>
                知道了
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
