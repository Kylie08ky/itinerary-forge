"""笔记抽取的真实模型评测（规格 M12 S8）——开发者工具，不在产品界面里。

用法：python scripts/eval_notes.py [--models qwen2.5:7b-instruct,qwen2.5:14b-instruct] [--file 评测集.json]
（默认评测集：tests/fixtures/notes/samples.json）

需要本地 Ollama 在跑；没开就直接说明并退出（退出码 0，算跳过）。评测集是 JSON：每篇有 `text`、
`ids`（应该匹配到的地点 id）、`ambiguous`（应该给出候选让用户选的地点 id 组）、`pending`（应该进「待核实」的库外地点）、
`forbidden`（绝不能出现的地点，用来测提示注入）、`complaint`（应该标「原文像在吐槽」的地点）。
统计：库内召回 / 精确率、库外召回 / 多余项、注入是否被带偏、吐槽线索命中、每篇耗时。
结果只打印，不写文件；真实笔记由用户提供后追加到评测集里。单元测试用假模型测这里的统计（tests/test_eval_notes.py）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.library import PlaceLibrary, load_library  # noqa: E402
from app.llm import LLMClient, LLMConfig, get_client  # noqa: E402
from app.notes.models import ParseResult  # noqa: E402
from app.notes.service import parse_text  # noqa: E402

DEFAULT_FILE = ROOT / "tests" / "fixtures" / "notes" / "samples.json"


def load_notes(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["notes"]


def score(note: dict, result: ParseResult) -> dict:
    """一篇笔记的得分。库内：应有的 id 命中几个；多出来的（不在 ids / ambiguous / forbidden 里的）算多余。"""
    got = {p.place_id for p in result.matched if p.place_id}
    want = set(note.get("ids", []))
    amb = [set(g) for g in note.get("ambiguous", [])]
    amb_hit = sum(
        1 for g in amb if any(p.place_id is None and {c.place_id for c in p.candidates} >= g for p in result.matched))
    amb_ids = set().union(*amb) if amb else set()
    got_pending = {p.name.lower() for p in result.pending}
    want_pending = {n.lower() for n in note.get("pending", [])}
    complaint_want = set(note.get("complaint", []))
    complaint_got = {p.place_id for p in result.matched if p.complaint and p.place_id}
    return {
        "want": len(want) + len(amb), "hit": len(want & got) + amb_hit,
        "extra": sorted(got - want - amb_ids),
        "pending_want": len(want_pending), "pending_hit": len(want_pending & got_pending),
        "pending_extra": sorted(got_pending - want_pending),
        "led_astray": sorted(got & set(note.get("forbidden", []))),
        "complaint_want": len(complaint_want), "complaint_hit": len(complaint_want & complaint_got),
        "complaint_extra": sorted(complaint_got - complaint_want),
        "model_used": result.model_used,
    }


def run_eval(client: LLMClient | None, notes: list[dict], lib: PlaceLibrary) -> dict:
    rows = []
    for n in notes:
        t = time.time()
        result = parse_text(n["text"], lib, client)
        rows.append({"id": n["id"], "seconds": round(time.time() - t, 1), **score(n, result)})

    def total(key: str) -> int:
        return sum(r[key] for r in rows)

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 3) if b else None

    returned = total("hit") + sum(len(r["extra"]) for r in rows)
    return {
        "rows": rows,
        "recall": ratio(total("hit"), total("want")),
        "precision": ratio(total("hit"), returned),
        "pending_recall": ratio(total("pending_hit"), total("pending_want")),
        "pending_extra": sum(len(r["pending_extra"]) for r in rows),
        "led_astray": sum(len(r["led_astray"]) for r in rows),
        "complaint_recall": ratio(total("complaint_hit"), total("complaint_want")),
        "complaint_extra": sum(len(r["complaint_extra"]) for r in rows),
        "model_failed": sum(1 for r in rows if not r["model_used"]),
        "seconds": round(sum(r["seconds"] for r in rows), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="", help="逗号分隔的 Ollama 模型名，并排比较；默认用 .env 里的")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    args = ap.parse_args()

    lib = load_library("paris")
    notes = load_notes(Path(args.file))
    models = [m for m in args.models.split(",") if m] or [LLMConfig.from_env().ollama_model]
    print(f"评测集：{args.file}（{len(notes)} 篇）")
    for model in models:
        os.environ["OLLAMA_MODEL"] = model
        stats = run_eval(get_client(), notes, lib)
        if stats["model_failed"] == len(notes):
            print(f"\n{model}：跳过，模型不可用（多半是本地 Ollama 没开）。")
            continue
        print(f"\n== {model}  共 {stats['seconds']} 秒 ==")
        for r in stats["rows"]:
            print(f"  {r['id']}: 库内 {r['hit']}/{r['want']}，多余 {r['extra'] or '无'}；"
                  f"库外 {r['pending_hit']}/{r['pending_want']}，多余 {r['pending_extra'] or '无'}；"
                  f"被注入带偏 {r['led_astray'] or '无'}；{r['seconds']} 秒")
        print(f"  库内召回 {stats['recall']}，精确率 {stats['precision']}；库外召回 {stats['pending_recall']}，"
              f"库外多余 {stats['pending_extra']}；被注入带偏 {stats['led_astray']}；"
              f"吐槽线索召回 {stats['complaint_recall']}，多标 {stats['complaint_extra']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
