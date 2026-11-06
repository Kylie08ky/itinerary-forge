"""自然语言填表评测脚本（scripts/eval_intake.py）的统计：规则项必须全对；真模型的判断力不在这里测。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_intake as E  # noqa: E402

from app.library import load_library  # noqa: E402

LIB = load_library("paris")
TODAY, SAMPLES = E.load_samples()


class Echo:
    """假模型：什么都不补。"""

    def complete_json(self, *, system, user, schema):
        return json.dumps({"themes": [], "pace": None, "restrictions": []})


def test_every_rule_item_in_the_eval_set_is_read_correctly_without_any_model():
    stats = E.run_eval(None, TODAY, SAMPLES, LIB)
    bad = {r["id"]: r["rule_bad"] for r in stats["rows"] if r["rule_bad"]}
    assert bad == {} and stats["rule_errors"] == 0
    assert stats["led_astray"] == 0 and stats["notes_missing"] == 0


def test_rules_alone_already_cover_the_theme_and_pace_words_in_the_set():
    stats = E.run_eval(Echo(), TODAY, SAMPLES, LIB)
    soft_missing = [(r["id"], r["soft_want"] - r["soft_hit"]) for r in stats["rows"] if r["soft_hit"] < r["soft_want"]]
    assert soft_missing == [("s11", 1)]            # 只有「少走路 / 不吃辣」这类限制要靠模型


def test_score_flags_wrong_rule_items_and_injection():
    sample = {"id": "x", "text": "10月28日出发玩3天", "expect": {"start_date": "2026-10-29", "days": 3},
              "forbidden_must": ["disneyland-paris"]}
    from app.intake_nl import parse_intent

    row = E.score(sample, parse_intent(sample["text"], LIB, None, today=TODAY))
    assert row["rule_bad"] == ["start_date", "end_date"]            # 日期错；评测集没写 end_date，却被读出来了
    assert row["led_astray"] == []
