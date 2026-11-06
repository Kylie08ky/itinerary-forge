"""住址出发的周边一日游（规格 08 3.7）：目的地列表、两种车型、「排一天」的需求、API。假 Transitous 回放真实回复。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.daytrips import DayTripError, crosses_border, day_trip_info, day_trip_labels
from app.daytrips_live import build_live_request, fare_note, list_destinations, make_origin, offline_destinations
from app.generator import generate_from_theme
from app.llm import LLMError
from app.schemas import TravelRequest
from app.transit import TransitError
from app.versions import PlanStore
from tests.transit_fake import HOME, broken_transitous, fake_transitous

DAY = date(2026, 12, 12)


def stuttgart():
    return make_origin(HOME["city"], HOME["country"], HOME["lat"], HOME["lng"])


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM,
                                 transit=fake_transitous()))


def body(**over) -> dict:
    return {"city": HOME["city"], "city_zh": HOME["city_zh"], "country": "DE", "lat": HOME["lat"], "lng": HOME["lng"],
            "date": "2026-12-12", **over}


# --------------------------------------------------------------------------- #
# 出发地
# --------------------------------------------------------------------------- #
def test_the_chinese_name_of_the_home_city_is_only_known_for_cities_we_have():
    assert stuttgart().city_zh == "斯图加特"
    assert make_origin("Aachen", "DE", 50.77, 6.08).city_zh == "亚琛"
    assert make_origin("Heidelberg", "DE", 49.4, 8.69).city_zh == "海德堡"
    assert make_origin("Reutlingen", "DE", 48.49, 9.2).city_zh is None            # 不知道就不编，页面只写当地名
    with pytest.raises(DayTripError):
        make_origin("", "DE", 1, 1)


# --------------------------------------------------------------------------- #
# 目的地列表
# --------------------------------------------------------------------------- #
def test_the_list_gives_each_destination_a_fastest_and_a_regional_option_with_ticket_info():
    out = list_destinations(stuttgart(), DAY, fake_transitous())
    by_city = {d["city"]["id"]: d for d in out["destinations"]}
    assert {"heidelberg", "ludwigsburg", "ulm"} <= set(by_city)
    assert "liege" not in by_city and "koeln" not in by_city                       # 离斯图加特太远的不查
    hd = by_city["heidelberg"]
    kinds = {o["kind"]: o for o in hd["options"]}
    assert set(kinds) == {"fastest", "regional"}
    assert kinds["fastest"]["needs_ticket"] and kinds["fastest"]["long_distance"]
    assert kinds["regional"]["covered"] and not kinds["regional"]["needs_ticket"]
    assert kinds["fastest"]["minutes"] < kinds["regional"]["minutes"]
    assert kinds["fastest"]["db_url"].startswith("https://www.bahn.de/")
    assert "Heidelberg" in kinds["fastest"]["db_query"] and "12 月 12 日" in kinds["fastest"]["db_query"]
    assert hd["default_kind"] == "fastest"                                          # 没勾月票：默认最快
    assert out["source_url"] == "https://transitous.org" and out["dticket_url"].startswith("https://www.bahn.de/")


def test_with_a_deutschlandticket_the_regional_option_is_the_default():
    out = list_destinations(stuttgart(), DAY, fake_transitous(), dticket=True)
    hd = next(d for d in out["destinations"] if d["city"]["id"] == "heidelberg")
    assert hd["default_kind"] == "regional" and out["dticket"] is True
    # 没有区间车方案的（跨境），仍然是最快
    sx = next(d for d in out["destinations"] if d["city"]["id"] == "strasbourg")
    assert [o["kind"] for o in sx["options"]] == ["fastest"] and sx["default_kind"] == "fastest" and sx["cross_border"]
    assert sx["options"][0]["needs_ticket"] and "跨境" in sx["options"][0]["fare_note"]


def test_destinations_are_sorted_by_the_time_of_the_default_option():
    out = list_destinations(stuttgart(), DAY, fake_transitous())
    mins = [next(o["minutes"] for o in d["options"] if o["kind"] == d["default_kind"]) for d in out["destinations"]]
    assert mins == sorted(mins)


def test_a_dead_timetable_service_is_an_error_and_a_preset_origin_falls_back_to_the_stored_table():
    with pytest.raises(TransitError):
        list_destinations(stuttgart(), DAY, broken_transitous())
    off = offline_destinations("stuttgart", DAY)
    assert off["offline"] is True and len(off["destinations"]) >= 7
    hd = next(d for d in off["destinations"] if d["city"]["id"] == "heidelberg")
    assert hd["options"][0]["needs_ticket"] is None and hd["options"][0]["rides"] == []     # 不区分车型，页面写明


# --------------------------------------------------------------------------- #
# 排一天
# --------------------------------------------------------------------------- #
def test_the_request_uses_the_chosen_option_and_keeps_only_the_city_and_station_not_the_street():
    req, info = build_live_request(stuttgart(), "heidelberg", DAY, fake_transitous(), kind="fastest", back="19:00")
    assert req.city_id == "heidelberg" and req.day_trip_from == "address"
    o = req.day_trip_origin
    assert o.name_local == "Stuttgart" and o.name_zh == "斯图加特" and o.station == "Stuttgart Hauptbahnhof"
    assert "ICE 2090" in o.ride_note and "需另买票" in o.ride_note
    blob = req.model_dump_json()
    assert "Königstraße" not in blob and "48.7788" not in blob and "9.1778" not in blob      # 住址不进行程
    assert req.transport.outbound.number == "ICE 2090" and req.transport.outbound.to_place == "Heidelberg Hauptbahnhof"
    assert info["leave"] == "19:00" and info["option"]["kind"] == "fastest"


def test_the_regional_option_is_planned_with_the_regional_last_train_and_says_the_ticket_covers_it():
    req, info = build_live_request(stuttgart(), "heidelberg", DAY, fake_transitous(), kind="regional")
    assert "RE1 → RE71" in req.day_trip_origin.ride_note and "Deutschlandticket" in req.day_trip_origin.ride_note
    assert req.transport.outbound.number == "RE1 → RE71"
    assert info["last_return"] == "21:24"                                          # 区间车的末班比 ICE（22:14）早
    assert info["leave"] <= info["last_return"]


def test_a_return_after_the_last_train_or_before_you_could_see_anything_is_refused_with_the_reason():
    with pytest.raises(DayTripError, match="还晚"):
        build_live_request(stuttgart(), "heidelberg", DAY, fake_transitous(), kind="fastest", back="23:30")
    with pytest.raises(DayTripError, match="太早"):
        build_live_request(stuttgart(), "heidelberg", DAY, fake_transitous(), kind="fastest", back="11:00")
    with pytest.raises(DayTripError, match="没有这个目的地"):
        build_live_request(stuttgart(), "koeln", DAY, fake_transitous())
    with pytest.raises(DayTripError, match="这类班次"):
        build_live_request(stuttgart(), "strasbourg", DAY, fake_transitous(), kind="regional")


def test_the_default_return_is_evening_in_season_and_never_after_the_last_train_of_the_kind():
    _, any_kind = build_live_request(stuttgart(), "heidelberg", DAY, fake_transitous(), kind="fastest")
    assert any_kind["leave"] == "21:00" and any_kind["last_return"] == "22:14"       # 圣诞市场在期内：留到 21:00
    _, regional = build_live_request(stuttgart(), "heidelberg", DAY, fake_transitous(), kind="regional")
    assert regional["leave"] == "21:00" and regional["last_return"] == "21:24"


def test_the_plan_labels_and_the_itinerary_come_from_the_request_without_a_preset_origin():
    req, _ = build_live_request(stuttgart(), "heidelberg", DAY, fake_transitous(), kind="fastest", back="19:00")
    assert day_trip_info(req) == {"city": "海德堡 Heidelberg", "origin": "斯图加特 Stuttgart"}
    arrive, back = day_trip_labels(req)
    assert "从斯图加特 Stuttgart 出发，抵达海德堡 Heidelberg（Heidelberg Hauptbahnhof）" in arrive
    assert "返回斯图加特 Stuttgart" in back
    assert not crosses_border(req)
    first = generate_from_theme(req, "daytrip", client=None).itinerary.days[0].items[0]
    assert "乘 ICE 2090" in first.notes and "护照" not in first.notes
    assert TravelRequest.model_validate(req.model_dump(mode="json")) == req            # 保存再读出来不变


def test_crossing_the_border_adds_the_passport_reminder_and_the_cross_border_ticket_note():
    req, _ = build_live_request(stuttgart(), "strasbourg", DAY, fake_transitous(), kind="fastest", back="19:30")
    assert crosses_border(req) and "跨境" in req.day_trip_origin.ride_note
    first = generate_from_theme(req, "daytrip", client=None).itinerary.days[0].items[0]
    assert first.notes.startswith("出境：带好护照")


def test_the_fare_note_never_names_a_price():
    req, _ = build_live_request(stuttgart(), "heidelberg", DAY, fake_transitous(), kind="fastest", back="19:00")
    assert "€" not in req.day_trip_origin.ride_note and "欧" not in req.day_trip_origin.ride_note
    assert fare_note.__doc__ and "不报价格" in fare_note.__doc__


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_geocode_endpoint_returns_candidates_and_a_dead_service_is_a_503(client, tmp_path):
    r = client.post("/daytrips/geocode", json={"q": "Königstraße 28, Stuttgart"})
    assert r.status_code == 200
    first = r.json()[0]
    assert first["city"] == "Stuttgart" and first["city_zh"] == "斯图加特" and first["country"] == "DE"
    assert client.post("/daytrips/geocode", json={"q": "ab"}).status_code == 422
    dead = TestClient(create_app(store=PlanStore(root=tmp_path / "p2"), client_factory=DeadLLM,
                                 transit=broken_transitous()))
    assert dead.post("/daytrips/geocode", json={"q": "Königstraße 28"}).status_code == 503


def test_search_endpoint_lists_destinations_and_the_deutschlandticket_flag_changes_the_default(client):
    plain = client.post("/daytrips/search", json=body()).json()
    ticket = client.post("/daytrips/search", json=body(dticket=True)).json()
    pick = lambda d: next(x for x in d["destinations"] if x["city"]["id"] == "heidelberg")["default_kind"]  # noqa: E731
    assert pick(plain) == "fastest" and pick(ticket) == "regional"


def test_search_falls_back_to_the_stored_table_for_a_preset_origin_when_the_service_is_down(tmp_path):
    dead = TestClient(create_app(store=PlanStore(root=tmp_path / "p3"), client_factory=DeadLLM,
                                 transit=broken_transitous()))
    got = dead.post("/daytrips/search", json=body(origin="stuttgart"))
    assert got.status_code == 200 and got.json()["offline"] is True
    assert dead.post("/daytrips/search", json=body()).status_code == 503              # 自己填的地址没有回退


def test_plan_request_endpoint_builds_a_payload_that_creates_a_one_day_plan(client):
    r = client.post("/daytrips/plan-request", json=body(destination="heidelberg", kind="regional", back="20:00"))
    assert r.status_code == 200, r.text
    got = r.json()
    p = got["payload"]
    assert p["day_trip_from"] == "address" and p["day_trip_origin"]["station"] == "Stuttgart Hauptbahnhof"
    created = client.post("/plans", json=p)
    assert created.status_code == 201, created.text
    detail = client.get(f"/plans/{created.json()['plan_id']}").json()
    assert detail["day_trip_info"] == {"city": "海德堡 Heidelberg", "origin": "斯图加特 Stuttgart"}
    assert "Königstraße" not in str(detail) and "48.7788" not in str(detail)
    bad = client.post("/daytrips/plan-request", json=body(destination="heidelberg", back="23:50"))
    assert bad.status_code == 422 and "还晚" in bad.json()["detail"]["message"]
