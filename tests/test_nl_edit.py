"""自然语言修改（规格 M04 S2）：模型只负责把一句话翻译成「操作列表」，操作全部走编辑函数，
新增的地点只能来自地点库；做不到或没看懂的操作被跳过，并且告诉用户。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.editing import SetLock, apply_edit
from app.generator import generate_from_theme
from app.library import load_library
from app.llm import LLMError
from app.nl_edit import NlEditError, nl_revise
from app.schemas import TravelRequest
from app.versions import PlanStore

LIB = load_library("paris")
SAT = date(2026, 10, 3)


def make_request(days=5) -> TravelRequest:
    return TravelRequest(destination="Paris", start_date=SAT, end_date=SAT + timedelta(days=days - 1),
                         budget_level="medium", travel_style="balanced", interests=["art"])


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        self.prompts.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)


@pytest.fixture(scope="module")
def req():
    return make_request()


@pytest.fixture(scope="module")
def base(req):
    return generate_from_theme(req, "art", client=None, library=LIB).itinerary


def ids(it, d):
    return [i.place_id for i in next(x for x in it.days if x.date == d).items if i.place_id]


def all_ids(it):
    return {i.place_id for d in it.days for i in d.items if i.place_id}


def test_an_added_place_comes_from_the_library(base, req):
    d = date(2026, 10, 4)
    edit = {"op": "add", "place_id": "cafe-de-flore", "date": "2026-10-04"}
    llm = FakeLLM({"edits": [edit], "explanation": "加咖啡馆"})
    res = nl_revise(base, req, "第 2 天加一个咖啡馆", llm, LIB)
    assert "cafe-de-flore" in ids(res.itinerary, d)
    assert res.applied and "花神咖啡馆" in res.applied[0] and res.skipped == []
    item = next(i for i in next(x for x in res.itinerary.days if x.date == d).items if i.place_id == "cafe-de-flore")
    assert item.location.startswith("Café de Flore")           # 条目内容来自地点库，不是模型写的


def test_the_prompt_only_offers_library_places_that_are_not_already_in_the_trip(base, req):
    llm = FakeLLM({"edits": []})
    nl_revise(base, req, "换点别的", llm, LIB)
    _system, user = llm.prompts[0]
    assert "cafe-de-flore" in user
    for pid in all_ids(base):
        assert f"| {pid} |" not in user.split("可以加入的地点")[1]


def test_a_made_up_place_is_skipped_and_nothing_changes(base, req):
    llm = FakeLLM({"edits": [{"op": "add", "place_id": "totally-made-up-cafe", "date": "2026-10-04"}]})
    res = nl_revise(base, req, "加个咖啡馆", llm, LIB)
    assert res.applied == [] and res.skipped and "地点库" in res.skipped[0]
    assert res.itinerary == base


def test_malformed_operations_are_skipped_not_fatal(base, req):
    d = date(2026, 10, 4)
    llm = FakeLLM({"edits": [{"op": "teleport"}, {"nope": 1},
                             {"op": "add", "place_id": "cafe-de-flore", "date": "2026-10-04"}]})
    res = nl_revise(base, req, "x", llm, LIB)
    assert "cafe-de-flore" in ids(res.itinerary, d)
    assert len(res.skipped) == 2 and len(res.applied) == 1


def test_a_locked_place_is_not_touched_by_a_sentence(base, req):
    d = date(2026, 10, 4)
    target = ids(base, d)[0]
    locked = apply_edit(base, req, SetLock(place_id=target, locked=True), LIB).itinerary
    llm = FakeLLM({"edits": [{"op": "remove", "place_id": target}]})
    res = nl_revise(locked, req, f"删掉{target}", llm, LIB)
    assert res.applied == [] and "锁定" in res.skipped[0]
    assert target in ids(res.itinerary, d)


def test_at_most_five_operations_are_applied(base, req):
    pool = [p for p in ("cafe-de-flore", "angelina", "laduree-champs", "pierre-herme-st-germain",
                        "rue-montorgueil", "rue-cler", "pantheon") if p not in all_ids(base)]
    edits = [{"op": "add", "place_id": p, "date": "2026-10-04"} for p in pool]
    res = nl_revise(base, req, "每样都加", FakeLLM({"edits": edits}), LIB)
    assert len(res.applied) <= 5 and any("最多" in s for s in res.skipped)


def test_other_days_are_left_alone(base, req):
    llm = FakeLLM({"edits": [{"op": "add", "place_id": "cafe-de-flore", "date": "2026-10-04"}]})
    res = nl_revise(base, req, "第 2 天加个咖啡馆", llm, LIB)
    for day in base.days:
        if day.date != date(2026, 10, 4):
            assert next(x for x in res.itinerary.days if x.date == day.date) == day


def test_an_unusable_model_is_a_clear_error(base, req):
    with pytest.raises(NlEditError, match="模型"):
        nl_revise(base, req, "x", FakeLLM(LLMError("没启动")), LIB)
    with pytest.raises(NlEditError, match="解析"):
        nl_revise(base, req, "x", FakeLLM("这不是 JSON"), LIB)


def test_a_legacy_plan_without_a_theme_cannot_use_the_op_path(base, req):
    with pytest.raises(NlEditError, match="主题"):
        nl_revise(base.model_copy(update={"generation": None}), req, "x", FakeLLM({"edits": []}), LIB)


# --------------------------------------------------------------------------- #
# 接口：自然语言修改自动成为新版本
# --------------------------------------------------------------------------- #
def payload() -> dict:
    return {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-07",
            "budget_level": "medium", "travel_style": "balanced", "interests": ["art"], "theme": "art"}


def make_client(tmp_path, reply) -> TestClient:
    llm = FakeLLM(LLMError("生成时模型不可用"))
    app = create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=lambda: llm)
    c = TestClient(app)
    pid = c.post("/plans", json=payload()).json()["plan_id"]
    llm.reply = reply                                  # 生成完再换成「修改时」的回复
    c.fake_llm = llm                                   # 测试里可以继续改它的回复
    return c, pid


def test_revision_endpoint_makes_a_version_with_a_change_summary(tmp_path):
    c, pid = make_client(tmp_path, {"edits": [{"op": "add", "place_id": "cafe-de-flore", "date": "2026-10-04"}]})
    r = c.post(f"/plans/{pid}/revisions", json={"feedback": "第 2 天加一个咖啡馆"})
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == 2 and "findings" in body["report"]
    assert "2026-10-04" in body["change"]["changed_days"]
    assert any("花神咖啡馆" in a for a in body["change"]["added"])
    hist = c.get(f"/plans/{pid}").json()["history"]
    assert [(h["version"], h["trigger"]) for h in hist] == [(1, "initial"), (2, "user-feedback")]
    assert hist[1]["note"] == "第 2 天加一个咖啡馆"


def test_revision_endpoint_refuses_when_nothing_could_be_applied(tmp_path):
    c, pid = make_client(tmp_path, {"edits": [{"op": "add", "place_id": "made-up", "date": "2026-10-04"}]})
    r = c.post(f"/plans/{pid}/revisions", json={"feedback": "加一个不存在的店"})
    assert r.status_code == 422 and "地点库" in str(r.json()["detail"])
    assert c.get(f"/plans/{pid}").json()["latest_version"] == 1           # 没有生成空白新版本


# --------------------------------------------------------------------------- #
# 一句话要删 3 处及以上：先确认，再执行同样的操作（规格 M04，你的决定）
# --------------------------------------------------------------------------- #
def removals(it: dict, n: int) -> list[dict]:
    ids = [i["place_id"] for d in it["days"] for i in d["items"] if i.get("place_id")]
    return [{"op": "remove", "place_id": p} for p in ids[:n]]


def test_translate_returns_the_operations_without_applying_them(base, req):
    from app.nl_edit import translate
    victims = [i.place_id for d in base.days for i in d.items if i.place_id][:3]
    llm = FakeLLM({"edits": [{"op": "remove", "place_id": p} for p in victims]})
    t = translate(base, req, "删三个", llm, LIB)
    assert len(t.edits) == 3 and t.skipped == []


def test_a_sentence_that_deletes_three_places_asks_for_confirmation_first(tmp_path):
    c, pid = make_client(tmp_path, None)
    ops = removals(c.get(f"/plans/{pid}").json()["itinerary"], 3)
    c.fake_llm.reply = {"edits": ops}
    r = c.post(f"/plans/{pid}/revisions", json={"feedback": "把这三个都删掉"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["needs_confirm"] is True and len(detail["ops"]) == 3 and len(detail["preview"]) == 3
    assert c.get(f"/plans/{pid}").json()["latest_version"] == 1             # 确认之前什么都没改

    ok = c.post(f"/plans/{pid}/revisions", json={"feedback": "把这三个都删掉", "ops": detail["ops"]})
    assert ok.status_code == 201
    body = ok.json()
    assert body["version"] == 2 and len(body["change"]["applied"]) == 3
    left = {i["place_id"] for d in body["itinerary"]["days"] for i in d["items"] if i.get("place_id")}
    assert not left & {o["place_id"] for o in ops}


def test_two_deletions_need_no_confirmation(tmp_path):
    c, pid = make_client(tmp_path, None)
    c.fake_llm.reply = {"edits": removals(c.get(f"/plans/{pid}").json()["itinerary"], 2)}
    assert c.post(f"/plans/{pid}/revisions", json={"feedback": "删两个"}).status_code == 201


def test_confirmed_operations_are_validated_again_server_side(tmp_path):
    c, pid = make_client(tmp_path, None)
    bad = [{"op": "add", "place_id": "made-up", "date": "2026-10-04"}, {"op": "teleport"}]
    r = c.post(f"/plans/{pid}/revisions", json={"feedback": "x", "ops": bad})
    assert r.status_code == 422                                              # 一个都做不成
