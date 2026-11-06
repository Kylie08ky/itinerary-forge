import type { SharedPlan } from "../types";

/** 分享页里「分享的人勾了才有」的行李清单和待办（只读：没有勾选框的交互、没有修改入口；规格 M08 S5b）。 */
export function SharedCompanion({ data }: { data: SharedPlan }) {
  const { packing, todos } = data;
  if (!packing && !todos) return null;
  return (
    <>
      {packing && (
        <section className="card shared-packing" data-testid="shared-packing" aria-label="行李清单">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>
            行李清单 <span className="muted">· 已带 {packing.packed}/{packing.total}</span>
          </h2>
          {packing.total === 0 && <p className="muted">还没有清单内容。</p>}
          {packing.categories.map((c) => (
            <div className="pack-cat" key={c}>
              <h3>{c}</h3>
              <ul className="shared-list">
                {packing.items
                  .filter((i) => i.category === c)
                  .map((i, k) => (
                    <li key={k} className={i.packed ? "done" : undefined}>
                      <span aria-hidden>{i.packed ? "☑" : "☐"}</span> {i.text}
                      <span className="sr-only">{i.packed ? "（已带）" : "（还没带）"}</span>
                    </li>
                  ))}
              </ul>
            </div>
          ))}
        </section>
      )}
      {todos && (
        <section className="card shared-todos" data-testid="shared-todos" aria-label="待办">
          <h2 style={{ marginTop: 0, fontSize: 16 }}>
            待办 <span className="muted">· {todos.filter((t) => !t.done).length} 项没完成</span>
          </h2>
          {todos.length === 0 && <p className="muted">没有待办。</p>}
          <ul className="shared-list">
            {todos.map((t, k) => (
              <li key={k} className={t.done ? "done" : undefined}>
                <span aria-hidden>{t.done ? "☑" : "☐"}</span> {t.text}
                {t.due && <span className="muted"> · 截止 {t.due}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
