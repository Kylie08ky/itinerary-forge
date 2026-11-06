import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { BudgetData, Currency, TravelerDraft } from "../types";
import { PartyEditor, partyError, toTravelers } from "./PartyEditor";

export function fmtCny(n: number | null | undefined): string {
  return n == null ? "–" : `¥${Math.round(n).toLocaleString("zh-CN")}`;
}

export function fmtEur(n: number | null | undefined): string {
  if (n == null) return "待补";
  const whole = Math.abs(n - Math.round(n)) < 0.005;
  return `€${n.toLocaleString("en-US", { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: whole ? 0 : 2 })}`;
}

/** 按币种格式化一个已经是该币种的金额。 */
export function fmtIn(currency: Currency | string | null | undefined, n: number | null | undefined): string {
  return currency === "EUR" ? fmtEur(n) : fmtCny(n);
}

/** 「¥1,863 · €249」/「€249 · ¥1,863」：结算货币在前，另一种并列；拿不到汇率时只有欧元。 */
export function money(eur: number, cny: number | null, main: Currency): string {
  if (cny == null) return fmtEur(eur);
  return main === "EUR" ? `${fmtEur(eur)} · ${fmtCny(cny)}` : `${fmtCny(cny)} · ${fmtEur(eur)}`;
}

const OPEN_KEY = "atp:budget-open";

function loadOpen(): boolean {
  try {
    return window.localStorage.getItem(OPEN_KEY) !== "0";
  } catch {
    return true;
  }
}

const SOURCE_TAG: Record<string, string> = {
  library: "地点库价格",
  estimate: "估算",
  official: "官方票价",
  user: "你填的",
};

const CURRENCY_NAME: Record<Currency, string> = { CNY: "人民币", EUR: "欧元" };

/**
 * 行程页的预算卡（规格 M09 S1 / S2）：门票 / 餐饮 / 交通 / 住宿，逐人计价，结算货币为主、另一种并列，人均和总计；
 * 出行人、结算货币、预算上限、餐饮自填都能在这里改（立刻重算，不产生行程版本）；
 * 设了上限且超出时给出最多 3 条更低价的替换建议。取不到预算时整张卡不显示。
 */
