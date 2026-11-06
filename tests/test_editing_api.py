"""编辑与版本的 HTTP 接口（规格 M04 S3–S7）。

编辑接口是无状态的：前端把「当前正在编辑的行程」和一个操作一起发来，后端算出新行程返回，
撤销栈留在前端；点「保存为新版本」时才写进数据库，并且保存前按地点库重新整理一遍事实。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.llm import LLMError
from app.versions import PlanStore


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


def payload(**over) -> dict:
    body = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-07",
            "budget_level": "medium", "travel_style": "balanced", "interests": ["art"], "theme": "art"}
    body.update(over)
    return body


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


@pytest.fixture
def plan(client) -> dict:
    r = client.post("/plans", json=payload())
    assert r.status_code == 201
    return r.json()


def places(itinerary: dict, d: str) -> list[str]:
    day = next(x for x in itinerary["days"] if x["date"] == d)
    return [i["place_id"] for i in day["items"] if i.get("place_id")]


def busy_day(itinerary: dict, n: int = 2) -> str:
    return next(d["date"] for d in itinerary["days"] if len(places(itinerary, d["date"])) >= n)


def edit(client, plan, op: dict, itinerary: dict | None = None):
    return client.post(f"/plans/{plan['plan_id']}/edit", json={"itinerary": itinerary or plan["itinerary"], "edit": op})


# --------------------------------------------------------------------------- #
def test_move_returns_the_new_itinerary_report_and_touched_days(client, plan):
    it = plan["itinerary"]
    src = busy_day(it)
    pid = places(it, src)[0]
    dst = next(d["date"] for d in it["days"] if d["date"] != src and places(it, d["date"]))
    r = edit(client, plan, {"op": "move", "place_id": pid, "to_date": dst})
    assert r.status_code == 200
    body = r.json()
    assert pid in places(body["itinerary"], dst) and pid not in places(body["itinerary"], src)
    assert set(body["touched"]) == {src, dst}
    assert "findings" in body["report"] and isinstance(body["notes"], list)


def test_moving_onto_a_closed_day_is_accepted_and_reported(client, plan):
    it = plan["itinerary"]
    louvre_day = next(d["date"] for d in it["days"] if "louvre" in places(it, d["date"]))
    tuesday = "2026-10-06"
    if louvre_day == tuesday:
        pytest.skip("卢浮宫恰好已经在周二")
    body = edit(client, plan, {"op": "move", "place_id": "louvre", "to_date": tuesday}).json()
    codes = {(f["code"], f["day"]) for f in body["report"]["findings"]}
    assert ("closed_day", tuesday) in codes


def test_invalid_edits_are_422_with_a_human_message(client, plan):
    it = plan["itinerary"]
    r = edit(client, plan, {"op": "add", "place_id": "not-in-library", "date": "2026-10-03"})
    assert r.status_code == 422 and "地点库" in r.json()["detail"]
    r = edit(client, plan, {"op": "move", "place_id": places(it, busy_day(it))[0], "to_date": "2030-01-01"})
    assert r.status_code == 422 and "日期" in r.json()["detail"]
    assert edit(client, plan, {"op": "teleport"}).status_code == 422                   # 不认识的操作
    assert client.post("/plans/nope/edit", json={"itinerary": it, "edit": {"op": "reschedule"}}).status_code == 404


def test_lock_then_reschedule_over_http_keeps_the_locked_place(client, plan):
    it = plan["itinerary"]
    d = busy_day(it)
    pid = places(it, d)[-1]
    before = next(i for i in next(x for x in it["days"] if x["date"] == d)["items"] if i.get("place_id") == pid)
    locked = edit(client, plan, {"op": "lock", "place_id": pid, "locked": True}).json()["itinerary"]
    after = edit(client, plan, {"op": "reschedule"}, itinerary=locked).json()["itinerary"]
    now = next(i for i in next(x for x in after["days"] if x["date"] == d)["items"] if i.get("place_id") == pid)
    assert now["time"] == before["time"] and now["locked"] is True


def test_alternatives_endpoint(client, plan):
    it = plan["itinerary"]
    pid = places(it, busy_day(it))[0]
    r = client.post(f"/plans/{plan['plan_id']}/alternatives", json={"itinerary": it, "place_id": pid})
    assert r.status_code == 200
    alts = r.json()
    assert 1 <= len(alts) <= 3 and all(a["reason"] and a["address"] for a in alts)
    used = {p for d in it["days"] for p in places(it, d["date"])}
    assert not used & {a["place_id"] for a in alts}
    assert client.post(f"/plans/{plan['plan_id']}/alternatives",
                       json={"itinerary": it, "place_id": "nope"}).status_code == 422


# --------------------------------------------------------------------------- #
# 保存版本 / 回滚
# --------------------------------------------------------------------------- #
def test_save_as_a_named_version_and_read_it_back(client, plan):
    it = plan["itinerary"]
    d = busy_day(it)
    edited = edit(client, plan, {"op": "reorder", "date": d, "place_ids": list(reversed(places(it, d)))}).json()
    r = client.post(f"/plans/{plan['plan_id']}/versions",
                    json={"itinerary": edited["itinerary"], "name": "朋友确认版", "note": "调了顺序"})
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == 2 and "findings" in body["report"]
    detail = client.get(f"/plans/{plan['plan_id']}").json()
    assert detail["latest_version"] == 2
    assert [(h["version"], h["name"], h["trigger"]) for h in detail["history"]] == [
        (1, "", "initial"), (2, "朋友确认版", "manual-save")]
    assert places(detail["itinerary"], d) == list(reversed(places(it, d)))
    assert places(client.get(f"/plans/{plan['plan_id']}?version=1").json()["itinerary"], d) == places(it, d)


def test_saving_does_not_trust_facts_sent_by_the_browser(client, plan):
    """购票链接、清单、条目的类型 / 位置都按地点库重算，前端伪造的内容不会进数据库。"""
    it = plan["itinerary"]
    forged = {**it, "booking_checklist": [{**it["booking_checklist"][0], "url": "https://evil.example/steal"}],
              "risk_notes": ["假的提醒"]}
    pid = it["booking_checklist"][0]["place_id"]
    day = next(d for d in forged["days"] if pid in places(forged, d["date"]))
    item = next(i for i in day["items"] if i.get("place_id") == pid)
    item["location"] = "伪造的位置"
    r = client.post(f"/plans/{plan['plan_id']}/versions", json={"itinerary": forged, "name": "x"})
    assert r.status_code == 201
    saved = r.json()["itinerary"]
    assert all("evil" not in (b["url"] or "") for b in saved["booking_checklist"])
    assert "假的提醒" not in saved["risk_notes"]
    day2 = next(d for d in saved["days"] if pid in places(saved, d["date"]))
    assert next(i for i in day2["items"] if i.get("place_id") == pid)["location"] != "伪造的位置"


def test_saving_rejects_unknown_places_and_free_form_items(client, plan):
    it = plan["itinerary"]
    bad = {**it, "days": [{**it["days"][0], "items": [{**it["days"][0]["items"][0], "place_id": "ghost"}]},
                          *it["days"][1:]]}
    assert client.post(f"/plans/{plan['plan_id']}/versions", json={"itinerary": bad}).status_code == 422
    free = {**it, "days": [{**it["days"][0], "items": [{"time": "10:00", "title": "神秘咖啡馆", "type": "food",
                                                      "location": "某处", "duration_minutes": 30}]},
                           *it["days"][1:]]}
    assert client.post(f"/plans/{plan['plan_id']}/versions", json={"itinerary": free}).status_code == 422


def test_restore_makes_a_new_version_equal_to_the_old_one(client, plan):
    it = plan["itinerary"]
    d = busy_day(it)
    edited = edit(client, plan, {"op": "remove", "place_id": places(it, d)[0]}).json()["itinerary"]
    v2 = client.post(f"/plans/{plan['plan_id']}/versions", json={"itinerary": edited, "name": "删了一个"}).json()
    assert v2["version"] == 2
    r = client.post(f"/plans/{plan['plan_id']}/versions/1/restore")
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == 3
    assert places(body["itinerary"], d) == places(it, d)                       # 内容回到 v1
    hist = client.get(f"/plans/{plan['plan_id']}").json()["history"]
    assert [h["version"] for h in hist] == [1, 2, 3]                           # v1、v2 都还在
    assert hist[2]["trigger"] == "rollback" and "v1" in hist[2]["note"]
    assert client.post(f"/plans/{plan['plan_id']}/versions/9/restore").status_code == 404


def test_save_requires_a_matching_trip(client, plan):
    it = plan["itinerary"]
    wrong = {**it, "days": it["days"][:-1]}
    r = client.post(f"/plans/{plan['plan_id']}/versions", json={"itinerary": wrong})
    assert r.status_code == 422 and "天" in r.json()["detail"]


def test_version_history_says_what_actually_changed(client, plan):
    """版本历史里每一版都要有「改了什么」，而不是笼统的一句「手动编辑后保存」。"""
    it = plan["itinerary"]
    d = busy_day(it)
    pid = places(it, d)[0]
    edited = edit(client, plan, {"op": "remove", "place_id": pid}).json()["itinerary"]
    client.post(f"/plans/{plan['plan_id']}/versions", json={"itinerary": edited, "name": "删一个"})
    client.post(f"/plans/{plan['plan_id']}/versions/1/restore")
    hist = client.get(f"/plans/{plan['plan_id']}").json()["history"]
    assert "天有改动" in hist[1]["change_summary"] and "移除" in hist[1]["change_summary"]
    assert "v2" in hist[2]["change_summary"] and "新增" in hist[2]["change_summary"]      # 回滚：相对上一版加回来了
