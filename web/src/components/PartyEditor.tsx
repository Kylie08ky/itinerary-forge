import { useState } from "react";
import type { Traveler, TravelerDraft } from "../types";

export const RESIDENCE_OPTIONS: { v: Traveler["residence"]; t: string }[] = [
  { v: "none", t: "不长居欧洲" },
  { v: "fr", t: "长居法国" },
  { v: "eu", t: "长居其他欧盟国家" },
];

export const MAX_TRAVELERS = 20;

const SAVED_KEY = "atp:saved-travelers";

type SavedTraveler = Traveler & { name: string };

/** 年龄 → 身份标签（只是显示用的分档；优惠由后端按各景点的规则逐人算）。 */
export function roleOf(age: number | null): string {
  if (age === null) return "待填年龄";
  if (age < 12) return "儿童";
  if (age < 18) return "青少年";
  if (age >= 65) return "老人";
  return "成人";
}

/** 常用出行人：只存在这台设备的浏览器里（localStorage），不上传、不随行程分享。 */
export function loadSavedTravelers(): SavedTraveler[] {
  try {
    const raw = JSON.parse(localStorage.getItem(SAVED_KEY) ?? "[]");
    return Array.isArray(raw) ? raw.filter((t) => t && typeof t.name === "string" && typeof t.age === "number") : [];
  } catch {
    return [];
  }
}

function storeSavedTravelers(list: SavedTraveler[]) {
  try {
    localStorage.setItem(SAVED_KEY, JSON.stringify(list.slice(0, 30)));
  } catch {
    /* 隐私模式 / 配额满：不影响填表 */
  }
}

/**
 * 出行人列表（规格 M01 S4b / M09 S1h / 07 P09）：每个人可填姓名（选填，随行程保存）、年龄（必填，自动显示儿童 / 成人 / 老人）、
 * 是否学生，城市有「长居身份」优惠时再多一项长居地。首页「行程信息」和行程页的预算卡共用。
 * saveable：新建页里开启「常用出行人」（存在本机，下次一键加入）。
 */
