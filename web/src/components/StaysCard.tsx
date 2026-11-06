import { useState } from "react";
import { api, ApiError } from "../api";
import type { AreaRec, PlanResponse, Stay } from "../types";

interface Form {
  name: string;
  check_in: string;
  check_out: string;
  area: string;
  address: string;
  nightly: string;
  note: string;
  lat: number | null;
  lng: number | null;
  origAddress: string;
}

function emptyForm(checkIn: string, checkOut: string, area = ""): Form {
  return { name: "", check_in: checkIn, check_out: checkOut, area, address: "", nightly: "", note: "", lat: null, lng: null, origAddress: "" };
}

function fromStay(s: Stay): Form {
  return {
    name: s.name,
    check_in: s.check_in,
    check_out: s.check_out,
    area: s.area ?? "",
    address: s.address ?? "",
    nightly: s.nightly_eur == null ? "" : String(s.nightly_eur),
    note: s.note ?? "",
    lat: s.lat,
    lng: s.lng,
    origAddress: s.address ?? "",
  };
}

function toStay(f: Form): Stay {
  const addressChanged = f.address.trim() !== f.origAddress.trim();
  return {
    name: f.name.trim(),
    check_in: f.check_in,
    check_out: f.check_out,
    area: f.area.trim() || null,
    address: f.address.trim() || null,
    // 地址改了就丢掉旧坐标，让后端重新定位；没改就保留
    lat: addressChanged ? null : f.lat,
    lng: addressChanged ? null : f.lng,
    nightly_eur: f.nightly.trim() === "" ? null : Number(f.nightly),
    note: f.note.trim() || null,
  };
}

function nights(s: Stay): number {
  return Math.round((new Date(s.check_out).getTime() - new Date(s.check_in).getTime()) / 86_400_000);
}

function locateText(s: Stay): string {
  if (s.lat != null) return "已按地址定位";
  if (s.area) return `路线按 ${s.area} 的中心估算`;
  return "没能定位，路线按巴黎市中心估算";
}

/**
 * 住宿（规格 M06 S5 / S5b）：每家住宿有入住 / 退房日期，重新排程时是每天路线的起终点。
 * 保存会生成一个「住宿更新」版本；酒店名、地址是你自己填的，系统不推荐具体酒店，只告诉你住哪个区更省路。
 */
