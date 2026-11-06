import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { ErrorPopup } from "../confirm";
import type { NoteSource, NoteTip, ParsedPlace, ParseResult, PendingPlace } from "../types";

export const MAX_NOTE_CHARS = 8000;

export interface ImportPick {
  place_id: string;
  name_zh: string;
  state: "must" | "avoid";
  source: NoteSource;
}
export interface ImportAccepted {
  picks: ImportPick[];
  pending: PendingPlace[];
}

type RowState = "must" | "default" | "avoid";
interface RowPick {
  state: RowState;
  pid: string | null; // 置信度「低」的行要用户从候选里选
}

const CONF_ZH = { high: "高", medium: "中", low: "低" } as const;
const STATE_ZH: Record<RowState, string> = { must: "必去", default: "默认", avoid: "不去" };

/** 默认：能放心加的标「必去」；原文像吐槽 / 整修闭馆 / 没选定候选的，不替用户决定（规格 M12 S1 / S4 / S5）。 */
function initialPick(p: ParsedPlace): RowPick {
  if (p.confidence === "low") return { state: "default", pid: null };
  if (p.complaint || p.closed_long_term) return { state: "default", pid: p.place_id };
  return { state: "must", pid: p.place_id };
}

export function mapsSearchUrl(name: string): string {
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${name} Paris`)}`;
}

export function TipChips({ tips }: { tips: NoteTip[] }) {
  if (tips.length === 0) return null;
  return (
    <>
      <div className="ni-tips">
        {tips.map((t) => (
          <span
            key={t.kind}
            className={`ni-tip kind-${t.kind}${t.conflict ? " conflict" : ""}`}
            title={t.text ? `原文：${t.text}` : undefined}
            data-testid="note-tip"
          >
            {t.label}
          </span>
        ))}
      </div>
      {tips.filter((t) => t.conflict).map((t) => (
        <div className="ni-conflict" key={t.kind} data-testid="note-tip-conflict">
          ⚠️ {t.conflict}
        </div>
      ))}
    </>
  );
}

/**
 * 从笔记导入（规格 M12 S1–S6）：粘贴正文 → 解析 → 确认页（逐个看置信度 / 原文片段 / 提示，决定要不要）→ 加入候选池。
 * 后端只返回解析结果，不存正文；这里确认后只把每个地点最多 200 字的原文片段交给首页，随行程保存。
 */
