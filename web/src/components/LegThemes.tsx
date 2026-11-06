import { useEffect, useState } from "react";
import { api } from "../api";
import { IconCheck, IconSpark } from "../icons";
import { coverStyle, themeLook } from "../themes";
import type { CityInfo, ThemeInfo } from "../types";
import type { LegChoices } from "./LegOptions";

/** 多城市里后续城市的主题：和第一座城市同样的卡片，单选（不选 = 均衡）。选了深度路线时主题由路线决定。 */
export function LegThemes({
  index,
  name,
  city,
  value,
  onChange,
}: {
  index: number;
  name: string;
  city: CityInfo | null;
  value: LegChoices;
  onChange: (p: Partial<LegChoices>) => void;
}) {
  const [themes, setThemes] = useState<ThemeInfo[]>([]);
  useEffect(() => {
    if (!city) {
      setThemes([]);
      return;
    }
    let alive = true;
    api.themes(city.id).then((t) => alive && setThemes(t)).catch(() => alive && setThemes([]));
    return () => {
      alive = false;
    };
  }, [city?.id]);

  const byRoute = !!value.routeId;
  const balanced = !byRoute && !value.theme;
  const pick = (t: ThemeInfo | null) =>
    onChange({ theme: t?.id ?? "", themeName: t?.name_zh ?? "", routeId: null, routeStops: null });
  return (
    <div className="leg-themes" data-testid={`leg-themes-${index}`}>
      <h3>{name}</h3>
      {!city ? (
        <p className="muted">这座城市还没有数据，先在上面填好目的地。</p>
      ) : (
        <div className="theme-picker">
          {themes.length > 0 && (
            <button
              type="button"
              className={`theme-btn balanced${balanced ? " active" : ""}`}
              aria-pressed={balanced}
              data-testid={`leg-theme-balanced-${index}`}
              onClick={() => pick(null)}
            >
              <span className="theme-icon" aria-hidden>
                <IconSpark size={18} />
              </span>
              <span className="theme-text">
                <span className="theme-label">均衡 · 让我来推荐</span>
                <span className="theme-hint">没有特定倾向，各类主题均匀搭配</span>
              </span>
              {balanced && (
                <span className="theme-check" aria-hidden>
                  <IconCheck size={14} />
                </span>
              )}
            </button>
          )}
          {themes.map((t) => {
            const sel = !byRoute && value.theme === t.id;
            return (
              <button
                type="button"
                key={t.id}
                className={`theme-btn${sel ? " active" : ""}`}
                aria-pressed={sel}
                data-testid={`leg-theme-${index}-${t.id}`}
                onClick={() => pick(t)}
              >
                <span className="theme-icon" style={coverStyle(t.id)} aria-hidden>
                  {themeLook(t.id).icon}
                </span>
                <span className="theme-text">
                  <span className="theme-label">{t.name_zh}</span>
                  <span className="theme-hint">{t.tagline}</span>
                </span>
                {sel && (
                  <span className="theme-check" aria-hidden>
                    <IconCheck size={14} />
                  </span>
                )}
              </button>
            );
          })}
          {themes.length === 0 && <span className="muted">主题加载中…</span>}
        </div>
      )}
      {byRoute && <p className="muted">已选深度路线「{value.routeName}」，主题由路线决定。</p>}
    </div>
  );
}
