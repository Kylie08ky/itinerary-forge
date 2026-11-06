import type { CultureStory } from "../types";

/** 「电影《午夜巴黎》」：中文译名优先，没有就用原名。招牌写成「招牌：马卡龙」。 */
export function storyLabel(s: CultureStory): string {
  const name = s.title_zh ?? s.title;
  return s.kind === "food" ? `${s.kind_zh}：${name}` : `${s.kind_zh}《${name}》`;
}

/**
 * 这个地点的故事（规格 09 十五）：它出现在哪些电影 / 纪录片 / 书里。数据来自带来源的策展库，用户不用选，走到这里自然看到。
 * limit：行程条目里最多显示几条，多的提示去「详情」看全部。
 */
export function Stories({ stories, limit }: { stories: CultureStory[]; limit?: number }) {
  if (stories.length === 0) return null;
  const shown = limit ? stories.slice(0, limit) : stories;
  return (
    <ul className="item-stories" data-testid="item-stories" aria-label="这里的故事">
      {shown.map((s) => (
        <li key={s.work_id}>
          <b>{storyLabel(s)}</b>
          {s.title_zh && <span className="muted"> {s.title}</span>}：{s.note}
        </li>
      ))}
      {shown.length < stories.length && (
        <li className="muted" data-testid="item-stories-more">
          还有 {stories.length - shown.length} 条，点「详情」查看
        </li>
      )}
    </ul>
  );
}
