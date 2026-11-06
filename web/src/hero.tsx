import { useEffect, useState } from "react";
import { mediaSrc } from "./api";
import type { AttractionRef, Itinerary, PlaceImageInfo } from "./types";

/** 首页背景按这个顺序取第一个有大图的标志性地点（规格 M02 S7c）。 */
const HOME_ORDER = ["eiffel-tower", "louvre", "notre-dame", "pont-alexandre-iii", "orsay", "montmartre-sacre-coeur", "versailles"];

export interface HeroPick {
  placeId: string;
  image: PlaceImageInfo;
}

function heroOf(p: AttractionRef | undefined): PlaceImageInfo | null {
  return p?.media?.hero ?? null;
}

export function pickHomeHero(places: AttractionRef[]): HeroPick | null {
  const byId = new Map(places.map((p) => [p.place_id as string, p]));
  for (const id of HOME_ORDER) {
    const h = heroOf(byId.get(id));
    if (h) return { placeId: id, image: h };
  }
  for (const p of places) {
    const h = heroOf(p);
    if (h && p.place_id) return { placeId: p.place_id, image: h };
  }
  return null;
}

/** 行程页封面：这趟行程里主要点优先、有大图的第一个（规格 06 S15）；一个都没有就是 null（退回城市主色渐变）。 */
export function pickTripHero(it: Itinerary, places: AttractionRef[]): HeroPick | null {
  const byId = new Map(places.map((p) => [p.place_id as string, p]));
  const ids = it.days.flatMap((d) => d.items.filter((i) => i.type !== "meal" && i.place_id).map((i) => i.place_id as string));
  const unique = [...new Set(ids)];
  // 标志性的（在首页那份清单里的）排前面，其余按在行程里出现的顺序
  const ranked = [...unique.filter((id) => HOME_ORDER.includes(id)).sort((a, b) => HOME_ORDER.indexOf(a) - HOME_ORDER.indexOf(b)),
    ...unique.filter((id) => !HOME_ORDER.includes(id))];
  for (const id of ranked) {
    const h = heroOf(byId.get(id));
    if (h) return { placeId: id, image: h };
  }
  return null;
}

/** 多城市行程：每座城市各挑一张封面大图（该城市自己的点里主要点优先、有大图的第一个）；没有大图的城市不出现。 */
export function pickCityHeroes(it: Itinerary, places: AttractionRef[], cityIds: string[]): HeroPick[] {
  const out: HeroPick[] = [];
  for (const cid of cityIds) {
    const mine: Itinerary = {
      ...it,
      days: it.days.map((d) => ({ ...d, items: d.items.filter((i) => (i.city_id ?? d.city_id) === cid) })),
    };
    const pick = pickTripHero(mine, places);
    if (pick && !out.some((o) => o.image.url === pick.image.url)) out.push(pick);
  }
  return out;
}

export type HeroStatus = "none" | "loading" | "ready" | "failed";

/**
 * 预加载大图：加载成功才用，失败（文件丢了 / 断网）就当没有，整块退回渐变（规格 06 S11 / S17）。
 * 「加载中」单独算一种状态：封面舞台在这期间就按故事版排好，图片到了再换上，不会先矮后高地跳一下。
 */
export function useHeroStatus(image: PlaceImageInfo | null | undefined): HeroStatus {
  const url = image ? mediaSrc(image.url) : null;
  const [status, setStatus] = useState<HeroStatus>(url ? "loading" : "none");
  useEffect(() => {
    if (!url) {
      setStatus("none");
      return;
    }
    setStatus("loading");
    let live = true;
    const img = new Image();
    img.onload = () => live && setStatus("ready");
    img.onerror = () => live && setStatus("failed");
    img.src = url;
    return () => {
      live = false;
    };
  }, [url]);
  return status;
}

export function useHeroReady(image: PlaceImageInfo | null | undefined): boolean {
  return useHeroStatus(image) === "ready";
}

/** 署名（规格 06 S19）：摄影：作者 · 授权（带链接）· 来源 ↗，永远和大图一起显示。 */
export function HeroCredit({ image, className = "hero-credit" }: { image: PlaceImageInfo; className?: string }) {
  return (
    <p className={className} data-testid="hero-credit">
      摄影：{image.author} ·{" "}
      {image.license_url ? (
        <a href={image.license_url} target="_blank" rel="noreferrer">
          {image.license}
        </a>
      ) : (
        image.license
      )}{" "}
      ·{" "}
      <a href={image.source_url} target="_blank" rel="noreferrer">
        来源 ↗
      </a>
    </p>
  );
}
