"""跑同一批 persona 过不同 Ollama 模型，对比生成质量。

    python -m scripts.eval_models qwen2.5:7b-instruct qwen2.5:14b-instruct
    python -m scripts.eval_models --repair --personas persona-01-museum-foodie qwen2.5:14b-instruct

对每个 (模型, persona) 记录：生成耗时、尝试次数、最终 error / warning 数与代码。
不追求严格评测，只给一个横向感觉。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.intake import run_intake
from app.llm import LLMConfig, OllamaClient
from app.planner import generate_itinerary
from app.revision import auto_repair
from app.validator import validate_itinerary

ROOT = Path(__file__).resolve().parents[1]
PERSONA_DIR = ROOT / "data" / "personas"
DEFAULT_PERSONAS = [
    "persona-01-museum-foodie",
    "persona-02-family-relaxed",
    "persona-03-photographer-intense",
]


def _load(name: str) -> dict:
    raw = json.loads((PERSONA_DIR / f"{name}.json").read_text(encoding="utf-8"))
    raw.pop("_meta", None)
    return raw


def run_one(model: str, persona: str, *, repair: bool) -> dict:
    request = run_intake(_load(persona)).request
    client = OllamaClient(LLMConfig(**{**LLMConfig.from_env().__dict__, "ollama_model": model}))

    t0 = time.perf_counter()
    try:
        plan = generate_itinerary(request, client=client, max_retries=2)
        itinerary, attempts, note = plan.itinerary, plan.attempts, ""
        if repair:
            rep = auto_repair(itinerary, request, client=client, max_rounds=2)
            itinerary = rep.itinerary
            note = f"+{len(rep.rounds)}轮修复"
    except Exception as exc:  # noqa: BLE001
        return {"model": model, "persona": persona, "secs": time.perf_counter() - t0,
                "attempts": "-", "errors": "GEN FAIL", "warnings": str(exc)[:60], "note": ""}

    report = validate_itinerary(itinerary, request)
    return {
        "model": model, "persona": persona,
        "secs": round(time.perf_counter() - t0),
        "attempts": attempts,
        "errors": ",".join(sorted({f.code for f in report.errors})) or "—",
        "warnings": ",".join(sorted({f.code for f in report.warnings})) or "—",
        "note": note,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+", help="Ollama 模型名，如 qwen2.5:7b-instruct")
    ap.add_argument("--personas", nargs="+", default=DEFAULT_PERSONAS)
    ap.add_argument("--repair", action="store_true", help="生成后跑自动修复循环再统计")
    args = ap.parse_args()

    rows = []
    for model in args.models:
        for persona in args.personas:
            print(f"… {model} / {persona}", flush=True)
            rows.append(run_one(model, persona, repair=args.repair))

    w = {k: max(len(str(r[k])) for r in rows + [{"model": "model", "persona": "persona",
         "secs": "secs", "attempts": "try", "errors": "errors", "warnings": "warnings", "note": "note"}])
         for k in ("model", "persona", "secs", "attempts", "errors", "warnings", "note")}
    hdr = ("model", "persona", "secs", "attempts", "errors", "warnings", "note")
    print("\n" + " | ".join(h.ljust(w[h]) for h in hdr))
    print("-+-".join("-" * w[h] for h in hdr))
    for r in rows:
        print(" | ".join(str(r[h]).ljust(w[h]) for h in hdr))


if __name__ == "__main__":
    main()
