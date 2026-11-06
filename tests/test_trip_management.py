"""行程管理：平移日期、复制、归档（规格 M04 S8 / S13 / S14）。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.generator import generate_from_theme
from app.library import load_library
from app.llm import LLMError
from app.schemas import TravelRequest
from app.shifting import ShiftError, new_conflicts, shift_plan
from app.validator import validate_itinerary
from app.versions import PlanNotFound, PlanStore

LIB = load_library("paris")


def make_request(**over) -> TravelRequest:
    body = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["art"], "arrival_time": "2026-10-03T10:30:00",
            "departure_time": "2026-10-06T18:00:00"}
    body.update(over)
    return TravelRequest.model_validate(body)


@pytest.fixture(scope="module")
def trip():
    req = make_request()
    return req, generate_from_theme(req, "art", client=None, library=LIB).itinerary


# --------------------------------------------------------------------------- #
# 纯函数：平移
# --------------------------------------------------------------------------- #
def test_shift_moves_every_date_and_keeps_the_rest(trip):
    req, it = trip
    it2, req2 = shift_plan(it, req, date(2026, 10, 8), LIB)
    assert req2.start_date == date(2026, 10, 8) and req2.end_date == date(2026, 10, 11)
    assert [d.date for d in it2.days] == [d.date + timedelta(days=5) for d in it.days]
    assert it2.trip.date_range.start == date(2026, 10, 8) and it2.trip.date_range.end == date(2026, 10, 11)
    assert req2.arrival_time == datetime(2026, 10, 8, 10, 30) and req2.departure_time == datetime(2026, 10, 11, 18, 0)
    for a, b in zip(it.days, it2.days, strict=True):                       # 时间、地点、顺序原样
        assert [(i.time, i.title, i.place_id) for i in a.items] == [(i.time, i.title, i.place_id) for i in b.items]
    assert it.days[0].date == date(2026, 10, 3)                             # 原对象没被改动


def test_shift_backwards_works_too(trip):
    req, it = trip
    it2, req2 = shift_plan(it, req, date(2026, 9, 28), LIB)
    assert req2.start_date == date(2026, 9, 28) and it2.days[0].date == date(2026, 9, 28)


def test_shift_carries_the_flights_or_trains_along(trip):
    """填了来回交通的行程平移：去程到达、返程出发也要跟着挪；以前请求对不上，接口 500，页面误报「离线」。"""
    _, it = trip
    req = make_request(transport={
        "outbound": {"mode": "flight", "number": "MU553", "depart_at": "2026-10-03T06:00:00",
                     "arrive_at": "2026-10-03T10:30:00"},
        "inbound": {"mode": "flight", "number": "MU554", "depart_at": "2026-10-06T18:00:00"}})
    it2, req2 = shift_plan(it, req, date(2026, 10, 4), LIB)
    assert req2.transport.outbound.arrive_at == datetime(2026, 10, 4, 10, 30)
    assert req2.transport.outbound.depart_at == datetime(2026, 10, 4, 6, 0)
    assert req2.transport.inbound.depart_at == datetime(2026, 10, 7, 18, 0)
    assert req2.transport.outbound.number == "MU553"
    assert req2.arrival_time == datetime(2026, 10, 4, 10, 30)


def test_shifting_to_the_same_date_is_refused(trip):
    req, it = trip
    with pytest.raises(ShiftError, match="日期没变"):
        shift_plan(it, req, req.start_date, LIB)


def test_shift_clears_the_date_bound_reasons_and_says_so(trip):
    req, it = trip
    assert it.generation and it.generation.day_reasons
    it2, _ = shift_plan(it, req, date(2026, 10, 8), LIB)
    assert it2.generation.day_reasons == {}
    assert any("平移" in n for n in it2.generation.notes)
    assert not any("平移" in n for n in it.generation.notes)


def test_shifting_again_does_not_repeat_the_note(trip):
    # 用户反馈：平移过两次的行程里，「日期平移后…」的提示出现了两条一模一样的
    req, it = trip
    it2, req2 = shift_plan(it, req, date(2026, 10, 8), LIB)
    it3, _ = shift_plan(it2, req2, date(2026, 10, 15), LIB)
    assert sum("平移" in n for n in it3.generation.notes) == 1


def test_shifted_itinerary_is_consistent_with_the_shifted_request(trip):
    req, it = trip
    it2, req2 = shift_plan(it, req, date(2026, 10, 8), LIB)
    # 平移只该影响「哪天是星期几」相关的规则；日期、天数、抵离缓冲这类规则的 error 数不变
    err = lambda r: sorted(f.code for f in r.errors if f.code not in ("closed_day", "long_term_closed"))  # noqa: E731
    assert err(validate_itinerary(it2, req2)) == err(validate_itinerary(it, req))


def test_new_conflicts_lists_only_what_the_shift_caused(trip):
    req, it = trip
    before = validate_itinerary(it, req)
    found = None
    for k in range(1, 8):                                                   # 找一个会让某天撞上闭馆日的平移量
        it2, req2 = shift_plan(it, req, req.start_date + timedelta(days=k), LIB)
        after = validate_itinerary(it2, req2)
        conflicts = new_conflicts(it, before, it2, after)
        if conflicts:
            found = (k, it2, after, conflicts)
            break
    assert found, "艺术主题里有周二 / 周一闭馆的馆，总有一个平移量会撞上"
    _, it2, after, conflicts = found
    for c in conflicts:
        assert c["code"] in ("closed_day", "long_term_closed")
        assert c["day"] and c["title"] and "闭馆" in c["message"]
    # 平移前就有的问题不算「因平移新出现的」
    pre = {(f.code, it.days[[d.date.isoformat() for d in it.days].index(f.day)].items[f.item_index].title)
           for f in before.findings if f.day and f.item_index is not None}
    assert all((c["code"], c["title"]) not in pre for c in conflicts)
    assert len(conflicts) <= len([f for f in after.findings if f.code in ("closed_day", "long_term_closed")])


# --------------------------------------------------------------------------- #
# 存储：每个版本带着自己的需求、归档、复制
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(root=tmp_path / "plans")


def test_a_version_carries_its_own_request_and_new_versions_inherit_the_latest(store, trip):
    req, it = trip
    pid = store.create(req, it)
    it2, req2 = shift_plan(it, req, date(2026, 10, 8), LIB)
    v2 = store.add_version(pid, it2, trigger="date-shift", request=req2)
    assert store.request(pid, 1).start_date == date(2026, 10, 3)
    assert store.request(pid, v2).start_date == date(2026, 10, 8)
    assert store.request(pid).start_date == date(2026, 10, 8)               # 不指定版本 = 最新版本的需求
    v3 = store.add_version(pid, it2, trigger="manual-save")                 # 之后的版本默认沿用最新的需求
    assert store.request(pid, v3).start_date == date(2026, 10, 8)
    v4 = store.add_version(pid, it, trigger="rollback", request=store.request(pid, 1))
    assert store.request(pid, v4).start_date == date(2026, 10, 3)


def test_summaries_show_the_dates_of_the_latest_version(store, trip):
    req, it = trip
    pid = store.create(req, it)
    it2, req2 = shift_plan(it, req, date(2026, 10, 8), LIB)
    store.add_version(pid, it2, trigger="date-shift", request=req2)
    assert store.summaries()[0].request.start_date == date(2026, 10, 8)


def test_archive_and_restore(store, trip):
    req, it = trip
    pid = store.create(req, it)
    assert [s.plan_id for s in store.summaries()] == [pid] and store.is_archived(pid) is False
    store.set_archived(pid, True)
    assert store.is_archived(pid) is True
    assert store.summaries() == [] and [s.plan_id for s in store.summaries(archived=True)] == [pid]
    store.set_archived(pid, False)
    assert [s.plan_id for s in store.summaries()] == [pid]
    with pytest.raises(PlanNotFound):
        store.set_archived("nope", True)


def test_duplicate_copies_the_latest_version_into_an_independent_plan(store, trip):
    req, it = trip
    pid = store.create(req, it)
    it2, req2 = shift_plan(it, req, date(2026, 10, 8), LIB)
    store.add_version(pid, it2, trigger="date-shift", request=req2)
    new = store.duplicate(pid)
    assert new != pid and store.latest_version(new) == 1
    assert store.load(new).days[0].date == date(2026, 10, 8) and store.request(new).start_date == date(2026, 10, 8)
    assert store.copied_from(new) == pid and store.copied_from(pid) is None
    store.add_version(new, it, trigger="manual-save", request=req)          # 改副本不动原行程
    assert store.latest_version(pid) == 2 and store.load(pid).days[0].date == date(2026, 10, 8)


def test_a_database_from_before_these_columns_is_upgraded_in_place(tmp_path, trip):
    root = tmp_path / "plans"
    req, it = trip
    s1 = PlanStore(root=root)
    pid = s1.create(req, it)
    with sqlite3.connect(root / "plans.db") as con:
        con.execute("ALTER TABLE plans DROP COLUMN archived_at")
        con.execute("ALTER TABLE plans DROP COLUMN copied_from")
        con.execute("ALTER TABLE versions DROP COLUMN request_json")
        con.execute("PRAGMA user_version = 4")
    s2 = PlanStore(root=root)
    assert s2.request(pid).start_date == date(2026, 10, 3) and s2.is_archived(pid) is False
    s2.set_archived(pid, True)
    assert s2.summaries() == []


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(create_app(store=store, client_factory=DeadLLM))


@pytest.fixture
def plan(client) -> dict:
    r = client.post("/plans", json={
        "destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art", "arrival_time": "2026-10-03 10:30",
        "departure_time": "2026-10-06 18:00"})
    assert r.status_code == 201, r.text
    return r.json()


def shift(client, plan, start: str):
    return client.post(f"/plans/{plan['plan_id']}/shift", json={"start_date": start})


def test_shift_endpoint_creates_a_labelled_version_with_shifted_dates(client, plan):
    r = shift(client, plan, "2026-10-08")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 2
    assert [d["date"] for d in body["itinerary"]["days"]] == ["2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
    hist = client.get(f"/plans/{plan['plan_id']}").json()["history"]
    assert hist[-1]["trigger"] == "date-shift"
    summary = hist[-1]["change_summary"]
    assert "+5 天" in summary and "10-03" in summary and "10-08" in summary
    assert isinstance(body["conflicts"], list)


def test_old_versions_are_still_checked_against_their_own_dates(client, plan):
    pid = plan["plan_id"]
    before = client.get(f"/plans/{pid}?version=1").json()
    shift(client, plan, "2026-10-08")
    after = client.get(f"/plans/{pid}?version=1").json()
    assert after["itinerary"]["days"][0]["date"] == "2026-10-03"
    assert after["report"] == before["report"]                              # 旧版本的质检结果不受平移影响


def test_shift_to_the_same_date_is_422_and_makes_no_version(client, plan):
    r = shift(client, plan, "2026-10-03")
    assert r.status_code == 422 and "日期没变" in r.text
    assert client.get(f"/plans/{plan['plan_id']}").json()["latest_version"] == 1


def test_shift_reports_conflicts_with_closed_days(client, plan):
    found = None
    for k in range(1, 8):
        start = (date(2026, 10, 3) + timedelta(days=k)).isoformat()
        r = shift(client, plan, start)
        assert r.status_code == 201
        if r.json()["conflicts"]:
            found = r.json()
            break
        client.post(f"/plans/{plan['plan_id']}/versions/1/restore")           # 回到原日期再试下一个
    assert found, "总有一个平移量会让某天撞上闭馆日"
    c = found["conflicts"][0]
    assert {"day", "title", "message", "code"} <= set(c)


def test_edit_and_save_after_a_shift_use_the_shifted_dates(client, plan):
    pid = plan["plan_id"]
    shifted = shift(client, plan, "2026-10-08").json()
    body = {"itinerary": shifted["itinerary"], "name": "平移后", "note": ""}
    saved = client.post(f"/plans/{pid}/versions", json=body)
    assert saved.status_code == 201, saved.text
    assert saved.json()["itinerary"]["days"][0]["date"] == "2026-10-08"
    assert client.get(f"/plans/{pid}").json()["itinerary"]["days"][0]["date"] == "2026-10-08"


def test_rollback_to_a_pre_shift_version_restores_the_old_dates_and_request(client, plan):
    pid = plan["plan_id"]
    shift(client, plan, "2026-10-08")
    r = client.post(f"/plans/{pid}/versions/1/restore")
    assert r.status_code == 201 and r.json()["itinerary"]["days"][0]["date"] == "2026-10-03"
    card = next(c for c in client.get("/plan-cards").json() if c["plan_id"] == pid)
    assert card["start_date"] == "2026-10-03"


def test_cards_show_the_shifted_dates(client, plan):
    shift(client, plan, "2026-10-08")
    card = next(c for c in client.get("/plan-cards").json() if c["plan_id"] == plan["plan_id"])
    assert (card["start_date"], card["end_date"]) == ("2026-10-08", "2026-10-11")


def test_duplicate_endpoint(client, plan):
    pid = plan["plan_id"]
    r = client.post(f"/plans/{pid}/duplicate")
    assert r.status_code == 201
    copy = r.json()
    assert copy["plan_id"] != pid and copy["version"] == 1
    assert copy["itinerary"]["days"] == plan["itinerary"]["days"] or len(copy["itinerary"]["days"]) == 4
    cards = {c["plan_id"]: c for c in client.get("/plan-cards").json()}
    assert cards[copy["plan_id"]]["copied_from"] == pid and cards[pid]["copied_from"] is None
    detail = client.get(f"/plans/{copy['plan_id']}").json()
    assert detail["copied_from"] == pid
    # 各改各的
    client.post(f"/plans/{copy['plan_id']}/versions", json={"itinerary": copy["itinerary"], "name": "x", "note": ""})
    assert client.get(f"/plans/{pid}").json()["latest_version"] == 1


def test_archive_endpoint_hides_from_the_default_list_but_keeps_everything(client, plan):
    pid = plan["plan_id"]
    token = client.post(f"/plans/{pid}/share").json()["token"]
    r = client.put(f"/plans/{pid}/archive", json={"archived": True})
    assert r.status_code == 200 and r.json() == {"archived": True}
    assert pid not in {c["plan_id"] for c in client.get("/plan-cards").json()}
    archived = client.get("/plan-cards", params={"archived": "true"}).json()
    assert [c["plan_id"] for c in archived] == [pid] and archived[0]["archived"] is True
    assert client.get(f"/plans/{pid}").json()["archived"] is True            # 仍能打开
    assert client.get(f"/share/{token}").status_code == 200                  # 分享链接不受影响
    client.put(f"/plans/{pid}/archive", json={"archived": False})
    assert pid in {c["plan_id"] for c in client.get("/plan-cards").json()}


def test_management_endpoints_404_for_unknown_plans(client):
    assert client.post("/plans/nope/shift", json={"start_date": "2026-10-08"}).status_code == 404
    assert client.post("/plans/nope/duplicate").status_code == 404
    assert client.put("/plans/nope/archive", json={"archived": True}).status_code == 404


# --------------------------------------------------------------------------- #
# 永久删除（规格 M04 S15）
# --------------------------------------------------------------------------- #
def test_store_delete_removes_the_plan_with_all_its_versions_shares_and_tasks(store, trip):
    req, it = trip
    pid = store.create(req, it)
    store.add_version(pid, it, trigger="manual-save")
    token = store.create_share(pid)
    other = store.create(req, it)
    store.delete(pid)
    assert pid not in store.list_plans() and other in store.list_plans()
    with pytest.raises(PlanNotFound):
        store.latest_version(pid)
    assert store.resolve_share(token) is None
    with store.database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM versions WHERE plan_id = ?", (pid,)).fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM versions WHERE plan_id = ?", (other,)).fetchone()[0] == 1


def test_deleting_the_original_keeps_its_copies_and_clears_their_copied_from(store, trip):
    req, it = trip
    pid = store.create(req, it)
    copy = store.duplicate(pid)
    store.delete(pid)
    assert store.copied_from(copy) is None and store.load(copy).days


def test_store_delete_unknown_plan_raises(store):
    with pytest.raises(PlanNotFound):
        store.delete("nope")


def test_delete_endpoint_removes_everything_and_the_share_link_stops_working(client, plan):
    pid = plan["plan_id"]
    token = client.post(f"/plans/{pid}/share").json()["token"]
    client.post(f"/plans/{pid}/duplicate")
    assert client.delete(f"/plans/{pid}").status_code == 204
    assert client.get(f"/plans/{pid}").status_code == 404
    assert pid not in {c["plan_id"] for c in client.get("/plan-cards").json()}
    assert client.get(f"/share/{token}").status_code == 404
    assert len(client.get("/plan-cards").json()) == 1                      # 副本还在


def test_delete_works_for_archived_plans_and_unknown_ids_are_404(client, plan):
    pid = plan["plan_id"]
    client.put(f"/plans/{pid}/archive", json={"archived": True})
    assert client.delete(f"/plans/{pid}").status_code == 204
    assert client.get("/plan-cards", params={"archived": "true"}).json() == []
    assert client.delete(f"/plans/{pid}").status_code == 404
