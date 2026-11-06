"""换城市 / 到达 / 离开那天的接驳文字按交通方式写（规格 12 S3）：火车不写机场，各城市不写别的城市的线路。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.generator import _item
from app.library import load_library
from app.scheduler.models import Stop
from app.schemas import Transport, TransportLeg, TransportMode, TravelRequest

LIB = load_library("paris")
D = date(2026, 12, 22)


def request(out: TransportMode, back: TransportMode) -> TravelRequest:
    t0 = datetime(2026, 12, 22, 10, 0)
    t1 = datetime(2026, 12, 24, 16, 0)
    transport = Transport(
        outbound=TransportLeg(mode=out, depart_at=t0 - timedelta(hours=3), arrive_at=t0),
        inbound=TransportLeg(mode=back, depart_at=t1))
    return TravelRequest(destination="Paris", start_date=D, end_date=D + timedelta(days=2), budget_level="medium",
                         travel_style="balanced", interests=["art"], transport=transport,
                         arrival_time=t0, departure_time=t1)


def stop(edge: str) -> Stop:
    title = "抵达并前往住宿" if edge == "arrive" else "前往车站，去佛罗伦萨"
    return Stop(kind="logistics", title=title, start=600, duration=60, edge=edge)


@pytest.mark.parametrize("mode", [TransportMode.train, TransportMode.high_speed_rail, TransportMode.coach])
@pytest.mark.parametrize("edge", ["arrive", "leave"])
def test_ground_transfer_talks_about_the_station(mode, edge):
    req = request(mode, mode)
    item = _item(stop(edge), LIB, False, req)
    text = item.transport + item.notes + item.location
    assert "车站与住宿之间" in item.transport
    assert "机场" not in text and "航班" not in text and "RER" not in text
    assert "实际车次" in item.notes


def test_flight_transfer_still_talks_about_the_airport():
    req = request(TransportMode.flight, TransportMode.flight)
    item = _item(stop("arrive"), LIB, False, req)
    assert "机场与住宿之间" in item.transport
    assert "实际航班" in item.notes


def test_only_paris_names_the_rer_airport_line():
    from app.generator import _transport
    req = request(TransportMode.flight, TransportMode.flight)
    assert "RER" in _transport(stop("arrive"), False, req)
    assert "RER" not in _transport(stop("arrive"), False, req, city(tier="standard", metro=True))


# --------------------------------------------------------------------------- #
# 相邻两点的通勤方式按城市写
# --------------------------------------------------------------------------- #
def city(**kw):
    from app.cities import load_city
    return load_city("paris").model_copy(update=kw)


def test_full_tier_city_keeps_its_own_words():
    assert city().transit_word("地铁") == "地铁"
    assert city().transit_word("RER/火车") == "RER/火车"
    assert city().transit_word("步行") == "步行"


def test_city_without_a_metro_never_says_metro():
    florence = city(tier="standard", metro=False)
    assert florence.transit_word("地铁") == "公交"
    assert florence.transit_word("RER/火车") != "RER/火车"
    assert florence.transit_word("步行") == "步行"


def test_city_with_a_metro_says_metro_but_not_another_citys_rail():
    rome = city(tier="standard", metro=True)
    assert rome.transit_word("地铁") == "地铁"
    assert "RER" not in rome.transit_word("RER/火车")


def test_auto_built_city_that_is_unsure_says_public_transport():
    assert city(tier="standard").transit_word("地铁") == "公共交通"


def test_generated_item_uses_the_citys_word():
    from app.scheduler.models import Stop
    req = request(TransportMode.train, TransportMode.train)
    s = Stop(kind="place", title="x", start=600, duration=60, travel_min=12, travel_mode="地铁")
    from app.generator import _transport
    florence = city(tier="standard", metro=False)
    assert "地铁" not in _transport(s, False, req, florence)
    assert "公交约 12 分钟" in _transport(s, False, req, florence)


# --------------------------------------------------------------------------- #
# 那天特别要提醒的事（官网写明的提前闭馆 / 改时间）要写进当天的行程项（规格 12 S1）
# --------------------------------------------------------------------------- #
def test_the_date_note_of_a_place_shows_up_on_that_day_only():
    import copy

    from app.library import PlaceLibrary

    lib0 = load_library("paris")
    pid = next(p.id for p in lib0.places.values() if p.category.value == "museum")
    places = copy.deepcopy(lib0.places)
    places[pid] = places[pid].model_copy(update={"date_notes": {"12-24": "官网写明 12 月 24 日只开 9:30–14:00"}})
    lib = PlaceLibrary(city="paris", places=places, themes=lib0.themes)
    req = request(TransportMode.flight, TransportMode.flight)
    s = Stop(kind="place", title="x", place_id=pid, start=600, duration=60, travel_min=5, travel_mode="步行")
    on = _item(s, lib, False, req, day=date(2026, 12, 24))
    off = _item(s, lib, False, req, day=date(2026, 12, 23))
    assert "12 月 24 日只开 9:30–14:00" in on.notes
    assert "只开 9:30" not in off.notes


# --------------------------------------------------------------------------- #
# 圣诞假期的通用提醒（规格 12：餐厅没有数据，只写一条通用提醒）
# --------------------------------------------------------------------------- #
def _risk(start: date) -> list[str]:
    from app.generator import generate_from_theme

    req = TravelRequest(destination="Paris", start_date=start, end_date=start + timedelta(days=2),
                        budget_level="medium", travel_style="balanced", interests=["art"])
    return generate_from_theme(req, sorted(LIB.themes)[0], client=None, library=LIB).itinerary.risk_notes


def test_a_trip_over_christmas_gets_one_holiday_reminder():
    notes = [n for n in _risk(date(2026, 12, 23)) if "圣诞" in n]
    assert len(notes) == 1 and "餐厅" in notes[0] and "官网" in notes[0]


def test_a_trip_outside_the_holiday_gets_none():
    assert not [n for n in _risk(date(2026, 10, 3)) if "圣诞" in n]


def test_an_unchecked_place_on_christmas_days_says_to_check_the_official_site():
    import copy

    from app.library import PlaceLibrary

    lib0 = load_library("paris")
    pid = next(p.id for p in lib0.places.values() if p.category.value == "museum")
    places = copy.deepcopy(lib0.places)
    places[pid] = places[pid].model_copy(update={"hours_verified": False})
    lib = PlaceLibrary(city="paris", places=places, themes=lib0.themes)
    req = request(TransportMode.flight, TransportMode.flight)
    s = Stop(kind="place", title="x", place_id=pid, start=600, duration=60, travel_min=5, travel_mode="步行")
    assert "圣诞" in _item(s, lib, False, req, day=date(2026, 12, 25)).notes
    assert "官网" in _item(s, lib, False, req, day=date(2026, 12, 25)).notes
    assert "圣诞" not in _item(s, lib, False, req, day=date(2026, 12, 23)).notes
    checked = places[pid].model_copy(update={"hours_verified": True})
    lib2 = PlaceLibrary(city="paris", places={**places, pid: checked}, themes=lib0.themes)
    assert "圣诞" not in _item(s, lib2, False, req, day=date(2026, 12, 25)).notes
