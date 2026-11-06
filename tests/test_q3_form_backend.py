"""Q3 新建表单的后端部分（规格 07 P06–P09、P15）：出行人姓名、来回交通、按交通生成的提醒、建议天数、主题不设上限。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import booking
from app.api import create_app
from app.cities import load_city
from app.companion import suggest_todos, transport_todos
from app.llm import LLMError
from app.schemas import Traveler, TravelRequest
from app.versions import PlanStore

NOW = date(2026, 10, 14)
BASE = {"destination": "Paris", "start_date": "2026-11-10", "end_date": "2026-11-13", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"]}


def transport(out_mode="flight", back_mode="flight", **over) -> dict:
    t = {"outbound": {"mode": out_mode, "depart_at": "2026-11-09T22:30", "arrive_at": "2026-11-10T09:40",
                      "from_place": "上海浦东", "number": "MU553"},
         "inbound": {"mode": back_mode, "depart_at": "2026-11-13T17:05"}}
    for k, v in over.items():
        leg, field = k.split("__")
        t[leg][field] = v
    return t


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(booking, "today", lambda: NOW)


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


# ---- P09：出行人姓名 ----
def test_a_traveler_name_is_stripped_and_blank_means_none():
    assert Traveler(name="  小明 ").name == "小明" and Traveler(name="   ").name is None and Traveler().name is None
    with pytest.raises(ValidationError):
        Traveler(name="名" * 21)


def test_a_missing_name_is_not_written_out_so_old_plans_keep_their_json():
    assert "name" not in Traveler(age=8).model_dump()
    assert Traveler(name="小明", age=8).model_dump()["name"] == "小明"


# ---- P07：来回交通 ----
def test_transport_sets_the_arrival_and_departure_times():
    req = TravelRequest.model_validate({**BASE, "transport": transport()})
    assert req.arrival_time.isoformat() == "2026-11-10T09:40:00"
    assert req.departure_time.isoformat() == "2026-11-13T17:05:00"
    assert req.transport.outbound.number == "MU553" and req.transport.inbound.arrive_at is None


def test_transport_is_optional_for_old_plans_and_scripts_but_not_written_out_when_absent():
    req = TravelRequest.model_validate(BASE)
    assert req.transport is None and "transport" not in req.model_dump()


@pytest.mark.parametrize("bad, message", [
    ({"outbound__arrive_at": None}, "去程要填出发时间和到达时间"),
    ({"outbound__depart_at": None}, "去程要填出发时间和到达时间"),
    ({"inbound__depart_at": None}, "返程要填出发时间"),
    ({"outbound__arrive_at": "2026-11-09T20:00"}, "到达时间要晚于出发时间"),
    ({"outbound__arrive_at": "2026-11-11T09:40", "outbound__depart_at": "2026-11-11T05:00"}, "开始日期"),
    ({"inbound__depart_at": "2026-11-12T10:00"}, "离开日期"),
])
def test_transport_must_be_complete_ordered_and_match_the_trip_dates(bad, message):
    with pytest.raises(ValidationError, match=message):
        TravelRequest.model_validate({**BASE, "transport": transport(**bad)})


def test_an_unknown_transport_mode_is_rejected():
    with pytest.raises(ValidationError):
        TravelRequest.model_validate({**BASE, "transport": transport(out_mode="rocket")})


def test_the_plan_is_scheduled_around_the_arrival_time_from_the_transport(client):
    r = client.post("/plans", json={**BASE, "theme": "art", "transport": transport(
        outbound__arrive_at="2026-11-10T15:30", outbound__depart_at="2026-11-10T09:00")})
    assert r.status_code == 201, r.text
    first_day = r.json()["itinerary"]["days"][0]["items"]
    visits = [i for i in first_day if i["type"] not in ("logistics", "meal")]
    assert all(i["time"] >= "16:00" for i in visits), first_day                           # 下午 3 点半到：之后才开始排


# ---- P07：值机 / 取票提醒 ----
def test_flights_get_check_in_reminders_the_day_before_each_departure():
    req = TravelRequest.model_validate({**BASE, "transport": transport()})
    got = {k: (text, due) for k, text, due, _why in transport_todos(req.transport)}
    assert got["sug-checkin-out"] == ("去程网上值机", date(2026, 11, 8))
    assert got["sug-checkin-back"] == ("返程网上值机", date(2026, 11, 12))


def test_trains_get_a_ticket_and_arrive_early_reminder_on_the_day_and_other_modes_get_none():
    rail = TravelRequest.model_validate({**BASE, "transport": transport("high_speed_rail", "train")})
    got = {k: due for k, _t, due, _w in transport_todos(rail.transport)}
    assert got == {"sug-ticket-out": date(2026, 11, 9), "sug-ticket-back": date(2026, 11, 13)}
    for mode in ("drive", "coach", "other"):
        other = TravelRequest.model_validate({**BASE, "transport": transport(mode, mode)})
        assert transport_todos(other.transport) == []


def test_the_reminder_wording_is_a_general_tip_not_an_official_rule():
    req = TravelRequest.model_validate({**BASE, "transport": transport()})
    why = " ".join(w for _k, _t, _d, w in transport_todos(req.transport))
    assert "以你的航司为准" in why and "不是官方规定" in why


def test_with_transport_the_generic_check_in_todo_is_replaced_and_without_it_nothing_changes():
    req = TravelRequest.model_validate({**BASE, "transport": transport()})
    with_tr = {s["key"] for s in suggest_todos(date(2026, 11, 10), NOW, [], set(), req.transport)}
    assert "sug-checkin-out" in with_tr and "sug-checkin-back" in with_tr and "sug-checkin" not in with_tr
    without = {s["key"] for s in suggest_todos(date(2026, 11, 10), NOW, [], set())}
    assert "sug-checkin" in without and "sug-checkin-out" not in without                 # 老行程保持原样
    drive = TravelRequest.model_validate({**BASE, "transport": transport("drive", "drive")})
    keys = {s["key"] for s in suggest_todos(date(2026, 11, 10), NOW, [], set(), drive.transport)}
    assert not keys & {"sug-checkin", "sug-checkin-out", "sug-checkin-back"}


def test_the_todos_endpoint_offers_them_for_a_plan_with_flights(client):
    pid = client.post("/plans", json={**BASE, "theme": "art", "transport": transport()}).json()["plan_id"]
    got = {s["key"]: s for s in client.get(f"/plans/{pid}/todos").json()["suggestions"]}
    assert got["sug-checkin-out"]["text"] == "去程网上值机" and got["sug-checkin-out"]["due"] == "2026-11-08"


# ---- P09：预算里的姓名 ----
def test_names_show_up_in_the_budget_for_the_page_only_when_filled_and_never_in_exports(client):
    party = [{"name": "小明", "age": 8}, {"age": 21, "residence": "fr", "student": True}]
    pid = client.post("/plans", json={**BASE, "theme": "art", "travelers": party}).json()["plan_id"]
    b = client.get(f"/plans/{pid}/budget").json()
    assert b["travelers"][0]["name"] == "小明" and "name" not in b["travelers"][1]
    html = client.get(f"/plans/{pid}/itinerary.html").text
    md = client.get(f"/plans/{pid}/itinerary.md").text
    # 导出里没有姓名（和年龄一样不外传）
    assert "小明" not in html and "小明" not in md


# ---- P06：建议天数；P15：主题不设上限 ----
def test_the_city_carries_its_suggested_days_and_the_cities_endpoint_exposes_them(client):
    advice = load_city("paris").suggested_days
    assert (advice.min, advice.max, advice.quality_max) == (4, 6, 14) and advice.note
    paris = next(c for c in client.get("/cities").json() if c["id"] == "paris")
    assert paris["suggested_days"] == {"min": 4, "max": 6, "quality_max": 14, "fill_max": 22, "note": advice.note}


def test_days_outside_the_suggestion_only_add_a_soft_note_and_never_block():
    two = TravelRequest.model_validate({**BASE, "end_date": "2026-11-11"})
    assert any("建议 4-6 天" in n and "质量会下降" not in n for n in two.notes)
    eight = TravelRequest.model_validate({**BASE, "end_date": "2026-11-17"})
    assert any("建议 4-6 天" in n and "代表性景点" not in n for n in eight.notes)
    long = TravelRequest.model_validate({**BASE, "end_date": "2026-11-25"})                    # 16 天：超过 14
    assert any("建议 4-6 天" in n and "代表性景点" in n for n in long.notes)
    huge = TravelRequest.model_validate({**BASE, "end_date": "2026-12-10"})                    # 31 天：超过 22
    assert any("排满约 22 天" in n and "自由日" in n for n in huge.notes)
    four = TravelRequest.model_validate(BASE)
    assert not four.notes


def test_every_theme_can_be_picked_at_once_and_the_plan_is_made(client):
    all_ids = [t["id"] for t in client.get("/themes").json()]
    themes = [{"id": t, "weight": 3} for t in all_ids]
    r = client.post("/plans", json={**BASE, "theme": all_ids[0], "themes": themes})
    assert r.status_code == 201, r.text
    assert len(r.json()["itinerary"]["days"]) == 4


def test_picking_every_theme_with_equal_weights_is_named_balanced_and_a_subset_keeps_its_names(client):
    all_ids = [t["id"] for t in client.get("/themes").json()]
    plan = client.post("/plans", json={**BASE, "theme": all_ids[0],
                                       "themes": [{"id": t, "weight": 3} for t in all_ids]}).json()
    assert plan["itinerary"]["generation"]["theme_name"] == "均衡"
    skewed = client.post("/plans", json={**BASE, "theme": "art", "themes": [
        {"id": t, "weight": 5 if t == "art" else 3} for t in all_ids]}).json()
    assert skewed["itinerary"]["generation"]["theme_name"] != "均衡"                       # 有倾向就不叫均衡
    some = client.post("/plans", json={**BASE, "theme": "art", "themes": [
        {"id": "art", "weight": 3}, {"id": "food", "weight": 3}]}).json()
    assert some["itinerary"]["generation"]["theme_name"] == "艺术 + 美食"