export function PartyEditor({
  value,
  onChange,
  disabled,
  showResidence = true,
  saveable = false,
}: {
  value: TravelerDraft[];
  onChange: (next: TravelerDraft[]) => void;
  disabled?: boolean;
  showResidence?: boolean;
  saveable?: boolean;
}) {
  const [saved, setSaved] = useState<SavedTraveler[]>(() => (saveable ? loadSavedTravelers() : []));
  const patch = (i: number, p: Partial<TravelerDraft>) => onChange(value.map((t, k) => (k === i ? { ...t, ...p } : t)));
  const full = value.length >= MAX_TRAVELERS;

  function saveOne(t: TravelerDraft) {
    const name = (t.name ?? "").trim();
    if (!name || t.age === null) return;
    const next = [{ name, age: t.age, residence: t.residence, student: t.student }, ...saved.filter((s) => s.name !== name)];
    setSaved(next);
    storeSavedTravelers(next);
  }

  function forget(name: string) {
    const next = saved.filter((s) => s.name !== name);
    setSaved(next);
    storeSavedTravelers(next);
  }

  /** 加入一位常用出行人：第一位还是没填过的空白卡时直接替换它，不留一张空卡。 */
  function addSaved(s: SavedTraveler) {
    if (value.some((t) => (t.name ?? "").trim() === s.name)) return;
    const blank = value.length === 1 && !value[0].name && value[0].age === 30 && value[0].residence === "none" && !value[0].student;
    onChange(blank ? [{ ...s }] : [...value, { ...s }]);
  }

  return (
    <div className="party-editor" data-testid="party-editor">
      {saveable && saved.length > 0 && (
        <div className="party-saved" data-testid="party-saved">
          <span className="muted">常用出行人（仅存在这台设备）：</span>
          {saved.map((s) => (
            <span className="saved-chip" key={s.name}>
              <button
                type="button"
                className="tool"
                disabled={disabled || full || value.some((t) => (t.name ?? "").trim() === s.name)}
                onClick={() => addSaved(s)}
              >
                ＋ {s.name}（{s.age} 岁）
              </button>
              <button type="button" className="chip-x" aria-label={`忘掉常用出行人 ${s.name}`} onClick={() => forget(s.name)}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {value.map((t, i) => (
        <div className="party-row" key={i} data-testid="party-row">
          <span className="party-no">第 {i + 1} 位</span>
          <label className="party-name">
            姓名
            <input
              value={t.name ?? ""}
              maxLength={20}
              placeholder="选填"
              disabled={disabled}
              aria-label={`第 ${i + 1} 位的姓名`}
              onChange={(e) => patch(i, { name: e.target.value })}
            />
          </label>
          <label className="party-age">
            年龄
            <input
              type="number"
              min={0}
              max={120}
              value={t.age ?? ""}
              placeholder="必填"
              disabled={disabled}
              aria-label={`第 ${i + 1} 位的年龄`}
              onChange={(e) =>
                patch(i, { age: e.target.value === "" ? null : Math.max(0, Math.min(120, Math.floor(Number(e.target.value)))) })
              }
            />
          </label>
          <span className={`role-chip${t.age === null ? " pending" : ""}`} data-testid="party-role">
            {roleOf(t.age)}
          </span>
          {showResidence && (
            <label className="party-res">
              长居
              <select
                value={t.residence}
                disabled={disabled}
                aria-label={`第 ${i + 1} 位长居哪里`}
                onChange={(e) => patch(i, { residence: e.target.value as Traveler["residence"] })}
              >
                {RESIDENCE_OPTIONS.map((o) => (
                  <option key={o.v} value={o.v}>
                    {o.t}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="party-student">
            <input
              type="checkbox"
              checked={t.student}
              disabled={disabled}
              aria-label={`第 ${i + 1} 位是学生`}
              onChange={(e) => patch(i, { student: e.target.checked })}
            />
            学生
          </label>
          <span className="party-actions">
            {saveable && (
              <button
                type="button"
                className="tool"
                disabled={disabled || !(t.name ?? "").trim() || t.age === null}
                title="要先填姓名和年龄"
                aria-label={`把第 ${i + 1} 位存为常用出行人`}
                onClick={() => saveOne(t)}
              >
                存为常用
              </button>
            )}
            <button
              type="button"
              className="tool"
              disabled={disabled || full}
              aria-label={`复制第 ${i + 1} 位`}
              onClick={() => onChange([...value.slice(0, i + 1), { ...t, name: "" }, ...value.slice(i + 1)])}
            >
              复制
            </button>
            {value.length > 1 && (
              <button
                type="button"
                className="tool danger"
                disabled={disabled}
                aria-label={`删除第 ${i + 1} 位`}
                onClick={() => onChange(value.filter((_, k) => k !== i))}
              >
                删除
              </button>
            )}
          </span>
        </div>
      ))}
      <div className="party-foot">
        <button
          type="button"
          className="tool"
          disabled={disabled || full}
          onClick={() => onChange([...value, { age: null, residence: "none", student: false }])}
        >
          ＋ 添加出行人
        </button>
        <span className="muted">
          共 {value.length} 人
          {showResidence && " · 「长居」指常年在欧洲生活（如留学、工作），很多景点对他们有欧盟居民价或 26 岁以下免费"}
        </span>
      </div>
    </div>
  );
}

/** 提交前检查：每位出行人都得填年龄。返回第一条错误，没问题返回 null。 */
export function partyError(value: TravelerDraft[]): string | null {
  if (value.length < 1 || value.length > MAX_TRAVELERS) return `出行人要有 1 到 ${MAX_TRAVELERS} 位`;
  const i = value.findIndex((t) => t.age === null);
  return i >= 0 ? `第 ${i + 1} 位出行人的年龄还没填` : null;
}

/** 草稿 → 提交用的出行人（调用前先用 partyError 检查过）；姓名没填就不带这个字段。 */
export function toTravelers(value: TravelerDraft[]): Traveler[] {
  return value.map((t) => {
    const { name, ...rest } = t;
    const clean = (name ?? "").trim();
    return { ...rest, age: t.age ?? 30, ...(clean ? { name: clean } : {}) };
  });
}
