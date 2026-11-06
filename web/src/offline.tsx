import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { ConfirmProvider } from "./confirm";
import { IconChart, IconHome, IconList, IconPin, IconPlus, IconSun } from "./icons";
import { applyCity, DEFAULT_CITY } from "./themes";
import { useActiveTrip } from "./today";
import { useUnfinishedTasks } from "./useUnfinishedTasks";

/** api.ts 在「响应来自缓存」或「请求根本发不出去」时发 CACHE_HIT；拿到新鲜响应时发 FRESH。 */
export const CACHE_HIT = "atp:cache-hit";
export const FRESH = "atp:fresh";

/**
 * 只读状态（规格 M15 S4）：
 * - offline：浏览器自己说没网（飞行模式）；
 * - stale：请求发不出去 / 后端连不上，Service Worker 拿缓存顶上了（或干脆没有缓存）。
 *   不能只信 navigator.onLine：弱网、后端挂了、某些模拟离线的环境里它仍是 true。
 * 两种情况下页面都只能看，不能改、不能生成、不能分享。
 */
export function useReadOnly(): { readOnly: boolean; offline: boolean; stale: boolean } {
  const [offline, setOffline] = useState(() => typeof navigator !== "undefined" && !navigator.onLine);
  const [stale, setStale] = useState(false);

  useEffect(() => {
    const on = () => {
      setOffline(false);
      setStale(false);
    };
    const off = () => setOffline(true);
    const hit = () => setStale(true);
    const fresh = () => setStale(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    window.addEventListener(CACHE_HIT, hit);
    window.addEventListener(FRESH, fresh);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
      window.removeEventListener(CACHE_HIT, hit);
      window.removeEventListener(FRESH, fresh);
    };
  }, []);

  return { readOnly: offline || stale, offline, stale };
}

/** 所有页面顶部的状态条：离线或后端连不上时提示「只读」。 */
export function OfflineBanner() {
  const { offline, stale } = useReadOnly();
  if (!offline && !stale) return null;
  return (
    <div className="offline-banner" role="status">
      <b>离线 · 只读</b>
      <span> 连不上网络或后端，显示的是上次联网时保存的内容，地图底图可能不完整。修改、生成、分享需要联网。</span>
    </div>
  );
}

/** 全局导航条（规格 06-ui-design）：品牌 + 新建行程 / 我的行程 / 模型用量，当前页高亮；公开分享页不显示。 */
/** 「我的行程」旁的红点：还有几份在生成（只剩失败的就显示 !），从哪个页面都看得见（规格 07 P19）。 */
function GeneratingBadge() {
  const tasks = useUnfinishedTasks();
  const running = (tasks ?? []).filter((t) => t.status !== "failed").length;
  const failed = (tasks ?? []).length - running;
  if (running === 0 && failed === 0) return null;
  return (
    <span
      className={`gen-badge${running === 0 ? " failed" : ""}`}
      data-testid="gen-badge"
      title={running > 0 ? `${running} 份正在生成` : `${failed} 份生成失败`}
    >
      {running > 0 ? running : "!"}
    </span>
  );
}

export function AppNav() {
  const { pathname } = useLocation();
  if (pathname.startsWith("/s/")) return null;
  const cls = ({ isActive }: { isActive: boolean }) => (isActive ? "active" : undefined);
  return (
    <header className="appnav">
      <NavLink to="/" className="logo" aria-label="AI 旅行规划 首页">
        <span className="logo-mark">
          <IconPin size={17} />
        </span>
        <span className="logo-text">旅行规划</span>
      </NavLink>
      <nav aria-label="主导航">
        <NavLink to="/new" className={cls}>
          <IconPlus size={16} />
          新建行程
        </NavLink>
        <NavLink to="/daytrips" className={cls}>
          <IconPin size={16} />
          周边一日游
        </NavLink>
        <NavLink to="/plans" className={cls}>
          <IconList size={16} />
          我的行程
          <GeneratingBadge />
        </NavLink>
        <NavLink to="/usage" className={cls}>
          <IconChart size={16} />
          模型用量
        </NavLink>
        <NavLink to="/about" className={cls}>
          <IconList size={16} />
          版本足迹
        </NavLink>
      </nav>
      <span className="nav-spacer" />
    </header>
  );
}

/** 手机上的底部悬浮标签栏（规格 06-ui-design）：深绿色圆角，当前项白色胶囊；桌面端不显示（CSS 控制）。 */
export function TabBar() {
  const { pathname } = useLocation();
  const trip = useActiveTrip(pathname); // 行程进行中才有「今天」
  if (pathname.startsWith("/s/")) return null;
  const cls = ({ isActive }: { isActive: boolean }) => (isActive ? "active" : undefined);
  return (
    <nav className="tabbar" aria-label="底部导航">
      <NavLink to="/" end className={cls}>
        <IconHome size={20} />
        <span>首页</span>
      </NavLink>
      <NavLink to="/new" className={cls}>
        <IconPlus size={20} />
        <span>新建</span>
      </NavLink>
      <NavLink to="/plans" className={cls}>
        <IconList size={20} />
        <span>我的行程</span>
        <GeneratingBadge />
      </NavLink>
      {trip.card && (
        <NavLink to="/today" className={cls}>
          <IconSun size={20} />
          <span>今天</span>
        </NavLink>
      )}
      <NavLink to="/usage" className={cls}>
        <IconChart size={20} />
        <span>用量</span>
      </NavLink>
    </nav>
  );
}

export function Layout() {
  useEffect(() => applyCity(DEFAULT_CITY), []);
  return (
    <ConfirmProvider>
      <AppNav />
      <OfflineBanner />
      <Outlet />
      <TabBar />
    </ConfirmProvider>
  );
}
