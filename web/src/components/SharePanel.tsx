import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { ErrorPopup, useConfirm } from "../confirm";
import type { ShareOptions, ShareStatus } from "../types";

const SHARE_CHOICES: { key: keyof ShareOptions; label: string; hint: string }[] = [
  { key: "packing", label: "行李清单", hint: "朋友能看到带什么、哪些已带（只读）" },
  { key: "todos", label: "待办", hint: "事项、截止日、是否完成（只读）" },
  { key: "notes", label: "行程备注", hint: "你写在每一条安排下面的备注（只读）" },
];

/** 公开只读分享（规格 M08 S5）：生成链接 → 复制发给朋友；随时可以关闭，关闭后链接立刻失效。 */
export function SharePanel({
  planId,
  dayTrip = false,
  onToast,
  onClose,
}: {
  planId: string;
  dayTrip?: boolean;
  onToast: (msg: string) => void;
  onClose: () => void;
}) {
  const [st, setSt] = useState<ShareStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const confirm = useConfirm();

  useEffect(() => {
    api.shareStatus(planId).then(setSt).catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [planId]);

  const url = st?.path ? `${window.location.origin}${st.path}` : "";

  async function create() {
    setBusy(true);
    setError(null);
    try {
      setSt(await api.createShare(planId));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function setOption(key: keyof ShareOptions, on: boolean) {
    if (!st) return;
    const before = st;
    setSt({ ...st, options: { ...st.options, [key]: on } }); // 先打上勾，失败了再退回去
    try {
      setSt(await api.setShareOptions(planId, { ...before.options, [key]: on }));
    } catch (e) {
      setSt(before);
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function revoke() {
    const yes = await confirm({
      title: "关闭分享链接？",
      message: "关闭后，已经发出去的链接会立刻失效（朋友再打开会看到「链接已失效」）。需要时可以重新生成一个新链接。",
      confirmLabel: "关闭链接",
      danger: true,
    });
    if (!yes) return;
    setBusy(true);
    try {
      await api.revokeShare(planId);
      setSt({ active: false, token: null, path: null, options: { packing: false, todos: false, notes: false } });
      onToast("分享链接已关闭");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !error && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, error]);

  // 弹窗：不管页面滚到哪，点「分享」都立刻出现在当前画面里
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
    <div className="modal share-panel" role="dialog" aria-modal="true" aria-label="分享给朋友" onClick={(e) => e.stopPropagation()}>
      <button className="modal-close" onClick={onClose} aria-label="关闭">
        ✕
      </button>
      <h2>分享给朋友</h2>
      <ErrorPopup message={error} onClose={() => setError(null)} />
      {st && !st.active && (
        <>
          <p className="muted">
            生成一个链接，知道链接的人不用登录就能只读查看这份行程（时间轴、地图、购票清单），不能修改。
            他们看到的始终是你保存的最新版本。
          </p>
          <button className="primary" onClick={create} disabled={busy}>
            {busy ? "生成中…" : "生成分享链接"}
          </button>
        </>
      )}
      {st?.active && (
        <>
          <div className="share-row">
            <input readOnly value={url} aria-label="分享链接" onFocus={(e) => e.currentTarget.select()} />
            <button
              className="tool"
              onClick={() => {
                navigator.clipboard?.writeText(url).then(() => onToast("分享链接已复制"));
              }}
            >
              复制
            </button>
            <button className="tool danger" onClick={revoke} disabled={busy}>
              关闭链接
            </button>
          </div>
          <fieldset className="share-options" data-testid="share-options">
            <legend>一起分享（默认都不分享）</legend>
            {SHARE_CHOICES.filter((c) => !(dayTrip && (c.key === "packing" || c.key === "todos"))).map((c) => ( // 一日游没有行李清单和待办
              <label key={c.key}>
                <input
                  type="checkbox"
                  checked={st.options[c.key]}
                  onChange={(e) => void setOption(c.key, e.target.checked)}
                />
                <span>
                  {c.label}
                  <span className="muted"> · {c.hint}</span>
                </span>
              </label>
            ))}
            <p className="muted">
              勾选立刻生效，链接不变，朋友刷新就能看到。预算、出行人年龄、预订确认码、住宿不提供分享。
            </p>
          </fieldset>
          <p className="muted">链接里有一长串随机字符，没有链接的人猜不到。不想再分享时点「关闭链接」。</p>
        </>
      )}
    </div>
    </div>
  );
}
