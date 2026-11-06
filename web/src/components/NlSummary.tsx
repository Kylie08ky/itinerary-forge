import type { NlIntent } from "../types";

/**
 * 「我是这样理解的」（规格 06 S12）：把一句话读成了什么、没读出来什么，一条条列出来。
 * 表单已经按它填好了，但**没有生成**——你确认 / 修改后再点「生成行程」。
 */
export function NlSummary({ result, onClose }: { result: NlIntent; onClose: () => void }) {
  const nothing = result.understood.length === 0;
  return (
    <section className="card nl-summary" id="nl-summary" data-testid="nl-result" aria-label="我是这样理解的">
      <header className="nl-head">
        <h2>{nothing ? "这句话里我没读出能填的内容" : "我是这样理解的"}</h2>
        <button type="button" className="tool" data-testid="nl-close" onClick={onClose}>
          收起
        </button>
      </header>
      {!nothing && (
        <>
          <ul className="nl-chips" data-testid="nl-understood">
            {result.understood.map((s) => (
              <li className="nl-chip" key={s}>
                {s}
              </li>
            ))}
          </ul>
          <p className="muted nl-hint">下面的表单已经按这些填好了。<b>还没有生成行程</b>：检查一下、需要的话改一改，再点最下面的「生成行程」。</p>
        </>
      )}
      {result.missing.length > 0 && (
        <div className="banner warn" data-testid="nl-missing">
          没读出来，请在下面选 / 填：
          <ul>
            {result.missing.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </div>
      )}
      {result.notes.length > 0 && (
        <div className="banner" data-testid="nl-notes">
          <ul>
            {result.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </div>
      )}
      {!result.model_used && (
        <div className="banner" data-testid="nl-no-model">
          本地模型这次没有参与（没开或返回有误），只按规则读了：日期、天数、人数、预算、地点库里的地点和常见的主题词都照常读；「想逛逛街」这类说法和特别要求可能漏掉。
        </div>
      )}
    </section>
  );
}
