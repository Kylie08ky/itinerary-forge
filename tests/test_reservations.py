"""预订记录与最晚订票日（规格 M06 S3 / S4）。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import booking
from app.api import create_app
from app.booking import book_by, urgency
from app.library import Place, load_library
from app.llm import LLMError
from app.versions import PlanStore

LIB = load_library("paris")
FREE = next(p.id for p in LIB.places.values() if p.ticketing.value == "none")
WITH_LEAD = {"louvre": 14, "sainte-chapelle": 7, "seine-cruise": 7, "disneyland-paris": 7, "moulin-rouge": 30}


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_book_by_is_the_visit_date_minus_the_lead_time():
    assert book_by(date(2026, 10, 4), 14) == date(2026, 9, 20)
    assert book_by(date(2026, 10, 4), None) is None


def test_urgency_levels():
    today = date(2026, 9, 20)
    assert urgency(date(2026, 9, 27), today, booked=False) == "ok"          # 还有 7 天
    assert urgency(date(2026, 9, 23), today, booked=False) == "soon"        # 还有 3 天
    assert urgency(date(2026, 9, 20), today, booked=False) == "soon"        # 就是今天
    assert urgency(date(2026, 9, 19), today, booked=False) == "late"        # 已经过了建议日
    assert urgency(None, today, booked=False) == "none"                     # 没有明确天数：只说建议尽早
    assert urgency(date(2026, 9, 1), today, booked=True) == "booked"        # 已订的不再提醒
    assert urgency(None, today, booked=True) == "booked"
    assert urgency(date(2026, 9, 1), today, booked=False, visit=date(2026, 9, 10)) == "past"   # 参观日已过：不再催
    assert urgency(date(2026, 9, 1), today, booked=True, visit=date(2026, 9, 10)) == "booked"


# --------------------------------------------------------------------------- #
# 地点库里的提前订票天数：只给说法明确的地点，且必须有依据
# --------------------------------------------------------------------------- #
def test_only_the_five_places_with_a_stated_lead_time_have_lead_days():
    got = {p.id: p.booking.lead_days for p in LIB.places.values() if p.booking.lead_days}
    assert got == WITH_LEAD


def test_every_lead_days_is_backed_by_a_stated_booking_note():
    for pid in WITH_LEAD:
        p = LIB.places[pid]
        assert p.booking.lead_time_hint or p.ticket_note, pid
        assert p.ticketing.value != "none"


def test_a_lead_time_without_any_basis_is_rejected():
    base = LIB.places["louvre"].model_dump()
    base["booking"] = {"required": True, "lead_days": 14}
    base["ticket_note"] = None
    with pytest.raises(ValidationError, match="依据"):
        Place.model_validate(base)
    free = LIB.places[FREE].model_dump()
    free["booking"] = {"required": False, "lead_days": 3, "lead_time_hint": "随便"}
    with pytest.raises(ValidationError, match="无需订票"):
        Place.model_validate(free)


# --------------------------------------------------------------------------- #
# 接口
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
        "destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art", "arrival_time": "2026-10-03 10:30",
        "departure_time": "2026-10-06 18:00"})
    assert r.status_code == 201
    return r.json()


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(booking, "today", lambda: date(2026, 9, 25))


def items(client, plan, **params) -> dict[str, dict]:
    r = client.get(f"/plans/{plan['plan_id']}/reservations", params=params)
    assert r.status_code == 200, r.text
    return {i["place_id"]: i for i in r.json()["items"]}


def visit(plan, pid) -> date:
    return min(date.fromisoformat(d["date"]) for d in plan["itinerary"]["days"]
               for i in d["items"] if i.get("place_id") == pid)


def test_the_list_follows_the_booking_checklist_with_visit_dates_and_deadlines(client, plan):
    got = items(client, plan)
    checklist = {b["place_id"] for b in plan["itinerary"]["booking_checklist"] if b.get("place_id")}
    assert checklist and set(got) == checklist
    for pid, it in got.items():
        assert it["visit_date"] == visit(plan, pid).isoformat()
        assert it["status"] == "todo" and it["confirmation_code"] is None and it["in_itinerary"] is True
        assert it["must_online"] in (True, False) and it["title"]


def test_places_with_a_stated_lead_time_get_a_deadline_and_the_rest_just_say_book_early(client, plan):
    got = items(client, plan)
    for pid, it in got.items():
        if pid in WITH_LEAD:
            assert it["book_by"] == (visit(plan, pid) - timedelta(days=WITH_LEAD[pid])).isoformat()
            assert it["lead_days"] == WITH_LEAD[pid] and it["urgency"] in ("ok", "soon", "late")
            assert "经验值" in it["lead_basis"] and "非官方" in it["lead_basis"]
        else:
            assert it["book_by"] is None and it["lead_days"] is None and it["urgency"] == "none"
            assert it["lead_basis"] is None


def test_urgency_follows_todays_date(client, plan, monkeypatch):
    pid = next(p for p in items(client, plan) if p in WITH_LEAD)
    due = date.fromisoformat(items(client, plan)[pid]["book_by"])
    monkeypatch.setattr(booking, "today", lambda: due - timedelta(days=10))
    assert items(client, plan)[pid]["urgency"] == "ok"
    monkeypatch.setattr(booking, "today", lambda: due - timedelta(days=2))
    assert items(client, plan)[pid]["urgency"] == "soon"
    monkeypatch.setattr(booking, "today", lambda: due + timedelta(days=1))
    assert items(client, plan)[pid]["urgency"] == "late"


def put(client, plan, pid, **body):
    return client.put(f"/plans/{plan['plan_id']}/reservations/{pid}", json=body)


def test_marking_booked_stores_the_code_and_survives_reloads_and_turns_off_reminders(client, plan, monkeypatch):
    pid = next(p for p in items(client, plan) if p in WITH_LEAD)
    monkeypatch.setattr(booking, "today", lambda: visit(plan, pid))         # 参观当天还没订：必然 late
    assert items(client, plan)[pid]["urgency"] == "late"
    r = put(client, plan, pid, status="booked", confirmation_code="LV-123456", slot="10:00 入场", note="买了两张")
    assert r.status_code == 200, r.text
    it = items(client, plan)[pid]
    saved = (it["status"], it["confirmation_code"], it["slot"], it["note"])
    assert saved == ("booked", "LV-123456", "10:00 入场", "买了两张")
    assert it["urgency"] == "booked" and it["updated_at"]
    put(client, plan, pid, status="todo")                                   # 改回未订，确认码保留（可能只是点错了）
    again = items(client, plan)[pid]
    assert again["status"] == "todo" and again["confirmation_code"] == "LV-123456"


def test_deleting_a_record_resets_the_item(client, plan):
    pid = next(iter(items(client, plan)))
    put(client, plan, pid, status="booked", confirmation_code="X1")
    assert client.delete(f"/plans/{plan['plan_id']}/reservations/{pid}").status_code == 204
    it = items(client, plan)[pid]
    assert it["status"] == "todo" and it["confirmation_code"] is None


def test_only_places_that_need_a_ticket_can_have_a_reservation(client, plan):
    assert put(client, plan, FREE, status="booked").status_code == 422     # 无需订票
    assert put(client, plan, "no-such-place", status="booked").status_code == 422
    assert put(client, plan, "louvre", status="maybe").status_code == 422
    assert put(client, plan, "louvre", status="booked", confirmation_code="x" * 61).status_code == 422
    assert client.put("/plans/nope/reservations/louvre", json={"status": "booked"}).status_code == 404


def test_a_record_for_a_place_removed_from_the_trip_is_kept_and_flagged(client, plan):
    pid = next(iter(items(client, plan)))
    put(client, plan, pid, status="booked", confirmation_code="KEEP-ME")
    # 版本 2：把这个地点从行程里删掉
    base = f"/plans/{plan['plan_id']}"
    removed = client.post(f"{base}/edit", json={"itinerary": plan["itinerary"],
                                                "edit": {"op": "remove", "place_id": pid}}).json()
    assert client.post(f"{base}/versions", json={"itinerary": removed["itinerary"], "name": "", "note": ""}
                       ).status_code == 201
    got = items(client, plan)
    assert pid in got and got[pid]["in_itinerary"] is False and got[pid]["confirmation_code"] == "KEEP-ME"
    assert got[pid]["urgency"] == "booked"                                   # 不在行程里了，但已订的记录还在


def test_shifting_the_dates_flags_bookings_made_for_the_old_date(client, plan):
    pid = next(iter(items(client, plan)))
    old_visit = items(client, plan)[pid]["visit_date"]
    put(client, plan, pid, status="booked", confirmation_code="OLD")
    assert items(client, plan)[pid]["date_changed"] is False
    client.post(f"/plans/{plan['plan_id']}/shift", json={"start_date": "2026-10-08"})
    it = items(client, plan)[pid]
    assert it["visit_date"] != old_visit and it["booked_for"] == old_visit and it["date_changed"] is True


def test_reservation_records_are_private_to_the_owner(client, plan):
    pid = next(iter(items(client, plan)))
    put(client, plan, pid, status="booked", confirmation_code="SECRET-CODE")
    token = client.post(f"/plans/{plan['plan_id']}/share").json()["token"]
    assert "SECRET-CODE" not in client.get(f"/share/{token}").text


def test_the_pdf_html_shows_booked_codes_and_deadlines(client, plan):
    pid = next(p for p in items(client, plan) if p in WITH_LEAD)
    put(client, plan, pid, status="booked", confirmation_code="PDF-CODE-9", slot="14:30")
    html = client.get(f"/plans/{plan['plan_id']}/itinerary.html").text
    assert "PDF-CODE-9" in html and "已订" in html and "14:30" in html
    md = client.get(f"/plans/{plan['plan_id']}/itinerary.md").text
    assert "PDF-CODE-9" in md
    other = next((p for p in items(client, plan) if p in WITH_LEAD and p != pid), None)
    if other:
        assert "建议最晚" in html


def test_duplicate_copies_records_and_delete_removes_them(client, plan, store):
    pid = next(iter(items(client, plan)))
    put(client, plan, pid, status="booked", confirmation_code="COPY-ME")
    copy = client.post(f"/plans/{plan['plan_id']}/duplicate").json()
    got = client.get(f"/plans/{copy['plan_id']}/reservations").json()["items"]
    assert any(i["confirmation_code"] == "COPY-ME" for i in got)
    client.delete(f"/plans/{plan['plan_id']}")
    with store.database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM reservations WHERE plan_id = ?", (plan["plan_id"],)).fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM reservations WHERE plan_id = ?", (copy["plan_id"],)).fetchone()[0] == 1


def test_an_old_version_lists_its_own_checklist(client, plan):
    first = items(client, plan, version=1)
    assert first == items(client, plan)
