import type { CityInfo } from "../types";

type Tone = "ok" | "soft" | "warn";

/** 按城市的建议天数给一句话判断（规格 07 P06）：只提醒，不限制能选多少天。 */
export function durationVerdict(days: number, city: CityInfo | null): { tone: Tone; text: string } | null {
  if (days < 1) return null;
  if (!city) return { tone: "soft", text: `共 ${days} 天。` };
  const a = city.suggested_days;
  const name = city.name_zh;
  if (days < a.min) return { tone: "soft", text: `${name}建议 ${a.min}–${a.max} 天；${days} 天会比较赶，只能挑最有代表性的点。` };
  if (days <= a.max) return { tone: "ok", text: `${days} 天在${name}建议的 ${a.min}–${a.max} 天之内，节奏比较从容。` };
  if (days <= a.quality_max) return { tone: "soft", text: `${name}建议 ${a.min}–${a.max} 天；${days} 天会偏长，后面几天的点会没那么精彩，节奏默认放宽到轻松。` };
  if (a.fill_max && days > a.fill_max)
    return { tone: "warn", text: `${name}建议 ${a.min}–${a.max} 天；${name}的点最多排满约 ${a.fill_max} 天，多出来的天会留成自由日。可以照样生成。` };
  return { tone: "warn", text: `${name}建议 ${a.min}–${a.max} 天；超过 ${a.quality_max} 天主题里的点不够，后面大多会排其他主题的代表性景点。可以照样生成。` };
}

/** 选日期时旁边的时长参考：这座城市的建议 + 一条通用经验值。 */
export function DurationAdvice({ days, city }: { days: number; city: CityInfo | null }) {
  const v = durationVerdict(days, city);
  return (
    <div className="duration-advice" data-testid="duration-advice">
      {v && <div className={`advice-verdict ${v.tone}`} data-testid="duration-verdict">{v.text}</div>}
      <p className="muted advice-rule">
        经验值：国内玩一座城市一般不超过一周；玩一个国家约一周；跨多个国家约半个月。{city ? city.suggested_days.note + "。" : ""}
      </p>
    </div>
  );
}
