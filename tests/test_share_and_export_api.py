"""分享链接与导出接口（规格 M08 S4 / S5、M05 S5）。"""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.llm import LLMError
from app.versions import PlanStore


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
        "travel_style": "balanced", "interests": ["art"], "theme": "art"})
    assert r.status_code == 201
    return r.json()


# --------------------------------------------------------------------------- #
# 分享
# --------------------------------------------------------------------------- #
def test_creating_a_share_link_returns_a_long_random_token(client, plan):
    r = client.post(f"/plans/{plan['plan_id']}/share")
    assert r.status_code == 201
    body = r.json()
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", body["token"])
    assert body["path"] == f"/s/{body['token']}"


def test_creating_twice_returns_the_same_active_link(client, plan):
    a = client.post(f"/plans/{plan['plan_id']}/share").json()["token"]
    b = client.post(f"/plans/{plan['plan_id']}/share").json()["token"]
    assert a == b


def test_the_shared_route_carries_the_same_ticket_total_as_the_plan_page(client, plan):
    """分享页的统计卡要和行程页一样显示门票合计，不能写「待补」（规格 M08 S9）。"""
    token = client.post(f"/plans/{plan['plan_id']}/share").json()["token"]
    shared = client.get(f"/share/{token}").json()["route"]
    own = client.get(f"/plans/{plan['plan_id']}/route").json()
    assert shared["ticket_eur"] == own["ticket_eur"] and shared["ticket_eur"] is not None
    assert shared["ticket_currency"] == "EUR"


def test_share_status_tells_whether_a_link_is_active(client, plan):
    pid = plan["plan_id"]
    none = {"packing": False, "todos": False, "notes": False}
    assert client.get(f"/plans/{pid}/share").json() == {"active": False, "token": None, "path": None, "options": none}
    token = client.post(f"/plans/{pid}/share").json()["token"]
    got = client.get(f"/plans/{pid}/share").json()
    assert got == {"active": True, "token": token, "path": f"/s/{token}", "options": none}


def test_anyone_with_the_token_can_read_the_itinerary_and_route_without_other_ids(client, plan):
    token = client.post(f"/plans/{plan['plan_id']}/share").json()["token"]
    r = client.get(f"/share/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["itinerary"]["days"] and body["route"]["days"]
    assert len(body["route"]["days"]) == len(body["itinerary"]["days"])
    assert body["version"] == plan["version"]
    # 公开页不暴露行程号、需求原文（含住宿区域等）
    assert plan["plan_id"] not in r.text
    assert "request" not in body and "plan_id" not in body


def test_share_shows_the_latest_saved_version(client, plan):
    pid = plan["plan_id"]
    token = client.post(f"/plans/{pid}/share").json()["token"]
    it = plan["itinerary"]
    saved = client.post(f"/plans/{pid}/versions", json={"itinerary": it, "name": "朋友确认版", "note": ""}).json()
    assert client.get(f"/share/{token}").json()["version"] == saved["version"]


def test_revoked_link_returns_410_and_a_new_link_is_a_different_token(client, plan):
    pid = plan["plan_id"]
    token = client.post(f"/plans/{pid}/share").json()["token"]
    assert client.delete(f"/plans/{pid}/share").status_code == 204
    r = client.get(f"/share/{token}")
    assert r.status_code == 410
    assert "链接已失效" in r.text
    assert client.get(f"/plans/{pid}/share").json()["active"] is False
    again = client.post(f"/plans/{pid}/share").json()["token"]
    assert again != token
    assert client.get(f"/share/{token}").status_code == 410          # 老链接不会复活
    assert client.get(f"/share/{again}").status_code == 200


def test_unknown_token_is_404_and_revoking_without_a_link_is_harmless(client, plan):
    assert client.get("/share/not-a-real-token-0000000000000000").status_code == 404
    assert client.delete(f"/plans/{plan['plan_id']}/share").status_code == 204


def test_sharing_an_unknown_plan_is_404(client):
    assert client.post("/plans/nope/share").status_code == 404
    assert client.get("/plans/nope/share").status_code == 404
    assert client.delete("/plans/nope/share").status_code == 404


def test_old_database_without_the_shares_table_is_upgraded_in_place(tmp_path):
    root = tmp_path / "plans"
    store = PlanStore(root=root)
    with sqlite3.connect(root / "plans.db") as con:
        con.execute("DROP TABLE shares")
        con.execute("PRAGMA user_version = 2")
    store2 = PlanStore(root=root)
    with store2.database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM shares").fetchone()[0] == 0
    assert store is not store2


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def test_ics_endpoint_serves_a_calendar_download(client, plan):
    r = client.get(f"/plans/{plan['plan_id']}/itinerary.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert ".ics" in r.headers["content-disposition"]
    n_items = sum(len(d["items"]) for d in plan["itinerary"]["days"])
    assert r.text.count("BEGIN:VEVENT") == n_items


def test_kml_endpoint_serves_a_map_download(client, plan):
    r = client.get(f"/plans/{plan['plan_id']}/itinerary.kml")
    assert r.status_code == 200
    assert "kml" in r.headers["content-type"]
    assert r.text.count("<Folder>") == len(plan["itinerary"]["days"])


def test_route_endpoint_returns_colored_days_with_google_links(client, plan):
    r = client.get(f"/plans/{plan['plan_id']}/route")
    assert r.status_code == 200
    body = r.json()
    assert len(body["days"]) == len(plan["itinerary"]["days"])
    assert body["total_km"] > 0 and body["place_count"] > 0
    for d in body["days"]:
        assert re.fullmatch(r"#[0-9a-f]{6}", d["color"])
        if len(d["stops"]) < 2:                                   # 只有一个点的那天（凡尔赛一日游）没有路线可画
            assert d["google_urls"] == []
            continue
        assert d["google_urls"] and all(u.startswith("https://www.google.com/maps/dir/") for u in d["google_urls"])


def test_export_endpoints_404_for_unknown_plan_and_version(client, plan):
    for ext in ("ics", "kml"):
        assert client.get(f"/plans/nope/itinerary.{ext}").status_code == 404
        assert client.get(f"/plans/{plan['plan_id']}/itinerary.{ext}?version=99").status_code == 404
    assert client.get("/plans/nope/route").status_code == 404
