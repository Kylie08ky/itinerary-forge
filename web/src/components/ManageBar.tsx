import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import { useConfirm } from "../confirm";
import { IconArchive, IconCopy, IconMore, IconShift, IconTrash } from "../icons";
import type { Itinerary, PlanResponse, ShiftConflict } from "../types";

function addDays(iso: string, n: number): string {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

/**
 * 「更多」菜单（规格 06-ui-design + M04 S8 / S13 / S14）：复制链接、导出、平移日期、复制行程、归档 / 恢复、删除。
 * 平移日期和平移后的冲突提示是弹窗；平移会生成新版本，星期变了可能撞上闭馆日，弹窗里列出新冲突并提供「重新排程」。
 * `extra` 是菜单最上面的几项（复制链接、导出……），由行程页传进来。
 */
export function ManageBar({
  planId,
  startDate,
  endDate,
  canShift,
  disabled,
  archived,
  extra,
  onVersion,
  onReload,
  onToast,
  onError,
}: {
  planId: string;
  startDate: string;
  endDate: string;
  canShift: boolean; // 只能平移最新版本
  disabled: boolean; // 编辑中 / 离线
  archived: boolean;
  extra?: React.ReactNode;
  onVersion: (v: number) => void;
  onReload: () => void;
  onToast: (m: string) => void;
  onError: (m: string) => void;
}) {
  const navigate = useNavigate();
  const confirm = useConfirm();
  const [menu, setMenu] = useState(false);
  const [open, setOpen] = useState(false);
  const [date, setDate] = useState(startDate);
  const [busy, setBusy] = useState(false);
  const [conflicts, setConflicts] = useState<ShiftConflict[]>([]);
  const [shifted, setShifted] = useState<Itinerary | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

  // 点菜单外面 / 按 Esc 关闭菜单
  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setMenu(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenu(false);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  const delta = date ? Math.round((new Date(date).getTime() - new Date(startDate).getTime()) / 86_400_000) : 0;

  async function run<T>(fn: () => Promise<T>): Promise<T | null> {
    setBusy(true);
    try {
      return await fn();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : String(e));
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function doShift(e: React.FormEvent) {
    e.preventDefault();
    const res: PlanResponse | null = await run(() => api.shiftDates(planId, date));
    if (!res) return;
    setOpen(false);
    setConflicts(res.conflicts);
    setShifted(res.itinerary);
    onVersion(res.version);
    onToast(`已平移，新建 v${res.version}`);
  }

  async function doReschedule() {
    if (!shifted) return;
    const saved = await run(async () => {
      const edited = await api.edit(planId, shifted, { op: "reschedule" });
      return api.saveVersion(planId, edited.itinerary, "按新日期重新排程", "");
    });
    if (!saved) return;
    setConflicts([]);
    setShifted(null);
    onVersion(saved.version);
    onToast(`已按新日期重新排程，新建 v${saved.version}`);
  }

  async function doDuplicate() {
    const res = await run(() => api.duplicatePlan(planId));
    if (!res) return;
    onToast("已复制，现在打开的是副本");
    navigate(`/plans/${res.plan_id}`);
  }

  async function stillGone(): Promise<boolean> {
    try {
      await api.getPlan(planId);
      return false;
    } catch (e) {
      return e instanceof ApiError && e.status === 404;
    }
  }

  async function doDelete() {
    const yes = await confirm({
      title: "永久删除这份行程？",
      message: "会连同所有版本和已发出的分享链接一起删掉，不能恢复。（只是不想看到它的话，用「归档」就行。）",
      confirmLabel: "永久删除",
      danger: true,
    });
    if (!yes) return;
    const ok = await run(() => api.deletePlan(planId));
    if (ok === null && !(await stillGone())) return;
    navigate("/plans");
  }

  async function doArchive() {
    const ok = await run(() => api.archivePlan(planId, !archived));
    if (!ok) return;
    onToast(archived ? "已恢复" : "已归档，行程收进「我的行程」的「已归档」里");
    onReload();
  }

  const pick = (fn: () => void) => () => {
    setMenu(false);
    fn();
  };

  return (
    <>
      <div className="more-wrap" ref={wrap}>
        <button
          className="btn-ghost more-btn"
          aria-haspopup="menu"
          aria-expanded={menu}
          onClick={() => setMenu((v) => !v)}
        >
          <IconMore size={17} />
          更多
        </button>
        {menu && (
          <div className="more-menu" onClick={(e) => e.target instanceof HTMLAnchorElement && setMenu(false)}>
            {extra}
            <div className="menu-sep" />
            <button
              className="menu-item"
              disabled={disabled || !canShift || busy}
              title={canShift ? undefined : "只能平移最新版本"}
              onClick={pick(() => {
                setDate(startDate);
                setOpen(true);
              })}
            >
              <IconShift size={16} />
              平移日期
            </button>
            <button className="menu-item" disabled={disabled || busy} onClick={pick(() => void doDuplicate())}>
              <IconCopy size={16} />
              复制行程
            </button>
            <button className="menu-item" disabled={disabled || busy} onClick={pick(() => void doArchive())}>
              <IconArchive size={16} />
              {archived ? "恢复" : "归档"}
            </button>
            <div className="menu-sep" />
            <button className="menu-item danger" disabled={disabled || busy} onClick={pick(() => void doDelete())}>
              <IconTrash size={16} />
              删除
            </button>
          </div>
        )}
      </div>

      {open && (
        <div className="modal-backdrop" role="presentation" onClick={() => setOpen(false)}>
          <form
            className="modal shift-form"
            role="dialog"
            aria-modal="true"
            aria-label="平移日期"
            onClick={(e) => e.stopPropagation()}
            onSubmit={doShift}
          >
            <h2>平移日期</h2>
            <label>
              新的开始日期
              <input type="date" value={date} onChange={(e) => setDate(e.target.value)} required />
            </label>
            <span className="muted">
              {delta === 0
                ? "日期没变"
                : `整体${delta > 0 ? "推迟" : "提前"} ${Math.abs(delta)} 天：${startDate} ～ ${endDate} → ${date} ～ ${addDays(endDate, delta)}`}
            </span>
            <span className="muted">会生成一个新版本，旧版本都还在。</span>
            <div className="confirm-actions">
              <button className="tool" type="button" onClick={() => setOpen(false)}>
                取消
              </button>
              <button className="primary" type="submit" disabled={busy || delta === 0}>
                {busy ? "平移中…" : "确认平移"}
              </button>
            </div>
          </form>
        </div>
      )}

      {conflicts.length > 0 && (
        <div className="modal-backdrop" role="presentation">
          <div className="modal shift-conflicts" role="dialog" aria-modal="true" aria-label="平移后的新冲突">
            <h2>平移后有 {conflicts.length} 处新的冲突</h2>
            <p className="muted" style={{ margin: 0 }}>
              星期变了，下面这些点碰上了闭馆日：
            </p>
            <ul>
              {conflicts.map((c, i) => (
                <li key={i}>{c.message}</li>
              ))}
            </ul>
            <div className="confirm-actions">
              <button
                className="tool"
                onClick={() => {
                  setConflicts([]);
                  setShifted(null);
                }}
              >
                稍后再处理
              </button>
              <button className="primary" onClick={doReschedule} disabled={busy || disabled}>
                {busy ? "重新排程中…" : "重新排程以避开闭馆日"}
              </button>
            </div>
            <span className="muted">已锁定的点保持原样；会再生成一个新版本。</span>
          </div>
        </div>
      )}
    </>
  );
}
