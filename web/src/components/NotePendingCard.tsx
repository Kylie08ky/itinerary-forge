import type { PendingPlace } from "../types";
import { mapsSearchUrl } from "./NoteImport";

/**
 * 行程页的「待核实」卡（规格 M12 S2）：笔记里提到、但地点库里没有的地点。
 * 它们没有开放时间和闭馆日，没法可靠排进行程，所以只列在这里；原文片段让你想起当时为什么要去。
 */
export function NotePendingCard({ pending }: { pending: PendingPlace[] }) {
  return (
    <section className="card pending-card" data-testid="pending-card" aria-label="待核实的地点">
      <h3>
        笔记里提到、还没核实的地点 <span className="ni-unverified">事实未核对</span>
      </h3>
      <p className="muted">
        地点库里没有这些地方，没有开放时间和闭馆日，没法可靠排进行程，所以<b>没有出现在上面的安排里</b>。出发前请自己查好营业时间再决定要不要加。
      </p>
      {pending.map((p) => (
        <div className="pool-pending-row" key={p.name} data-testid="pending-row">
          <b>{p.name}</b>
          <a className="tag link" href={mapsSearchUrl(p.name)} target="_blank" rel="noreferrer">
            在地图上查 ↗
          </a>
          {p.snippet && <blockquote className="ni-snippet">{p.snippet}</blockquote>}
        </div>
      ))}
    </section>
  );
}
