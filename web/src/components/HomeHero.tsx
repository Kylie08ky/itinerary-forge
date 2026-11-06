import { useState, type ReactNode } from "react";
import { HeroCredit, useHeroReady } from "../hero";
import { IconUpload } from "../icons";
import { coverStyle } from "../themes";
import { mediaSrc } from "../api";
import type { PlaceImageInfo } from "../types";

export const NL_MAX = 500;
/** 首页输入卡下面的示例短句（规格 06 S24）：点一下只是把整句填进输入框。 */
export const SUGGESTIONS = [
  "下周二去罗马玩 4 天，从上海出发",
  "这周六到下周二去泉州，两个人",
  "周末去上海玩 2 天，两个人，喜欢咖啡馆",
  "圣诞去意大利，罗马、佛罗伦萨",
];
const PLACEHOLDER = "10月20号去巴黎玩4天，两个人，喜欢艺术和咖啡馆，预算700欧，想去奥赛博物馆……";

/**
 * 「一句话」输入框（规格 M01 S10、07 P04）：首页是毛玻璃样式，新建行程页是普通卡片样式，行为一样。
 * 点「开始规划」只是把这句话交出去（首页跳到新建页、新建页直接解析），**不会自动生成行程**。
 */
export function NlForm({
  variant,
  busy,
  disabled,
  onRead,
  onImport,
  initial = "",
}: {
  variant: "glass" | "plain";
  busy: boolean;
  disabled: boolean;
  onRead: (text: string) => void;
  onImport: () => void;
  initial?: string;
}) {
  const [text, setText] = useState(initial);
  const length = text.trim().length;
  const tooLong = length > NL_MAX;
  const cls = variant === "glass" ? "glass" : "plain";
  return (
    <>
    <form
      className={`${cls}-card nl-form`}
      data-testid="nl-card"
      onSubmit={(e) => {
        e.preventDefault();
        if (length > 0 && !tooLong && !busy && !disabled) onRead(text.trim());
      }}
    >
      <textarea
        className={`${cls}-text`}
        data-testid="nl-text"
        aria-label="用一句话说说你想怎么玩"
        placeholder={PLACEHOLDER}
        value={text}
        rows={variant === "glass" ? 3 : 2}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) e.currentTarget.form?.requestSubmit();
        }}
      />
      <div className={`${cls}-foot`}>
        <button type="button" className={`${cls}-icon`} data-testid="nl-import" aria-label="导入笔记" title="导入朋友分享的笔记" onClick={onImport}>
          <IconUpload size={18} />
        </button>
        <span className={`${cls}-count${tooLong ? " over" : ""}`} data-testid="nl-count">
          {length > 0 ? `${length} / ${NL_MAX}` : "Ctrl + Enter 也可以提交"}
        </span>
        <button type="submit" className={`${cls}-go`} data-testid="nl-go" disabled={length === 0 || tooLong || busy || disabled}>
          {busy ? "读取中…" : "开始规划"}
        </button>
      </div>
    </form>
    {variant === "glass" && (
      <div className="nl-suggest" role="group" aria-label="示例">
        {SUGGESTIONS.map((x) => (
          <button key={x} type="button" data-testid="nl-suggest" onClick={() => setText(x)}>
            {x}
          </button>
        ))}
      </div>
    )}
    </>
  );
}

/**
 * 首页首屏（规格 06 S11–S13、07 P01）：全屏实拍背景 + 大标题 + 一张毛玻璃提示卡。
 * 背景是「下一站」那座城市的大图；没有行程时轮换各城市的大图。没有大图 / 加载失败：退回城市主色渐变。
 */
export function HomeHero({
  hero,
  busy,
  disabled,
  onRead,
  onImport,
  next,
}: {
  next?: ReactNode;
  hero: PlaceImageInfo | null;
  busy: boolean;
  disabled: boolean;
  onRead: (text: string) => void;
  onImport: () => void;
}) {
  const ready = useHeroReady(hero);
  return (
    <div className={`home-hero${ready ? " has-photo" : ""}`} style={ready ? undefined : coverStyle(null)} data-testid="home-hero">
      {ready && hero && (
        <>
          <div className="hero-photo" data-testid="hero-photo" style={{ backgroundImage: `url("${mediaSrc(hero.url)}")` }} />
          <div className="hero-shade" />
        </>
      )}
      <div className="home-hero-inner">
        <span className="hero-kicker">ITINERARY</span>
        <h1>你想去哪里？</h1>
        <NlForm variant="glass" busy={busy} disabled={disabled} onRead={onRead} onImport={onImport} />
        {next && <div className="hero-next">{next}</div>}
      </div>
      {ready && hero && <HeroCredit image={hero} />}
    </div>
  );
}
