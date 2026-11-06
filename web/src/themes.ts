/** 主题封面配置（规格 06-ui-design）：没有景点照片，封面 =「城市主色渐变 + 竖向接缝纹理 + 淡淡的大图标」；各主题在城市色上偏一点色相。 */
export interface ThemeLook {
  icon: string;
  tint: string; // 渐变终点的色相偏移
}

export const THEME_LOOK: Record<string, ThemeLook> = {
  art: { icon: "🎨", tint: "#4a3a72" },
  fashion: { icon: "👗", tint: "#7a3445" },
  shopping: { icon: "🛍️", tint: "#7a5b24" },
  museums: { icon: "🏛️", tint: "#2b4378" },
  entertainment: { icon: "🎭", tint: "#6d2c48" },
  family: { icon: "👨‍👩‍👧", tint: "#1f6a50" },
  photography: { icon: "📷", tint: "#1f6272" },
  food: { icon: "🥐", tint: "#7a4a22" },
};

const FALLBACK: ThemeLook = { icon: "🧭", tint: "#2d5a6e" };

export function themeLook(id: string | null | undefined): ThemeLook {
  return (id && THEME_LOOK[id]) || FALLBACK;
}

/** 目的地 → 城市配色键（cities.css 里的 data-city）；没有专属配色的城市返回 null，用中性色。 */
const CITY_KEYS: Record<string, string> = { paris: "paris", 巴黎: "paris" };

export function cityKey(destination: string | null | undefined): string | null {
  if (!destination) return null;
  return CITY_KEYS[destination.trim().toLowerCase()] ?? null;
}

export const DEFAULT_CITY = "paris"; // 首页 / 列表页还没选目的地时用（目前只支持巴黎）

export function applyCity(destination: string | null | undefined): void {
  const key = cityKey(destination);
  if (key) document.documentElement.dataset.city = key;
  else delete document.documentElement.dataset.city;
}

/** 封面背景：城市主色渐变 + 锌皮屋顶式的竖向接缝纹理 + 一处柔光；主题只在城市色上偏一点色相。颜色全走 CSS 变量，换城市自动跟着变。 */
export function coverStyle(id: string | null | undefined): React.CSSProperties {
  const t = themeLook(id);
  return {
    backgroundImage: [
      "repeating-linear-gradient(90deg, var(--seam) 0 1px, transparent 1px 26px)",
      "radial-gradient(60% 70% at 85% 0%, rgba(255,255,255,0.12), transparent 62%)",
      `linear-gradient(160deg, var(--cover-a) 0%, color-mix(in srgb, ${t.tint} 34%, var(--cover-b)) 100%)`,
    ].join(", "),
  };
}
