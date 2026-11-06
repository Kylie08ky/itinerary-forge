import type { WeatherDay } from "../types";

export const WEATHER_EMOJI: Record<string, string> = {
  sun: "☀️",
  partly: "⛅",
  cloud: "☁️",
  fog: "🌫️",
  rain: "🌧️",
  snow: "❄️",
  storm: "⛈️",
};

function temp(n: number | null): string {
  return n == null ? "–" : `${Math.round(n)}°`;
}

/**
 * 每天标题行右侧的天气（规格 M10 S1a / S1b）。
 * 15 天内是预报（带降雨概率，≥ 60% 高亮成雨天）；更远的是「去年同期」的实际天气，只作参考。
 */
export function WeatherChip({ day }: { day: WeatherDay }) {
  return (
    <span
      className={`day-weather${day.rainy ? " rainy" : ""}${day.source === "history" ? " history" : ""}`}
      data-testid="day-weather"
    >
      <span className="w-icon" aria-hidden>
        {WEATHER_EMOJI[day.icon] ?? "☁️"}
      </span>
      <b>{day.label}</b>
      <span className="w-temp">
        {temp(day.t_max)} / {temp(day.t_min)}
      </span>
      {day.source === "history" ? (
        <span className="tag history-tag" title="离出发还远，显示的是去年同一天的实际天气，仅作参考">
          去年同期
        </span>
      ) : day.rain_prob != null ? (
        <span className={day.rainy ? "tag rain" : "muted"}>降雨 {day.rain_prob}%</span>
      ) : day.rain_mm != null ? (
        <span className="muted">降水 {day.rain_mm} mm</span>
      ) : null}
    </span>
  );
}
