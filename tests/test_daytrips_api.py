"""周边一日游的接口和「排一天」生成的行程（规格 08 3.4）。用真实的巴士 / 火车时间表和一日游目的地数据。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.daytrips import destinations_for
from app.library import load_library
from app.llm import LLMError
from app.versions import PlanStore

DAY = "2026-12-12"                      # 周六，圣诞市场季
SEASONAL = json.loads((Path(__file__).resolve().parents[1] / "data" / "seasonal" / "christmas-markets.json")
                      .read_text(encoding="utf-8"))["markets"]


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
def test_the_origins_are_stuttgart_and_aachen_with_german_and_chinese_names(client):
    got = {o["id"]: o for o in client.get("/daytrips/origins").json()}
    assert set(got) == {"stuttgart", "aachen"}
    assert got["stuttgart"]["name_local"] == "Stuttgart" and got["stuttgart"]["name_zh"] == "斯图加特"
    assert got["aachen"]["station"]["name"].lower().startswith("aachen")


def test_the_list_gives_every_destination_its_time_source_highlights_and_draft_count(client):
    body = client.get("/daytrips", params={"origin": "stuttgart", "date": DAY}).json()
    assert body["origin"]["name_zh"] == "斯图加特" and body["date"] == DAY
    dests = body["destinations"]
    assert len(dests) >= 7
    assert [d["minutes"] for d in dests] == sorted(d["minutes"] for d in dests)               # 近的在前
    for d in dests:
        assert d["source_url"].startswith("https://") and d["checked_at"].startswith("2026")
        assert d["arrive"] and d["last_return"] and d["station"]["name"]
        assert len(d["highlights"]) >= 2
        assert all(" · " in h["label"] or h["name_local"] is None for h in d["highlights"])
        assert d["places"] >= 5 and 0 <= d["drafts"] <= d["places"]
        assert d["city"]["name_local"] and d["city"]["name_zh"]


def test_christmas_markets_are_flagged_in_or_out_of_season_by_the_chosen_date(client):
    def by_city(day):
        body = client.get("/daytrips", params={"origin": "stuttgart", "date": day}).json()
        return {d["city"]["id"]: d for d in body["destinations"]}

    inside = by_city(DAY)
    summer = by_city("2026-07-04")
    for m in SEASONAL:
        if m["city"] not in inside:
            continue
        mine = next(x for x in inside[m["city"]]["seasonal"] if x["start"] == m["start"])
        assert mine["in_season"] is (m["start"] <= DAY <= m["end"]), m["city"]
        assert all(not x["in_season"] for x in summer[m["city"]]["seasonal"])
        assert mine["source_url"] == m["source_url"] and mine["confirmed_by"] == m["confirmed_by"]


def test_an_unknown_origin_is_a_404_that_names_the_supported_ones(client):
    r = client.get("/daytrips", params={"origin": "berlin", "date": DAY})
    assert r.status_code == 404 and "stuttgart" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# 排一天
# --------------------------------------------------------------------------- #
def test_the_request_endpoint_builds_a_task_payload_from_the_timetable(client):
    r = client.get("/daytrips/request", params={"origin": "stuttgart", "destination": "heidelberg", "date": DAY,
                                                "depart": "09:00", "back": "19:00"})
    assert r.status_code == 200
    body = r.json()
    p = body["payload"]
    assert p["destination"] == "heidelberg" and p["day_trip_from"] == "stuttgart" and p["theme"] == "daytrip"
    assert p["start_date"] == p["end_date"] == DAY
    pair = next(d.pair for d in destinations_for("stuttgart") if d.city.id == "heidelberg")
    assert body["minutes"] == pair.minutes and body["leave"] == "19:00" and body["last_return"] == pair.last_return


def test_a_return_that_is_too_early_is_a_422_with_the_reason(client):
    r = client.get("/daytrips/request", params={"origin": "stuttgart", "destination": "heidelberg", "date": DAY,
                                                "depart": "09:00", "back": "10:00"})
    assert r.status_code == 422 and "返程" in r.json()["detail"]["message"]


def make_day(client, destination: str, day: str = DAY, **kw) -> dict:
    q = {"origin": "stuttgart", "destination": destination, "date": day, **kw}
    payload = client.get("/daytrips/request", params=q).json()["payload"]
    r = client.post("/plans", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("dest", ["tuebingen", "heidelberg", "ludwigsburg"])
def test_the_one_day_plan_starts_at_the_station_ends_with_the_way_back_with_lunch_and_places(client, dest):
    plan = make_day(client, dest, depart="09:00", back="19:00")
    day = plan["itinerary"]["days"][0]
    items = day["items"]
    assert len(plan["itinerary"]["days"]) == 1
    first, last = items[0], items[-1]
    assert first["type"] == "logistics" and "斯图加特 Stuttgart" in first["title"] and "抵达" in first["title"]
    assert last["type"] == "logistics" and "返回斯图加特 Stuttgart" in last["title"]
    assert "机场" not in first["title"] + last["title"] + first["notes"] + last["notes"]
    assert sum(1 for i in items if i.get("place_id")) >= 2
    assert any(i["type"] == "meal" and "午餐" in i["title"] for i in items)
    assert last["time"] <= "18:30"                                                  # 19:00 返程 - 30 分钟缓冲
    assert not [f for f in plan["report"]["findings"] if f["severity"] == "error"], plan["report"]["findings"]


def test_the_market_is_scheduled_only_when_the_date_is_inside_its_season(client):
    in_dec = make_day(client, "heidelberg", "2026-12-12")["itinerary"]["days"][0]["items"]    # 不填返程：当季留到晚上
    in_jul = make_day(client, "heidelberg", "2026-07-04")["itinerary"]["days"][0]["items"]
    market = "heidelberg-weihnachtsmarkt"
    assert any(i.get("place_id") == market for i in in_dec), [i["title"] for i in in_dec]
    assert all(i.get("place_id") != market for i in in_jul)
    findings = make_day(client, "heidelberg", "2026-07-04")["report"]["findings"]
    assert not [f for f in findings if f["code"] == "closed_day"]


def test_the_validator_warns_when_an_out_of_season_market_is_forced_into_a_plan():
    from app.daytrips import build_day_request
    from app.generator import generate_from_theme
    from app.validator import validate_itinerary

    lib = load_library("heidelberg")
    in_season = generate_from_theme(build_day_request("stuttgart", "heidelberg", date(2026, 12, 12)), "daytrip",
                                    client=None, library=lib).itinerary
    assert any(i.place_id == "heidelberg-weihnachtsmarkt" for d in in_season.days for i in d.items)
    forced = in_season.model_copy(deep=True)
    forced.days[0].date = date(2026, 7, 4)                                  # 同一份行程，硬搬到夏天
    report = validate_itinerary(forced, build_day_request("stuttgart", "heidelberg", date(2026, 7, 4)))
    assert any(f.code == "closed_day" and "不在期内" in f.message for f in report.findings)


# --------------------------------------------------------------------------- #
# 一日游没有预算数据：不能让预算接口和导出 500（规格 08 3.6）
# --------------------------------------------------------------------------- #
def test_a_day_trip_has_no_budget_estimate_and_says_why_instead_of_failing(client):
    pid = make_day(client, "heidelberg")["plan_id"]
    r = client.get(f"/plans/{pid}/budget")
    assert r.status_code == 404
    assert "一日游" in r.json()["detail"] and "DB Navigator" in r.json()["detail"]
    assert client.get(f"/plans/{pid}/budget-settings").status_code == 200          # 出行人设置照常能用
    assert client.get(f"/plans/{pid}").json()["day_trip_from"] == "stuttgart"          # 前端据此隐藏住宿卡


def test_a_day_trip_still_exports_to_markdown_and_html_without_a_cost_section(client):
    pid = make_day(client, "heidelberg")["plan_id"]
    md = client.get(f"/plans/{pid}/itinerary.md")
    html = client.get(f"/plans/{pid}/itinerary.html")
    assert md.status_code == 200 and html.status_code == 200
    assert "费用估算" not in md.text and "Heidelberg" in md.text


def test_a_day_trip_with_a_budget_cap_is_still_generated(client):
    q = {"origin": "stuttgart", "destination": "heidelberg", "date": DAY}
    payload = client.get("/daytrips/request", params=q).json()["payload"]
    r = client.post("/plans", json={**payload, "budget_cap": 80})
    assert r.status_code == 201, r.text
