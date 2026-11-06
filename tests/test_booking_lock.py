"""勾选「已订」+ 填入馆时间 -> 把景点锁定在这个时间，当天其余安排围绕它重排（规格 M06 S4）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import booking
from app.api import create_app
from app.editing import SetEntryTime, apply_edit, entry_target
from app.generator import generate_from_theme
from app.library import load_library
from app.llm import LLMError
from app.schemas import Itinerary, TravelRequest
from app.versions import PlanStore

LIB = load_library("paris")


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def mins(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:])


# --------------------------------------------------------------------------- #
# 纯函数：时段怎么落
# --------------------------------------------------------------------------- #
def test_a_single_entry_time_is_used_as_is():
    assert entry_target(current=11 * 60, start=10 * 60, end=None) == 10 * 60


def test_a_window_keeps_the_current_start_when_it_already_fits():
    assert entry_target(current=11 * 60, start=10 * 60, end=12 * 60) == 11 * 60
    assert entry_target(current=10 * 60, start=10 * 60, end=12 * 60) == 10 * 60
    assert entry_target(current=12 * 60, start=10 * 60, end=12 * 60) == 12 * 60


def test_a_window_moves_the_start_to_the_nearest_edge_when_it_does_not_fit():
    assert entry_target(current=9 * 60, start=10 * 60, end=10 * 60 + 30) == 10 * 60          # 太早 -> 时段开始
    assert entry_target(current=15 * 60, start=10 * 60, end=10 * 60 + 30) == 10 * 60 + 30    # 太晚 -> 时段结束


def test_the_edit_op_validates_time_format_and_order():
    SetEntryTime(place_id="louvre", start="10:00", end="10:30")
    bad_ones = ({"start": "9:00"}, {"start": "25:00"}, {"start": "10:00", "end": "9:00"},
                {"start": "10:00", "end": "10:00"})
    for bad in bad_ones:
        with pytest.raises(ValidationError):
            SetEntryTime(place_id="louvre", **bad)


# --------------------------------------------------------------------------- #
# 编辑操作：锁定 + 重排当天
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def trip() -> tuple[TravelRequest, Itinerary]:
    req = TravelRequest.model_validate({
        "destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "arrival_time": "2026-10-20T10:30:00",
        "departure_time": "2026-10-23T18:00:00"})
    return req, generate_from_theme(req, "art", client=None, library=LIB).itinerary


def a_place_on_a_regular_day(it: Itinerary) -> tuple[int, str]:
    for i, d in enumerate(it.days[1:-1], start=1):             # 避开有航班缓冲的首尾天
        ids = [x.place_id for x in d.items if x.place_id]
        if ids:
            return i, ids[0]
    raise AssertionError("没有合适的天")


def item_of(it: Itinerary, day: int, pid: str):
    return next(i for i in it.days[day].items if i.place_id == pid)


def test_setting_an_entry_time_locks_the_item_at_that_time(trip):
    req, it = trip
    day, pid = a_place_on_a_regular_day(it)
    res = apply_edit(it, req, SetEntryTime(place_id=pid, start="15:30"), LIB)
    got = item_of(res.itinerary, day, pid)
    assert got.time == "15:30" and got.locked is True
    assert res.touched == [it.days[day].date]
    starts = [mins(i.time) for i in res.itinerary.days[day].items]
    assert starts == sorted(starts)                                    # 当天顺序仍然按时间排
    assert item_of(it, day, pid).locked is False                       # 原行程没被改动


def test_other_days_are_not_touched(trip):
    req, it = trip
    day, pid = a_place_on_a_regular_day(it)
    res = apply_edit(it, req, SetEntryTime(place_id=pid, start="09:30"), LIB)
    for i, (a, b) in enumerate(zip(it.days, res.itinerary.days, strict=True)):
        if i != day:
            assert a.items == b.items


def test_a_window_that_the_current_start_already_fits_only_adds_the_lock(trip):
    req, it = trip
    day, pid = a_place_on_a_regular_day(it)
    cur = mins(item_of(it, day, pid).time)
    res = apply_edit(it, req, SetEntryTime(place_id=pid, start=hhmm(max(cur - 30, 0)), end=hhmm(cur + 30)), LIB)
    got = item_of(res.itinerary, day, pid)
    assert got.time == hhmm(cur) and got.locked is True


def test_a_window_later_than_the_current_start_moves_it_to_the_window_start(trip):
    req, it = trip
    day, pid = a_place_on_a_regular_day(it)
    res = apply_edit(it, req, SetEntryTime(place_id=pid, start="16:00", end="16:30"), LIB)
    assert item_of(res.itinerary, day, pid).time == "16:00"


def test_the_item_must_be_in_the_trip(trip):
    from app.editing import EditError

    req, it = trip
    absent = next(p for p in LIB.places if p not in {x.place_id for d in it.days for x in d.items})
    with pytest.raises(EditError):
        apply_edit(it, req, SetEntryTime(place_id=absent, start="10:00"), LIB)


# --------------------------------------------------------------------------- #
# 接口：勾选 + 入馆时间 -> 新版本
# --------------------------------------------------------------------------- #
class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(root=tmp_path / "plans")


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(create_app(store=store, client_factory=DeadLLM))


@pytest.fixture
def plan(client) -> dict:
    r = client.post("/plans", json={
        "destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art", "arrival_time": "2026-10-20 10:30",
        "departure_time": "2026-10-23 18:00"})
    assert r.status_code == 201
    return r.json()


def a_booking_place(plan) -> str:
    mid = [d for d in plan["itinerary"]["days"][1:-1]]
    for d in mid:
        for i in d["items"]:
            needs_ticket = {b.get("place_id") for b in plan["itinerary"]["booking_checklist"]}
            if i.get("place_id") in needs_ticket:
                return i["place_id"]
    raise AssertionError("中间几天里没有需要订票的地点")


def put(client, plan, pid, **body):
    return client.put(f"/plans/{plan['plan_id']}/reservations/{pid}", json=body)


def latest(client, plan) -> dict:
    return client.get(f"/plans/{plan['plan_id']}").json()


def item_in(detail: dict, pid: str) -> dict:
    return next(i for d in detail["itinerary"]["days"] for i in d["items"] if i.get("place_id") == pid)


def test_ticking_booked_with_an_entry_time_locks_the_itinerary_and_makes_a_labelled_version(client, plan):
    pid = a_booking_place(plan)
    r = put(client, plan, pid, status="booked", entry_from="15:30")
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["version"] == 2 and row["entry_from"] == "15:30" and row["locked"] is True
    detail = latest(client, plan)
    assert detail["latest_version"] == 2
    it = item_in(detail, pid)
    assert it["time"] == "15:30" and it["locked"] is True
    hist = detail["history"][-1]
    assert hist["trigger"] == "booking-lock"
    assert "15:30" in hist["change_summary"] and "已锁定" in hist["change_summary"]


def test_saving_the_same_time_again_does_not_make_another_version(client, plan):
    pid = a_booking_place(plan)
    put(client, plan, pid, status="booked", entry_from="15:30")
    again = put(client, plan, pid, status="booked", entry_from="15:30", confirmation_code="ABC")
    assert again.json()["version"] is None
    assert latest(client, plan)["latest_version"] == 2


def test_changing_the_time_moves_the_lock(client, plan):
    pid = a_booking_place(plan)
    put(client, plan, pid, status="booked", entry_from="15:30")
    r = put(client, plan, pid, status="booked", entry_from="11:00")
    assert r.json()["version"] == 3
    assert item_in(latest(client, plan), pid)["time"] == "11:00"


def test_a_time_window_is_scheduled_inside_the_window(client, plan):
    pid = a_booking_place(plan)
    r = put(client, plan, pid, status="booked", entry_from="16:00", entry_to="16:30")
    assert r.json()["version"] == 2
    it = item_in(latest(client, plan), pid)
    assert "16:00" <= it["time"] <= "16:30" and it["locked"] is True
    assert "16:00" in latest(client, plan)["history"][-1]["change_summary"]
    assert "16:30" in latest(client, plan)["history"][-1]["change_summary"]


def test_unticking_releases_the_lock_the_booking_added_but_keeps_the_code(client, plan):
    pid = a_booking_place(plan)
    put(client, plan, pid, status="booked", entry_from="15:30", confirmation_code="KEEP-1")
    r = put(client, plan, pid, status="todo")
    assert r.json()["version"] == 3 and r.json()["locked"] is False and r.json()["confirmation_code"] == "KEEP-1"
    detail = latest(client, plan)
    assert item_in(detail, pid)["locked"] is False and item_in(detail, pid)["time"] == "15:30"   # 时间不动，只是解锁
    assert detail["history"][-1]["trigger"] == "booking-unlock"


def test_clearing_the_time_also_releases_the_lock(client, plan):
    pid = a_booking_place(plan)
    put(client, plan, pid, status="booked", entry_from="15:30")
    r = put(client, plan, pid, status="booked", entry_from="")
    assert r.json()["version"] == 3 and r.json()["entry_from"] is None
    assert item_in(latest(client, plan), pid)["locked"] is False


def test_ticking_without_a_time_does_not_touch_the_itinerary(client, plan):
    pid = a_booking_place(plan)
    r = put(client, plan, pid, status="booked", confirmation_code="X")
    assert r.json()["version"] is None and latest(client, plan)["latest_version"] == 1


def test_invalid_times_are_422(client, plan):
    pid = a_booking_place(plan)
    assert put(client, plan, pid, status="booked", entry_from="9am").status_code == 422
    assert put(client, plan, pid, status="booked", entry_from="10:00", entry_to="09:00").status_code == 422
    assert put(client, plan, pid, status="booked", entry_to="10:00").status_code == 422        # 只有结束没有开始
    assert latest(client, plan)["latest_version"] == 1


def test_a_booking_for_a_place_not_in_the_trip_is_stored_without_changing_the_itinerary(client, plan):
    absent = next(p.id for p in LIB.places.values()
                  if p.ticketing.value == "online_required"
                  and p.id not in {i.get("place_id") for d in plan["itinerary"]["days"] for i in d["items"]})
    r = put(client, plan, absent, status="booked", entry_from="10:00")
    assert r.status_code == 200 and r.json()["version"] is None and r.json()["entry_from"] == "10:00"
    assert latest(client, plan)["latest_version"] == 1


def test_the_entry_time_shows_up_in_the_exports_and_is_copied_with_the_plan(client, plan, monkeypatch):
    monkeypatch.setattr(booking, "today", lambda: __import__("datetime").date(2026, 10, 1))
    pid = a_booking_place(plan)
    put(client, plan, pid, status="booked", entry_from="15:30", entry_to="16:00", confirmation_code="T-1")
    html = client.get(f"/plans/{plan['plan_id']}/itinerary.html").text
    assert "15:30" in html and "16:00" in html and "T-1" in html
    copy = client.post(f"/plans/{plan['plan_id']}/duplicate").json()
    row = next(i for i in client.get(f"/plans/{copy['plan_id']}/reservations").json()["items"] if i["place_id"] == pid)
    assert (row["entry_from"], row["entry_to"], row["locked"]) == ("15:30", "16:00", True)


def test_the_list_view_carries_the_entry_fields(client, plan):
    pid = a_booking_place(plan)
    put(client, plan, pid, status="booked", entry_from="15:30")
    row = next(i for i in client.get(f"/plans/{plan['plan_id']}/reservations").json()["items"] if i["place_id"] == pid)
    assert row["entry_from"] == "15:30" and row["entry_to"] is None and row["locked"] is True
