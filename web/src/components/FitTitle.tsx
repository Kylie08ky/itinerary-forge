import { useLayoutEffect, useRef, type ElementType } from "react";

/** 封面大标题：一行放得下就用样式表里的字号，放不下就等比缩小到刚好一行（不再折成「重 / 庆市」那样的两行）。 */
export function FitTitle({
  as: Tag = "h1",
  text,
  minPx = 28,
  className,
}: {
  as?: ElementType;
  text: string;
  minPx?: number;
  className?: string;
}) {
  const ref = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => {
      el.style.fontSize = "";
      el.style.whiteSpace = "nowrap";
      const room = el.clientWidth;
      const need = el.scrollWidth;
      if (room > 0 && need > room) {
        const base = parseFloat(getComputedStyle(el).fontSize);
        const size = Math.floor((base * room) / need) - 1;
        el.style.fontSize = `${Math.max(minPx, size)}px`;
        // 缩到下限还放不下（城市很多）：这时才允许换行
        el.style.whiteSpace = size < minPx ? "normal" : "nowrap";
      }
    };
    fit();
    const ro = new ResizeObserver(fit);
    if (el.parentElement) ro.observe(el.parentElement);
    void document.fonts?.ready.then(fit);
    return () => ro.disconnect();
  }, [text, minPx]);
  return (
    <Tag ref={ref} className={className} data-fit-title>
      {text}
    </Tag>
  );
}
