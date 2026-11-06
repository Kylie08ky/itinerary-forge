import { useEffect, useState } from "react";
import type { ReservationItem } from "../types";

export type ReservationPatch = {
  status: "todo" | "booked";
  confirmation_code?: string;
  slot?: string;
  note?: string;
  entry_from?: string;
  entry_to?: string;
};

function mmdd(iso: string): string {
  return iso.slice(5); // 2026-09-20 -> 09-20
}

/**
 * 一行购票清单下面的「订票状态」（规格 M06 S3 / S4）：
 * 最晚订票日提醒；勾选「已订」（清单项前面的框）后，填入馆时间 → 行程里这个景点被锁定在这个时间，当天其余安排自动调整；
 * 时段票再填「时段结束」，会尽量排在时段内。
 */
export function ReservationControls({
  item,
  disabled,
  onSave,
}: {
  item: ReservationItem;
  disabled: boolean;
  onSave: (placeId: string, patch: ReservationPatch) => Promise<void>;
}) {
  const [from, setFrom] = useState(item.entry_from ?? "");
  const [to, setTo] = useState(item.entry_to ?? "");
  const [code, setCode] = useState(item.confirmation_code ?? "");
  const [note, setNote] = useState(item.note ?? "");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setFrom(item.entry_from ?? "");
    setTo(item.entry_to ?? "");
    setCode(item.confirmation_code ?? "");
    setNote(item.note ?? "");
  }, [item.entry_from, item.entry_to, item.confirmation_code, item.note]);

  const booked = item.status === "booked";

  async function save(patch: Partial<ReservationPatch>) {
    setBusy(true);
    try {
      await onSave(item.place_id, { status: item.status, ...patch });
    } finally {
      setBusy(false);
    }
  }

  const blur = (key: "confirmation_code" | "note" | "entry_from" | "entry_to", value: string, orig: string | null) => {
    if (value.trim() !== (orig ?? "")) void save({ [key]: value });
  };

  return (
    <div className={`res-ctl urgency-${item.urgency}`}>
      <div className="res-line">
        {item.urgency === "late" && <span className="tag urgent">马上订</span>}
        {item.urgency === "soon" && <span className="tag soon">快到了</span>}
        {booked && <span className="tag booked-ok">已订 ✓</span>}
        {booked && item.locked && item.entry_from && (
          <span className="tag locked-tag">
            🔒 已锁定在 {item.entry_from}
            {item.entry_to ? `（时段 ${item.entry_from}–${item.entry_to}）` : ""}
          </span>
        )}
        {!booked && item.urgency === "past" && <span className="muted">参观日已过</span>}
        {!booked && item.urgency !== "past" && item.book_by && (
          <span className="muted">
            建议最晚 <b>{mmdd(item.book_by)}</b> 前订（参观日 {mmdd(item.visit_date ?? "")}；{item.lead_basis}）
          </span>
        )}
        {!booked && item.urgency !== "past" && !item.book_by && item.in_itinerary && (
          <span className="muted">建议尽早订（没有明确的提前天数，不编数字）· 订好了就勾上前面的框</span>
        )}
        {item.date_changed && item.booked_for && (
          <span className="tag soon">行程日期变了：这笔预订是为 {mmdd(item.booked_for)} 订的</span>
        )}
        {!item.in_itinerary && <span className="muted">（这个地点已不在当前行程里，记录保留）</span>}
      </div>
      {booked && (
        <>
          <div className="res-edit">
            <label className="res-field">
              入馆时间
              <input
                type="time"
                className="res-input"
                aria-label={`${item.title}的入馆时间`}
                value={from}
                disabled={disabled || busy}
                onChange={(e) => setFrom(e.target.value)}
                onBlur={() => blur("entry_from", from, item.entry_from)}
              />
            </label>
            <label className="res-field">
              时段结束 <span className="hint">（时段票才填）</span>
              <input
                type="time"
                className="res-input"
                aria-label={`${item.title}的时段结束`}
                value={to}
                disabled={disabled || busy || !from}
                onChange={(e) => setTo(e.target.value)}
                onBlur={() => blur("entry_to", to, item.entry_to)}
              />
            </label>
            <input
              className="res-input"
              aria-label={`${item.title}的确认码`}
              placeholder="确认码（选填）"
              maxLength={60}
              value={code}
              disabled={disabled || busy}
              onChange={(e) => setCode(e.target.value)}
              onBlur={() => blur("confirmation_code", code, item.confirmation_code)}
            />
            <input
              className="res-input wide"
              aria-label={`${item.title}的预订备注`}
              placeholder="备注（选填）"
              maxLength={300}
              value={note}
              disabled={disabled || busy}
              onChange={(e) => setNote(e.target.value)}
              onBlur={() => blur("note", note, item.note)}
            />
          </div>
          {!item.entry_from && (
            <div className="muted res-hint">
              填了入馆时间，行程里这个景点会锁定在这个时间，当天其他安排自动调整；是时段票的话再填时段结束，会尽量排在时段内。
            </div>
          )}
        </>
      )}
    </div>
  );
}
