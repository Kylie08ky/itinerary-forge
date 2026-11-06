import { Fragment, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { ErrorPopup } from "../confirm";
import { localLabel } from "../names";
import type { NoteSource, PlaceDetail } from "../types";
import { storyLabel } from "./Stories";
import { Paperclip } from "./FilesTab";
import { TipChips } from "./NoteImport";
import { PlaceIntro, PlacePhoto } from "./PlaceMedia";

const CATEGORY_ZH: Record<string, string> = {
  museum: "博物馆",
  sight: "景点",
  park: "公园",
  shopping: "购物",
  food: "餐饮",
  entertainment: "演出娱乐",
  activity: "活动",
};
const BEST_TIME_ZH: Record<string, string> = {
  morning: "上午最佳",
  afternoon: "下午最佳",
  evening: "晚上最佳",
  golden_hour: "黄昏最佳",
  any: "全天都行",
};
const WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

/** 浏览量：不到 1 万写整数，1 万以上写「1.4 万」。 */
function viewsText(n: number): string {
  return n < 10000 ? String(n) : `${(n / 10000).toFixed(1).replace(/\.0$/, "")}万`;
}

function fmtMinutes(min: number): string {
  if (min < 60) return `${min} 分钟`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${h} 小时${m ? ` ${m} 分钟` : ""}`;
}

/** 一项事实的核对状态：绿色勾 + 日期，或灰色「待核对」（规格 M02 S4）。 */
function Verified({ ok, at, label }: { ok: boolean; at: string | null; label: string }) {
  return ok && at ? (
    <span className="vstat ok">✓ {label}已于 {at} 自动核对</span>
  ) : (
    <span className="vstat todo">○ {label}待核对（来自策展录入）</span>
  );
}

/** 地点详情卡：地点库里的信息 + 照片和简介（带署名 / 来源），每项事实都标核对状态。 */
export function PlaceCard({
  placeId,
  city,
  note,
  files = 0,
  onOpenFiles,
  onClose,
}: {
  placeId: string;
  city?: string; // 地点库在哪座城市；不传就是默认城市（周边一日游的目的地要传）
  note?: NoteSource | null; // 这个地点是从哪段笔记来的（规格 M12 S3）
  files?: number; // 挂在这个地点上的文件数（规格 M10 S5）；只在行程页里传
  onOpenFiles?: () => void;
  onClose: () => void;
}) {
  const [p, setP] = useState<PlaceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setP(null);
    setError(null);
    api
      .placeDetail(placeId, city)
      .then((d) => live && setP(d))
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [placeId, city]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const monthDay = (k: string) => `${Number(k.slice(0, 2))} 月 ${Number(k.slice(3))} 日`;
  const closed = p
    ? p.long_term_closed
      ? "目前长期闭馆（整修）"
      : [
          p.closed_weekdays.length ? `每${p.closed_weekdays.map((d) => WEEKDAY_ZH[d]).join("、")}闭馆` : "",
          p.closed_dates?.length ? `${p.closed_dates.map(monthDay).join("、")}闭馆` : "",
        ]
          .filter(Boolean)
          .join("；") || (p.verification.hours_verified ? "没有固定的每周闭馆日" : "出发前请到官网确认")
    : "";
  const dateNotes = p ? Object.entries(p.date_notes ?? {}).sort() : [];

  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <div className="modal place-card" role="dialog" aria-modal="true" aria-label="地点详情" onClick={(e) => e.stopPropagation()}>
        <button className="modal-close" onClick={onClose} aria-label="关闭">
          ✕
        </button>
        <ErrorPopup message={error} onClose={() => setError(null)} />
        {!p && !error && <div className="muted">读取地点信息…</div>}
        {p && (
          <>
            {p.media?.image && <PlacePhoto image={p.media.image} name={p.name_zh} />}
            <header className="pc-head">
              <h2>
                {p.name_zh}
                {localLabel(p.name_zh, p.name_local) && (
                  <span className="local-name" data-testid="place-local-name"> · {localLabel(p.name_zh, p.name_local)}</span>
                )}
              </h2>
              {onOpenFiles && <Paperclip count={files} onClick={onOpenFiles} label={`${p.name_zh}的文件`} />}
            </header>
            <div className="pc-sub">
              {p.name_local || p.local_language === "zh" ? "" : `${p.name_en} · `}
              {CATEGORY_ZH[p.category] ?? p.category} · {p.area}
              {p.name_local_check?.status === "draft" && (
                <span className="tag" title="当地名只有一个来源或两个来源对不上，出发前请再确认"> · 名称待核对</span>
              )}
            </div>
            <div className="pc-badges">
              {p.fame && (
                <span
                  className="tag"
                  data-testid="place-fame"
                  title="名气 = 维基百科里这个地点有多少种语言版本；它只代表知名度，不代表一定适合你"
                >
                  名气：维基百科 {p.fame.sitelinks} 种语言版本{p.fame.rank ? ` · 全城第 ${p.fame.rank}` : ""}
                </span>
              )}
              {p.fame?.views !== undefined && (
                <span className="tag" data-testid="place-views" title="近 12 个月中文维基百科这个条目的浏览量（只算真人）">
                  {`近一年约 ${viewsText(p.fame.views)}次浏览`}
                </span>
              )}
              {p.voyage && (
                <a className="tag" data-testid="place-voyage" href={p.voyage.url} target="_blank" rel="noreferrer"
                   title="维基导游（Wikivoyage，CC BY-SA 4.0）的城市页里列了这个地点">
                  维基导游推荐 ↗
                </a>
              )}
              {p.data_status === "draft" && (
                <span className="tag" data-testid="place-draft" title="来自开放数据（OpenStreetMap / 维基数据），还没有逐项核对">
                  开放数据草稿
                </span>
              )}
              {p.season && (
                <span className="tag walkin" data-testid="place-season">
                  当季活动 {p.season.start} 至 {p.season.end}
                  {p.season.confirmed_by === "official" ? "" : "（日期没能在官网核对，出发前请确认）"}
                </span>
              )}
              {p.ticketing === "online_required" && <span className="tag book">必须提前线上购票</span>}
              {p.ticketing === "online_or_walkin" && <span className="tag walkin">建议线上购票 · 现场也可排队</span>}
              {p.ticketing === "none" && <span className="tag">无需购票 / 免费</span>}
              {p.official_url && (
                <a className="tag link" href={p.official_url} target="_blank" rel="noreferrer">
                  官网购票 ↗
                </a>
              )}
            </div>
            {p.long_term_closed && <div className="banner warn">⚠️ 该馆目前长期闭馆（整修），出发前务必查官网确认。</div>}
            {p.media?.intro && <PlaceIntro intro={p.media.intro} />}

            {note && (
              <section className="note-source" data-testid="card-note-source">
                <h4>📝 来自你导入的笔记</h4>
                <TipChips tips={note.tips} />
                <blockquote className="ni-snippet">{note.snippet || "（没有原文片段）"}</blockquote>
                <div className="muted">提示由规则从笔记里提取，只是参考；开放时间、闭馆日、票价以下面地点库里核对过的为准。</div>
              </section>
            )}

            <section className="pc-section">
              <h3>基本信息</h3>
              <dl className="facts">
                <dt>地址</dt>
                <dd>{p.address ?? "—"}</dd>
                {p.metro && (
                  <>
                    <dt>交通</dt>
                    <dd>{p.metro}</dd>
                  </>
                )}
                <dt>建议停留</dt>
                <dd>
                  {fmtMinutes(p.duration_min)} · {BEST_TIME_ZH[p.best_time] ?? p.best_time}
                </dd>
                {p.price_eur != null && (
                  <>
                    <dt>票价</dt>
                    <dd>约 €{p.price_eur}</dd>
                  </>
                )}
                <dt>闭馆日</dt>
                <dd>
                  {closed}
                  {!p.verification.hours_verified && (p.closed_weekdays.length > 0 || !!p.closed_dates?.length) && "（出发前请到官网确认）"}
                </dd>
                {dateNotes.map(([k, v]) => (
                  <Fragment key={k}>
                    <dt>{monthDay(k)}</dt>
                    <dd>{v}</dd>
                  </Fragment>
                ))}
              </dl>
            </section>

            {p.ticket_note && (
              <section className="pc-section">
                <h3>购票说明</h3>
                <p className="ticket-note">{p.ticket_note}</p>
              </section>
            )}

            {p.highlights.length > 0 && (
              <section className="pc-section highlights">
                <h3>参观重点</h3>
                <ul className="pc-list">
                  {p.highlights.map((h) => (
                    <li key={h}>{h}</li>
                  ))}
                </ul>
              </section>
            )}

            {!!p.stories?.length && (
              <section className="pc-section" data-testid="card-stories">
                <h3>这里的故事</h3>
                <ul className="pc-list">
                  {p.stories.map((s) => (
                    <li key={s.work_id}>
                      <b>{storyLabel(s)}</b>
                      {s.title_zh && <span className="muted"> {s.title}</span>}
                      {s.creator && (
                        <span className="muted">
                          {" "}
                          · {s.creator}，{s.year}
                        </span>
                      )}
                      ：{s.note}{" "}
                      <a href={s.source.url} target="_blank" rel="noreferrer" className="muted">
                        来源 ↗
                      </a>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {p.note && (
              <section className="pc-section">
                <h3>小贴士</h3>
                <p className="pc-tip">{p.note}</p>
              </section>
            )}

            {p.themes.length > 0 && (
              <section className="pc-section place-themes">
                <h3>属于主题</h3>
                <div className="pc-chips">
                  {p.themes.map((t) => (
                    <span className="chip" key={t.id} title={`权重 ${t.weight}`}>
                      {t.name_zh} · {t.role === "anchor" ? "主要点" : "辅助点"}
                    </span>
                  ))}
                </div>
              </section>
            )}

            <details className="verification">
              <summary>数据核对记录</summary>
              <div className="verification-list">
                <Verified ok={p.verification.coords_verified} at={p.verification.verified_at} label="坐标" />
                <Verified ok={p.verification.hours_verified} at={p.verification.verified_at} label="闭馆日" />
                {p.official_url && p.verification.url_status === "ok" && p.verification.verified_at && (
                  <span className="vstat ok">✓ 官网链接已于 {p.verification.verified_at} 自动核对</span>
                )}
                {p.official_url && p.verification.url_status === "bot_protected" && (
                  <span className="vstat todo">○ 官网有访问限制，程序没能自动打开核对，出发前请自己打开确认</span>
                )}
                {p.verification.ticketing_checked_at && (
                  <span className="vstat ok">✓ 购票方式已于 {p.verification.ticketing_checked_at} 查证（资料多为第三方，以官网为准）</span>
                )}
              </div>
            </details>
          </>
        )}
      </div>
    </div>
  );
}
