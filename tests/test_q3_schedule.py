"""Q3 排程（规格 07 二点六「Q3-排程的具体规则」、P15、P16）：

权重真的起作用、每个主题有保底点数、均衡 = 地标优先、每天至少 2 个点、午饭不能跳过。
排程器是纯函数，这里不需要任何假模型；整体指标用 Q1 的请求集（scripts/eval_schedule.py）量。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_schedule as E  # noqa: E402

from app.library import Zone, load_library  # noqa: E402
from app.scheduler import schedule  # noqa: E402
from app.scheduler.engine import is_lunch, theme_targets  # noqa: E402
from app.schemas import TravelRequest  # noqa: E402

LIB = load_library("paris")
ALL = sorted(LIB.themes)
WED = date(2026, 11, 4)
CORE_LANDMARKS = {"eiffel-tower", "louvre", "notre-dame", "pont-alexandre-iii", "orsay", "montmartre-sacre-coeur"}
LUNCH_MAX = {"relaxed": 90, "balanced": 75, "intense": 30}


def request(themes=None, days=4, style="balanced", start=WED, arrive="10:30", leave="18:00") -> TravelRequest:
    end = start + timedelta(days=days - 1)
    body = {"destination": "Paris", "start_date": start, "end_date": end, "budget_level": "medium",
            "travel_style": style, "interests": ["art"],
            "arrival_time": datetime.fromisoformat(f"{start}T{arrive}") if arrive else None,
            "departure_time": datetime.fromisoformat(f"{end}T{leave}") if leave else None}
    if themes:
        body["themes"] = [{"id": t, "weight": w} for t, w in themes]
    return TravelRequest.model_validate(body)


def lunches(draft):
    return [(d, next((s for s in d.stops if is_lunch(s)), None)) for d in draft.days]


def place_ids(draft) -> list[str]:
    return [s.place_id for d in draft.days for s in d.stops if s.kind == "place"]


def count_by_theme(draft, themes) -> dict[str, int]:
    members = {t: {p.id for p, _m in LIB.theme_members(t)} for t in themes}
    ids = place_ids(draft)
    return {t: sum(1 for pid in ids if pid in members[t]) for t in themes}


# --------------------------------------------------------------------------- #
# 1. 每个主题的点数目标
# --------------------------------------------------------------------------- #
def test_targets_follow_the_weights_but_every_chosen_theme_keeps_a_floor():
    got = theme_targets([("art", 5), ("food", 3), ("shopping", 1)], 8, LIB)
    assert got == {"art": 4, "food": 3, "shopping": 1}                    # 权重 1 只沾一点（保底 1），权重 ≥3 保底 2


def test_equal_weights_give_every_theme_at_least_two_when_there_is_room():
    got = theme_targets([("art", 3), ("food", 3), ("shopping", 3)], 8, LIB)
    assert sum(got.values()) == 8 and min(got.values()) >= 2


def test_with_many_themes_each_gets_one_first_and_the_rest_is_shared_equally():
    all8 = [(t, 3) for t in ALL]
    assert theme_targets(all8, 8, LIB) == dict.fromkeys(ALL, 1)
    ten = theme_targets(all8, 10, LIB)
    assert sum(ten.values()) == 10 and min(ten.values()) == 1 and max(ten.values()) == 2
    assert theme_targets(all8, 16, LIB) == dict.fromkeys(ALL, 2)          # 够 2 × 8 个点才保证每个 2 个


def test_more_themes_than_slots_gives_one_each_to_as_many_themes_as_fit_and_never_exceeds_the_slots():
    got = theme_targets([(t, 3) for t in ALL], 5, LIB)
    assert sum(got.values()) == 5 and sorted(got.values()) == [0, 0, 0, 1, 1, 1, 1, 1]


def test_a_single_theme_gets_all_the_slots_and_the_targets_are_deterministic():
    assert theme_targets([("art", 3)], 7, LIB) == {"art": 7}
    sel = [(t, 3) for t in ALL]
    assert theme_targets(sel, 11, LIB) == theme_targets(list(sel), 11, LIB)


# --------------------------------------------------------------------------- #
# 2. 排出来的行程：权重生效、每个主题有点
# --------------------------------------------------------------------------- #
def test_mixed_themes_follow_their_weights_better_than_before():
    draft = schedule(request([("art", 5), ("food", 3), ("photography", 1)]), "art", LIB)
    counts = count_by_theme(draft, ["art", "food", "photography"])
    assert counts["art"] > counts["food"] >= counts["photography"] >= 1, counts


def test_three_equal_themes_each_get_at_least_two_points_in_four_days():
    themes = [("art", 3), ("food", 3), ("shopping", 3)]
    counts = count_by_theme(schedule(request(themes), "art", LIB), [t for t, _w in themes])
    assert min(counts.values()) >= 2, counts


def test_eight_themes_in_five_days_cover_every_theme_and_every_day_has_two_points():
    draft = schedule(request([(t, 3) for t in ALL], days=5), "art", LIB)
    assert min(count_by_theme(draft, ALL).values()) >= 1
    for d in draft.days:
        n = sum(1 for s in d.stops if s.kind == "place")
        if any(LIB.get_place(s.place_id).zone.value == "day_trip" for s in d.stops if s.kind == "place"):
            continue                                                       # 郊区一日游整天只排一处
        floor = 1 if any(s.kind == "logistics" for s in d.stops) else 2          # 到达 / 离开的半天至少 1 个
        assert n >= floor, (d.date, n)


def test_balanced_leads_with_the_citys_landmarks():
    draft = schedule(request([(t, 3) for t in ALL]), "art", LIB)
    assert len(CORE_LANDMARKS & set(place_ids(draft))) >= 3, place_ids(draft)


# --------------------------------------------------------------------------- #
# 3. 每天至少 2 个点（整个 Q1 请求集 + 探针）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def results() -> dict:
    return E.run_all()


def test_no_full_day_is_left_with_a_single_point_in_the_whole_request_set(results):
    for group in ("cases", "probes"):
        for cid, r in results[group].items():
            assert r["status"] == "ok", (cid, r)
            assert r["short_days"] == 0, (cid, r["places_per_day"])


def test_hard_constraints_and_speed_do_not_regress(results):
    for group in ("cases", "probes"):
        for cid, r in results[group].items():
            assert r["errors"] == 0 and r["repeat_places"] == 0, (cid, r["error_codes"])
    summary = E.summarize(results)
    assert summary["median_ms"] < 15 and summary["max_ms"] < 600          # 基线 3 / 110 ms 的 5 倍内


def test_the_weight_gap_goal_for_the_mixes(results):
    # 「总」偏差把同时属于几个主题的点各算一次（基线 0.24）；「主」偏差把每个点只算给权重最高的主题，更接近真实占比
    for cid in ("mix-5-3-1", "mix-art-food-shopping", "mix-3-themes-7d"):
        # P20：白天每天只有 2 个主要点（以前 2–4 个），点数少了粒度变粗，0.15 放宽到 0.25
        assert results["cases"][cid]["theme_weight_gap_primary"] <= 0.25, cid
    assert results["cases"]["mix-5-3-1"]["theme_weight_gap"] <= 0.2           # 总偏差也比基线 0.24 好
    assert min(results["cases"]["mix-art-food-shopping"]["theme_counts"].values()) >= 2
    assert results["probes"]["probe-8-themes"]["themes_covered"] == 8


# --------------------------------------------------------------------------- #
# 4. 午饭不能跳过
# --------------------------------------------------------------------------- #
def test_every_request_in_the_set_has_lunch_on_every_day_that_needs_one(results):
    for group in ("cases", "probes"):
        for cid, r in results[group].items():
            assert r["lunch_missing"] == 0, (cid, r["lunch_days"], r["days"])


@pytest.mark.parametrize("style", ["relaxed", "balanced", "intense"])
@pytest.mark.parametrize("theme", ALL)
def test_lunch_is_on_every_full_day_in_every_pace_and_intense_is_a_quick_one(theme, style):
    draft = schedule(request(days=4, style=style), theme, LIB)
    for d, lunch in lunches(draft):
        if any(s.kind == "logistics" for s in d.stops) or not any(s.kind == "place" for s in d.stops):
            continue                                                       # 到达 / 离开的半天、空天另说
        if any(LIB.get_place(s.place_id).zone.value == "day_trip" for s in d.stops if s.kind == "place"):
            continue                                                       # 郊区一日游：午餐在景区内解决
        assert lunch is not None, (theme, style, d.date)
        assert 11 * 60 + 30 <= lunch.start <= 14 * 60, lunch
        if lunch.kind == "meal":                                           # 当午饭的点（红孩子市场）用它自己的游览时长
            assert 20 <= lunch.duration <= LUNCH_MAX[style], lunch
    if style == "intense":
        assert all(lunch.duration <= 30 for _d, lunch in lunches(draft) if lunch and lunch.kind == "meal")


def test_a_quick_lunch_is_titled_as_such_and_a_normal_one_keeps_its_old_title():
    quick = schedule(request(style="intense"), "art", LIB)
    assert all("简餐" in m.title for _d, m in lunches(quick) if m)
    normal = schedule(request(style="balanced"), "art", LIB)
    assert any(m.title == "午餐（自行选餐厅）" for _d, m in lunches(normal) if m)


def test_no_lunch_is_required_when_you_arrive_late_or_leave_before_noon():
    late = schedule(request(arrive="15:30"), "art", LIB)
    assert lunches(late)[0][1] is None                                      # 15:30 才到：第一天没有午饭
    early = schedule(request(leave="11:00"), "art", LIB)
    assert lunches(early)[-1][1] is None                                    # 11:00 就走：最后一天没有午饭
    in_town = [m for d, m in lunches(late)[1:-1]
               if not any(LIB.places[s.place_id].zone == Zone.day_trip for s in d.stops if s.kind == "place")]
    assert all(m is not None for m in in_town)                               # 中间整天都有（一日游那天不排）
