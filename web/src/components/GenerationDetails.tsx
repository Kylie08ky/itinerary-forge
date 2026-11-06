import { useEffect } from "react";
import { QaBadge } from "../ui";
import type { Itinerary, ValidationReport } from "../types";
import { TracePanel } from "./TracePanel";

/**
 * 「生成详情」弹窗（规格 07 P10）：质检结果、生成过程（耗时与模型调用）、模型降级说明、排程说明。
 * 这些是给「想知道系统是怎么做的」的人看的，不放在行程页顶部占地方；点「更多 → 生成详情」才出现。
 */
export function GenerationDetails({
  planId,
  it,
  report,
  onClose,
}: {
  planId: string;
  it: Itinerary;
  report: ValidationReport;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const gen = it.generation;
  const notes = [...new Set(gen?.notes ?? [])];
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal gen-details" role="dialog" aria-modal="true" aria-label="生成详情" onClick={(e) => e.stopPropagation()}>
        <button className="modal-close" type="button" aria-label="关闭" onClick={onClose}>
          ×
        </button>
        <h2>生成详情</h2>
        {gen && <div className="gen-theme">主题：{gen.theme_name}</div>}
        {gen?.narration === "template" && (
          <div className="banner warn">
            {gen.narration_note ?? "说明文字由模板生成"}。地点、顺序、时间和预约信息不受影响。
          </div>
        )}
        <QaBadge report={report} />
        <TracePanel planId={planId} />
        {notes.length > 0 && (
          <ul className="gen-notes">
            {notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
