import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { AttachmentsData, PackingData, PackingItem, TodoItem, TodosData } from "../types";
import { type DayRef, type FileFilter, FilesTab, type NamedRef } from "./FilesTab";

type Tab = "packing" | "todos" | "files";

const OPEN_KEY = "atp:companion-open";
const TAB_KEY = "atp:companion-tab";

function loadFlag(key: string, fallback: string): string {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function saveFlag(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* 存不了就算了 */
  }
}

/** 待办的截止提示：已过期 / 今天 / 3 天内是红色，更远的是灰色日期，已完成不提醒。 */
export function dueLabel(t: TodoItem): { text: string; hot: boolean } {
  const u = t.urgency;
  if (u.level === "done") return { text: t.due ?? "", hot: false };
  if (u.level === "none" || !t.due) return { text: "没设截止日", hot: false };
  if (u.level === "overdue") return { text: `已过期 ${Math.abs(u.days ?? 0)} 天`, hot: true };
  if (u.level === "today") return { text: "今天截止", hot: true };
  if (u.level === "soon") return { text: `还有 ${u.days} 天`, hot: true };
  return { text: t.due, hot: false };
}

/**
 * 行程页的「随行」卡（规格 M10 S2 / S3）：行李清单 + 待办。都是你自己的状态，不产生行程版本。
 * 取不到数据时整张卡不显示（不影响别的功能）。
 */
