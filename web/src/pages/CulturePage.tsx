import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { ErrorPopup } from "../confirm";
import type { CultureWork, FoodDish } from "../types";

const KIND_ORDER: CultureWork["kind"][] = [
  "film",
  "documentary",
  "book",
  "podcast",
];

/**
 * 作品参考页（规格 09 十五）：这座城市的电影 / 纪录片 / 书，每一部写它带你看哪些地方。
 * 这是**参考**，不是步骤：行程里走到这些地方时会自动提示「这里的故事」，用户不用先看、也不用选。
 * 作品和「带你看的地方」都来自核对过来源的策展数据。
 */
export function CulturePage() {
  const [works, setWorks] = useState<CultureWork[] | null>(null);
  const [dishes, setDishes] = useState<FoodDish[]>([]);
  const [kind, setKind] = useState<"all" | "food" | CultureWork["kind"]>("all");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .culture("paris")
      .then(setWorks)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
    api
      .food("paris")
      .then(setDishes)
      .catch(() => setDishes([])); // 美食是补充：取不到就不显示这一类
  }, []);

  const kinds = useMemo(() => {
    const out: { kind: CultureWork["kind"]; label: string; count: number }[] =
      [];
    for (const k of KIND_ORDER) {
      const own = (works ?? []).filter((w) => w.kind === k);
      if (own.length > 0)
        out.push({ kind: k, label: own[0].kind_zh, count: own.length });
    }
    return out;
  }, [works]);

  const shown = (works ?? []).filter((w) => kind === "all" || w.kind === kind);
  const showFood = kind === "food";

  return (
    <div className="wrap culture-page">
      <div className="page-head">
        <div>
          <h1>了解巴黎</h1>
          <p className="muted" data-testid="culture-intro">
            行程里走到这些地方时，会自动提示这里的故事。
          </p>
        </div>
      </div>
      {error && <ErrorPopup message={error} onClose={() => setError(null)} />}
      {works === null && !error && <p className="muted">加载中…</p>}
      {works !== null && (
        <>
          <div className="culture-kinds" role="tablist" aria-label="作品类型">
            <button
              type="button"
              role="tab"
              aria-selected={kind === "all"}
              className={`chip${kind === "all" ? " on" : ""}`}
              data-testid="culture-kind-all"
              onClick={() => setKind("all")}
            >
              全部 {works.length}
            </button>
            {kinds.map((k) => (
              <button
                key={k.kind}
                type="button"
                role="tab"
                aria-selected={kind === k.kind}
                className={`chip${kind === k.kind ? " on" : ""}`}
                data-testid={`culture-kind-${k.kind}`}
                onClick={() => setKind(k.kind)}
              >
                {k.label} {k.count}
              </button>
            ))}
            {dishes.length > 0 && (
              <button
                type="button"
                role="tab"
                aria-selected={showFood}
                className={`chip${showFood ? " on" : ""}`}
                data-testid="culture-kind-food"
                onClick={() => setKind("food")}
              >
                美食 {dishes.length}
              </button>
            )}
          </div>
          <div className="culture-list">
            {showFood &&
              dishes.map((d) => (
                <article
                  key={d.id}
                  className="card culture-card"
                  data-testid={`food-dish-${d.id}`}
                >
                  <header>
                    <span className="culture-kind-tag">{d.kind_zh}</span>
                    <h2>
                      {d.name_zh} · <span lang="fr">{d.name_local}</span>
                    </h2>
                  </header>
                  <p className="culture-summary">{d.summary}</p>
                  {d.places.length > 0 && (
                    <ul className="culture-places">
                      {d.places.map((p) => (
                        <li
                          key={p.place_id}
                          data-testid={`food-place-${d.id}-${p.place_id}`}
                        >
                          <strong>{p.name}</strong>
                          <span className="muted"> — {p.note}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <footer>
                    <a
                      className="muted"
                      href={d.source.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      来源 ↗
                    </a>
                  </footer>
                </article>
              ))}
            {!showFood &&
              shown.map((w) => (
                <article
                  key={w.id}
                  className="card culture-card"
                  data-testid={`culture-work-${w.id}`}
                >
                  <header>
                    <span className="culture-kind-tag">{w.kind_zh}</span>
                    <h2>
                      {w.title_zh ? `${w.title_zh} · ` : ""}
                      <span lang="und">{w.title}</span>
                    </h2>
                    <p className="muted">
                      {w.creator} · {w.year}
                    </p>
                  </header>
                  {w.summary && <p className="culture-summary">{w.summary}</p>}
                  <ul className="culture-places">
                    {w.places.map((p) => (
                      <li
                        key={p.place_id}
                        data-testid={`culture-place-${w.id}-${p.place_id}`}
                      >
                        <strong>{p.name}</strong>
                        <span className="muted"> — {p.note}</span>
                      </li>
                    ))}
                  </ul>
                  {w.routes.length > 0 && (
                    <p
                      className="culture-routes muted"
                      data-testid={`culture-routes-${w.id}`}
                    >
                      路线：{w.routes.map((r) => r.name_zh).join("、")}
                    </p>
                  )}
                  <footer>
                    <a
                      className="muted"
                      href={w.source.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      来源 ↗
                    </a>
                  </footer>
                </article>
              ))}
          </div>
        </>
      )}
    </div>
  );
}