export function BudgetCard({
  planId,
  version,
  disabled,
  onSwap,
  onError,
}: {
  planId: string;
  version: number | undefined;
  dayTrip?: boolean; // （保留给调用方）没有预算数据时后端回 404 和原因，这里显示成一句说明
  disabled: boolean;
  onSwap: (pid: string, altPid: string) => void;
  onError: (msg: string) => void;
}) {
  const [data, setData] = useState<BudgetData | null>(null);
  const [off, setOff] = useState<string | null>(null); // 这份行程没有预算（一日游）：后端说明原因
  const [open, setOpen] = useState(loadOpen);
  const [party, setParty] = useState<TravelerDraft[]>([]);
  const [currency, setCurrency] = useState<Currency>("CNY");
  const [cap, setCap] = useState("");
  const [capCurrency, setCapCurrency] = useState<Currency>("CNY");
  const [meal, setMeal] = useState("");
  const [busy, setBusy] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
  const statusRef = useRef<HTMLDivElement>(null);

  const apply = useCallback((b: BudgetData) => {
    setData(b);
    setParty(b.travelers.map((t) => ({ name: t.name, age: t.age, residence: t.residence, student: t.student })));
    setCurrency(b.currency);
    setCap(b.cap == null ? "" : String(b.cap.amount));
    setCapCurrency(b.cap?.currency ?? b.currency);
    setMeal(b.meal.is_override ? String(b.meal.value) : "");
  }, []);

  const refresh = useCallback(async () => {
    if (version === undefined) return;
    try {
      apply(await api.budget(planId, version));
      setOff(null);
    } catch (e) {
      setData(null);
      setOff(e instanceof ApiError && e.status === 404 ? e.message : null);
    }
  }, [planId, version, apply]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 保存之后直接把「超出 / 在预算内」的提示带到眼前，不用自己往上翻
  useEffect(() => {
    if (!justSaved || !data) return;
    setJustSaved(false);
    const el = statusRef.current ?? document.querySelector('[data-testid="budget-card"]');
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [justSaved, data]);

  function toggle() {
    setOpen((v) => {
      try {
        window.localStorage.setItem(OPEN_KEY, v ? "0" : "1");
      } catch {
        /* 存不了就算了 */
      }
      return !v;
    });
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    const pe = partyError(party);
    if (pe) {
      onError(`${pe}。`);
      return;
    }
    const capNum = cap.trim() === "" ? null : Number(cap);
    const mealNum = meal.trim() === "" ? null : Number(meal);
    if ((capNum !== null && (!Number.isFinite(capNum) || capNum < 0)) || (mealNum !== null && (!Number.isFinite(mealNum) || mealNum < 0))) {
      onError("预算上限和餐饮金额要填不小于 0 的数字（不想设就留空）。");
      return;
    }
    setBusy(true);
    try {
      await api.saveBudgetSettings(planId, {
        travelers: toTravelers(party),
        currency,
        cap_amount: capNum,
        cap_currency: capCurrency,
        meal_eur_per_day: mealNum,
      });
      setOpen(true);
      await refresh();
      setJustSaved(true);
    } catch (err) {
      onError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (!data && off) {
    return (
      <section className="card budget-card budget-off" data-testid="budget-off" aria-label="预算">
        <header className="budget-head">
          <h2>预算</h2>
        </header>
        <p className="muted">{off}</p>
      </section>
    );
  }
  if (!data) return null;
  const t = data.total;
  const main = data.currency;
  const mainTotal = main === "EUR" ? t.eur_total : t.cny_total;
  const sideTotal = main === "EUR" ? t.cny_total : t.eur_total;
  const n = data.travelers.length;
  const status = data.status;

  return (
    <section className="card budget-card" data-testid="budget-card" aria-label="预算">
      <header className="budget-head">
        <button type="button" className={`day-toggle${open ? "" : " collapsed"}`} aria-expanded={open} aria-label={open ? "收起预算" : "展开预算"} onClick={toggle}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="m6 9 6 6 6-6" />
          </svg>
        </button>
        <h2>预算</h2>
        <span className="budget-total" data-testid="budget-total">
          {mainTotal != null ? fmtIn(main, mainTotal) : fmtEur(t.eur_total)}
          {mainTotal != null && sideTotal != null && <small> · {main === "EUR" ? fmtCny(sideTotal) : fmtEur(sideTotal)}</small>}
        </span>
        <span className="muted">{n > 1 ? `${n} 人合计` : "总计"}（估算）</span>
      </header>

      {open && (
        <>
          {status === "over" && data.cap && (
            <div className="budget-alert over" role="alert" data-testid="budget-over" ref={statusRef}>
              估算 {mainTotal != null ? fmtIn(main, mainTotal) : fmtEur(t.eur_total)}，<b>超出预算 {fmtIn(data.status_currency, data.status_amount)}</b>（上限{" "}
              {fmtIn(data.cap.currency, data.cap.amount)}）。
              {data.pending_count > 0 && "还有待补项，实际可能更高。"}
            </div>
          )}
          {status === "within" && data.cap && (
            <div className="budget-alert ok" data-testid="budget-within" ref={statusRef}>
              在预算内，还剩 <b>{fmtIn(data.status_currency, data.status_amount)}</b>（上限 {fmtIn(data.cap.currency, data.cap.amount)}）。
              {data.pending_count > 0 && "还有待补项，实际可能更高。"}
            </div>
          )}
          {status === "over" && data.gap && (
            <div className="budget-alert gap" role="status" data-testid="budget-gap">
              {data.gap.level === "unreachable" ? (
                <>
                  <b>差距很大，换景点解决不了。</b>就算所有门票都免费，门票以外的费用（餐饮 / 交通 / 住宿）也要{" "}
                  {fmtIn(main, data.gap.non_ticket)}，已经超过上限。建议提高上限，或减少天数 / 人数，或在下面把餐饮每人每天改成更低的数。
                </>
              ) : (
                <>
                  <b>差距比较大</b>（超出上限 {Math.round((data.gap.over_ratio ?? 0) * 100)}%）。门票合计只有 {fmtIn(main, data.gap.tickets)}，
                  就算全省掉也未必够，建议同时放宽上限。
                </>
              )}
            </div>
          )}
          {status === "unknown" && <div className="budget-alert" ref={statusRef}>暂时拿不到汇率，没法和人民币预算上限比较（可以把上限改成欧元）。</div>}

          {status === "over" && (
            <div className="budget-suggest" data-testid="budget-suggestions">
              {data.suggestions.length === 0 ? (
                <span className="muted">没有更便宜的同主题替代。</span>
              ) : (
                <>
                  <b>可以这样省：</b>
                  {data.suggestions.map((s) => (
                    <div className="sug-row" key={s.place_id} data-testid="budget-suggestion">
                      <span>
                        把「{s.title}」（{fmtEur(s.cost_eur)}）换成「{s.alt_title}」（{s.alt_cost_eur === 0 ? "免费" : fmtEur(s.alt_cost_eur)}），省{" "}
                        <b>{fmtIn(main, s.saving)}</b>
                      </span>
                      <button type="button" className="tool" disabled={disabled} onClick={() => onSwap(s.place_id, s.alt_place_id)}>
                        编辑行程，换成它
                      </button>
                    </div>
                  ))}
                </>
              )}
            </div>
          )}

          <div className="budget-rows">
            {data.categories.map((c) => (
              <div className="b-row" key={c.key} data-testid={`budget-${c.key}`}>
                <div className="b-top">
                  <b>{c.label}</b>
                  <span className={`tag src-${c.source}`}>{SOURCE_TAG[c.source] ?? c.source}</span>
                  <span className="spacer" />
                  <span className="b-amount">{c.unknown ? "待补" : money(c.eur_total, c.cny_total, main)}</span>
                </div>
                <div className="b-bar" aria-hidden>
                  <i style={{ width: `${Math.max(c.share * 100, c.eur_total > 0 ? 2 : 0)}%` }} />
                </div>
                {n > 1 && !c.unknown && <div className="muted b-note">人均 {money(c.eur_person, c.cny_person, main)}</div>}
                {c.pending.length > 0 && (
                  <div className="b-pending" data-testid={`budget-${c.key}-pending`}>
                    待补（{c.pending.length}）：{c.pending.join("、")}
                  </div>
                )}
                {(c.source_note || c.detail) && (
                  // 计算依据默认收起（规格 07 P12）：只留结论，想知道怎么算的再点开
                  <details className="b-info">
                    <summary aria-label={`${c.label}的计算依据`}>ⓘ 计算依据</summary>
                    {c.source_note && <div className="muted b-note">{c.source_note}</div>}
                    {c.detail && <div className="muted b-detail">{c.detail}</div>}
                  </details>
                )}
              </div>
            ))}
            <div className="b-row sum">
              <div className="b-top">
                <b>合计</b>
                <span className="spacer" />
                <span className="b-amount">{money(t.eur_total, t.cny_total, main)}</span>
              </div>
              <div className="muted b-note">人均 {money(t.eur_person, t.cny_person, main)}</div>
            </div>
          </div>

          {data.discounts.length > 0 && (
            <div className="budget-discounts" data-testid="budget-discounts">
              <b>已计入的优惠</b>
              {data.discounts.map((d) => (
                <div key={d.traveler} className="muted" data-testid="budget-discount">
                  第 {d.traveler} 位（{d.name ? `${d.name}，` : ""}{d.who}）省 {fmtEur(d.saved_eur)}：{d.places.join("、")}
                </div>
              ))}
            </div>
          )}

          <form className="budget-form" onSubmit={save} noValidate>
            <div className="budget-form-title">出行人与设置</div>
            <PartyEditor value={party} onChange={setParty} disabled={disabled} />
            <div className="budget-form-row">
              <label>
                结算货币
                <select value={currency} onChange={(e) => setCurrency(e.target.value as Currency)} aria-label="结算货币" disabled={disabled}>
                  <option value="CNY">人民币（旁边并列欧元）</option>
                  <option value="EUR">欧元（巴黎当地货币，旁边并列人民币）</option>
                </select>
              </label>
              <label>
                总预算上限
                <span className="inline-pair">
                  <input
                    type="number"
                    min={0}
                    placeholder="不设就留空"
                    value={cap}
                    onChange={(e) => setCap(e.target.value)}
                    aria-label="总预算上限"
                    disabled={disabled}
                  />
                  <select value={capCurrency} onChange={(e) => setCapCurrency(e.target.value as Currency)} aria-label="上限的货币" disabled={disabled}>
                    <option value="CNY">{CURRENCY_NAME.CNY}</option>
                    <option value="EUR">{CURRENCY_NAME.EUR}</option>
                  </select>
                </span>
              </label>
              <label>
                餐饮每人每天（欧元）
                <input
                  type="number"
                  min={0}
                  placeholder={`估算 ${data.meal.default}`}
                  value={meal}
                  onChange={(e) => setMeal(e.target.value)}
                  aria-label="餐饮每人每天（欧元）"
                  disabled={disabled}
                />
              </label>
              <button type="submit" className="btn-primary" disabled={disabled || busy}>
                保存
              </button>
            </div>
          </form>

          <div className="muted budget-foot">
            {data.fx ? (
              <span data-testid="budget-fx">
                汇率 1 欧元 = {data.fx.rate} 元（欧洲央行参考汇率，{data.fx.date}
                {data.fx.stale ? "，旧数据" : ""}）
              </span>
            ) : (
              <span data-testid="budget-fx">暂时拿不到汇率，只显示欧元</span>
            )}
          </div>
          {data.notes
            .filter((x) => !x.startsWith("暂时拿不到汇率"))
            .map((x) => (
              <div className="muted budget-note" key={x} data-testid="budget-note">
                {x}
              </div>
            ))}
        </>
      )}
    </section>
  );
}
