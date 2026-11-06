import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

export interface ConfirmOptions {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 危险操作（删除等）：确认按钮用红色，默认焦点放在「取消」上，防止手滑回车。 */
  danger?: boolean;
}

type Ask = (opts: ConfirmOptions) => Promise<boolean>;

const Ctx = createContext<Ask | null>(null);

/**
 * 页面内的确认对话框，替代 window.confirm。
 * 原因：应用内嵌的浏览器面板里 window.confirm 会瞬间返回「取消」，所有靠它确认的操作都会静默失效；
 * 自己画一个对话框，在任何环境里行为一致，样式也和页面统一，浏览器验收也能直接点。
 */
export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [pending, setPending] = useState<{ opts: ConfirmOptions; resolve: (v: boolean) => void } | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const okRef = useRef<HTMLButtonElement>(null);

  const ask = useCallback<Ask>(
    (opts) =>
      new Promise<boolean>((resolve) => {
        setPending((cur) => {
          cur?.resolve(false); // 上一个还没答就来了新的：把上一个当作取消
          return { opts, resolve };
        });
      }),
    [],
  );

  const answer = useCallback(
    (v: boolean) => {
      pending?.resolve(v);
      setPending(null);
    },
    [pending],
  );

  useEffect(() => {
    if (!pending) return;
    (pending.opts.danger ? cancelRef : okRef).current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") answer(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pending, answer]);

  return (
    <Ctx.Provider value={ask}>
      {children}
      {pending && (
        <div className="modal-backdrop confirm-backdrop" role="presentation" onClick={() => answer(false)}>
          <div
            className="modal confirm-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-label={pending.opts.title ?? "请确认"}
            onClick={(e) => e.stopPropagation()}
          >
            <h2>{pending.opts.title ?? "请确认"}</h2>
            <p className="confirm-msg">{pending.opts.message}</p>
            <div className="confirm-actions">
              <button ref={cancelRef} className="tool" data-testid="confirm-cancel" onClick={() => answer(false)}>
                {pending.opts.cancelLabel ?? "取消"}
              </button>
              <button
                ref={okRef}
                className={pending.opts.danger ? "primary danger-solid" : "primary"}
                data-testid="confirm-ok"
                onClick={() => answer(true)}
              >
                {pending.opts.confirmLabel ?? "确认"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Ctx.Provider>
  );
}

/**
 * 错误弹窗：错误出现在用户当前看得到的地方（视口中央），而不是页面最顶部的横幅里——页面一滚动横幅就看不见了。
 * message 为 null 时不显示；点「知道了」/ 按 Esc / 点背景都会关闭。
 */
export function ErrorPopup({ message, onClose }: { message: string | null; onClose: () => void }) {
  const okRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!message) return;
    okRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [message, onClose]);
  if (!message) return null;
  return (
    <div className="modal-backdrop notice-backdrop" role="presentation" onClick={onClose}>
      <div className="modal confirm-dialog notice-dialog" role="alertdialog" aria-modal="true" aria-label="出错了"
        onClick={(e) => e.stopPropagation()}>
        <h2>出错了</h2>
        <p className="confirm-msg">{message}</p>
        <div className="confirm-actions">
          <button ref={okRef} className="primary" data-testid="notice-ok" onClick={onClose}>
            知道了
          </button>
        </div>
      </div>
    </div>
  );
}

export function useConfirm(): Ask {
  const ask = useContext(Ctx);
  if (!ask) throw new Error("useConfirm 必须在 ConfirmProvider 里用");
  return ask;
}
