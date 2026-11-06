"""自然语言填表的真实模型评测（规格 M01 S13）——开发者工具，不在产品界面里。

用法：python scripts/eval_intake.py [--models qwen2.5:7b-instruct,qwen2.5:14b-instruct] [--file 评测集.json]
      [--rules-only]

评测集（tests/fixtures/intake/samples.json）：每句有 `expect`——这句话**应该**读出来的项；没写的项表示「不该读出来」。
统计两类：
- 规则项（日期 / 天数 / 人数 / 预算 / 币种 / 必去 / 不去）：必须全对，错一个就是 bug（这些不靠模型）；
- 理解项（主题 / 节奏）：看召回和多读；模型只补这一层。
另外检查注入句子没有改变任何字段（`forbidden_must`）。结果只打印；换模型 / 改提示词 / 改规则后重跑对比。
需要本地 Ollama；没开就只跑规则（模型一栏显示「没开」）。单元测试用假模型测统计（tests/test_eval_intake.py）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.intake_nl import Intent, parse_intent  # noqa: E402
from app.library import PlaceLibrary, load_library  # noqa: E402
from app.llm import LLMClient, LLMConfig, get_client  # noqa: E402

DEFAULT_FILE = ROOT / "tests" / "fixtures" / "intake" / "samples.json"
RULE_KEYS = ("start_date", "end_date", "days", "travelers_adults", "kids", "budget_cap", "currency", "must_go_ids",
             "avoid_ids")
SOFT_KEYS = ("theme_ids", "travel_style", "restrictions")


def load_samples(path: Path = DEFAULT_FILE) -> tuple[date, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return date.fromisoformat(data["today"]), data["samples"]


def observed(intent: Intent) -> dict:
    """把 Intent 压成和 expect 同样的形状，方便逐项对比。"""
    f = intent.fields
    out = {k: f[k] for k in ("start_date", "end_date", "days", "budget_cap", "currency", "must_go_ids", "avoid_ids",
                             "theme_ids", "travel_style", "restrictions") if k in f}
    if "travelers" in f:
        out["travelers_adults"] = sum(1 for t in f["travelers"] if t["age"] >= 18)
        kids = sorted(t["age"] for t in f["travelers"] if t["age"] < 18)
        if kids:
            out["kids"] = kids
    return out


def _hit(got, want) -> bool:
    if isinstance(want, list):
        if want and isinstance(want[0], str) and got and any(len(g) > 3 for g in got):
            # 限制（一整句话）：期望的每一段都要出现在某一条里
            return all(any(w in g for g in got) for w in want)
        return set(got or []) >= set(want)
    return got == want


def score(sample: dict, intent: Intent) -> dict:
    expect, got = sample.get("expect", {}), observed(intent)
    rule_bad = [k for k in RULE_KEYS if got.get(k) != expect.get(k)]
    soft_want = {k: expect[k] for k in SOFT_KEYS if k in expect}
    soft_hit = sum(1 for k, v in soft_want.items() if _hit(got.get(k), v))
    soft_extra = [k for k in SOFT_KEYS if k in got and k not in expect]
    note = expect.get("note_contains")
    return {
        "rule_bad": rule_bad, "soft_want": len(soft_want), "soft_hit": soft_hit, "soft_extra": soft_extra,
        "note_ok": note is None or any(note in n for n in intent.notes),
        "led_astray": [p for p in sample.get("forbidden_must", []) if p in got.get("must_go_ids", [])],
        "model_used": intent.model_used,
    }


def run_eval(client: LLMClient | None, today: date, samples: list[dict], lib: PlaceLibrary) -> dict:
    rows = []
    for s in samples:
        t = time.time()
        intent = parse_intent(s["text"], lib, client, today=today)
        rows.append({"id": s["id"], "seconds": round(time.time() - t, 1), **score(s, intent)})
    soft_want = sum(r["soft_want"] for r in rows)
    return {
        "rows": rows,
        "rule_errors": sum(len(r["rule_bad"]) for r in rows),
        "soft_recall": round(sum(r["soft_hit"] for r in rows) / soft_want, 3) if soft_want else None,
        "soft_extra": sum(len(r["soft_extra"]) for r in rows),
        "notes_missing": sum(1 for r in rows if not r["note_ok"]),
        "led_astray": sum(len(r["led_astray"]) for r in rows),
        "model_failed": sum(1 for r in rows if not r["model_used"]),
        "seconds": round(sum(r["seconds"] for r in rows), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="", help="逗号分隔的 Ollama 模型名，并排比较；默认用 .env 里的")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--rules-only", action="store_true", help="不调模型，只看规则")
    args = ap.parse_args()

    lib = load_library("paris")
    today, samples = load_samples(Path(args.file))
    print(f"评测集：{args.file}（{len(samples)} 句，今天固定 {today}）")
    models = [m for m in args.models.split(",") if m] or [LLMConfig.from_env().ollama_model]
    runs = [None] if args.rules_only else models
    for model in runs:
        client = None
        if model is not None:
            os.environ["OLLAMA_MODEL"] = model
            client = get_client()
        stats = run_eval(client, today, samples, lib)
        label = model or "只用规则"
        if model is not None and stats["model_failed"] == len(samples):
            label += "（模型没开，只有规则）"
        print(f"\n== {label}  共 {stats['seconds']} 秒 ==")
        for r in stats["rows"]:
            bad = f"规则项错：{r['rule_bad']}" if r["rule_bad"] else "规则项全对"
            soft = f"理解项 {r['soft_hit']}/{r['soft_want']}" + (f"，多读 {r['soft_extra']}" if r["soft_extra"] else "")
            print(f"  {r['id']}: {bad}；{soft}；{r['seconds']} 秒" + ("" if r["note_ok"] else "；缺提醒"))
        print(f"  规则项错误 {stats['rule_errors']}；理解项召回 {stats['soft_recall']}，多读 {stats['soft_extra']}；"
              f"缺提醒 {stats['notes_missing']}；被注入带偏 {stats['led_astray']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
