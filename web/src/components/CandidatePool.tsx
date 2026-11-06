import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { ErrorPopup } from "../confirm";
import { bilingual, localLabel } from "../names";
import type { AttractionRef, LookupHit, LookupResult, NoteSource, PendingPlace, ThemeDetail } from "../types";
import { type ImportAccepted, mapsSearchUrl, NoteImport, TipChips } from "./NoteImport";

/** 候选池里一个地点的用户设置（规格 M02 S5）：必去 / 不去 / 默认，以及可选的权重覆盖。 */
export interface PoolPref {
  state: "default" | "must" | "avoid";
  weight: number | null; // null = 用主题里的默认权重
}
export type PoolPrefs = Record<string, PoolPref>;

/** 把面板里的设置转成后端请求字段。 */
export function prefsToPayload(prefs: PoolPrefs): {
  must_go_ids: string[];
  avoid_ids: string[];
  place_weights: Record<string, number>;
} {
  const must: string[] = [];
  const avoid: string[] = [];
  const weights: Record<string, number> = {};
  for (const [pid, p] of Object.entries(prefs)) {
    if (p.state === "must") must.push(pid);
    else if (p.state === "avoid") avoid.push(pid);
    else if (p.weight != null) weights[pid] = p.weight;
  }
  return { must_go_ids: must, avoid_ids: avoid, place_weights: weights };
}

const LOOKUP_REASON: Record<string, string> = {
  no_key: "没配高德 Key，没法在高德里搜。",
  quota: "本月高德搜索次数用完了，先用地点库。",
  unreachable: "暂时连不上高德，稍后再试。",
};

const ROLE_ZH: Record<string, string> = { anchor: "主要点", support: "辅助点" };

interface Row {
  place: AttractionRef;
  defaultWeight: number | null; // 所选主题里的默认权重；搜索加进来的池外地点为 null
  role: string | null;
}

