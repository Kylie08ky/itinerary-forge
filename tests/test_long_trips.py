"""长行程（规格 07 Q5）：超长行程的天数提示、排不满的天合成一条说明、库里的点用完时不报错。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.cities import load_city
from app.library import load_library
from app.llm import LLMError
from app.versions import PlanStore

LIB = load_library("paris")


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


def trip(client, theme: str, days: int, **extra) -> dict:
    start = date(2026, 12, 12)
    end = start + timedelta(days=days - 1)
    payload = {"destination": "Paris", "start_date": start.isoformat(), "end_date": end.isoformat(),
               "budget_level": "medium", "travel_style": "balanced", "interests": [theme], "theme": theme,
               "arrival_time": f"{start} 10:00", "departure_time": f"{end} 18:00", **extra}
    r = client.post("/plans", json=payload)
    assert r.status_code == 201, r.text
    return client.get(f"/plans/{r.json()['plan_id']}").json()


def place_ids(day: dict) -> list[str]:
    return [i["place_id"] for i in day["items"] if i.get("place_id")]


# --------------------------------------------------------------------------- #
# 生成：14 天艺术
# --------------------------------------------------------------------------- #
def test_a_14_day_art_trip_has_places_every_day(client):
    days = trip(client, "art", 14)["itinerary"]["days"]
    assert len(days) == 14 and all(place_ids(d) for d in days)
    assert len({p for d in days for p in place_ids(d)}) == sum(len(place_ids(d)) for d in days)      # 没有重复的点


def test_a_relaxed_long_trip_has_two_daytime_places_a_day(client):
    days = trip(client, "art", 10, travel_style="relaxed")["itinerary"]["days"]
    for d in days[1:-1]:
        daytime = [i for i in d["items"] if i.get("place_id") and i["time"] < "16:30"]
        assert len(daytime) <= 2, d["date"]


# --------------------------------------------------------------------------- #
# 超长行程
# --------------------------------------------------------------------------- #
def test_paris_says_how_many_days_it_can_fill_and_the_form_note_mentions_it():
    advice = load_city("paris").suggested_days
    assert (advice.min, advice.max, advice.quality_max, advice.fill_max) == (4, 6, 14, 22)
    assert load_city("ludwigsburg").suggested_days.fill_max is None


def test_a_trip_beyond_what_the_library_can_fill_gets_one_merged_note_for_the_free_days(client):
    detail = trip(client, "entertainment", 30)
    notes = detail["itinerary"]["generation"]["notes"]
    assert not any("这天只排得下" in n for n in notes)
    merged = [n for n in notes if "排不满" in n and "自由安排" in n]
    assert len(merged) == 1, notes
    free = [d for d in detail["itinerary"]["days"] if d["theme"] == "自由安排"]
    assert all(d["items"][0]["type"] in ("rest", "meal", "logistics") for d in free)


def test_a_trip_within_what_the_library_can_fill_has_no_free_day_note(client):
    notes = trip(client, "art", 20)["itinerary"]["generation"]["notes"]
    assert not any("排不满" in n for n in notes)


def test_the_request_note_warns_beyond_the_fill_limit_but_never_blocks(client):
    detail = trip(client, "art", 25)
    assert detail["itinerary"]["days"]
    r = client.get("/cities").json()
    paris = next(c for c in r if c["id"] == "paris")
    assert paris["suggested_days"]["fill_max"] == 22
