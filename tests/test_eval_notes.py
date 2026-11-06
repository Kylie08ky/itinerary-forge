"""笔记评测脚本（scripts/eval_notes.py）的统计：用回放的假模型，真模型的判断力不在这里测。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_notes as E  # noqa: E402

from app.library import load_library  # noqa: E402

LIB = load_library("paris")
NOTES = E.load_notes(E.DEFAULT_FILE)


class Replay:
    def __init__(self, by_text):
        self.by_text = by_text

    def complete_json(self, *, system, user, schema):
        for text, names in self.by_text.items():
            if text[:30] in user:
                return json.dumps({"places": [{"name": n} for n in names]}, ensure_ascii=False)
        return '{"places": []}'


def recorded():
    return Replay({n["text"]: n["model_names"] for n in NOTES})


def test_the_recorded_model_output_scores_well_on_the_samples():
    stats = E.run_eval(recorded(), NOTES, LIB)
    assert stats["recall"] == 1.0 and stats["precision"] == 1.0
    assert stats["pending_recall"] == 1.0 and stats["pending_extra"] == 0 and stats["led_astray"] == 0
    assert stats["complaint_recall"] == 1.0 and stats["complaint_extra"] == 0
    assert stats["model_failed"] == 0 and len(stats["rows"]) == len(NOTES)


def test_without_a_model_the_library_places_still_count_but_pending_is_missed():
    stats = E.run_eval(None, NOTES, LIB)
    assert stats["recall"] >= 0.8 and stats["pending_recall"] == 0.0 and stats["model_failed"] == len(NOTES)


def test_score_counts_extra_and_led_astray_places():
    note = {"id": "x", "text": "卢浮宫真大", "ids": ["orsay"], "forbidden": ["louvre"], "pending": [], "ambiguous": []}
    row = E.score(note, E.parse_text(note["text"], LIB, None))
    assert row["hit"] == 0 and row["extra"] == ["louvre"] and row["led_astray"] == ["louvre"]
