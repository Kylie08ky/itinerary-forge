import type { TransportLegDraft, TransportMode } from "../types";

export const MODE_OPTIONS: { v: TransportMode; t: string }[] = [
  { v: "flight", t: "飞机" },
  { v: "high_speed_rail", t: "高铁" },
  { v: "train", t: "火车" },
  { v: "coach", t: "大巴" },
  { v: "drive", t: "自驾" },
  { v: "other", t: "其他" },
];

export interface TransportDraft {
  out: TransportLegDraft;
  back: TransportLegDraft;
}

const blankLeg = (): TransportLegDraft => ({ mode: "flight", depart_at: "", arrive_at: "", place: "", number: "" });

export function emptyTransport(): TransportDraft {
  return { out: blankLeg(), back: blankLeg() };
}

/**
 * 默认值：按开始 / 离开日期预填，到达 10:00、返程出发 18:00（去程出发要早于到达，取 7:00），用户只需要改钟点（规格 07 P07）。
 * 日期不全时留空。
 */
export function defaultTransport(start: string, end: string): TransportDraft {
  const at = (date: string, hhmm: string) => (date.length === 10 ? `${date}T${hhmm}` : "");
  return {
    out: { ...blankLeg(), depart_at: at(start, "07:00"), arrive_at: at(start, "10:00") },
    back: { ...blankLeg(), depart_at: at(end, "18:00") },
  };
}

export type TransportErrors = Partial<Record<"out_depart" | "out_arrive" | "back_depart", string>>;

/**
 * 交通是必填项（规格 07 P07）：去程要有出发 + 到达时间，返程要有出发时间；
 * 到达那天就是行程开始日、返程出发那天就是离开日（后端同样校验）。
 */
export function transportErrors(t: TransportDraft, start: string, end: string): TransportErrors {
  const e: TransportErrors = {};
  if (!t.out.depart_at) e.out_depart = "去程出发时间必填";
  if (!t.out.arrive_at) e.out_arrive = "去程到达时间必填";
  else if (start && t.out.arrive_at.slice(0, 10) !== start) e.out_arrive = `到达时间要在开始日期 ${start} 当天（到达那天算行程第一天）`;
  if (t.out.depart_at && t.out.arrive_at && t.out.arrive_at <= t.out.depart_at) e.out_arrive = "到达时间要晚于出发时间";
  if (!t.back.depart_at) e.back_depart = "返程出发时间必填";
  else if (end && t.back.depart_at.slice(0, 10) !== end) e.back_depart = `返程出发时间要在离开日期 ${end} 当天`;
  return e;
}

/** 草稿 → 提交用的 transport（选填项没填就不带）。 */
export function toTransport(t: TransportDraft) {
  const seconds = (v: string) => (v.length === 16 ? `${v}:00` : v);
  return {
    outbound: {
      mode: t.out.mode,
      depart_at: seconds(t.out.depart_at),
      arrive_at: seconds(t.out.arrive_at),
      ...(t.out.place.trim() ? { from_place: t.out.place.trim() } : {}),
      ...(t.out.number.trim() ? { number: t.out.number.trim() } : {}),
    },
    inbound: {
      mode: t.back.mode,
      depart_at: seconds(t.back.depart_at),
      ...(t.back.place.trim() ? { to_place: t.back.place.trim() } : {}),
      ...(t.back.number.trim() ? { number: t.back.number.trim() } : {}),
    },
  };
}

/** 日期改了，已填的到达 / 出发时间跟着换日期（时间不变），免得用户重填。 */
export function moveLegDate(value: string, date: string): string {
  return value && date ? `${date}${value.slice(10)}` : value;
}

const NUMBER_LABEL: Record<TransportMode, string> = {
  flight: "航班号",
  high_speed_rail: "车次",
  train: "车次",
  coach: "班次",
  drive: "备注",
  other: "备注",
};

/**
 * 来回交通（规格 07 P07 / M06 S6）：方式 + 时间必填，出发地 / 航班号 / 车次选填。
 * 时间都是当地时间；到达时间决定第一天从几点开始排，返程出发时间决定最后一天几点前收尾。
 */
