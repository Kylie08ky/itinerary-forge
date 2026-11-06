"""Judge 评测脚本的流程（scripts/eval_judge.py）：用例构造、统计。真模型的判断力不在这里测。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_judge as E  # noqa: E402

from app.judge import _prompt  # noqa: E402
from app.library import load_library  # noqa: E402
from app.llm import LLMError  # noqa: E402

LIB = load_library("paris")


@pytest.fixture(scope="module")
def cases():
    return E.build_cases(LIB)


def kinds_of(cases, tid):
    return sorted(c.kind for c in cases if c.theme_id == tid)


def test_every_theme_gets_a_match_a_mismatch_and_a_near_miss_case(cases):
    for tid in LIB.themes:
        near, opp = E.nearest_theme(LIB, tid), E.opposite_theme(LIB, tid)
        want = ["match", "mismatch"] + ([] if near == opp else ["near"])
        assert kinds_of(cases, tid) == sorted(want), tid
    assert len(cases) >= 2 * len(LIB.themes)


def test_a_fake_case_uses_another_themes_places_but_claims_the_judged_theme(cases):
    for c in cases:
        gen = c.itinerary.generation
        assert gen.theme_id == c.theme_id and gen.themes == [c.theme_id]
        if c.kind == "match":
            assert c.source_theme == c.theme_id
        else:
            assert c.source_theme != c.theme_id
        want = E.opposite_theme(LIB, c.theme_id) if c.kind == "mismatch" else E.nearest_theme(LIB, c.theme_id)
        if c.kind != "match":
            assert c.source_theme == want


def test_the_opposite_and_nearest_themes_are_deterministic_and_never_the_theme_itself():
    for tid in LIB.themes:
        for pick in (E.opposite_theme, E.nearest_theme):
            other = pick(LIB, tid)
            assert other != tid and other == pick(LIB, tid)


def test_opposite_overlaps_least_and_nearest_overlaps_most():
    for tid in LIB.themes:
        overlaps = {o: E._overlap(LIB, tid, o) for o in LIB.themes if o != tid}
        assert overlaps[E.opposite_theme(LIB, tid)] == min(overlaps.values())
        assert overlaps[E.nearest_theme(LIB, tid)] == max(overlaps.values())


def test_the_member_share_is_computed_without_a_model_and_separates_the_cases(cases):
    for tid in LIB.themes:
        by = {c.kind: c.member_share for c in cases if c.theme_id == tid}
        assert 0 <= by["mismatch"] <= by["match"] <= 1
        assert by["match"] >= 0.6, f"{tid} 的行程里只有 {by['match']:.0%} 是本主题的地点"
        assert by["mismatch"] <= 0.5, tid


def test_member_share_of_an_itinerary_without_places_is_zero(cases):
    it = cases[0].itinerary
    empty = it.model_copy(update={"days": [d.model_copy(update={"items": [i.model_copy(update={"place_id": None})
                                                                           for i in d.items]}) for d in it.days]})
    assert E.member_share(LIB, empty, cases[0].theme_id) == 0.0


class Oracle:
    """假评委：认得每个用例的提示词，对得上给 5、近邻冒充给 2、冒充给 1（flip 反过来）。"""

    def __init__(self, cases, flip=False):
        self.by_user = {_prompt(c.itinerary, LIB): c for c in cases}
        self.flip = flip

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        case = self.by_user[user]
        score = {"match": 5, "near": 2, "mismatch": 1}[case.kind]
        if self.flip:
            score = 6 - score
        return json.dumps({"score": score, "reason": f"假评委：{case.kind}"}, ensure_ascii=False)


def test_a_perfect_judge_separates_every_theme_including_near_misses(cases):
    stats = E.run_eval(Oracle(cases), cases, LIB)
    n = len(LIB.themes)
    assert stats["match_mean"] == 5 and stats["mismatch_mean"] == 1 and stats["near_mean"] == 2
    assert stats["separated"] == stats["pairs"] == n and stats["near_separated"] == stats["near_pairs"] > 0
    assert stats["match_ok"] == stats["match_n"] and stats["mismatch_flagged"] == stats["mismatch_n"]
    assert stats["near_flagged"] == stats["near_n"] and stats["failed"] == 0 and stats["weak"] == []
    assert set(stats["per_theme"]) == set(LIB.themes)


def test_a_backwards_judge_shows_up_as_zero_separation_and_lists_weak_cases(cases):
    stats = E.run_eval(Oracle(cases, flip=True), cases, LIB)
    assert stats["separated"] == 0 and stats["near_separated"] == 0
    assert stats["match_ok"] == 0 and stats["mismatch_flagged"] == 0
    kinds = {w["kind"] for w in stats["weak"]}
    assert kinds == {"match", "near", "mismatch"} and all(w["reason"].startswith("假评委") for w in stats["weak"])


def test_runs_multiply_the_samples_and_the_spread_is_reported(cases):
    stats = E.run_eval(Oracle(cases), cases, LIB, runs=3)
    assert stats["match_n"] == 3 * len(LIB.themes) == stats["mismatch_n"]
    assert all(lo == hi for sp in stats["spread"].values() for lo, hi in sp.values())


def test_an_unstable_judge_shows_a_min_max_spread(cases):
    class Wobbly(Oracle):
        n = 0

        def complete_json(self, *, system, user, schema):
            Wobbly.n += 1
            case = self.by_user[user]
            score = 5 if (case.kind == "match" and Wobbly.n % 2) else {"match": 3, "near": 2, "mismatch": 1}[case.kind]
            return json.dumps({"score": score, "reason": "x"})

    stats = E.run_eval(Wobbly(cases), cases, LIB, runs=2)
    assert any(lo != hi for sp in stats["spread"].values() for lo, hi in sp.values())


def test_failures_are_counted_separately_and_do_not_distort_the_means(cases):
    class Flaky(Oracle):
        n = 0

        def complete_json(self, *, system, user, schema):
            Flaky.n += 1
            if Flaky.n % 2 == 0:
                raise LLMError("掉线")
            return super().complete_json(system=system, user=user, schema=schema)

    stats = E.run_eval(Flaky(cases), cases, LIB)
    assert stats["failed"] > 0
    assert stats["match_mean"] in (None, 5) and stats["mismatch_mean"] in (None, 1)


def test_a_dead_model_means_every_evaluation_failed(cases):
    class Dead:
        def complete_json(self, *, system, user, schema):
            raise LLMError("没开")

    stats = E.run_eval(Dead(), cases, LIB)
    assert stats["failed"] == len(cases) and stats["match_mean"] is None and stats["pairs"] == 0
