/** 景点名的显示规则（规格 07 P18）：中文名 · 当地语言名；当地名和中文名没区别或没有时只显示一个。 */

function foldName(s: string): string {
  return s
    .replace(/œ/gi, "oe")
    .replace(/æ/gi, "ae")
    .replace(/ß/g, "ss")
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]/gu, "");
}

/** 要在中文名旁边显示的当地名；没有或和中文名没区别 → null。 */
export function localLabel(nameZh: string, local?: string | null): string | null {
  const l = (local ?? "").trim();
  if (!l || foldName(l) === foldName(nameZh)) return null;
  return l;
}

/** 「卢浮宫 · Musée du Louvre」；没有当地名就只有中文名。 */
export function bilingual(nameZh: string, local?: string | null): string {
  const l = localLabel(nameZh, local);
  return l ? `${nameZh} · ${l}` : nameZh;
}