export function StaysCard({
  planId,
  stays,
  tripStart,
  tripEnd,
  disabled,
  onSaved,
  onError,
}: {
  planId: string;
  stays: Stay[];
  tripStart: string;
  tripEnd: string;
  disabled: boolean;
  onSaved: (res: PlanResponse) => void;
  onError: (msg: string) => void;
}) {
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [form, setForm] = useState<Form>(emptyForm(tripStart, tripEnd));
  const [reschedule, setReschedule] = useState(false);
  const [busy, setBusy] = useState(false);
  const [recs, setRecs] = useState<{ recommendations: AreaRec[]; caveat: string } | null>(null);
  const [recOpen, setRecOpen] = useState(false);

  const totalEur = stays.reduce((sum, s) => sum + (s.nightly_eur ?? 0) * nights(s), 0);
  const priced = stays.some((s) => s.nightly_eur != null);

  function startNew(area = "") {
    const last = stays[stays.length - 1];
    const from = last && last.check_out < tripEnd ? last.check_out : tripStart;
    setForm(emptyForm(from, tripEnd, area));
    setEditing("new");
  }

  async function save(list: Stay[]) {
    setBusy(true);
    try {
      const res = await api.saveStays(planId, list, reschedule);
      setEditing(null);
      onSaved(res);
    } catch (e) {
      onError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const s = toStay(form);
    const list = editing === "new" ? [...stays, s] : stays.map((x, i) => (i === editing ? s : x));
    void save(list);
  }

  async function toggleRec() {
    setRecOpen((v) => !v);
    if (recs) return;
    try {
      setRecs(await api.stayRecommendation(planId));
    } catch (e) {
      onError(e instanceof ApiError ? e.message : String(e));
    }
  }

  const set = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  return (
    <div className="card stays-card">
      <div className="stays-head">
        <h2 style={{ margin: 0, fontSize: 16 }}>住宿</h2>
        <span className="spacer" />
        <button className="tool" onClick={() => void toggleRec()} aria-expanded={recOpen}>
          住哪个区？
        </button>
        <button className="tool" disabled={disabled || busy} onClick={() => startNew()}>
          ＋ 添加住宿
        </button>
      </div>

      {recOpen && (
        <div className="stay-rec">
          {!recs && <div className="muted">计算中…</div>}
          {recs && recs.recommendations.length === 0 && <div className="muted">行程里还没有（市区的）景点，先生成行程再来看。</div>}
          {recs?.recommendations.map((r, i) => (
            <div className="rec-row" key={r.area}>
              <span>
                <b>
                  {i + 1}. {r.area}
                </b>
                <span className="muted"> · {r.reason}</span>
              </span>
              <button className="tool" disabled={disabled || busy} onClick={() => startNew(r.area)}>
                用这个区添加住宿
              </button>
            </div>
          ))}
          {recs && <div className="muted rec-caveat">{recs.caveat}</div>}
        </div>
      )}

      {stays.length === 0 && editing === null && (
        <p className="muted" style={{ margin: "10px 0 0" }}>
          还没填住宿。填了之后每天顶部会显示「今晚住哪」，重新排程时以它为每天路线的起终点。不知道住哪个区，点上面的「住哪个区？」。
        </p>
      )}

      {stays.map((s, i) => (
        <div className="stay-row" key={`${s.name}-${s.check_in}`}>
          <div>
            <b>{s.name}</b>
            <span className="muted">
              {" "}
              · {s.check_in.slice(5)} → {s.check_out.slice(5)}（{nights(s)} 晚）
              {s.area ? ` · ${s.area}` : ""}
              {s.nightly_eur != null ? ` · €${s.nightly_eur}/晚` : ""}
            </span>
            <div className="muted stay-sub">
              {s.address ? `${s.address} · ` : ""}
              {locateText(s)}
              {s.note ? ` · ${s.note}` : ""}
            </div>
          </div>
          <span className="stay-actions">
            <button
              className="tool"
              disabled={disabled || busy}
              onClick={() => {
                setForm(fromStay(s));
                setEditing(i);
              }}
            >
              编辑
            </button>
            <button className="tool danger" disabled={disabled || busy} onClick={() => void save(stays.filter((_, j) => j !== i))}>
              删除
            </button>
          </span>
        </div>
      ))}
      {priced && (
        <div className="muted stay-total">
          住宿合计约 €{totalEur.toFixed(0)}（按你填的每晚价格 × 晚数；没填价格的不算）
        </div>
      )}

      {editing !== null && (
        <form className="stay-form" onSubmit={submit}>
          <div className="row2">
            <label>
              名称
              <input value={form.name} onChange={set("name")} required maxLength={80} placeholder="例：Hôtel Test" />
            </label>
            <label>
              所在区
              <input value={form.area} onChange={set("area")} placeholder="例：9区" />
            </label>
          </div>
          <div className="row2">
            <label>
              入住
              <input type="date" value={form.check_in} min={tripStart} max={tripEnd} onChange={set("check_in")} required />
            </label>
            <label>
              退房
              <input type="date" value={form.check_out} min={tripStart} max={tripEnd} onChange={set("check_out")} required />
            </label>
          </div>
          <label>
            地址 <span className="hint">（选填；填了会尝试用 OpenStreetMap 定位，定位不到就按所在区的中心估算）</span>
            <input value={form.address} onChange={set("address")} maxLength={200} placeholder="例：12 Rue de Rivoli, 75001 Paris" />
          </label>
          <div className="row2">
            <label>
              每晚价格（€，选填）
              <input type="number" min={0} step="1" value={form.nightly} onChange={set("nightly")} />
            </label>
            <label>
              备注
              <input value={form.note} onChange={set("note")} maxLength={200} />
            </label>
          </div>
          <label className="check-inline">
            <input type="checkbox" checked={reschedule} onChange={(e) => setReschedule(e.target.checked)} />
            保存后按新住宿重新排程（已锁定的点不动）
          </label>
          <div className="share-row">
            <button className="primary" type="submit" disabled={busy}>
              {busy ? "保存中…" : "保存住宿"}
            </button>
            <button className="tool" type="button" onClick={() => setEditing(null)}>
              取消
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
