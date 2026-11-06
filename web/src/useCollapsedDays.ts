import { useCallback, useState } from "react";

const PREFIX = "atp:collapsed-days:";

function load(key: string): Set<string> {
  try {
    const raw = window.localStorage.getItem(PREFIX + key);
    const arr: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set(); // 无痕窗口 / 浏览器禁止存储 / 内容坏了：退回「全部展开」
  }
}

function save(key: string, dates: Set<string>): void {
  try {
    if (dates.size === 0) window.localStorage.removeItem(PREFIX + key);
    else window.localStorage.setItem(PREFIX + key, JSON.stringify([...dates]));
  } catch {
    /* 存不了就算了：只是下次打开不记得 */
  }
}

/**
 * 每天「收起 / 展开」的状态（规格 06-ui-design S9）：记在这台浏览器里，每份行程 / 每个分享链接各记各的。
 * key 是行程号或 "s:" + 分享 token。
 */
export function useCollapsedDays(key: string) {
  const [loadedFor, setLoadedFor] = useState(key);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => load(key));

  // 换了一份行程（路由参数变了，组件没卸载）：重新读那一份的记录
  if (loadedFor !== key) {
    setLoadedFor(key);
    setCollapsed(load(key));
  }

  const update = useCallback(
    (fn: (cur: Set<string>) => Set<string>) => {
      setCollapsed((cur) => {
        const next = fn(cur);
        save(key, next);
        return next;
      });
    },
    [key],
  );

  const toggle = useCallback(
    (date: string) =>
      update((cur) => {
        const next = new Set(cur);
        if (!next.delete(date)) next.add(date);
        return next;
      }),
    [update],
  );
  /** 只展开某一天（目录条点到收起的那一天时用）。 */
  const expand = useCallback(
    (date: string) =>
      update((cur) => {
        if (!cur.has(date)) return cur;
        const next = new Set(cur);
        next.delete(date);
        return next;
      }),
    [update],
  );
  const expandAll = useCallback(() => update(() => new Set()), [update]);
  const collapseAll = useCallback((dates: string[]) => update(() => new Set(dates)), [update]);

  return { collapsed, toggle, expand, expandAll, collapseAll };
}
