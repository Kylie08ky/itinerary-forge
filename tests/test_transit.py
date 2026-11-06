"""实时查公共交通（规格 08 3.7）：分类、时间换算、挑方案、DB 链接。用录下来的真实 Transitous 回复，不联网。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.transit import (
    DB_SEARCH_URL,
    TransitError,
    central_europe_offset,
    choose_options,
    clean_address_query,
    covered_by_deutschlandticket,
    db_query_text,
    from_utc_iso,
    haversine_km,
    latest_return,
    parse_address,
    rank_addresses,
    to_utc_iso,
)
from tests.transit_fake import broken_transitous, fake_transitous, fixture

NINE = datetime(2026, 12, 12, 9, 0)


def its(name: str) -> list[dict]:
    return fixture(name)["itineraries"]


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
def test_central_european_time_follows_the_summer_time_rule_without_tzdata():
    assert central_europe_offset(date(2026, 12, 12)) == 1
    assert central_europe_offset(date(2026, 7, 4)) == 2
    assert central_europe_offset(date(2026, 3, 28)) == 1 and central_europe_offset(date(2026, 3, 29)) == 2
    assert central_europe_offset(date(2026, 10, 24)) == 2 and central_europe_offset(date(2026, 10, 25)) == 1


def test_local_time_round_trips_through_utc():
    assert to_utc_iso(datetime(2026, 12, 12, 9, 0)) == "2026-12-12T08:00:00Z"
    assert to_utc_iso(datetime(2026, 7, 4, 9, 0)) == "2026-07-04T07:00:00Z"
    assert from_utc_iso("2026-12-12T08:00:00Z") == datetime(2026, 12, 12, 9, 0)
    assert from_utc_iso("2026-07-04T22:30:00Z") == datetime(2026, 7, 5, 0, 30)          # 过了午夜换日期


# --------------------------------------------------------------------------- #
# 车型：ICE 要另买票，区间车 Deutschlandticket 覆盖
# --------------------------------------------------------------------------- #
def test_stuttgart_to_heidelberg_has_a_fast_ice_that_needs_a_ticket_and_a_slower_regional_option_that_does_not():
    o = choose_options(its("plan_stuttgart_heidelberg.json"), NINE, domestic=True, city="Stuttgart",
                       local_itineraries=its("plan_stuttgart_heidelberg_regional.json"))
    fast, regional = o["fastest"], o["regional"]
    assert fast is not None and regional is not None
    assert fast.label() == "ICE 2090" and not fast.covered and fast.rides[-1].category == "long"
    assert regional.label() == "RE1 → RE71" and regional.covered
    assert all(r.category == "local" for r in regional.rides)
    assert regional.minutes > fast.minutes                          # 区间车更慢：这正是要讲清楚的
    assert fast.station_from == "Stuttgart Hauptbahnhof"            # 站名只有「Hauptbahnhof」时补上城市
    assert fast.as_dict()["needs_ticket"] is True and regional.as_dict()["needs_ticket"] is False
    assert fast.as_dict()["long_distance"] and not regional.as_dict()["long_distance"]


def test_a_crossing_of_the_border_is_never_covered_even_by_regional_trains():
    o = choose_options(its("plan_stuttgart_strasbourg.json"), NINE, domestic=False, city="Stuttgart")
    assert o["fastest"] is not None and not o["fastest"].covered and o["regional"] is None
    assert not covered_by_deutschlandticket({"legs": [{"mode": "REGIONAL_RAIL"}]}, domestic=False)
    assert covered_by_deutschlandticket({"legs": [{"mode": "WALK"}, {"mode": "REGIONAL_RAIL"}]}, domestic=True)


@pytest.mark.parametrize("mode", ["HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "COACH", "RAIL", "FERRY", "OTHER"])
def test_anything_that_is_not_local_transport_counts_as_needing_a_ticket(mode):
    legs = [{"mode": "REGIONAL_RAIL"}, {"mode": mode}]
    assert not covered_by_deutschlandticket({"legs": legs}, domestic=True)


def test_when_the_fastest_is_already_a_regional_train_there_is_only_one_option():
    local = its("plan_stuttgart_heidelberg_regional.json")
    o = choose_options(local, NINE, domestic=True, city="Stuttgart")
    assert o["fastest"].covered and o["regional"] is None


def test_only_trains_leaving_after_the_chosen_time_count():
    o = choose_options(its("plan_stuttgart_heidelberg.json"), datetime(2026, 12, 12, 20, 0), domestic=True,
                       city="Stuttgart")
    assert o == {"fastest": None, "regional": None}


def test_the_last_train_home_is_the_latest_departure_for_the_chosen_kind():
    any_kind = latest_return(its("plan_heidelberg_stuttgart_last.json"), regional_only=False, domestic=True)
    regional = latest_return(its("plan_heidelberg_stuttgart_last_regional.json"), regional_only=True, domestic=True)
    assert any_kind is not None and regional is not None
    assert regional < any_kind                                      # 区间车的末班比 ICE 早
    assert latest_return([], regional_only=False, domestic=True) is None


# --------------------------------------------------------------------------- #
# 地址、链接、距离
# --------------------------------------------------------------------------- #
def test_geocoding_gives_at_most_four_labelled_addresses_with_city_and_country():
    found = fake_transitous().geocode("Königstraße 28, Stuttgart")
    assert 1 <= len(found) <= 4
    a = found[0]
    assert a.city == "Stuttgart" and a.country == "DE" and "70173" in a.label and "Königstraße 28" in a.label
    assert 48.7 < a.lat < 48.9 and 9.1 < a.lng < 9.3
    assert fake_transitous().geocode("Rue du Faubourg National, Strasbourg")[0].country == "FR"
    assert parse_address({"type": "WAY", "name": "x", "lat": 1, "lon": 1}) is None


def test_the_db_link_goes_to_the_search_page_and_what_to_type_is_written_next_to_it():
    assert DB_SEARCH_URL == "https://www.bahn.de/buchung/fahrplan/suche"        # 带站名的预填链接试过不行
    text = db_query_text("Stuttgart Hauptbahnhof", "Heidelberg Hbf", datetime(2026, 12, 12, 9, 56))
    assert text == "Stuttgart Hauptbahnhof → Heidelberg Hbf，12 月 12 日 09:56"


def test_haversine_distances_are_about_right():
    assert 100 < haversine_km((48.78, 9.18), (48.585, 7.734)) < 115              # 斯图加特 → 斯特拉斯堡直线约 108 公里
    assert haversine_km((50.77, 6.08), (48.78, 9.18)) > 250


def test_the_client_caches_and_turns_failures_into_one_error_type():
    t = fake_transitous()
    t.geocode("Königstraße 28, Stuttgart")
    t.geocode("Königstraße 28, Stuttgart")
    assert len(t._cache) == 1
    with pytest.raises(TransitError, match="暂时连不上"):
        broken_transitous().geocode("anything")


# --------------------------------------------------------------------------- #
# 读懂用户写的地址
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, cleaned", [
    ("Königstraße 28 Stuttgart 3. OG Zimmer 305", "Königstraße 28 Stuttgart"),        # 楼层 / 房间号会让门牌被认成 3
    ("Zimmer 305, Königstraße 28, 70173 Stuttgart", "Königstraße 28, 70173 Stuttgart"),
    ("Königstr. 28, Stuttgart", "Königstraße 28, Stuttgart"),
    ("Pfaffenwaldring 7 Haus B Stuttgart", "Pfaffenwaldring 7 Stuttgart"),
    ("Studentenwohnheim Allmandring 4, Stuttgart", "Allmandring 4, Stuttgart"),
    ("斯图加特 Königstraße 28", "Stuttgart Königstraße 28"),                          # 中文城市名换成德语名
    ("  Hauptstr.   12 ,  Heidelberg ", "Hauptstraße 12, Heidelberg"),
    ("Leuschnerstraße 67,70176 Stuttgart", "Leuschnerstraße 67, 70176 Stuttgart"),  # 逗号后没空格曾找不到门牌
])
def test_the_address_is_tidied_before_it_is_searched(raw, cleaned):
    assert clean_address_query(raw, {"斯图加特": "Stuttgart"}) == cleaned


def test_chinese_only_text_leaves_nothing_to_search_and_is_not_sent():
    assert clean_address_query("我家在斯图加特市中心") == ""
    assert fake_transitous().geocode("我家在斯图加特市中心") == []


def test_results_naming_the_city_you_typed_come_first_and_the_city_falls_back_for_small_places():
    rows = [
        {"type": "ADDRESS", "name": "Weinbergstraße 5", "lat": 1, "lon": 1, "country": "DE", "zip": "72793",
         "areas": [{"name": "Pfullingen", "adminLevel": 8, "default": True}]},
        {"type": "ADDRESS", "name": "Weinbergstraße 5", "lat": 2, "lon": 2, "country": "DE", "zip": "72760",
         "areas": [{"name": "Reutlingen", "adminLevel": 8, "default": True}]},
    ]
    got = rank_addresses([a for a in (parse_address(r) for r in rows) if a], "Weinbergstraße 5, Reutlingen")
    assert [a.city for a in got] == ["Reutlingen", "Pfullingen"]
    village = parse_address({"type": "ADDRESS", "name": "Dorfstraße 1", "lat": 1, "lon": 1, "country": "DE",
                             "zip": "99999", "areas": [{"name": "Land", "adminLevel": 4}]})
    assert village is not None and village.city == "Land"                           # 小地方没有市一级：不丢掉
    no_areas = parse_address({"type": "ADDRESS", "name": "Dorfstraße 1", "lat": 1, "lon": 1, "country": "DE",
                              "zip": "99999"})
    assert no_areas is not None and no_areas.city == "99999"


def test_when_the_street_is_not_in_the_city_you_typed_the_city_centre_and_station_are_offered_as_an_approximation():
    import httpx

    from app.transit import Transitous, city_hint

    other_town = {"type": "ADDRESS", "name": "Weinbergstraße 5", "lat": 48.4, "lon": 9.2, "country": "DE",
                  "zip": "72793", "areas": [{"name": "Pfullingen", "adminLevel": 8, "default": True}]}
    centre = {"type": "PLACE", "name": "Reutlingen", "lat": 48.49, "lon": 9.21, "country": "DE", "zip": "72764",
              "areas": [{"name": "Reutlingen", "adminLevel": 8, "default": True}]}
    station = {"type": "STOP", "name": "Reutlingen Hbf (D)", "lat": 48.48, "lon": 9.2, "country": "DE", "zip": "72764",
               "areas": [{"name": "Reutlingen", "adminLevel": 8, "default": True}]}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["text"])
        rows = [centre, station] if request.url.params["text"] == "Reutlingen" else [other_town]
        return httpx.Response(200, json=rows)

    t = Transitous(client=httpx.Client(transport=httpx.MockTransport(handler)))
    got = t.geocode("Weinbergstraße 5, Reutlingen")
    assert seen == ["Weinbergstraße 5, Reutlingen", "Reutlingen"]
    assert [a.city for a in got] == ["Reutlingen", "Reutlingen"]                   # 别的小镇的同名街道不再凑数
    assert city_hint("Hauptstraße 3, 70173 Stuttgart") == "Stuttgart" and city_hint("Hauptstraße 3 Ulm") == "Ulm"
    stuttgart_calls: list[str] = []

    def stuttgart(request: httpx.Request) -> httpx.Response:
        stuttgart_calls.append(request.url.params["text"])
        return httpx.Response(200, json=fixture("geocode_stuttgart.json"))

    Transitous(client=httpx.Client(transport=httpx.MockTransport(stuttgart))).geocode("Königstraße 28, Stuttgart")
    assert len(stuttgart_calls) == 1                              # 城市对得上时不额外查
