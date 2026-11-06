import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { ErrorPopup } from "../confirm";
import type { ChangelogEntry } from "../types";

/** 版本足迹：每次更新了什么，新的在上面，第一条是当前版本。数据在 data/changelog.json，每轮改动追加一条。 */
export function AboutPage() {
  const [entries, setEntries] = useState<ChangelogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .changelog()
      .then(setEntries)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, []);

  return (
    <div className="wrap about-page">
      <ErrorPopup message={error} onClose={() => setError(null)} />
      <h1>版本足迹</h1>
      {entries === null && !error && <p className="muted">加载中…</p>}
      {entries && entries.length === 0 && <p className="muted">还没有更新记录。</p>}
      <ol className="footprints" data-testid="footprints">
        {(entries ?? []).map((e, i) => (
          <li key={`${e.date}-${i}`} className="footprint" data-testid="footprint">
            <span className="fp-dot" aria-hidden />
            <div className="fp-head">
              <time dateTime={e.date}>{e.date}</time>
              {e.current && (
                <span className="fp-current" data-testid="footprint-current">
                  当前版本
                </span>
              )}
            </div>
            <h2>{e.title}</h2>
            <ul>
              {e.points.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </li>
        ))}
      </ol>
    </div>
  );
}