export function NoteImport({ onAccept, onClose }: { onAccept: (a: ImportAccepted) => void; onClose: () => void }) {
  const [mode, setMode] = useState<"text" | "url">("text");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [xhsCookie, setXhsCookie] = useState<boolean | null>(null); // .env 里有没有小红书 Cookie
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ParseResult | null>(null);
  const [picks, setPicks] = useState<RowPick[]>([]);
  const [keep, setKeep] = useState<boolean[]>([]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !error && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, error]);

  useEffect(() => {
    if (mode !== "url" || xhsCookie !== null) return;
    api.materialSources().then((s) => setXhsCookie(s.xhs_cookie)).catch(() => setXhsCookie(false));
  }, [mode, xhsCookie]);

  const length = text.trim().length;
  const tooLong = length > MAX_NOTE_CHARS;

  async function parse() {
    setBusy(true);
    try {
      const r = await api.parseMaterial(mode === "url" ? { url: url.trim() } : { text });
      setResult(r);
      setPicks(r.matched.map(initialPick));
      setKeep(r.pending.map(() => true));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      if (mode === "url") setMode("text"); // 链接读不到：切回「粘贴文本」，链接留在原处不丢
    } finally {
      setBusy(false);
    }
  }

  const chosen = useMemo(() => {
    if (!result) return [];
    const byPid = new Map<string, ImportPick>();
    result.matched.forEach((row, i) => {
      const pick = picks[i];
      if (!pick || pick.state === "default" || !pick.pid) return;
      const cand = row.candidates.find((c) => c.place_id === pick.pid);
      const prev = byPid.get(pick.pid);
      if (prev && prev.state === "must") return;
      byPid.set(pick.pid, {
        place_id: pick.pid,
        name_zh: cand?.name_zh ?? row.name_zh ?? pick.pid,
        state: pick.state,
        source: { place_id: pick.pid, snippet: row.snippet.slice(0, 200), tips: row.tips.slice(0, 8) },
      });
    });
    return [...byPid.values()];
  }, [result, picks]);

  const pendingKept = result ? result.pending.filter((_p, i) => keep[i]) : [];

  function setRow(i: number, next: Partial<RowPick>) {
    setPicks((cur) => cur.map((p, j) => (j === i ? { ...p, ...next } : p)));
  }

  function choose(i: number, row: ParsedPlace, pid: string) {
    setRow(i, { pid: pid || null, state: !pid ? "default" : row.complaint ? "default" : "must" });
  }

  const total = result ? result.matched.length + result.pending.length : 0;

  return (
    <div className="modal-backdrop" role="presentation">
      <div className="modal note-import" role="dialog" aria-modal="true" aria-label="从笔记导入" data-testid="note-import">
        <button type="button" className="modal-close" aria-label="关闭" onClick={onClose}>
          ✕
        </button>
        <h2>从笔记导入</h2>

        {!result && (
          <>
            <div className="seg ni-tabs" role="tablist" aria-label="导入方式">
              {(["text", "url"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  role="tab"
                  aria-selected={mode === m}
                  data-testid={`note-tab-${m}`}
                  className={`segbtn${mode === m ? " on" : ""}`}
                  onClick={() => setMode(m)}
                >
                  {m === "text" ? "粘贴文本" : "粘贴链接"}
                </button>
              ))}
            </div>
            {mode === "text" ? (
              <>
                <p className="muted ni-lead">
                  把朋友分享的小红书 / 攻略笔记的<b>正文</b>粘贴到这里。系统会找出里面提到的地点，匹配地点库，由你确认后放进候选池。
                  笔记正文只在这次解析里用，不会被保存。
                </p>
                <textarea
                  className="ni-text"
                  data-testid="note-text"
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  placeholder="在这里粘贴笔记正文…"
                  rows={10}
                  aria-label="笔记正文"
                />
              </>
            ) : (
              <>
                <p className="muted ni-lead">
                  粘贴一个网页链接或小红书分享链接，系统会读取页面上的文字再解析。读不到时会说明原因，并提示你改贴正文。
                </p>
                <p className="muted ni-lead" data-testid="note-xhs-status">
                  {xhsCookie === null
                    ? "正在检查小红书登录设置…"
                    : xhsCookie
                      ? "小红书：已配置你自己的登录 Cookie（只在读小红书链接时使用，不会发给别的网站，也不会被保存到行程里）。"
                      : "小红书：还没配置登录 Cookie。小红书笔记要登录才读得到——把你自己的 Cookie 填进项目根目录 .env 的 XHS_COOKIE（步骤见 README），或直接粘贴正文。其他网站不受影响。"}
                </p>
                <input
                  className="ni-url"
                  data-testid="note-url"
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://…"
                  aria-label="笔记链接"
                />
              </>
            )}
            <div className="ni-foot">
              {mode === "text" ? (
                <span className={`muted${tooLong ? " ni-over" : ""}`} data-testid="note-count">
                  {length} / {MAX_NOTE_CHARS} 字{tooLong && "：超过上限，请分几次粘贴"}
                </span>
              ) : (
                <span />
              )}
              <button type="button" className="tool" onClick={onClose}>
                取消
              </button>
              <button
                type="button"
                className="primary"
                data-testid="note-parse"
                disabled={busy || (mode === "text" ? length === 0 || tooLong : url.trim() === "")}
                onClick={parse}
              >
                {busy ? (mode === "url" ? "读取并解析中…" : "解析中…") : mode === "url" ? "读取并解析" : "解析"}
              </button>
            </div>
          </>
        )}

        {result && (
          <div data-testid="note-result">
            <p className="ni-summary" data-testid="note-summary">
              {total === 0
                ? "没有识别到任何地点。可以返回修改，换一段更完整的正文试试。"
                : `识别到 ${total} 个地点：${result.matched.length} 个在地点库里、${result.pending.length} 个库里没有。`}
              {result.source === "url" && result.source_host && (
                <span className="muted" data-testid="note-source-host"> 来自 {result.source_host}</span>
              )}
            </p>
            {!result.model_used && (
              <div className="banner warn" data-testid="note-no-model">
                本地模型这次没有读出库外地点（没开或返回有误），只识别了地点库里已有的名字；库外的地点没法识别。
              </div>
            )}
            {result.dropped_instructions > 0 && (
              <div className="banner" data-testid="note-dropped">
                已忽略 {result.dropped_instructions} 句像指令的文字（笔记正文只当普通文字看，不会被执行）。
              </div>
            )}

            {result.matched.length > 0 && <h3 className="ni-h">在地点库里</h3>}
            {result.matched.map((row, i) => {
              const pick = picks[i] ?? { state: "default" as RowState, pid: null };
              return (
                <div className={`ni-row ${pick.state}`} key={`${row.place_id ?? row.mention}-${i}`} data-testid="note-row"
                  data-pid={pick.pid ?? ""}>
                  <div className="ni-head">
                    <b>{row.name_zh ?? `「${row.mention}」`}</b>
                    {row.area && <span className="muted"> · {row.area}</span>}
                    <span className="muted"> · 笔记里写的：{row.mention}</span>
                    <span className={`ni-conf conf-${row.confidence}`} data-testid="note-conf">
                      置信度 {CONF_ZH[row.confidence]}
                    </span>
                  </div>
                  {row.confidence === "low" && (
                    <label className="ni-pick">
                      这个写法对得上好几个地点，请选一个：
                      <select value={pick.pid ?? ""} onChange={(e) => choose(i, row, e.target.value)} data-testid="note-candidate">
                        <option value="">（先不选）</option>
                        {row.candidates.map((c) => (
                          <option key={c.place_id} value={c.place_id}>
                            {c.name_zh} · {c.area}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {row.closed_long_term && (
                    <div className="ni-conflict">⛔ 整修闭馆中，生成时不会排进行程</div>
                  )}
                  {row.complaint && (
                    <div className="ni-complaint" data-testid="note-complaint">
                      原文像在吐槽——我没有替你决定去不去，要排除请选「不去」。
                    </div>
                  )}
                  <TipChips tips={row.tips} />
                  {row.snippet && <blockquote className="ni-snippet">{row.snippet}</blockquote>}
                  <div className="seg" role="group" aria-label={`${row.name_zh ?? row.mention}的候选设置`}>
                    {(["default", "must", "avoid"] as const).map((s) => (
                      <button
                        type="button"
                        key={s}
                        disabled={row.confidence === "low" && !pick.pid}
                        className={`segbtn${pick.state === s ? ` on on-${s}` : ""}`}
                        onClick={() => setRow(i, { state: s })}
                      >
                        {STATE_ZH[s]}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}

            {result.pending.length > 0 && (
              <>
                <h3 className="ni-h">地点库里没有（事实未核对）</h3>
                <p className="muted ni-lead">
                  这些地点没有开放时间和闭馆日，没法可靠排进行程。勾上的会先记在「待核实」里，<b>不会出现在生成的行程里</b>。
                </p>
              </>
            )}
            {result.pending.map((p, i) => (
              <label className="ni-row pending" key={p.name} data-testid="note-pending-row">
                <div className="ni-head">
                  <input
                    type="checkbox"
                    checked={keep[i] ?? false}
                    onChange={(e) => setKeep((cur) => cur.map((v, j) => (j === i ? e.target.checked : v)))}
                    aria-label={`先记着：${p.name}`}
                  />{" "}
                  <b>{p.name}</b>
                  <span className="ni-unverified">事实未核对</span>
                </div>
                {p.snippet && <blockquote className="ni-snippet">{p.snippet}</blockquote>}
              </label>
            ))}

            <div className="ni-foot">
              <button type="button" className="tool" data-testid="note-back" onClick={() => setResult(null)}>
                返回修改
              </button>
              <button
                type="button"
                className="primary"
                data-testid="note-accept"
                disabled={chosen.length === 0 && pendingKept.length === 0}
                onClick={() => onAccept({ picks: chosen, pending: pendingKept })}
              >
                加入候选池
                {chosen.length + pendingKept.length > 0 && `（${chosen.length} 个地点 + ${pendingKept.length} 个待核实）`}
              </button>
            </div>
          </div>
        )}
      </div>
      <ErrorPopup message={error} onClose={() => setError(null)} />
    </div>
  );
}
