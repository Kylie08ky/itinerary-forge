import type { CommuteLeg } from "../types";

/** 排程估的分钟数写在每一项的 transport 里（「从上一站出发，步行约 12 分钟（估算）」）。 */
function estimatedMinutes(transport: string | null | undefined): number | null {
  const m = transport?.match(/约 (\d+) 分钟/);
  return m ? Number(m[1]) : null;
}

/** 相邻两点之间高德算出的真实通勤（规格 M05 S7b）：只显示在页面上，不保存。 */
export function CommuteLine({ leg, transport }: { leg: CommuteLeg; transport?: string | null }) {
  const est = estimatedMinutes(transport);
  const extra = est != null ? leg.minutes - est : 0;
  return (
    <div className="commute-line" data-testid="commute-leg">
      <span>
        实际{leg.mode}约 <b>{leg.minutes} 分钟</b>
        {extra >= 10 && <span className="commute-extra">，比排程估的多 {extra} 分钟</span>}
      </span>
      {leg.steps.length > 1 && (
        <details>
          <summary>路线分段</summary>
          <ol>
            {leg.steps.map((s, k) => (
              <li key={k}>{s}</li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}

export const COMMUTE_REASON: Record<string, string> = {
  no_key: "没配高德 Key，先用排程的估算",
  quota: "本月查路线的次数用完，先用估算",
  unreachable: "暂时没查到，先用估算",
  not_amap_city: "这座城市不用高德",
};
