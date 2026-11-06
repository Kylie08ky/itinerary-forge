import { useEffect, useState } from "react";

/** 媒体查询的 React 版：窗口宽度变化时自动更新（桌面端并排地图 / 手机折叠地图靠它切换）。 */
export function useMedia(query: string): boolean {
  const get = () => (typeof window !== "undefined" ? window.matchMedia(query).matches : false);
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setMatches(m.matches);
    on();
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [query]);
  return matches;
}
