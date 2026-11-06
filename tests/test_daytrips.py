"""周边一日游（规格 08 3.4）：城际时间表、目的地列表、「排一天」的需求。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.cities import city_of, supported_cities
from app.daytrips import (
    DayTripError,
    PairTable,
    build_day_request,
    destinations_for,
    load_pairs,
    origin_ids,
)

TABLE = {
    "origins": {
        "stuttgart": {"name_local": "Stuttgart", "name_zh": "斯图加特", "country": "DE",
                      "station": {"name": "Stuttgart Hbf", "name_zh": "斯图加特中央火车站", "lat": 48.784,
                                  "lng": 9.182}},
    },
    "pairs": [
        {"origin": "stuttgart", "destination": "paris", "mode": "train", "minutes": 53, "transfers": 0,
         "station": {"name": "Paris Gare de l'Est", "name_zh": "巴黎东站", "lat": 48.876, "lng": 2.359},
         "last_return": "22:48", "source": "Transitous（DELFI 开放时刻表）", "source_url": "https://transitous.org",
         "checked_at": "2026-10-25", "sample_date": "2026-12-12"},
        {"origin": "stuttgart", "destination": "nowhere", "mode": "train", "minutes": 20, "transfers": 0,
         "station": {"name": "X", "name_zh": "X", "lat": 1, "lng": 1}, "last_return": "22:00",
         "source": "s", "source_url": "https://transitous.org", "checked_at": "2026-10-25",
         "sample_date": "2026-12-12"},
    ],
}


@pytest.fixture
def table() -> PairTable:
    return PairTable.model_validate(TABLE)


# --------------------------------------------------------------------------- #
# 时间表
# --------------------------------------------------------------------------- #
def test_a_pair_needs_its_source_and_check_date_and_sane_numbers():
    bad = {**TABLE["pairs"][0], "source": ""}
    with pytest.raises(ValidationError):
        PairTable.model_validate({**TABLE, "pairs": [bad]})
    with pytest.raises(ValidationError):
        PairTable.model_validate({**TABLE, "pairs": [{**TABLE["pairs"][0], "minutes": 0}]})
    with pytest.raises(ValidationError):
        PairTable.model_validate({**TABLE, "pairs": [{**TABLE["pairs"][0], "last_return": "25:99"}]})
    with pytest.raises(ValidationError, match="origin"):
        PairTable.model_validate({**TABLE, "pairs": [{**TABLE["pairs"][0], "origin": "berlin"}]})


def test_only_destinations_that_are_supported_cities_are_listed(table):
    got = destinations_for("stuttgart", table)
    assert [d.city.id for d in got] == ["paris"]                     # nowhere 不是支持的城市：不出现
    assert got[0].pair.minutes == 53 and got[0].city.name_zh == "巴黎"
    with pytest.raises(DayTripError, match="出发地"):
        destinations_for("berlin", table)


def test_the_real_table_has_checked_pairs_for_both_origins_and_every_destination_is_a_light_city():
    t = load_pairs()
    assert set(origin_ids(t)) == {"stuttgart", "aachen"}
    for oid in origin_ids(t):
        dests = destinations_for(oid, t)
        assert len(dests) >= 7, oid
        for d in dests:
            assert d.city.tier == "light" and d.city.currency == "EUR"
            assert 10 <= d.pair.minutes <= 130, (oid, d.city.id)
            assert d.pair.source_url.startswith("https://") and d.pair.checked_at.year == 2026
    assert {d.city.id for d in destinations_for("stuttgart", t)} >= {
        "tuebingen", "heidelberg", "ludwigsburg", "esslingen", "ulm", "baden-baden", "strasbourg"}


def test_destination_cities_are_found_by_chinese_german_and_english_names():
    for text in ("Tübingen", "tuebingen", "蒂宾根", "Tubingen"):
        assert city_of(text) is not None and city_of(text).id == "tuebingen", text
    assert "tuebingen" in {c.id for c in supported_cities()}


# --------------------------------------------------------------------------- #
# 排一天
# --------------------------------------------------------------------------- #
def test_the_day_request_is_a_one_day_trip_with_times_taken_from_the_timetable():
    req = build_day_request("stuttgart", "tuebingen", date(2026, 12, 12), depart="09:00", back="19:30")
    assert req.destination == "tuebingen" and req.start_date == req.end_date == date(2026, 12, 12)
    assert req.day_trip_from == "stuttgart"
    pair = next(d.pair for d in destinations_for("stuttgart") if d.city.id == "tuebingen")
    assert req.arrival_time == datetime(2026, 12, 12, 9, 0) + timedelta(minutes=pair.minutes)
    assert req.departure_time == datetime(2026, 12, 12, 19, 30)
    assert req.transport.outbound.mode == "train" and req.transport.inbound.mode == "train"
    assert req.transport.outbound.from_place == "Stuttgart Hbf"
    assert req.travel_style == "balanced"


def test_the_default_return_is_evening_in_season_afternoon_otherwise_and_never_after_the_last_train():
    in_dec = build_day_request("stuttgart", "heidelberg", date(2026, 12, 12))
    assert in_dec.departure_time.strftime("%H:%M") == "21:00"                  # 有圣诞市场：留到天黑后
    july = build_day_request("stuttgart", "heidelberg", date(2026, 7, 4))
    assert july.departure_time.strftime("%H:%M") == "18:30"
    strasbourg = build_day_request("stuttgart", "strasbourg", date(2026, 12, 12))
    last = next(d.pair.last_return for d in destinations_for("stuttgart") if d.city.id == "strasbourg")
    assert strasbourg.departure_time.strftime("%H:%M") == min("21:00", last)
    with pytest.raises(DayTripError, match="末班"):
        build_day_request("stuttgart", "strasbourg", date(2026, 12, 12), back="23:30")


def test_a_day_request_refuses_unknown_pairs_late_returns_and_a_return_before_arrival():
    with pytest.raises(DayTripError, match="目的地"):
        build_day_request("stuttgart", "paris", date(2026, 12, 12))
    with pytest.raises(DayTripError, match="返程"):
        build_day_request("stuttgart", "tuebingen", date(2026, 12, 12), depart="09:00", back="09:30")


# --------------------------------------------------------------------------- #
# 显示名和出境提醒（规格 08 3.6）
# --------------------------------------------------------------------------- #
def test_the_plan_gets_display_names_in_chinese_and_the_local_language():
    from app.daytrips import day_trip_info

    req = build_day_request("stuttgart", "heidelberg", date(2026, 12, 12))
    assert day_trip_info(req) == {"city": "海德堡 Heidelberg", "origin": "斯图加特 Stuttgart"}
    assert day_trip_info(build_day_request("aachen", "liege", date(2026, 12, 12)))["origin"] == "亚琛 Aachen"


def test_only_a_trip_across_a_border_reminds_about_the_passport():
    from app.daytrips import crosses_border
    from app.generator import generate_from_theme

    home = build_day_request("stuttgart", "heidelberg", date(2026, 12, 12))
    abroad = build_day_request("stuttgart", "strasbourg", date(2026, 12, 12))
    assert not crosses_border(home) and crosses_border(abroad)
    assert crosses_border(build_day_request("aachen", "maastricht", date(2026, 12, 12)))
    assert not crosses_border(build_day_request("aachen", "koeln", date(2026, 12, 12)))
    for req, expect in ((home, False), (abroad, True)):
        first = generate_from_theme(req, "daytrip", client=None).itinerary.days[0].items[0]
        assert ("护照" in first.notes) is expect, (req.destination, first.notes)
