import { useEffect, useState } from "react";
import { api } from "../api";
import type { AttractionRef, CityInfo, RouteCard, ThemeInfo } from "../types";
import { CandidatePool, type PoolPrefs } from "./CandidatePool";
import { PlaceCard } from "./PlaceCard";
import { RoutePicker } from "./RoutePicker";
import { StayDraftEditor, type StayDraft } from "./StayDraftEditor";

/** 多城市里一座后续城市自己的选项（规格 11）：主题（默认均衡）、深度路线、必去 / 不去、住宿。 */
export interface LegChoices {
  theme: string; // 空 = 均衡
  themeName: string;
  routeId: string | null;
  routeStops: string[] | null;
  routeTheme: string; // 选了路线：路线对应的主题
  routeName: string;
  prefs: PoolPrefs;
  stays: StayDraft[];
}

export const blankChoices = (): LegChoices => ({
  theme: "",
  themeName: "",
  routeId: null,
  routeStops: null,
  routeTheme: "",
  routeName: "",
  prefs: {},
  stays: [],
});

function dayCount(a: string, b: string): number {
  return a.length === 10 && b.length === 10 ? Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000) + 1 : 0;
}

export function LegOptions({
  city,
  start,
  end,
  value,
  onChange,
  onEnd,
}: {
  city: CityInfo | null;
  start: string;
  end: string;
  value: LegChoices;
  onChange: (p: Partial<LegChoices>) => void;
  onEnd: (end: string) => void; // 路线建议的天数：把离开日期改成这么多天之后
}) {
  const [themes, setThemes] = useState<ThemeInfo[]>([]);
  const [routes, setRoutes] = useState<RouteCard[]>([]);
  const [places, setPlaces] = useState<AttractionRef[]>([]);
  const [cardId, setCardId] = useState<string | null>(null);
  useEffect(() => {
    if (!city) {
      setThemes([]);
      setRoutes([]);
      setPlaces([]);
      return;
    }
    let alive = true;
    api.themes(city.id).then((t) => alive && setThemes(t)).catch(() => alive && setThemes([]));
    api.deepRoutes(city.id).then((r) => alive && setRoutes(r)).catch(() => alive && setRoutes([]));
    api.places(city.id).then((p) => alive && setPlaces(p)).catch(() => alive && setPlaces([]));
    return () => {
      alive = false;
    };
  }, [city?.id]);

  const route = routes.find((r) => r.id === value.routeId) ?? null;
  const themeIds = value.theme ? [value.theme] : themes.map((t) => t.id);
  const days = dayCount(start, end);
  return (
    <div className="leg-options">
      {city && (
        <details className="leg-more">
          <summary>路线、必去与住宿</summary>
          <RoutePicker
            routes={routes}
            city={city.id}
            selected={value.routeId}
            days={days}
            startDate={start}
            endDate={end}
            stops={value.routeStops}
            onStops={(ids) => onChange({ routeStops: ids })}
            onSelect={(id) => {
              const r = routes.find((x) => x.id === id);
              onChange(
                id && r
                  ? { routeId: id, routeStops: null, routeTheme: r.base_theme, routeName: r.name_zh, theme: "", themeName: "" }
                  : { routeId: null, routeStops: null },
              );
            }}
            onSuggestDays={(n) => {
              const d = new Date(`${start}T00:00:00Z`);
              d.setUTCDate(d.getUTCDate() + n - 1);
              onEnd(d.toISOString().slice(0, 10));
            }}
          />
          {!route && themeIds.length > 0 && (
            <CandidatePool
              city={city.id}
              themeIds={themeIds}
              places={places}
              prefs={value.prefs}
              onChange={(prefs) => onChange({ prefs })}
              onOpenPlace={setCardId}
            />
          )}
          <div className="party-block">
            <h3>住宿</h3>
            <StayDraftEditor value={value.stays} onChange={(stays) => onChange({ stays })} start={start} end={end} />
          </div>
        </details>
      )}
      {cardId && city && <PlaceCard placeId={cardId} city={city.id} onClose={() => setCardId(null)} />}
    </div>
  );
}
