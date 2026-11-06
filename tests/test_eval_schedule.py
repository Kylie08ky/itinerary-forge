"""Q1 排程评测基线（scripts/eval_schedule.py，规格 07 二点五）。

两件事：
1. 请求集本身合格（20 份、id 不重复、主题都存在、日期星期符合注释）；
2. 排程器的确定性指标和 tests/fixtures/schedule/baseline.json 里的快照一致——改了排程器 / 主题包 / 地点库让指标变了，
   这里会指出是哪份请求的哪个指标。确认新结果就是想要的之后：`python scripts/eval_schedule.py --write-baseline`。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_schedule as E  # noqa: E402

from app.library import load_library  # noqa: E402

LIB = load_library("paris")
BASE, CASES, PROBES = E.load_cases()


@pytest.fixture(scope="module")
def results() -> dict:
    return E.run_all()


def test_the_request_set_has_twenty_distinct_cases_with_real_themes():
    ids = [c["id"] for c in CASES]
    assert len(CASES) == 20 and len(set(ids)) == 20
    kinds = {c["kind"] for c in CASES}
    assert {"single", "pace", "closed-days", "mix", "long"} <= kinds
    used = {t for c in CASES for t in [c["theme"], *(x["id"] for x in c["request"].get("themes", []))]}
    assert used <= set(LIB.themes)
    assert len({c["theme"] for c in CASES if c["kind"] == "single"}) == 8             # 8 个主题各有一份
    probe_themes = {t["id"] for p in PROBES for t in p.get("themes", [])}
    assert probe_themes <= set(LIB.themes)


def test_the_closed_day_cases_really_start_on_a_monday_and_a_tuesday():
    by_id = {c["id"]: c for c in CASES}
    assert date.fromisoformat(by_id["museums-from-monday"]["start"]).weekday() == 0
    assert date.fromisoformat(by_id["art-from-tuesday"]["start"]).weekday() == 1


def test_a_request_is_built_from_the_shared_fields_the_start_and_the_days():
    req = E.build_request(BASE, CASES[0])
    assert req.start_date == date(2026, 11, 4) and req.trip_days == 4
    assert req.arrival_time.hour == 10 and req.arrival_time.minute == 30 and req.departure_time.hour == 18
    ten_days = next(c for c in CASES if c["id"] == "art-10d")
    assert E.build_request(BASE, ten_days).trip_days == 10


def test_the_validation_no_longer_caps_the_number_of_themes():
    # Q3：主题不设「最多 3 个」的上限（规格 07 P15）；4 个、8 个主题都能直接建请求
    for pid, count in (("probe-4-themes", 4), ("probe-8-themes", 8)):
        probe = next(p for p in PROBES if p["id"] == pid)
        no_bypass = {**probe, "bypass_validation": False, "request": {**probe["request"], "themes": probe["themes"]}}
        assert len(E.build_request(BASE, no_bypass).themes) == count


def test_hard_constraints_hold_for_every_request_in_the_set(results):
    for cid, r in results["cases"].items():
        assert r["status"] == "ok", (cid, r)
        assert r["errors"] == 0 and r["error_codes"] == [], (cid, r["error_codes"])
        assert r["repeat_places"] == 0, cid
        assert r["days"] == next(c["days"] for c in CASES if c["id"] == cid)


def test_the_scheduler_survives_every_probe(results):
    for pid, r in results["probes"].items():
        assert r["status"] == "ok", (pid, r)
        assert r["errors"] == 0, (pid, r["error_codes"])
    assert results["probes"]["probe-8-themes"]["themes_covered"] == 8


def test_metrics_match_the_recorded_baseline(results):
    want = json.loads(E.BASELINE.read_text(encoding="utf-8"))
    got = E.deterministic(results)
    diffs = []
    for group in ("cases", "probes"):
        for cid, metrics in want[group].items():
            for key, value in metrics.items():
                if got[group].get(cid, {}).get(key) != value:
                    diffs.append(f"{cid}.{key}: 基线 {value!r} → 现在 {got[group].get(cid, {}).get(key)!r}")
        assert set(got[group]) == set(want[group]), f"{group} 的请求集和基线不一致"
    assert not diffs, "排程指标和基线不一样（确认是想要的变化后用 --write-baseline 重录）：\n" + "\n".join(diffs[:20])


def test_the_run_is_deterministic(results):
    again = E.run_all()
    assert E.deterministic(again) == E.deterministic(results)


def test_summary_and_report_render_and_keep_the_hand_written_findings(results):
    s = E.summarize(results)
    assert s["cases"] == 20 and s["ok"] == 20 and s["total_errors"] == 0
    text = E.render_report(results, "## 解读\n\n我写的话")
    assert "# Q1 排程评测基线" in text and "art-10d" in text and "probe-8-themes" in text
    assert text.endswith("我写的话\n") and E.FINDINGS_MARK in text
    assert E.existing_findings().startswith("## 解读")                                  # 仓库里的报告带着人写的解读
