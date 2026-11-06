import { useCallback, useEffect, useRef, useState } from "react";

export interface NavItem {
  id: string; // 页面上对应栏目的元素 id；分组标题不对应栏目，id 只当作键
  label: string;
  onJump?: () => void; // 跳之前先做点什么（比如把收起的那一天展开）
  group?: string; // 分组标题（多城市行程里的「巴黎 第 1–4 天」）：点它跳到 target，不参与「当前在哪一栏」的高亮
  target?: string; // 分组标题要跳去的元素 id
}

/** 滚动时，栏目标题顶到这条线（距视口顶部，px）以上就算「当前在这一栏」。 */
const SPY_LINE = 175;

/**
 * 行程页的「本页目录」（规格 06-ui-design）：吸附在导航条下面，点一项滚到那一栏；滚动时高亮当前栏目。
 * 只列页面上真有（有高度）的栏目：预算 / 随行这些异步出现的卡片没出来之前不会有死链接。
 */
export function SectionNav({ items, variant = "bar" }: { items: NavItem[]; variant?: "bar" | "rail" }) {
  const [present, setPresent] = useState<Set<string>>(new Set());
  const [active, setActive] = useState<string>(items[0]?.id ?? "");
  const bar = useRef<HTMLElement>(null);
  const idsKey = items.filter((i) => !i.group).map((i) => i.id).join("|");

  const refresh = useCallback(() => {
    const ids = idsKey ? idsKey.split("|") : [];
    const here = new Set(
      ids.filter((id) => {
        const el = document.getElementById(id);
        return !!el && el.offsetHeight > 0;
      }),
    );
    setPresent((cur) => (cur.size === here.size && [...here].every((x) => cur.has(x)) ? cur : here));
    let cur = ids.find((id) => here.has(id)) ?? "";
    for (const id of ids) {
      if (!here.has(id)) continue;
      if (document.getElementById(id)!.getBoundingClientRect().top <= SPY_LINE) cur = id;
    }
    // 滚到页面底了：最后一栏标题可能永远顶不到那条线，直接算最后一栏
    if (window.scrollY > 0 && window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
      cur = [...ids].reverse().find((id) => here.has(id)) ?? cur;
    }
    setActive(cur);
  }, [idsKey]);

  useEffect(() => {
    let raf = 0;
    const tick = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(refresh);
    };
    tick();
    window.addEventListener("scroll", tick, { passive: true });
    window.addEventListener("resize", tick);
    const mo = new MutationObserver(tick); // 卡片异步出现 / 一天收起展开，高度变了就重新算
    mo.observe(document.body, { childList: true, subtree: true });
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", tick);
      window.removeEventListener("resize", tick);
      mo.disconnect();
    };
  }, [refresh]);

  // 当前项自动滚进目录条的可见范围（条目多、横向滚动时）
  useEffect(() => {
    const btn = bar.current?.querySelector<HTMLElement>(`[data-nav="${active}"]`);
    const scroller = bar.current?.querySelector<HTMLElement>(".section-nav-list");
    if (btn && scroller && variant === "rail") {
      // 竖栏：当前项滚进可见范围（一周的行程条目多）
      if (btn.offsetTop < scroller.scrollTop || btn.offsetTop + btn.offsetHeight > scroller.scrollTop + scroller.clientHeight) {
        scroller.scrollTo({ top: Math.max(0, btn.offsetTop - scroller.clientHeight / 2), behavior: "smooth" });
      }
    } else if (btn && scroller) {
      const left = btn.offsetLeft - scroller.clientWidth / 2 + btn.clientWidth / 2;
      scroller.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
    }
  }, [active, variant]);

  function jump(item: NavItem) {
    const id = item.target ?? item.id;
    item.onJump?.();
    // 展开一天需要等 React 渲染完再滚，不然目标位置会跳
    setTimeout(() => {
      document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
      settleOn(id);
    }, item.onJump ? 80 : 0);
  }

  /**
   * 平滑滚动的途中，上面各天的地图 / 图片才加载出来，会把目标顶下去（靠后的栏目尤其明显）：
   * 滚动停下后如果目标没在该在的位置，就再对一次（瞬间，不再平滑）。
   */
  function settleOn(id: string) {
    let last = -1;
    let ticks = 0;
    const timer = window.setInterval(() => {
      const el = document.getElementById(id);
      ticks += 1;
      if (!el || ticks > 24) return window.clearInterval(timer);
      const moving = Math.abs(window.scrollY - last) > 1;
      last = window.scrollY;
      if (moving) return;
      const rest =
        (parseFloat(getComputedStyle(el).scrollMarginTop) || 0) +
        (parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0);
      const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
      const atBottom = window.scrollY >= maxScroll - 2;
      if (Math.abs(el.getBoundingClientRect().top - rest) > 8 && !atBottom) el.scrollIntoView({ behavior: "auto", block: "start" });
      else window.clearInterval(timer);
    }, 120);
  }

  const shown = items.filter((i) => present.has(i.target ?? i.id) || (i.group && present.has(i.target ?? "")));
  if (shown.length < 2) return null;
  return (
    <nav className={`section-nav${variant === "rail" ? " rail" : ""}`} aria-label="本页目录" ref={bar} data-testid="section-nav">
      <div className="section-nav-list">
        {shown.map((i) => (
          <button
            key={i.id}
            type="button"
            data-nav={i.id}
            data-testid={i.group ? "nav-city" : undefined}
            className={i.group ? "nav-city" : i.id === active ? "active" : undefined}
            aria-current={!i.group && i.id === active ? "true" : undefined}
            onClick={() => jump(i)}
          >
            {i.label}
          </button>
        ))}
      </div>
    </nav>
  );
}
