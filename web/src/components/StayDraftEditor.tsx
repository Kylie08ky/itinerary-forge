export interface StayDraft {
  name: string; // 空 = 自动叫「住宿 N」
  area: string;
  address: string;
  check_in: string;
  check_out: string;
  nightly: string; // 欧元，空 = 不填
}

export const MAX_STAYS = 10;

export function newStay(prev: StayDraft | undefined, start: string, end: string): StayDraft {
  return { name: "", area: "", address: "", check_in: prev?.check_out || start, check_out: end, nightly: "" };
}

/** 提交前检查：返回第一条错误（没有问题返回 null）。规则和后端一致：区或地址至少一个、退房晚于入住、在行程内、互不重叠。 */
export function staysError(stays: StayDraft[], start: string, end: string): string | null {
  const sorted = [...stays].sort((a, b) => a.check_in.localeCompare(b.check_in));
  for (const s of stays) {
    const label = s.name.trim() || `第 ${stays.indexOf(s) + 1} 处住宿`;
    if (!s.area.trim() && !s.address.trim()) return `${label}：请填所在区或地址（至少一个），否则没法算每天的路线`;
    if (!s.check_in || !s.check_out) return `${label}：入住和退房日期都要填`;
    if (s.check_out <= s.check_in) return `${label}：退房日期要晚于入住日期`;
    if (s.check_in < start || s.check_out > end) return `${label}：日期要在行程的 ${start} 到 ${end} 之内`;
    if (s.nightly.trim() !== "" && (!Number.isFinite(Number(s.nightly)) || Number(s.nightly) < 0)) return `${label}：每晚价格要填不小于 0 的数字`;
  }
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i].check_in < sorted[i - 1].check_out) return "有两处住宿的日期重叠了，请改一下入住 / 退房日期";
  }
  return null;
}

/** 草稿 → 提交用的 stays。 */
export function toStays(stays: StayDraft[]) {
  return stays.map((s, i) => ({
    name: s.name.trim() || `住宿 ${i + 1}`,
    check_in: s.check_in,
    check_out: s.check_out,
    ...(s.area.trim() ? { area: s.area.trim() } : {}),
    ...(s.address.trim() ? { address: s.address.trim() } : {}),
    ...(s.nightly.trim() !== "" ? { nightly_eur: Number(s.nightly) } : {}),
  }));
}

/**
 * 住宿（规格 07 P08）：可以填多处，每处有入住 / 退房日期，用来决定每天路线从哪里出发、回到哪里。
 * 都是选填：先不确定也行，之后在行程页的「住宿」里再补。
 */
export function StayDraftEditor({
  value,
  onChange,
  start,
  end,
}: {
  value: StayDraft[];
  onChange: (next: StayDraft[]) => void;
  start: string;
  end: string;
}) {
  const patch = (i: number, p: Partial<StayDraft>) => onChange(value.map((s, k) => (k === i ? { ...s, ...p } : s)));
  return (
    <div className="stay-editor" data-testid="stay-editor">
      {value.length === 0 && <p className="muted">还没订也没关系，先不填；订好后可以在行程页的「住宿」里补上。</p>}
      {value.map((s, i) => (
        <div className="stay-draft" key={i} data-testid="stay-row">
          <div className="stay-draft-head">
            <b>第 {i + 1} 处住宿</b>
            <button type="button" className="tool danger" aria-label={`删除第 ${i + 1} 处住宿`} onClick={() => onChange(value.filter((_, k) => k !== i))}>
              删除
            </button>
          </div>
          <div className="stay-draft-grid">
            <div className="field">
              <label htmlFor={`stay_name_${i}`}>名称 <span className="hint">（选填）</span></label>
              <input id={`stay_name_${i}`} value={s.name} maxLength={80} placeholder={`住宿 ${i + 1}`} onChange={(e) => patch(i, { name: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor={`stay_area_${i}`}>所在区 / 地铁站</label>
              <input id={`stay_area_${i}`} value={s.area} maxLength={40} placeholder="例：9 区 / Opéra 站" onChange={(e) => patch(i, { area: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor={`stay_addr_${i}`}>地址 <span className="hint">（选填）</span></label>
              <input id={`stay_addr_${i}`} value={s.address} maxLength={200} onChange={(e) => patch(i, { address: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor={`stay_in_${i}`}>入住日期</label>
              <input id={`stay_in_${i}`} type="date" value={s.check_in} min={start} max={end} onChange={(e) => patch(i, { check_in: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor={`stay_out_${i}`}>退房日期</label>
              <input id={`stay_out_${i}`} type="date" value={s.check_out} min={start} max={end} onChange={(e) => patch(i, { check_out: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor={`stay_price_${i}`}>每晚价格 <span className="hint">（欧元）</span></label>
              <input id={`stay_price_${i}`} type="number" min={0} value={s.nightly} placeholder="算预算用" onChange={(e) => patch(i, { nightly: e.target.value })} />
            </div>
          </div>
        </div>
      ))}
      <div className="party-foot">
        <button
          type="button"
          className="tool"
          disabled={value.length >= MAX_STAYS || !start || !end}
          onClick={() => onChange([...value, newStay(value[value.length - 1], start, end)])}
        >
          ＋ 添加住宿
        </button>
      </div>
    </div>
  );
}