export function CompanionCard({
  planId,
  disabled,
  onError,
  files = null,
  onFiles,
  fileDays = [],
  filePlaces = [],
  fileReservations = [],
  fileFilter = null,
  onFileFilter,
  filesSignal = 0,
}: {
  planId: string;
  disabled: boolean;
  onError: (msg: string) => void;
  files?: AttachmentsData | null; // 文件附件（规格 M10 S5）；null = 没取到，不显示「文件」页签
  onFiles?: (d: AttachmentsData | null) => void;
  fileDays?: DayRef[];
  filePlaces?: NamedRef[];
  fileReservations?: NamedRef[];
  fileFilter?: FileFilter | null;
  onFileFilter?: (f: FileFilter | null) => void;
  filesSignal?: number; // 每加一，就展开这张卡并切到「文件」页签（点回形针时）
}) {
  const [open, setOpen] = useState(() => loadFlag(OPEN_KEY, "1") !== "0");
  const [tab, setTab] = useState<Tab>(() => {
    const saved = loadFlag(TAB_KEY, "packing");
    return saved === "todos" ? "todos" : "packing"; // 「文件」页签不记忆：只有点回形针 / 点页签才进
  });
  const [packing, setPacking] = useState<PackingData | null>(null);
  const [todos, setTodos] = useState<TodosData | null>(null);
  const [basis, setBasis] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    void Promise.all([api.packing(planId), api.todos(planId)])
      .then(([p, t]) => {
        if (!live) return;
        setPacking(p);
        setTodos(t);
      })
      .catch(() => {
        if (!live) return;
        setPacking(null);
        setTodos(null);
      });
    return () => {
      live = false;
    };
  }, [planId]);

  useEffect(() => {
    if (filesSignal > 0) {
      setOpen(true);
      setTab("files");
      requestAnimationFrame(() => document.getElementById("sec-companion")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
  }, [filesSignal]);

  const run = useCallback(
    async <T,>(fn: () => Promise<T>, apply: (v: T) => void) => {
      setBusy(true);
      try {
        apply(await fn());
      } catch (err) {
        onError(err instanceof ApiError ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [onError],
  );

  if (!packing || !todos) return null;

  // 勾选先在界面上立刻生效，再去存；存失败会用服务器的状态覆盖回来
  function optimisticPack(id: number, packed: boolean) {
    setPacking((p) =>
      p && {
        ...p,
        items: p.items.map((i) => (i.id === id ? { ...i, packed } : i)),
        packed: p.items.filter((i) => (i.id === id ? packed : i.packed)).length,
      },
    );
  }
  function optimisticTodo(id: number, done: boolean) {
    setTodos((t) =>
      t && {
        ...t,
        items: t.items.map((i) => (i.id === id ? { ...i, done, urgency: done ? { level: "done", days: null } : i.urgency } : i)),
      },
    );
  }

  function pick(t: Tab) {
    setTab(t);
    if (t !== "files") saveFlag(TAB_KEY, t);
  }

  function toggleOpen() {
    setOpen((v) => {
      saveFlag(OPEN_KEY, v ? "0" : "1");
      return !v;
    });
  }

  const urgent = todos.summary.urgent;

  return (
    <section className="card companion-card" data-testid="companion-card" aria-label="随行清单">
      <header className="companion-head">
        <button type="button" className={`day-toggle${open ? "" : " collapsed"}`} aria-expanded={open} aria-label={open ? "收起随行清单" : "展开随行清单"} onClick={toggleOpen}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="m6 9 6 6 6-6" />
          </svg>
        </button>
        <h2>随行清单</h2>
        <span className="muted" data-testid="companion-summary">
          行李 已带 {packing.packed}/{packing.total} · 待办 {todos.summary.open} 项
          {urgent > 0 && <b className="due-hot"> · {urgent} 项快到期 / 已过期</b>}
        </span>
      </header>

      {open && (
        <>
          <div className="seg-switch companion-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={tab === "packing"} className={`vbtn${tab === "packing" ? " active" : ""}`} onClick={() => pick("packing")}>
              行李清单
            </button>
            <button type="button" role="tab" aria-selected={tab === "todos"} className={`vbtn${tab === "todos" ? " active" : ""}`} onClick={() => pick("todos")}>
              待办{urgent > 0 ? `（${urgent}）` : ""}
            </button>
            {files && (
              <button type="button" role="tab" aria-selected={tab === "files"} data-testid="tab-files" className={`vbtn${tab === "files" ? " active" : ""}`} onClick={() => pick("files")}>
                文件{files.attachments.length > 0 ? `（${files.attachments.length}）` : ""}
              </button>
            )}
          </div>

          {tab === "files" && files ? (
            <FilesTab
              planId={planId}
              data={files}
              onData={(d) => onFiles?.(d)}
              disabled={disabled}
              days={fileDays}
              places={filePlaces}
              reservations={fileReservations}
              filter={fileFilter}
              onFilter={(f) => onFileFilter?.(f)}
              onError={onError}
            />
          ) : tab === "packing" ? (
            <PackingTab
              data={packing}
              basis={basis}
              busy={busy || disabled}
              onGenerate={() =>
                run(
                  () => api.generatePacking(planId),
                  (v) => {
                    setPacking(v);
                    setBasis(v.basis ?? null);
                  },
                )
              }
              onRestore={() => run(() => api.restorePacking(planId), setPacking)}
              onAdd={(category, text) => run(() => api.addPacking(planId, category, text), setPacking)}
              onPatch={(id, body) => {
                if (body.packed !== undefined) optimisticPack(id, body.packed);
                return run(() => api.patchPacking(planId, id, body), setPacking);
              }}
              onDelete={(id) => run(() => api.deletePacking(planId, id), setPacking)}
            />
          ) : (
            <TodosTab
              data={todos}
              busy={busy || disabled}
              onAdd={(text, due, key) => run(() => api.addTodo(planId, { text, due, key }), setTodos)}
              onPatch={(id, body) => {
                if (body.done !== undefined) optimisticTodo(id, body.done);
                return run(() => api.patchTodo(planId, id, body), setTodos);
              }}
              onDelete={(id) => run(() => api.deleteTodo(planId, id), setTodos)}
            />
          )}
        </>
      )}
    </section>
  );
}

// --------------------------------------------------------------------------- #
function PackingTab({
  data,
  basis,
  busy,
  onGenerate,
  onRestore,
  onAdd,
  onPatch,
  onDelete,
}: {
  data: PackingData;
  basis: string | null;
  busy: boolean;
  onGenerate: () => void;
  onRestore: () => void;
  onAdd: (category: string, text: string) => void;
  onPatch: (id: number, body: { packed?: boolean; text?: string }) => void;
  onDelete: (id: number) => void;
}) {
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [adding, setAdding] = useState<Record<string, string>>({});
  const pct = data.total ? Math.round((data.packed / data.total) * 100) : 0;
  const byCat = (c: string) => data.items.filter((i) => i.category === c);

  function commitEdit(item: PackingItem) {
    const text = draft.trim();
    setEditing(null);
    if (text && text !== item.text) onPatch(item.id, { text });
  }

  return (
    <div className="packing-tab" data-testid="packing-tab">
      <div className="packing-top">
        <div className="packing-progress" aria-label={`已带 ${data.packed}/${data.total}`}>
          <b data-testid="packing-progress">
            已带 {data.packed}/{data.total}
          </b>
          <div className="b-bar" aria-hidden>
            <i style={{ width: `${pct}%` }} />
          </div>
        </div>
        <span className="spacer" />
        {data.total === 0 ? (
          <button type="button" className="btn-primary" disabled={busy} onClick={onGenerate}>
            按行程生成清单
          </button>
        ) : (
          <button type="button" className="tool" disabled={busy} onClick={onGenerate} title="只补上还没有的推荐项，不动你勾过、改过、自己加的">
            补充推荐项
          </button>
        )}
        {data.hidden > 0 && (
          <button type="button" className="tool" disabled={busy} onClick={onRestore}>
            恢复推荐项（{data.hidden}）
          </button>
        )}
      </div>
      {basis && (
        <div className="muted packing-basis" data-testid="packing-basis">
          清单按：{basis}；行程 · 出行人 · 内容生成（建议，可随意增删）
        </div>
      )}
      {data.total === 0 && <p className="muted">还没有清单。点「按行程生成清单」，按季节、天数、出行人和行程内容生成，之后可以随意勾选、修改和添加。</p>}

      {data.categories.map((c) => {
        const items = byCat(c);
        const done = items.filter((i) => i.packed).length;
        return (
          <div className="pack-cat" key={c} data-testid={`pack-cat-${c}`}>
            <h3>
              {c} <span className="muted">{done}/{items.length}</span>
            </h3>
            {items.map((i) => (
              <div className={`pack-row${i.packed ? " packed" : ""}`} key={i.id} data-testid="pack-item">
                <label>
                  <input type="checkbox" checked={i.packed} disabled={busy} aria-label={`${i.text} 已带`} onChange={(e) => onPatch(i.id, { packed: e.target.checked })} />
                  {editing === i.id ? (
                    <input
                      className="pack-edit"
                      autoFocus
                      value={draft}
                      maxLength={120}
                      aria-label={`修改「${i.text}」`}
                      onChange={(e) => setDraft(e.target.value)}
                      onBlur={() => commitEdit(i)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") commitEdit(i);
                        if (e.key === "Escape") setEditing(null);
                      }}
                    />
                  ) : (
                    <span className="pack-text">{i.text}</span>
                  )}
                </label>
                {i.source_url && (
                  <a className="tag link" href={i.source_url} target="_blank" rel="noreferrer" title={`${i.source_note}（${i.checked_at} 核对）`}>
                    来源 ↗
                  </a>
                )}
                <span className="spacer" />
                <button type="button" className="tool" disabled={busy} aria-label={`修改「${i.text}」`} onClick={() => (setDraft(i.text), setEditing(i.id))}>
                  改
                </button>
                <button type="button" className="tool danger" disabled={busy} aria-label={`删除「${i.text}」`} onClick={() => onDelete(i.id)}>
                  删除
                </button>
              </div>
            ))}
            <form
              className="pack-add"
              onSubmit={(e) => {
                e.preventDefault();
                const text = (adding[c] ?? "").trim();
                if (!text) return;
                onAdd(c, text);
                setAdding({ ...adding, [c]: "" });
              }}
            >
              <input value={adding[c] ?? ""} maxLength={120} placeholder={`在「${c}」里添加…`} aria-label={`添加到${c}`} disabled={busy} onChange={(e) => setAdding({ ...adding, [c]: e.target.value })} />
              <button type="submit" className="tool" disabled={busy || !(adding[c] ?? "").trim()}>
                添加
              </button>
            </form>
          </div>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------- #
function TodosTab({
  data,
  busy,
  onAdd,
  onPatch,
  onDelete,
}: {
  data: TodosData;
  busy: boolean;
  onAdd: (text: string, due: string | null, key?: string) => Promise<void> | void;
  onPatch: (id: number, body: { done?: boolean; text?: string; due?: string | null }) => void;
  onDelete: (id: number) => void;
}) {
  const [text, setText] = useState("");
  const [due, setDue] = useState("");
  const [showSug, setShowSug] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const s = data.summary;

  function openSuggestions() {
    setPicked(new Set(data.suggestions.map((x) => x.key)));
    setShowSug((v) => !v);
  }

  function acceptPicked() {
    const chosen = data.suggestions.filter((x) => picked.has(x.key));
    // 一条一条加：每次都以服务器返回的最新状态为准，重复的会被后端忽略
    void (async () => {
      for (const x of chosen) await onAdd(x.text, x.due, x.key);
    })();
    setShowSug(false);
  }

  return (
    <div className="todos-tab" data-testid="todos-tab">
      <div className="todo-top">
        <b data-testid="todo-summary">
          {s.open} 项待办
          {s.urgent > 0 && <span className="due-hot"> · {s.urgent} 项快到期 / 已过期</span>}
        </b>
        <span className="spacer" />
        <button type="button" className="tool" disabled={busy} onClick={openSuggestions}>
          {showSug ? "收起推荐" : `看推荐待办${data.suggestions.length ? `（${data.suggestions.length}）` : ""}`}
        </button>
      </div>

      {showSug && (
        <div className="todo-suggest" data-testid="todo-suggestions">
          {data.suggestions.length === 0 ? (
            <span className="muted">推荐的都已经加过了。</span>
          ) : (
            <>
              <p className="muted">日期按出发日倒推，是建议不是官方规定；有明确提前天数的景点，截止日就是建议最晚订票日。</p>
              {data.suggestions.map((x) => (
                <label className="sug-line" key={x.key} data-testid="todo-suggestion">
                  <input
                    type="checkbox"
                    checked={picked.has(x.key)}
                    onChange={(e) => {
                      const next = new Set(picked);
                      if (e.target.checked) next.add(x.key);
                      else next.delete(x.key);
                      setPicked(next);
                    }}
                  />
                  <span>
                    {x.text} <b>{x.due}</b>
                    <span className="muted"> · {x.why}</span>
                  </span>
                </label>
              ))}
              <button type="button" className="btn-primary" disabled={busy || picked.size === 0} onClick={acceptPicked}>
                加入选中的（{picked.size}）
              </button>
            </>
          )}
        </div>
      )}

      {data.items.length === 0 && !showSug && <p className="muted">还没有待办。可以自己添加，或点「看推荐待办」。</p>}
      {data.items.map((t) => {
        const d = dueLabel(t);
        return (
          <div className={`todo-row${t.done ? " done" : ""}${d.hot ? " hot" : ""}`} key={t.id} data-testid="todo-item">
            <label>
              <input type="checkbox" checked={t.done} disabled={busy} aria-label={`${t.text} 已完成`} onChange={(e) => onPatch(t.id, { done: e.target.checked })} />
              <span className="todo-text">{t.text}</span>
            </label>
            <span className="spacer" />
            <input
              type="date"
              className="todo-due"
              value={t.due ?? ""}
              disabled={busy}
              aria-label={`「${t.text}」的截止日`}
              onChange={(e) => onPatch(t.id, { due: e.target.value || null })}
            />
            {!t.done && (
              <span className={`due-badge${d.hot ? " hot" : ""}`} data-testid="todo-due" data-level={t.urgency.level}>
                {d.text}
              </span>
            )}
            <button type="button" className="tool danger" disabled={busy} aria-label={`删除待办「${t.text}」`} onClick={() => onDelete(t.id)}>
              删除
            </button>
          </div>
        );
      })}

      <form
        className="todo-add"
        onSubmit={(e) => {
          e.preventDefault();
          if (!text.trim()) return;
          onAdd(text.trim(), due || null);
          setText("");
          setDue("");
        }}
      >
        <input value={text} maxLength={120} placeholder="添加待办，例如：办申根签证" aria-label="新待办" disabled={busy} onChange={(e) => setText(e.target.value)} />
        <input type="date" value={due} aria-label="新待办的截止日" disabled={busy} onChange={(e) => setDue(e.target.value)} />
        <button type="submit" className="btn-primary" disabled={busy || !text.trim()}>
          添加
        </button>
      </form>
    </div>
  );
}
