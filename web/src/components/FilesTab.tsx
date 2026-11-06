import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { useConfirm } from "../confirm";
import type { Attachment, AttachmentsData } from "../types";

export interface FileFilter {
  kind: Attachment["target_kind"];
  ref: string | null;
}
export interface NamedRef {
  id: string;
  name: string;
}
export interface DayRef {
  date: string;
  label: string;
}

/** 每个挂靠对象上有几个文件（回形针用）：{ day: {日期: n}, place: {地点: n}, reservation: {地点: n}, plan: n }。 */
export function fileCountsOf(data: AttachmentsData | null) {
  const out = { day: {} as Record<string, number>, place: {} as Record<string, number>,
    reservation: {} as Record<string, number>, plan: 0 };
  for (const a of data?.attachments ?? []) {
    if (a.target_kind === "plan") out.plan += 1;
    else if (a.target_ref) out[a.target_kind][a.target_ref] = (out[a.target_kind][a.target_ref] ?? 0) + 1;
  }
  return out;
}

/** 回形针 + 数量（规格 M10 S5i）：点一下跳到「文件」页签并筛选到这一项。没有文件就不显示。 */
export function Paperclip({ count, onClick, label }: { count: number; onClick: () => void; label: string }) {
  if (count <= 0) return null;
  return (
    <button type="button" className="clip" data-testid="clip" title={`${label}：${count} 个文件`} aria-label={`${label}：${count} 个文件`} onClick={onClick}>
      📎 {count}
    </button>
  );
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const TARGET_ZH: Record<Attachment["target_kind"], string> = { plan: "整个行程", day: "某一天", place: "某个地点", reservation: "某条预订" };

function filterKey(f: FileFilter | null): string {
  return f ? `${f.kind}|${f.ref ?? ""}` : "all";
}

/**
 * 「随行」卡里的「文件」页签（规格 M10 S5）：上传 / 拖进来 / 预览 / 下载 / 改挂靠 / 回收站。
 * 只收 PDF / PNG / JPG，单个 ≤ 10MB；文件只存在你自己的电脑上，不进分享页和导出。
 */
export function FilesTab({
  planId,
  data,
  onData,
  disabled,
  days,
  places,
  reservations,
  filter,
  onFilter,
  onError,
}: {
  planId: string;
  data: AttachmentsData;
  onData: (d: AttachmentsData | null) => void;
  disabled: boolean;
  days: DayRef[];
  places: NamedRef[];
  reservations: NamedRef[];
  filter: FileFilter | null;
  onFilter: (f: FileFilter | null) => void;
  onError: (msg: string) => void;
}) {
  const confirm = useConfirm();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [preview, setPreview] = useState<Attachment | null>(null);
  const [trashOpen, setTrashOpen] = useState(false);
  const [upKind, setUpKind] = useState<Attachment["target_kind"]>("plan");
  const [upRef, setUpRef] = useState("");

  useEffect(() => {
    if (!preview) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setPreview(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [preview]);

  const optionsFor = (kind: Attachment["target_kind"]): { value: string; label: string }[] =>
    kind === "day"
      ? days.map((d) => ({ value: d.date, label: d.label }))
      : kind === "place"
        ? places.map((p) => ({ value: p.id, label: p.name }))
        : kind === "reservation"
          ? reservations.map((p) => ({ value: p.id, label: p.name }))
          : [];

  function label(a: Attachment): string {
    if (a.target_kind === "plan") return "整个行程";
    if (a.target_kind === "day") return days.find((d) => d.date === a.target_ref)?.label ?? `某一天 ${a.target_ref}`;
    const list = a.target_kind === "place" ? places : reservations;
    const name = list.find((p) => p.id === a.target_ref)?.name ?? a.target_ref;
    return `${a.target_kind === "place" ? "地点" : "预订"}：${name}`;
  }

  async function reload() {
    onData(await api.attachments(planId));
  }

  async function upload(list: FileList | File[]) {
    const files = Array.from(list);
    if (files.length === 0) return;
    setBusy(true);
    try {
      for (const f of files) {
        if (f.size > data.limits.max_bytes) {
          onError(`「${f.name}」超过 10MB（当前 ${(f.size / 1024 / 1024).toFixed(1)} MB）`);
          continue;
        }
        if (upKind !== "plan" && !upRef) {
          onError("请先选好要挂到哪里，或改成「整个行程」");
          break;
        }
        try {
          await api.uploadAttachment(planId, f, { kind: upKind, ref: upKind === "plan" ? null : upRef });
        } catch (e) {
          onError(`「${f.name}」没传上去：${e instanceof ApiError ? e.message : String(e)}`);
        }
      }
      await reload();
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      await reload();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function retarget(a: Attachment, value: string) {
    const [kind, ref] = value.split("|") as [Attachment["target_kind"], string];
    await act(() => api.patchAttachment(planId, a.id, { target_kind: kind, target_ref: ref || null }));
  }

  async function removeForever(a: Attachment) {
    const yes = await confirm({
      title: "永久删除这个文件？",
      message: `「${a.name}」会从这台电脑上彻底删掉，没法恢复。`,
      confirmLabel: "永久删除",
      danger: true,
    });
    if (yes) await act(() => api.deleteAttachment(planId, a.id, true));
  }

  const shown = useMemo(
    () => data.attachments.filter((a) => !filter || (a.target_kind === filter.kind && (a.target_ref ?? null) === (filter.ref ?? null))),
    [data.attachments, filter],
  );

  // 筛选下拉：「全部」+ 现在有文件的每个挂靠对象
  const filterChoices = useMemo(() => {
    const seen = new Map<string, string>();
    for (const a of data.attachments) seen.set(`${a.target_kind}|${a.target_ref ?? ""}`, label(a));
    return [...seen.entries()];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.attachments, days, places, reservations]);

  const upOptions = optionsFor(upKind);

  return (
    <div
      className={`files-tab${drag ? " drag" : ""}`}
      data-testid="files-tab"
      onDragOver={(e) => {
        if (disabled) return;
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDrag(false);
        if (!disabled) void upload(e.dataTransfer.files);
      }}
    >
      {!disabled && (
        <div className="files-up">
          <input
            ref={input}
            type="file"
            hidden
            multiple
            accept="application/pdf,image/png,image/jpeg,.pdf,.png,.jpg,.jpeg"
            data-testid="file-input"
            onChange={(e) => e.target.files && void upload(e.target.files)}
          />
          <button type="button" className="primary" data-testid="file-upload" disabled={busy} onClick={() => input.current?.click()}>
            {busy ? "处理中…" : "上传文件"}
          </button>
          <label className="files-target">
            挂到
            <select
              value={upKind}
              data-testid="file-target-kind"
              onChange={(e) => {
                setUpKind(e.target.value as Attachment["target_kind"]);
                setUpRef("");
              }}
            >
              {(Object.keys(TARGET_ZH) as Attachment["target_kind"][]).map((k) => (
                <option key={k} value={k}>
                  {TARGET_ZH[k]}
                </option>
              ))}
            </select>
            {upKind !== "plan" && (
              <select value={upRef} data-testid="file-target-ref" onChange={(e) => setUpRef(e.target.value)} aria-label="挂靠对象">
                <option value="">（请选择）</option>
                {upOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            )}
          </label>
          <span className="muted">也可以把文件拖到这里。只收 PDF / PNG / JPG，单个 ≤ 10MB（最多 {data.limits.max_files} 个）。文件只存在你自己的电脑上，不会出现在分享页和导出里。</span>
        </div>
      )}

      {data.attachments.length > 0 && (
        <label className="files-filter">
          显示
          <select
            value={filterKey(filter)}
            data-testid="file-filter"
            onChange={(e) => {
              if (e.target.value === "all") onFilter(null);
              else {
                const [kind, ref] = e.target.value.split("|") as [Attachment["target_kind"], string];
                onFilter({ kind, ref: ref || null });
              }
            }}
          >
            <option value="all">全部（{data.attachments.length}）</option>
            {filterChoices.map(([key, text]) => (
              <option key={key} value={key}>
                {text}
              </option>
            ))}
          </select>
        </label>
      )}

      {shown.length === 0 && (
        <div className="muted files-empty" data-testid="files-empty">
          {data.attachments.length === 0 ? "还没有文件。酒店确认单、预订截图都可以传到这里。" : "这一项下面没有文件。"}
        </div>
      )}
      <ul className="files-list">
        {shown.map((a) => (
          <li className="file-row" key={a.id} data-testid="file-row" data-id={a.id}>
            <span className="file-icon" aria-hidden>
              {a.kind === "pdf" ? "📄" : "🖼️"}
            </span>
            <div className="file-main">
              {a.kind === "pdf" ? (
                <a href={api.attachmentUrl(planId, a.id)} target="_blank" rel="noopener noreferrer" className="file-name" data-testid="file-open">
                  {a.name}
                </a>
              ) : (
                <button type="button" className="linklike file-name" data-testid="file-open" onClick={() => setPreview(a)}>
                  {a.name}
                </button>
              )}
              <div className="muted file-meta">
                {a.kind.toUpperCase()} · {fmtSize(a.size)} · {a.uploaded_at.slice(0, 10)} · <span data-testid="file-target">{label(a)}</span>
              </div>
            </div>
            <div className="file-actions">
              <a className="tool" href={api.attachmentUrl(planId, a.id, true)} data-testid="file-download">
                下载
              </a>
              {!disabled && (
                <>
                  <select
                    className="tool"
                    aria-label={`${a.name}挂到哪里`}
                    value={`${a.target_kind}|${a.target_ref ?? ""}`}
                    data-testid="file-retarget"
                    onChange={(e) => void retarget(a, e.target.value)}
                  >
                    <option value="plan|">整个行程</option>
                    {(["day", "place", "reservation"] as const).map((k) => (
                      <optgroup key={k} label={TARGET_ZH[k]}>
                        {optionsFor(k).map((o) => (
                          <option key={o.value} value={`${k}|${o.value}`}>
                            {o.label}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                  <button type="button" className="tool" data-testid="file-delete" disabled={busy} onClick={() => void act(() => api.deleteAttachment(planId, a.id))}>
                    删除
                  </button>
                </>
              )}
            </div>
          </li>
        ))}
      </ul>

      {data.trash.length > 0 && (
        <div className="files-trash" data-testid="files-trash">
          <button type="button" className="linklike" aria-expanded={trashOpen} data-testid="trash-toggle" onClick={() => setTrashOpen(!trashOpen)}>
            回收站 {data.trash.length}
          </button>
          <span className="muted"> · 删掉的文件在这里放 {data.limits.trash_days} 天，之后自动清理</span>
          {trashOpen && (
            <ul className="files-list">
              {data.trash.map((a) => (
                <li className="file-row trashed" key={a.id} data-testid="trash-row">
                  <span className="file-icon" aria-hidden>
                    {a.kind === "pdf" ? "📄" : "🖼️"}
                  </span>
                  <div className="file-main">
                    <span className="file-name">{a.name}</span>
                    <div className="muted file-meta">
                      {fmtSize(a.size)} · 删于 {a.deleted_at?.slice(0, 10)}
                    </div>
                  </div>
                  {!disabled && (
                    <div className="file-actions">
                      <button type="button" className="tool" data-testid="trash-restore" disabled={busy} onClick={() => void act(() => api.patchAttachment(planId, a.id, { restore: true }))}>
                        恢复
                      </button>
                      <button type="button" className="tool danger" data-testid="trash-purge" disabled={busy} onClick={() => void removeForever(a)}>
                        永久删除
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {preview && (
        <div className="modal-backdrop" role="presentation" onClick={() => setPreview(null)}>
          <div className="modal file-preview" role="dialog" aria-modal="true" aria-label={`预览：${preview.name}`} onClick={(e) => e.stopPropagation()}>
            <button type="button" className="modal-close" aria-label="关闭" onClick={() => setPreview(null)}>
              ✕
            </button>
            <h2>{preview.name}</h2>
            <img src={api.attachmentUrl(planId, preview.id)} alt={preview.name} data-testid="file-preview-img" />
            <div className="confirm-actions">
              <a className="tool" href={api.attachmentUrl(planId, preview.id, true)}>
                下载
              </a>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