export function TransportEditor({
  value,
  onChange,
  errors,
}: {
  value: TransportDraft;
  onChange: (next: TransportDraft) => void;
  errors: TransportErrors;
}) {
  const out = (p: Partial<TransportLegDraft>) => onChange({ ...value, out: { ...value.out, ...p } });
  const back = (p: Partial<TransportLegDraft>) => onChange({ ...value, back: { ...value.back, ...p } });
  return (
    <div className="transport-editor" data-testid="transport-editor">
      <div className="leg-card" data-testid="leg-out">
        <h3>去程 <span className="hint">到达那天 = 行程第一天</span></h3>
        <div className="leg-grid">
          <div className="field">
            <label htmlFor="out_mode">去程交通方式</label>
            <select id="out_mode" value={value.out.mode} onChange={(e) => out({ mode: e.target.value as TransportMode })}>
              {MODE_OPTIONS.map((o) => (
                <option key={o.v} value={o.v}>{o.t}</option>
              ))}
            </select>
          </div>
          <div className="field" data-field="out_depart">
            <label htmlFor="out_depart">去程出发时间</label>
            <input id="out_depart" type="datetime-local" value={value.out.depart_at} onChange={(e) => out({ depart_at: e.target.value })} />
            {errors.out_depart && <div className="ferr">{errors.out_depart}</div>}
          </div>
          <div className="field" data-field="out_arrive">
            <label htmlFor="out_arrive">去程到达时间</label>
            <input
              id="out_arrive"
              type="datetime-local"
              value={value.out.arrive_at}
              onChange={(e) => out({ arrive_at: e.target.value })}
            />
            {errors.out_arrive && <div className="ferr">{errors.out_arrive}</div>}
          </div>
          <div className="field">
            <label htmlFor="out_place">出发地 <span className="hint">（选填）</span></label>
            <input id="out_place" value={value.out.place} maxLength={40} placeholder="例：上海浦东" onChange={(e) => out({ place: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="out_number">{NUMBER_LABEL[value.out.mode]} <span className="hint">（选填）</span></label>
            <input id="out_number" value={value.out.number} maxLength={20} onChange={(e) => out({ number: e.target.value })} />
          </div>
        </div>
      </div>
      <div className="leg-card" data-testid="leg-back">
        <h3>返程 <span className="hint">出发那天 = 行程最后一天</span></h3>
        <div className="leg-grid">
          <div className="field">
            <label htmlFor="back_mode">返程交通方式</label>
            <select id="back_mode" value={value.back.mode} onChange={(e) => back({ mode: e.target.value as TransportMode })}>
              {MODE_OPTIONS.map((o) => (
                <option key={o.v} value={o.v}>{o.t}</option>
              ))}
            </select>
          </div>
          <div className="field" data-field="back_depart">
            <label htmlFor="back_depart">返程出发时间</label>
            <input
              id="back_depart"
              type="datetime-local"
              value={value.back.depart_at}
              onChange={(e) => back({ depart_at: e.target.value })}
            />
            {errors.back_depart && <div className="ferr">{errors.back_depart}</div>}
          </div>
          <div className="field">
            <label htmlFor="back_place">回到哪里 <span className="hint">（选填）</span></label>
            <input id="back_place" value={value.back.place} maxLength={40} placeholder="例：上海" onChange={(e) => back({ place: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="back_number">{NUMBER_LABEL[value.back.mode]} <span className="hint">（选填）</span></label>
            <input id="back_number" value={value.back.number} maxLength={20} onChange={(e) => back({ number: e.target.value })} />
          </div>
        </div>
      </div>
      <p className="muted transport-tip" data-testid="transport-prefill-tip">
        已按上面的日期预填了钟点（到达 10:00、返程 18:00、出发 7:00），请改成你的实际时间；日期改了，这里的日期会跟着变。
      </p>
      <p className="muted transport-tip">
        选了飞机会提醒你值机，选高铁 / 火车会提醒取票、提前到站；这些提醒是通用建议，具体以航司 / 车站的说明为准。
      </p>
    </div>
  );
}
