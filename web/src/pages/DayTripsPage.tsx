import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import { ErrorPopup } from "../confirm";
import { useReadOnly } from "../offline";
import type { AddressCandidate, DayTripDestination, DayTripHome, DayTripList, DayTripOption, DayTripOrigin } from "../types";

function today(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

const short = (iso: string) => `${Number(iso.slice(5, 7))} 月 ${Number(iso.slice(8, 10))} 日`;

const HOME_KEY = "atp:daytrip-home";
const DTICKET_KEY = "atp:has-dticket";

/** 存在这个浏览器里的小设置（出发地、有没有 Deutschlandticket）；读不到 / 存不了就当没有，页面照常。 */
function readStored<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw == null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

function writeStored(key: string, value: unknown) {
  try {
    if (value == null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* 存不了就算了 */
  }
}

const NOT_FOUND =
  "没找到这个地址。试试：① 用德语写法「街道 门牌号, 邮编 城市」，例如 Königstraße 28, 70173 Stuttgart；" +
  "② 去掉楼层、房间号、楼栋；③ 不确定门牌号就只写街道和城市；④ 实在不行写邮编加城市（70569 Stuttgart），" +
  "或者你家附近的车站 / 大学 / 小区名。差几百米不影响选目的地。";

type Filter = "all" | "regional" | "long" | "border";
type Sort = "fastest" | "slowest" | "arrive" | "name";

const FILTERS: { id: Filter; label: string; test: (d: DayTripDestination) => boolean }[] = [
  { id: "all", label: "全部", test: () => true },
  // 区间火车：有「只坐区间车」的方案，或者最快的本身就是纯区间车（Deutschlandticket 通常覆盖）
  { id: "regional", label: "只看区间火车", test: (d) => d.options.some((o) => o.kind === "regional" || o.covered) },
  // 长途火车：最快的方案要坐 ICE / IC 这类长途列车（要另买票）
  { id: "long", label: "只看长途火车", test: (d) => d.options.some((o) => o.kind === "fastest" && o.long_distance) },
  { id: "border", label: "只看跨境", test: (d) => d.cross_border },
];

const SORTS: { id: Sort; label: string }[] = [
  { id: "fastest", label: "用时从短到长" },
  { id: "slowest", label: "用时从长到短" },
  { id: "arrive", label: "最早到达" },
  { id: "name", label: "城市名" },
];

/** 这张卡默认选哪个方案：筛选「区间火车 / 长途火车」时按筛选；否则勾了 Deutschlandticket 且有区间车就用它，否则用最快的。 */
function defaultKind(d: DayTripDestination, dticket: boolean, filter: Filter = "all"): DayTripOption["kind"] {
  if (filter === "long") return "fastest";
  if (filter === "regional") return d.options.some((o) => o.kind === "regional") ? "regional" : "fastest";
  return dticket && d.options.some((o) => o.kind === "regional") ? "regional" : "fastest";
}

function chosen(d: DayTripDestination, kind: DayTripOption["kind"]): DayTripOption {
  return d.options.find((o) => o.kind === kind) ?? d.options[0];
}

function Seasonal({ s }: { s: DayTripDestination["seasonal"][number] }) {
  return (
    <div className={`dt-season${s.in_season ? " on" : ""}`} data-testid="dt-season" data-in-season={s.in_season}>
      <span className="dt-season-ico" aria-hidden>
        🎄
      </span>
      <div className="dt-season-body">
        <div className="dt-season-line">
          <b>{s.label}</b>
          <span className="dt-season-date">
            {short(s.start)} 至 {short(s.end)}
          </span>
          <span className={`dt-season-state${s.in_season ? " on" : ""}`}>{s.in_season ? "你选的日期在期内" : "你选的日期不在期内"}</span>
        </div>
        <div className="dt-season-src">
          {s.confirmed_by === "official" ? "官方网站写明" : "日期没能在官方网站核对，出发前请确认"} ·{" "}
          <a href={s.source_url} target="_blank" rel="noreferrer">
            来源 ↗
          </a>
        </div>
      </div>
    </div>
  );
}

function OptionRow({ o, name, on, onPick, two }: { o: DayTripOption; name: string; on: boolean; onPick: () => void; two: boolean }) {
  const rides = o.rides.map((r) => `${r.label}（${r.mode_zh}）`).join(" → ");
  const fare =
    o.needs_ticket === null ? "没有区分车型" : o.covered ? "Deutschlandticket 通常覆盖" : "需另买票";
  return (
    <label className={`dt-opt${on ? " on" : ""}`} data-testid={`dt-opt-${o.kind}`} data-selected={on}>
      <input type="radio" name={name} checked={on} onChange={onPick} />
      <span className="dt-opt-main">
        <span className="dt-opt-head">
          <b>{o.kind === "regional" ? "只坐区间车" : two ? "最快" : "时刻表上最快"}</b>
          <span className="dt-opt-min">{o.minutes} 分钟</span>
          <span className={`dt-fare${o.needs_ticket === false ? " free" : o.needs_ticket ? " paid" : ""}`} data-testid="dt-fare">
            {fare}
          </span>
        </span>
        <span className="dt-opt-line">
          {o.depart} 出发 → {o.arrive} 到 · {o.transfers === 0 ? "直达" : `换乘 ${o.transfers} 次`}
          {rides && ` · ${rides}`}
        </span>
      </span>
    </label>
  );
}

function Card({
  d,
  list,
  dticket,
  kind,
  onKind,
  busy,
  disabled,
  onPlan,
}: {
  d: DayTripDestination;
  list: DayTripList;
  dticket: boolean;
  kind: DayTripOption["kind"];
  onKind: (k: DayTripOption["kind"]) => void;
  busy: boolean;
  disabled: boolean;
  onPlan: (d: DayTripDestination, kind: DayTripOption["kind"]) => void;
}) {
  const opt = chosen(d, kind);
  const inSeason = d.seasonal.some((s) => s.in_season);
  const two = d.options.length > 1;
  return (
    <article className={`dt-card${inSeason ? " in-season" : ""}`} data-testid="daytrip-card" data-city={d.city.id}>
      <div className="dt-time" data-testid="dt-minutes">
        <span className="dt-time-cap">单程</span>
        <span className="dt-time-num">{opt.minutes}</span>
        <span className="dt-time-unit">分钟</span>
        <span className="dt-time-note">从住址出发 · 含步行</span>
      </div>
      <div className="dt-body">
        <h3>
          {d.city.name_zh}
          {d.city.name_local !== d.city.name_zh && <span className="local-name"> · {d.city.name_local}</span>}
          {d.cross_border && <span className="dt-border">跨境</span>}
        </h3>
        <div className="dt-opts" role="radiogroup" aria-label={`${d.city.name_zh}的出行方案`}>
          {d.options.map((o) => (
            <OptionRow key={o.kind} o={o} name={`opt-${d.city.id}`} on={o.kind === opt.kind} onPick={() => onKind(o.kind)} two={two} />
          ))}
        </div>
        <p className="dt-farenote muted" data-testid="dt-farenote">
          {opt.fare_note}
          {dticket && opt.needs_ticket && !d.cross_border ? "。你有 Deutschlandticket，但这一趟含长途列车，不在月票范围内" : ""}
        </p>
        <ul className="dt-highlights" aria-label="亮点">
          {d.highlights.map((h) => (
            <li key={h.id}>{h.label}</li>
          ))}
        </ul>
        {d.seasonal.map((s) => (
          <Seasonal key={s.id} s={s} />
        ))}
        <details className="dt-details" data-testid="dt-details">
          <summary>数据来源和说明</summary>
          <p>
            用时和班次：
            <a href={list.source_url} target="_blank" rel="noreferrer">
              {list.source}
            </a>
            ，查的是你选的日期和出发时间之后的班次（时刻表上的用时，不是实时延误）。到 {d.station.name}（{d.station.name_zh}）。
          </p>
          <p>
            票价没有开放数据，这里不报价：点「在 DB 查」看实时班次和票价。Deutschlandticket 覆盖区间 / 本地交通，不含 ICE / IC / EC，跨境段另买票，
            <a href={list.dticket_url} target="_blank" rel="noreferrer">
              以 DB 官方说明为准 ↗
            </a>
            。景点是开放数据草稿，价格和开放时间出发前请到官网确认。
          </p>
        </details>
      </div>
      <footer className="dt-foot">
        <span className="dt-count">
          {d.places} 个点，
          {d.drafts === d.places ? "全部是开放数据草稿" : `其中 ${d.drafts} 个是开放数据草稿`}
          {" · "}
          <a href={opt.db_url} target="_blank" rel="noreferrer" data-testid="dt-db-link">
            在 DB 查实时班次和票价 ↗
          </a>
          <span className="dt-dbq" data-testid="dt-db-query">
            （到 DB 搜：{opt.db_query}）
          </span>
        </span>
        <button type="button" className="primary dt-go" data-testid="plan-day" disabled={disabled || busy} onClick={() => onPlan(d, opt.kind)}>
          {busy ? "生成中…" : "排一天"}
        </button>
      </footer>
    </article>
  );
}

/**
 * 周边一日游 `/daytrips`（规格 08 3.4、3.6、3.7）：「我住在这儿，附近哪些城市值得去一天」。
 * 填住址（或用市中心车站快捷入口）→ 现查从住址出发的路线：每座目的地给「最快」和「只坐区间车」两个方案，
 * 标清楚要不要另买票（Deutschlandticket 不含 ICE / IC）→ 「排一天」生成当天往返的行程。
 * 住址和「我有 Deutschlandticket」只存在这个浏览器里。
 */
export function DayTripsPage() {
  const navigate = useNavigate();
  const { readOnly } = useReadOnly();
  const [presets, setPresets] = useState<DayTripOrigin[]>([]);
  const [home, setHome] = useState<DayTripHome | null>(() => readStored<DayTripHome | null>(HOME_KEY, null));
  const [dticket, setDticket] = useState<boolean>(() => readStored<boolean>(DTICKET_KEY, false));
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<AddressCandidate[] | null>(null);
  const [finding, setFinding] = useState(false);
  const [changing, setChanging] = useState(false);
  const [date, setDate] = useState(today());
  const [depart, setDepart] = useState("09:00");
  const [back, setBack] = useState("");
  const [list, setList] = useState<DayTripList | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<Sort>("fastest");
  const [picked, setPicked] = useState<Record<string, DayTripOption["kind"]>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const msg = (e: unknown) => (e instanceof ApiError ? e.message : String(e));

  useEffect(() => {
    api.daytripOrigins().then(setPresets).catch((e) => setError(msg(e)));
  }, []);

  useEffect(() => {
    if (!home || !date || !/^\d\d:\d\d$/.test(depart)) return;
    let live = true;
    setLoading(true);
    const timer = window.setTimeout(() => {
      api
        .daytripSearch(home, date, depart)
        .then((l) => {
          if (!live) return;
          setList(l);
          setPicked({});
        })
        .catch((e) => live && setError(msg(e)))
        .finally(() => live && setLoading(false));
    }, 300);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [home, date, depart]);

  function chooseHome(h: DayTripHome | null) {
    setHome(h);
    writeStored(HOME_KEY, h);
    setList(null);
    setCandidates(null);
    setChanging(false);
    setQuery("");
  }

  async function find(e: React.FormEvent) {
    e.preventDefault();
    if (query.trim().length < 3) {
      setError("地址写长一点：街道 + 门牌号 + 城市，例如「Königstraße 28, Stuttgart」。");
      return;
    }
    setFinding(true);
    try {
      const found = await api.daytripGeocode(query.trim());
      setCandidates(found);
      if (found.length === 0) setError(NOT_FOUND);
    } catch (err) {
      setError(msg(err));
    } finally {
      setFinding(false);
    }
  }

  function toggleDticket(on: boolean) {
    setDticket(on);
    writeStored(DTICKET_KEY, on);
    setPicked({});
  }

  async function plan(d: DayTripDestination, kind: DayTripOption["kind"]) {
    if (!home) return;
    setBusy(d.city.id);
    try {
      const r = await api.daytripPlanRequest(home, { destination: d.city.id, date, depart, back: back || undefined, kind });
      const { task_id } = await api.createTask(r.payload);
      navigate(`/tasks/${task_id}`);
    } catch (e) {
      setError(msg(e));
      setBusy(null);
    }
  }

  const kindOf = (d: DayTripDestination) => picked[d.city.id] ?? defaultKind(d, dticket, filter);
  const counts = useMemo(
    () => Object.fromEntries(FILTERS.map((f) => [f.id, (list?.destinations ?? []).filter(f.test).length])) as Record<Filter, number>,
    [list],
  );
  const shown = useMemo(() => {
    const test = FILTERS.find((f) => f.id === filter)?.test ?? (() => true);
    const kind = (d: DayTripDestination) => picked[d.city.id] ?? defaultKind(d, dticket, filter);
    const minutes = (d: DayTripDestination) => chosen(d, kind(d)).minutes;
    const arrive = (d: DayTripDestination) => chosen(d, kind(d)).arrive;
    const by: Record<Sort, (a: DayTripDestination, b: DayTripDestination) => number> = {
      fastest: (a, b) => minutes(a) - minutes(b),
      slowest: (a, b) => minutes(b) - minutes(a),
      arrive: (a, b) => arrive(a).localeCompare(arrive(b)),
      name: (a, b) => a.city.name_zh.localeCompare(b.city.name_zh, "zh-CN"),
    };
    return (list?.destinations ?? []).filter(test).sort(by[sort]);
  }, [list, picked, dticket, filter, sort]);

  return (
    <div className="wrap wide daytrips" data-testid="daytrips-page">
      <header className="dt-hero">
        <h1>周边一日游</h1>
        <p>从你住的地方出发，当天坐火车往返，看看附近哪些城市值得去一天。用时是从你的住址算的，并标清楚要不要另买票。</p>
      </header>

      <section className="dt-bar" aria-label="出发条件">
        <div className="dt-field dt-field-origin" data-testid="dt-origin">
          <span className="dt-label">你的住址（只存在这个浏览器里）</span>
          {home && !changing ? (
            <div className="dt-home">
              <span className="dt-home-label" data-testid="dt-home-label">
                {home.label}
              </span>
              <button type="button" className="tool" data-testid="dt-change-home" onClick={() => setChanging(true)}>
                换一个
              </button>
            </div>
          ) : (
            <form className="dt-addr" onSubmit={find}>
              <input
                type="text"
                value={query}
                placeholder="街道 门牌号, 邮编 城市，如 Königstraße 28, 70173 Stuttgart（也可以只写街道和城市）"
                aria-label="住址"
                data-testid="dt-address"
                onChange={(e) => setQuery(e.target.value)}
              />
              <button type="submit" className="primary" disabled={finding} data-testid="dt-find">
                {finding ? "查找中…" : "查找"}
              </button>
              {home && (
                <button type="button" className="tool" onClick={() => setChanging(false)}>
                  取消
                </button>
              )}
            </form>
          )}
          {(!home || changing) && candidates && candidates.length > 0 && (
            <div className="dt-cands" role="list" aria-label="候选地址">
              {candidates.map((c) => (
                <button
                  type="button"
                  role="listitem"
                  key={`${c.lat},${c.lng}`}
                  className="dt-cand"
                  data-testid="dt-candidate"
                  onClick={() => chooseHome({ label: c.label, city: c.city, city_zh: c.city_zh, country: c.country, lat: c.lat, lng: c.lng })}
                >
                  {c.label}
                  <small>
                    {c.kind === "STOP" ? "车站 · " : c.kind === "PLACE" ? "地点 · " : ""}
                    {c.country}
                  </small>
                </button>
              ))}
              <p className="muted dt-cands-tip" data-testid="dt-cands-tip">
                没有你的门牌号？选同一条街上的任何一个就行，差几十米，用时差不了几分钟。
              </p>
            </div>
          )}
          {(!home || changing) && (
            <div className="dt-presets">
              <span className="muted">或者直接用市中心车站：</span>
              {presets.map((o) => (
                <button
                  type="button"
                  key={o.id}
                  className="dt-origin"
                  data-testid={`origin-${o.id}`}
                  onClick={() =>
                    chooseHome({
                      label: `${o.station.name}（${o.name_zh} ${o.name_local} 市中心车站）`,
                      city: o.name_local,
                      city_zh: o.name_zh,
                      country: o.country,
                      lat: o.station.lat,
                      lng: o.station.lng,
                      preset: o.id,
                    })
                  }
                >
                  {o.name_zh} {o.name_local}
                </button>
              ))}
            </div>
          )}
        </div>
        <label className="dt-field">
          <span className="dt-label">日期</span>
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} data-testid="dt-date" />
        </label>
        <label className="dt-field">
          <span className="dt-label">出发时间</span>
          <input type="time" value={depart} onChange={(e) => setDepart(e.target.value)} data-testid="dt-depart" />
        </label>
        <label className="dt-field">
          <span className="dt-label">最晚返程出发</span>
          <input type="time" value={back} onChange={(e) => setBack(e.target.value)} data-testid="dt-back" />
        </label>
        <label className="dt-dticket">
          <input type="checkbox" checked={dticket} onChange={(e) => toggleDticket(e.target.checked)} data-testid="dt-dticket" />
          <span>我有 Deutschlandticket（德国月票）</span>
        </label>
      </section>

      {!home && <p className="muted dt-empty">先填你的住址，或者点上面的市中心车站，就能看到附近的目的地。</p>}
      {home && (loading || list === null) && <div className="muted dt-loading">正在查从你的住址出发的班次…（约 20 秒：公共时刻表服务要一座城市一座城市地查，同一天同一地址 3 小时内再看会很快）</div>}
      {list?.offline && (
        <div className="banner warn" data-testid="dt-offline">
          时刻表服务暂时连不上：下面是之前存好的「从市中心车站出发」的用时，没有区分 ICE 和区间车，请到 DB 确认班次和票价。
        </div>
      )}
      {list && !!list.missing?.length && !loading && (
        <div className="banner warn" data-testid="dt-missing">
          有 {list.missing.length} 座城市这次没查到（时刻表服务太忙），改一下出发时间或稍后再试就会补上。
        </div>
      )}
      {list && list.destinations.length === 0 && !loading && (
        <p className="muted dt-empty" data-testid="dt-none">
          这个住址附近 160 公里内还没有收录的一日游目的地（现在收录的是斯图加特和亚琛周边各 7 座城市）。
        </p>
      )}
      {list && list.destinations.length > 0 && (
        <div className="dt-toolbar" data-testid="dt-toolbar">
          <div className="dt-filters" role="group" aria-label="筛选">
            {FILTERS.map((f) => (
              <button
                type="button"
                key={f.id}
                className={`dt-chip${filter === f.id ? " on" : ""}`}
                aria-pressed={filter === f.id}
                data-testid={`dt-filter-${f.id}`}
                disabled={f.id !== "all" && counts[f.id] === 0}
                onClick={() => {
                  setFilter(f.id);
                  setPicked({});
                }}
              >
                {f.label}
                <small>{counts[f.id]}</small>
              </button>
            ))}
          </div>
          <label className="dt-sort">
            <span className="dt-label">排序</span>
            <select value={sort} onChange={(e) => setSort(e.target.value as Sort)} data-testid="dt-sort">
              {SORTS.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      {list && shown.length > 0 && (
        <div className="dt-list" data-testid="dt-list">
          {shown.map((d) => (
            <Card
              key={d.city.id}
              d={d}
              list={list}
              dticket={dticket}
              kind={kindOf(d)}
              onKind={(k) => setPicked((p) => ({ ...p, [d.city.id]: k }))}
              busy={busy === d.city.id}
              disabled={readOnly || busy !== null}
              onPlan={plan}
            />
          ))}
        </div>
      )}
      <ErrorPopup message={error} onClose={() => setError(null)} />
    </div>
  );
}
