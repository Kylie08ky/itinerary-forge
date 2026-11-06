"""多城市旅行（规格 11）：每一段是一份单城市行程，换城市那天两段共用；交通没填就估算并标注。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from city_fixtures import write_small_city

from app.cities import load_city, reload_cities
from app.llm import LLMError
from app.trips import MAX_LEGS, TripError, TripSpec, build_plan, estimate_transfer, leg_dates

TODAY = date(2026, 10, 31)


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture(autouse=True)
def second_city(tmp_path, monkeypatch):
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path / "generated"))
    reload_cities()
    write_small_city("lyon", "里昂", "Lyon", (45.7640, 4.8357), "FR", "Europe/Paris", "EUR", TODAY)
    write_small_city("brussels", "布鲁塞尔", "Brussels", (50.8503, 4.3517), "BE", "Europe/Brussels", "EUR", TODAY)
    yield
    reload_cities()


def leg(dest: str, start: str, end: str, **more) -> dict:
    return {"destination": dest, "start_date": start, "end_date": end, **more}


PARIS = leg("Paris", "2026-12-12", "2026-12-14")
LYON = leg("里昂", "2026-12-14", "2026-12-16")


def spec(**over) -> dict:
    base = {"legs": [PARIS, LYON],
            "outbound": {"mode": "flight", "depart_at": "2026-12-12T06:00", "arrive_at": "2026-12-12T09:30"},
            "inbound": {"mode": "flight", "depart_at": "2026-12-16T18:00"},
            "common": {"budget_level": "medium", "travel_style": "balanced", "interests": ["均衡"]}}
    return {**base, **over}


# --------------------------------------------------------------------------- #
# 纯规则
# --------------------------------------------------------------------------- #
def test_the_day_of_changing_city_belongs_to_both_legs():
    s = TripSpec.model_validate(spec())
    assert leg_dates(s) == [(date(2026, 12, 12), date(2026, 12, 14)), (date(2026, 12, 14), date(2026, 12, 16))]


def test_a_leg_may_be_a_single_day_and_a_night_train_starts_the_next_city_the_next_day():
    one_day = leg("Paris", "2026-12-12", "2026-12-12")
    TripSpec.model_validate(spec(legs=[one_day, leg("里昂", "2026-12-12", "2026-12-16")]))
    night = TripSpec.model_validate(spec(legs=[PARIS, leg("里昂", "2026-12-15", "2026-12-17")]))
    _, transfers, payloads = build_plan(night)
    tr, estimated = transfers[0]
    assert estimated and (tr.depart_at, tr.arrive_at) == (datetime(2026, 12, 14, 20, 0), datetime(2026, 12, 15, 8, 0))
    assert payloads[1]["transport"]["outbound"]["arrive_at"].startswith("2026-12-15T08:00")


@pytest.mark.parametrize("bad, text", [
    ({"legs": [PARIS]}, "2–8 座"),
    ({"legs": [PARIS] * (MAX_LEGS + 1)}, "2–8 座"),
    ({"legs": [leg("Paris", "2026-12-14", "2026-12-12"), LYON]}, "离开日期不能早于开始日期"),
    ({"legs": [leg("Paris", "2026-12-01", "2026-12-20"), leg("里昂", "2026-12-20", "2026-12-22")]}, "最多玩 14 天"),
    ({"legs": [PARIS, leg("里昂", "2026-12-17", "2026-12-18")]}, "当天或第二天开始"),
    ({"legs": [PARIS, leg("里昂", "2026-12-13", "2026-12-18")]}, "当天或第二天开始"),
    ({"legs": [leg("Paris", "2026-12-12", "2026-12-12"), leg("里昂", "2026-12-12", "2026-12-12")]}, "至少要 2 天"),
])
def test_the_number_of_cities_and_dates_per_leg_are_limited(bad, text):
    with pytest.raises(ValueError, match=text):
        TripSpec.model_validate(spec(**bad))


def test_a_city_without_data_or_two_equal_neighbours_are_refused_with_a_plain_message():
    with pytest.raises(TripError, match="还没有数据"):
        build_plan(TripSpec.model_validate(spec(legs=[PARIS, leg("Nowhere", "2026-12-14", "2026-12-16")])))
    with pytest.raises(TripError, match="同一座城市"):
        build_plan(TripSpec.model_validate(spec(legs=[PARIS, leg("巴黎", "2026-12-14", "2026-12-16")])))


def test_a_missing_transfer_is_estimated_by_distance_and_marked_as_an_estimate():
    cities, transfers, payloads = build_plan(TripSpec.model_validate(spec()))
    tr, estimated = transfers[0]
    assert estimated is True and tr.mode.value == "high_speed_rail"                   # 巴黎 → 里昂约 390 公里
    assert tr.depart_at == datetime(2026, 12, 14, 10, 0) and tr.arrive_at.date() == date(2026, 12, 14)
    # 每一段的去程 / 返程：第一段去程是整趟的去程，返程是换城市；第二段去程是换城市，返程是整趟的返程
    assert payloads[0]["transport"]["inbound"]["depart_at"] == "2026-12-14T10:00:00"
    assert payloads[1]["transport"]["outbound"]["arrive_at"].startswith("2026-12-14T")
    assert payloads[1]["transport"]["inbound"]["depart_at"] == "2026-12-16T18:00:00"
    assert (payloads[0]["start_date"], payloads[0]["end_date"]) == ("2026-12-12", "2026-12-14")
    assert (payloads[1]["start_date"], payloads[1]["end_date"]) == ("2026-12-14", "2026-12-16")


def test_a_city_visited_for_a_single_day_is_left_after_arriving():
    legs = [PARIS, leg("里昂", "2026-12-14", "2026-12-14"), leg("布鲁塞尔", "2026-12-14", "2026-12-16")]
    _, transfers, _ = build_plan(TripSpec.model_validate(spec(legs=legs)))
    (first, _), (second, _) = transfers
    assert second.depart_at >= first.arrive_at + timedelta(hours=3) and second.arrive_at.date() == date(2026, 12, 14)


def test_a_filled_transfer_is_used_as_given_and_must_be_on_the_changing_day():
    ok = spec(transfers=[{"mode": "high_speed_rail", "depart_at": "2026-12-14T11:03", "arrive_at": "2026-12-14T13:05",
                          "number": "TGV 6603"}])
    _, transfers, payloads = build_plan(TripSpec.model_validate(ok))
    assert transfers[0][1] is False and payloads[0]["transport"]["inbound"]["number"] == "TGV 6603"
    wrong = spec(transfers=[{"mode": "high_speed_rail", "depart_at": "2026-12-15T11:03",
                             "arrive_at": "2026-12-15T13:05"}])
    with pytest.raises(TripError, match="2026-12-14 当天"):
        build_plan(TripSpec.model_validate(wrong))


def test_a_distance_that_cannot_be_covered_in_the_same_day_is_refused():
    paris, lyon = load_city("paris"), load_city("lyon")
    far = lyon.model_copy(update={"center": (-33.9, 151.2)})                          # 悉尼
    with pytest.raises(TripError, match="当天到不了"):
        estimate_transfer(paris, far, date(2026, 12, 14))