export function CandidatePool({
  city,
  themeIds,
  places,
  prefs,
  onChange,
  onOpenPlace,
  noteSources = {},
  notePending = [],
  onImported,
  onRemovePending,
  onPlaceAdded,
  amapCity = false,
  importSignal = 0,
}: {
  city?: string; // 地点库在哪座城市；不传就是默认城市
  themeIds: string[];
  places: AttractionRef[];
  prefs: PoolPrefs;
  onChange: (next: PoolPrefs) => void;
  onOpenPlace: (pid: string) => void;
  noteSources?: Record<string, NoteSource>; // 来自笔记的地点 -> 原文片段（规格 M12 S3）
  notePending?: PendingPlace[]; // 笔记里提到、地点库里没有的地点（规格 M12 S2）
  onImported?: (a: ImportAccepted) => void;
  onRemovePending?: (name: string) => void;
  onPlaceAdded?: (a: AttractionRef) => void; // 高德里搜到的点加进了地点库：让上层的地点列表也有它
  amapCity?: boolean; // 这座城市的地图是高德（国内）：库里搜不到时可以去高德里搜
  importSignal?: number; // 每加一，就打开「从笔记导入」弹窗（首页提示卡的圆形按钮）
}) {
  const [themes, setThemes] = useState<Record<string, ThemeDetail>>({});
  const [extra, setExtra] = useState<string[]>([]); // 搜索加进来的池外地点
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<AttractionRef[]>([]);
  const [importing, setImporting] = useState(false);
  useEffect(() => {
    if (importSignal > 0) setImporting(true);
  }, [importSignal]);
  const [openNote, setOpenNote] = useState<string | null>(null);
  const [lookup, setLookup] = useState<{ q: string; busy: boolean; result?: LookupResult } | null>(null);
  const [lookupError, setLookupError] = useState<string | null>(null);
  const seq = useRef(0);

  // 读所选主题的成员（带默认权重）
  useEffect(() => {
    for (const id of themeIds) {
      if (themes[id]) continue;
      api
        .theme(id, city)
        .then((t) => setThemes((cur) => ({ ...cur, [id]: t })))
        .catch(() => undefined);
    }
  }, [themeIds, themes, city]);

  // 库内搜索（输入即搜，丢掉过期的结果）
  useEffect(() => {
    const text = q.trim();
    setLookup(null);
    if (!text) {
      setHits([]);
      return;
    }
    const mine = ++seq.current;
    const t = setTimeout(() => {
      api
        .searchPlaces(text, city)
        .then((r) => mine === seq.current && setHits(r))
        .catch(() => mine === seq.current && setHits([]));
    }, 200);
    return () => clearTimeout(t);
  }, [q, city]);

  const byId = useMemo(() => new Map(places.map((p) => [p.place_id as string, p])), [places]);

  const rows: Row[] = useMemo(() => {
    const out: Row[] = [];
    const seen = new Set<string>();
    for (const tid of themeIds) {
      for (const m of themes[tid]?.members ?? []) {
        const place = byId.get(m.place_id);
        if (!place || seen.has(m.place_id)) continue;
        seen.add(m.place_id);
        out.push({ place, defaultWeight: m.weight, role: m.role });
      }
    }
    for (const pid of extra) {
      const place = byId.get(pid);
      if (place && !seen.has(pid)) {
        seen.add(pid);
        out.push({ place, defaultWeight: null, role: null });
      }
    }
    return out;
  }, [themeIds, themes, byId, extra]);

  const touched = Object.values(prefs).filter((p) => p.state !== "default" || p.weight != null).length;

  function set(pid: string, next: Partial<PoolPref>) {
    const cur = prefs[pid] ?? { state: "default", weight: null };
    const merged = { ...cur, ...next };
    const copy = { ...prefs };
    if (merged.state === "default" && merged.weight == null) delete copy[pid];
    else copy[pid] = merged;
    onChange(copy);
  }

  /** 笔记导入确认后：接受的地点进候选池（不在所选主题里的和「搜索加入」一样放进额外行），状态按确认页里选的。 */
  function applyImport(a: ImportAccepted) {
    const next = { ...prefs };
    const added: string[] = [];
    for (const p of a.picks) {
      if (!rows.some((r) => r.place.place_id === p.place_id) && !extra.includes(p.place_id)) added.push(p.place_id);
      next[p.place_id] = { state: p.state, weight: null };
    }
    if (added.length) setExtra((e) => [...e, ...added]);
    onChange(next);
    onImported?.(a);
    setImporting(false);
  }

  /** 点了才调高德（耗一次搜索配额）；输入时的联想只搜地点库。 */
  function searchAmap() {
    const text = q.trim();
    setLookup({ q: text, busy: true });
    const done = (result: LookupResult) => setLookup((cur) => (cur?.q === text ? { q: text, busy: false, result } : cur));
    api
      .lookupPlace(text, city)
      .then(done)
      .catch(() => done({ hits: [], reason: "unreachable" }));
  }

  /** 高德里搜到的点：先加进这座城市的地点库，之后和库里搜到的点一样加入并标为必去。 */
  function addFromAmap(h: LookupHit) {
    api
      .addCustomPlace(city ?? "", h)
      .then((a) => {
        onPlaceAdded?.(a);
        addFromSearch(a);
      })
      .catch((e) => setLookupError(e instanceof Error ? e.message : "没能加入这个地点"));
  }

  function addFromSearch(a: AttractionRef) {
    const pid = a.place_id as string;
    if (!rows.some((r) => r.place.place_id === pid)) setExtra((e) => [...e, pid]);
    set(pid, { state: "must" }); // 专门搜来加的，默认就是「想去」
    setQ("");
    setHits([]);
  }

  return (
    <>
    <details className="pool">
      <summary>
        自定义候选地点
        {touched > 0 && <span className="pool-count">已设置 {touched} 项</span>}
      </summary>

      <div className="pool-import">
        <button type="button" className="tool" data-testid="note-import-open" onClick={() => setImporting(true)}>
          📝 从笔记导入
        </button>
      </div>
      <div className="pool-search">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="在地点库里搜：名字、英文名或区域，比如「卢浮」「Orsay」「7区」"
          aria-label="搜索地点库"
        />
        {q.trim() && (
          <div className="pool-hits">
            {hits.length === 0 && (
              <div className="muted">
                地点库里没有找到。
                {!amapCity && "库外的地点暂不支持（没有开放时间和闭馆日，没法可靠排进行程）。"}
              </div>
            )}
            {hits.length === 0 && amapCity && !lookup && (
              <button type="button" className="tool" data-testid="amap-lookup" onClick={searchAmap}>
                在高德里搜「{q.trim()}」
              </button>
            )}
            {lookup?.busy && <div className="muted">正在高德里搜…</div>}
            {lookup?.result && lookup.result.hits.length === 0 && (
              <div className="muted" data-testid="amap-lookup-note">
                {LOOKUP_REASON[lookup.result.reason ?? ""] ?? "高德里也没有找到。"}
              </div>
            )}
            {lookup?.result?.hits.map((h) => (
              <div className="pool-hit" key={h.id} data-testid="amap-hit">
                <span>
                  <b>{h.name}</b>
                  <span className="muted">
                    {" "}
                    · {h.address || h.area || "没有地址"}
                    {h.type && ` · ${h.type}`}
                  </span>
                </span>
                <button type="button" className="tool" onClick={() => addFromAmap(h)}>
                  加入并标为必去
                </button>
              </div>
            ))}
            {hits.map((h) => (
              <div className="pool-hit" key={h.place_id}>
                <span>
                  <b>{bilingual(h.name_zh, h.name_local)}</b>
                  <span className="muted"> · {h.area}</span>
                </span>
                <button type="button" className="tool" onClick={() => addFromSearch(h)}>
                  加入并标为必去
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {rows.length === 0 && <div className="muted">主题成员读取中…</div>}
      <div className="pool-rows">
        {rows.map(({ place, defaultWeight, role }) => {
          const pid = place.place_id as string;
          const pref = prefs[pid] ?? { state: "default", weight: null };
          return (
            <div className={`pool-row ${pref.state}`} key={pid} data-pid={pid}>
              <div className="pool-name">
                <button type="button" className="linklike" onClick={() => onOpenPlace(pid)}>
                  {place.name_zh}
                </button>
                <span className="muted">
                  {localLabel(place.name_zh, place.name_local) && ` · ${localLabel(place.name_zh, place.name_local)}`}
                  {" "}
                  · {place.area}
                  {role && ` · ${ROLE_ZH[role] ?? role}`}
                  {defaultWeight != null && ` · 默认权重 ${defaultWeight}`}
                  {defaultWeight == null && !noteSources[pid] && " · 你搜索加入的"}
                </span>
                {pid.startsWith("amap-") && (
                  <span className="tag" data-testid="unchecked-hours">
                    营业时间没核对，出发前请自己查
                  </span>
                )}
                {noteSources[pid] && (
                  <button
                    type="button"
                    className="note-badge"
                    data-testid="note-badge"
                    aria-expanded={openNote === pid}
                    onClick={() => setOpenNote(openNote === pid ? null : pid)}
                  >
                    📝 来自笔记
                  </button>
                )}
                {noteSources[pid] && openNote === pid && (
                  <div className="note-source" data-testid="note-source">
                    <TipChips tips={noteSources[pid].tips} />
                    <blockquote className="ni-snippet">{noteSources[pid].snippet || "（没有原文片段）"}</blockquote>
                  </div>
                )}
              </div>
              <div className="seg" role="group" aria-label={`${place.name_zh}的候选设置`}>
                {(["default", "must", "avoid"] as const).map((s) => (
                  <button
                    type="button"
                    key={s}
                    className={`segbtn${pref.state === s ? ` on on-${s}` : ""}`}
                    onClick={() => set(pid, { state: s })}
                  >
                    {s === "default" ? "默认" : s === "must" ? "必去" : "不去"}
                  </button>
                ))}
              </div>
              <select
                className="tool weightsel"
                aria-label={`${place.name_zh}的权重`}
                value={pref.weight ?? ""}
                disabled={pref.state !== "default"}
                onChange={(e) => set(pid, { weight: e.target.value ? Number(e.target.value) : null })}
              >
                <option value="">权重：默认</option>
                {[1, 2, 3, 4, 5].map((w) => (
                  <option key={w} value={w}>
                    权重 {w}
                  </option>
                ))}
              </select>
            </div>
          );
        })}
      </div>

      {notePending.length > 0 && (
        <div className="pool-pending" data-testid="pool-pending">
          <h4>
            待核实（不会排进行程）<span className="ni-unverified">事实未核对</span>
          </h4>
          <p className="muted">
            笔记里提到、但地点库里没有的地点：没有开放时间和闭馆日，没法可靠排进行程。生成行程后会列在行程页的「待核实」里。
          </p>
          {notePending.map((p) => (
            <div className="pool-pending-row" key={p.name} data-testid="pool-pending-row">
              <b>{p.name}</b>
              <a className="tag link" href={mapsSearchUrl(p.name)} target="_blank" rel="noreferrer">
                在地图上查 ↗
              </a>
              <button type="button" className="tool" onClick={() => onRemovePending?.(p.name)}>
                移除
              </button>
              {p.snippet && <blockquote className="ni-snippet">{p.snippet}</blockquote>}
            </div>
          ))}
        </div>
      )}
    </details>
    {lookupError && <ErrorPopup message={lookupError} onClose={() => setLookupError(null)} />}
    {/* 弹窗放在 <details> 外面：面板没展开时（首页提示卡的按钮）它也要能显示 */}
    {importing && <NoteImport onAccept={applyImport} onClose={() => setImporting(false)} />}
    </>
  );
}
