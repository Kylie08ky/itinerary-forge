import { useState } from "react";
import type { TransportMode } from "../types";
import { MODE_OPTIONS } from "./TransportEditor";

export interface TransferValue {
  mode: TransportMode;
  depart_at: string; // 出发的日期 + 时间
  arrive_at: string;
  number: string | null;
}

/** 改换城市的交通：日期是定的（前一座城市的离开日、后一座的开始日），只改方式、钟点和车次航班号。 */
export function TransferDialog({
  value,
  from,
  to,
  fromDate,
  toDate,
  onClose,
  onSave,
}: {
  value: TransferValue;
  from: string;
  to: string;
  fromDate: string;
  toDate: string;
  onClose: () => void;
  onSave: (body: { mode: string; depart_at: string; arrive_at: string; number?: string }) => Promise<void>;
}) {
  const [mode, setMode] = useState<TransportMode>(value.mode);
  const [depart, setDepart] = useState(value.depart_at.slice(11, 16));
  const [arrive, setArrive] = useState(value.arrive_at.slice(11, 16));
  const [number, setNumber] = useState(value.number ?? "");
  const [busy, setBusy] = useState(false);
  const ready = /^\d\d:\d\d$/.test(depart) && /^\d\d:\d\d$/.test(arrive);

  async function save() {
    setBusy(true);
    try {
      await onSave({
        mode,
        depart_at: `${fromDate}T${depart}:00`,
        arrive_at: `${toDate}T${arrive}:00`,
        ...(number.trim() ? { number: number.trim() } : {}),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={busy ? undefined : onClose}>
      <div
        className="modal confirm-dialog transfer-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="改交通"
        data-testid="transfer-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <h2>
          {from} → {to}
        </h2>
        <div className="field">
          <label htmlFor="tf-mode">交通方式</label>
          <select id="tf-mode" value={mode} onChange={(e) => setMode(e.target.value as TransportMode)}>
            {MODE_OPTIONS.map((o) => (
              <option key={o.v} value={o.v}>
                {o.t}
              </option>
            ))}
          </select>
        </div>
        <div className="row2">
          <div className="field">
            <label htmlFor="tf-depart">{fromDate.slice(5)} 出发</label>
            <input id="tf-depart" type="time" value={depart} onChange={(e) => setDepart(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="tf-arrive">{toDate.slice(5)} 到达</label>
            <input id="tf-arrive" type="time" value={arrive} onChange={(e) => setArrive(e.target.value)} />
          </div>
        </div>
        <div className="field">
          <label htmlFor="tf-number">车次 / 航班号</label>
          <input id="tf-number" value={number} maxLength={20} onChange={(e) => setNumber(e.target.value)} />
        </div>
        <div className="confirm-actions">
          <button type="button" className="tool" disabled={busy} onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary"
            data-testid="transfer-save"
            disabled={busy || !ready}
            onClick={() => void save()}
          >
            {busy ? "正在重新排程…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
