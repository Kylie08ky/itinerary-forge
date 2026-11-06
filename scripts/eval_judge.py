"""LLM Judge 主题契合度的真实模型评测（规格 M07 S5）——开发者工具，不在产品界面里。

用法：python scripts/eval_judge.py [--runs 3] [--themes art,food]

需要本地 Ollama 在跑（见 .env）；没开就直接说明并退出（退出码 0，算跳过）。
每个主题造三种用例：
- 应该高分「对得上」：用这个主题生成的行程，拿这个主题来评；
- 应该低分「冒充」：用「和它最不像的主题」生成的行程，**冒充**这个主题来评（只改行程里记的所选主题，景点不动）；
- 应该更低「近邻冒充」：用「和它最像的主题」（成员地点重合最多，比如 艺术 vs 博物馆）生成的行程冒充——这才是真正考验。
统计：每类的平均分（和多次运行的最低 / 最高，看稳不稳）、「对得上 > 冒充」「对得上 > 近邻冒充」的主题数、
高分组 ≥ 3 分且低分组 < 3 分的比例；另外**不用模型**算一个「成员占比」（行程里属于所选主题的地点占比）做对照；
分数不理想的用例（对得上 < 4 分 / 冒充 ≥ 3 分）会把模型给的理由打印出来，方便看是评委偏了还是行程真的不对。
结果只打印，不写文件；换模型、改提示词、改选点规则后重跑对比。单元测试里用假模型测这里的流程（tests/test_eval_judge.py）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.generator import generate_from_theme  # noqa: E402
from app.judge import SYSTEM, THRESHOLD, JudgeError, _prompt, evaluate  # noqa: E402
from app.library import PlaceLibrary, load_library  # noqa: E402
from app.llm import LLMClient, LLMConfig, get_client  # noqa: E402
from app.schemas import Itinerary, TravelRequest  # noqa: E402

START = date(2026, 10, 3)
KINDS = ("match", "near", "mismatch")
KIND_ZH = {"match": "对得上", "near": "近邻冒充", "mismatch": "冒充"}


@dataclass
class Case:
    theme_id: str
    kind: str                 # match 应该高分 / mismatch 应该低分 / near 应该比 match 低
    itinerary: Itinerary
    source_theme: str         # 行程实际是按哪个主题生成的
    member_share: float = 0.0  # 行程里属于所选主题的地点占比（算法算的，不用模型）


def _itinerary(lib: PlaceLibrary, theme_id: str) -> Itinerary:
    req = TravelRequest(destination="Paris", start_date=START, end_date=START + timedelta(days=3),
                        budget_level="medium", travel_style="balanced", interests=["art"])
    return generate_from_theme(req, theme_id, client=None, library=lib).itinerary


def _members(lib: PlaceLibrary, theme_id: str) -> set[str]:
    return {p.id for p, _ in lib.theme_members(theme_id)}


def _overlap(lib: PlaceLibrary, a: str, b: str) -> float:
    ma, mb = _members(lib, a), _members(lib, b)
    return len(ma & mb) / max(len(ma | mb), 1)


def opposite_theme(lib: PlaceLibrary, theme_id: str) -> str:
    """和它最不像的主题：成员地点重合最少的那个（并列时按 id 排序，保证每次结果一样）。"""
    return min((t for t in sorted(lib.themes) if t != theme_id), key=lambda o: _overlap(lib, theme_id, o))


def nearest_theme(lib: PlaceLibrary, theme_id: str) -> str:
    """和它最像的主题：成员地点重合最多的那个（并列时按 id 排序）。"""
    return min((t for t in sorted(lib.themes) if t != theme_id), key=lambda o: (-_overlap(lib, theme_id, o), o))


def member_share(lib: PlaceLibrary, it: Itinerary, theme_id: str) -> float:
    """行程里的地点有多大比例属于这个主题（成员）；不依赖模型。"""
    ids = [i.place_id for d in it.days for i in d.items if i.place_id]
    mine = _members(lib, theme_id)
    return round(sum(1 for p in ids if p in mine) / len(ids), 2) if ids else 0.0


def _claim(it: Itinerary, theme_id: str, theme_name: str) -> Itinerary:
    """冒充：景点不动，只把行程里记的「所选主题」改成 theme_id。"""
    gen = it.generation.model_copy(update={"theme_id": theme_id, "theme_name": theme_name, "themes": [theme_id]})
    return it.model_copy(update={"generation": gen})


def build_cases(lib: PlaceLibrary, themes: list[str] | None = None) -> list[Case]:
    cases: list[Case] = []
    for tid in themes or sorted(lib.themes):
        match = _itinerary(lib, tid)
        name = match.generation.theme_name
        cases.append(Case(tid, "match", match, tid, member_share(lib, match, tid)))
        opp = opposite_theme(lib, tid)
        fake = _claim(_itinerary(lib, opp), tid, name)
        cases.append(Case(tid, "mismatch", fake, opp, member_share(lib, fake, tid)))
        near = nearest_theme(lib, tid)
        if near != opp:
            fake_near = _claim(_itinerary(lib, near), tid, name)
            cases.append(Case(tid, "near", fake_near, near, member_share(lib, fake_near, tid)))
    return cases


def run_eval(client: LLMClient, cases: list[Case], lib: PlaceLibrary, runs: int = 1) -> dict:
    """跑一遍，返回统计。评估失败的用例单独计数，不算进分数。"""
    scores: dict[tuple[str, str], list[int]] = {}
    reasons: dict[tuple[str, str], str] = {}
    failed = 0
    for case in cases:
        for _ in range(runs):
            try:
                r = evaluate(case.itinerary, client, lib)
            except JudgeError:
                failed += 1
                continue
            scores.setdefault((case.theme_id, case.kind), []).append(r.score)
            reasons[(case.theme_id, case.kind)] = r.reason

    def mean(xs: list[int]) -> float | None:
        return round(sum(xs) / len(xs), 2) if xs else None

    themes = sorted({t for t, _ in scores})
    per_theme = {t: {k: mean(scores.get((t, k), [])) for k in KINDS} for t in themes}
    spread = {t: {k: (min(scores[(t, k)]), max(scores[(t, k)])) for k in KINDS if (t, k) in scores} for t in themes}
    shares = {(c.theme_id, c.kind): c.member_share for c in cases}
    pairs = [(v["match"], v["mismatch"]) for v in per_theme.values()
             if v["match"] is not None and v["mismatch"] is not None]
    near_pairs = [(v["match"], v["near"]) for v in per_theme.values()
                  if v["match"] is not None and v["near"] is not None]

    def pool(kind: str) -> list[int]:
        return [s for (t, k), xs in scores.items() if k == kind for s in xs]

    allm, allx, alln = pool("match"), pool("mismatch"), pool("near")
    weak = [{"theme": t, "kind": k, "mean": per_theme[t][k], "reason": reasons[(t, k)]}
            for (t, k) in sorted(reasons)
            if (k == "match" and per_theme[t][k] is not None and per_theme[t][k] < 4)
            or (k != "match" and per_theme[t][k] is not None and per_theme[t][k] >= THRESHOLD)]
    return {
        "per_theme": per_theme, "spread": spread,
        "member_share": {t: {k: shares.get((t, k)) for k in KINDS} for t in themes},
        "match_mean": mean(allm), "mismatch_mean": mean(allx), "near_mean": mean(alln),
        "separated": sum(1 for a, b in pairs if a > b), "pairs": len(pairs),
        "near_separated": sum(1 for a, b in near_pairs if a > b), "near_pairs": len(near_pairs),
        "match_ok": sum(1 for s in allm if s >= THRESHOLD), "match_n": len(allm),
        "mismatch_flagged": sum(1 for s in allx if s < THRESHOLD), "mismatch_n": len(allx),
        "near_flagged": sum(1 for s in alln if s < THRESHOLD), "near_n": len(alln),
        "weak": weak, "failed": failed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--themes", default="", help="只评这些主题，逗号分隔")
    args = ap.parse_args()

    lib = load_library("paris")
    client = get_client()
    print(f"模型：{LLMConfig.from_env().ollama_model}（后端 {LLMConfig.from_env().backend}）")
    cases = build_cases(lib, [t for t in args.themes.split(",") if t] or None)
    sample = len(_prompt(cases[0].itinerary, lib))
    print(f"{len(cases)} 个用例 × {args.runs} 次；评审提示词 {len(SYSTEM)} 字，示例用户输入 {sample} 字", flush=True)
    try:
        stats = run_eval(client, cases, lib, args.runs)
    except Exception as exc:  # noqa: BLE001
        print(f"跳过：模型不可用（{exc}）")
        return 0
    if stats["failed"] == len(cases) * args.runs:
        print("跳过：每一次评估都失败了，多半是本地模型没开（Ollama）。")
        return 0
    print(f"\n{'主题':<14}" + "".join(f"{KIND_ZH[k]:<16}" for k in KINDS) + "成员占比（对得上 / 近邻 / 冒充）")
    for t, v in stats["per_theme"].items():
        cells = []
        for k in KINDS:
            lo_hi = stats["spread"][t].get(k)
            cells.append(f"{v[k]}" + (f" ({lo_hi[0]}–{lo_hi[1]})" if lo_hi and lo_hi[0] != lo_hi[1] else ""))
        sh = stats["member_share"][t]
        print(f"{t:<14}" + "".join(f"{c:<16}" for c in cells) + f"{sh['match']} / {sh['near']} / {sh['mismatch']}")
    print(f"\n平均分：对得上 {stats['match_mean']}，近邻冒充 {stats['near_mean']}，冒充 {stats['mismatch_mean']}")
    print(f"对得上 > 冒充的主题：{stats['separated']}/{stats['pairs']}；"
          f"对得上 > 近邻冒充的主题：{stats['near_separated']}/{stats['near_pairs']}")
    print(f"对得上 ≥ {THRESHOLD} 分：{stats['match_ok']}/{stats['match_n']}；"
          f"冒充 < {THRESHOLD} 分（会被提醒）：{stats['mismatch_flagged']}/{stats['mismatch_n']}；"
          f"近邻冒充 < {THRESHOLD} 分：{stats['near_flagged']}/{stats['near_n']}；评估失败 {stats['failed']} 次")
    if stats["weak"]:
        print("\n分数不理想的用例（模型给的理由）：")
        for w in stats["weak"]:
            print(f"  {w['theme']} · {KIND_ZH[w['kind']]} · {w['mean']} 分：{w['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
