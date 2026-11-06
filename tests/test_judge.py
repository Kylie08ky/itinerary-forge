"""LLM Judge 主题契合度（规格 M07 S5，开发者评测工具）：解析与容错、提示词内容。

不在产品界面里：没有接口、不存库。用假模型测流程；真模型的判断力靠 scripts/eval_judge.py（需要本地 Ollama）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.judge import (
    MAX_REASON_CHARS,
    SYSTEM,
    JudgeError,
    JudgeUnavailable,
    evaluate,
)
from app.library import load_library
from app.llm import LLMError
from app.versions import PlanStore


class Scripted:
    """评审提示词来了就按顺序给预设回答（最后一个会重复）；其它调用（写每天说明）一律「模型没开」，走模板。"""

    model_name = "fake-judge"
    backend = "stub"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict]] = []          # 只记评审的调用

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        if "旅行行程的评审" not in system:
            raise LLMError("narration off")
        self.calls.append((system, user, schema))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


def ok(score=4, reason="行程以博物馆为主，符合主题"):
    return {"score": score, "reason": reason}


@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(root=tmp_path / "plans")


def make_client(store, model) -> TestClient:
    return TestClient(create_app(store=store, client_factory=lambda: model))


def make_plan(client, theme="art") -> str:
    r = client.post("/plans", json={
        "destination": "Paris", "start_date": "2026-10-14", "end_date": "2026-10-17", "budget_level": "medium",
        "travel_style": "balanced", "interests": [theme], "theme": theme,
        "arrival_time": "2026-10-14 10:30", "departure_time": "2026-10-17 18:00"})
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def itinerary(store, theme="art"):
    c = make_client(store, Scripted(ok()))
    pid = make_plan(c, theme)
    return store.load(pid)


# --------------------------------------------------------------------------- #
# evaluate()
# --------------------------------------------------------------------------- #
def test_a_valid_answer_becomes_a_result_with_the_theme_name(store):
    it = itinerary(store)
    r = evaluate(it, Scripted(ok(5, "几乎全是美术馆")), load_library("paris"))
    assert (r.score, r.reason, r.theme_name) == (5, "几乎全是美术馆", it.generation.theme_name)


@pytest.mark.parametrize("raw,score", [({"score": "4", "reason": "x"}, 4), ({"score": 3.0, "reason": "x"}, 3),
                                       ({"score": 1, "reason": "x"}, 1)])
def test_scores_that_are_clearly_integers_in_another_shape_are_accepted(store, raw, score):
    assert evaluate(itinerary(store), Scripted(raw), load_library("paris")).score == score


def test_a_long_reason_is_cut_to_the_limit(store):
    r = evaluate(itinerary(store), Scripted(ok(4, "很长的理由" * 30)), load_library("paris"))
    assert len(r.reason) == MAX_REASON_CHARS and r.reason.endswith("…")


@pytest.mark.parametrize("bad", ["不是 JSON", "{}", {"score": 6, "reason": "x"}, {"score": 0, "reason": "x"},
                                 {"score": 4.5, "reason": "x"}, {"score": True, "reason": "x"},
                                 {"score": 4}, {"score": 4, "reason": "  "}, {"score": "很高", "reason": "x"},
                                 "[1, 2]"])
def test_an_invalid_answer_is_retried_once_then_reported(store, bad):
    model = Scripted(bad)
    with pytest.raises(JudgeError):
        evaluate(itinerary(store), model, load_library("paris"))
    assert len(model.calls) == 2


def test_a_bad_first_answer_is_recovered_by_the_retry(store):
    model = Scripted("乱码", ok(4))
    assert evaluate(itinerary(store), model, load_library("paris")).score == 4
    assert len(model.calls) == 2


def test_a_dead_model_is_reported_at_once_without_a_retry(store):
    model = Scripted(LLMError("连接被拒绝"))
    with pytest.raises(JudgeUnavailable, match="模型没有响应"):
        evaluate(itinerary(store), model, load_library("paris"))
    assert len(model.calls) == 1


def test_an_itinerary_without_a_theme_cannot_be_judged(store):
    it = itinerary(store).model_copy(update={"generation": None})
    with pytest.raises(JudgeError, match="不是按主题生成"):
        evaluate(it, Scripted(ok()), load_library("paris"))


def test_the_prompt_has_the_theme_and_each_days_items_but_nothing_about_prices_or_hours(store):
    it = itinerary(store)
    lib = load_library("paris")
    model = Scripted(ok())
    evaluate(it, model, lib)
    system, user, schema = model.calls[0]
    assert system == SYSTEM and "只评一件事" in system and "不要评路线" in system
    assert it.generation.theme_name in user and lib.themes[it.generation.theme_id].tagline in user
    for n, day in enumerate(it.days, start=1):
        assert f"第 {n} 天：" in user
        assert day.items[0].title in user
        assert f"{day.items[0].time} {day.items[0].title}" in user                         # 带开始时间
    for word in ("€", "票价", "闭馆", "开放时间", "确认码"):
        assert word not in user
    assert schema["properties"]["score"]["minimum"] == 1 and schema["properties"]["score"]["maximum"] == 5


def test_mixed_themes_all_appear_in_the_prompt(store):
    c = make_client(store, Scripted(ok()))
    r = c.post("/plans", json={
        "destination": "Paris", "start_date": "2026-10-14", "end_date": "2026-10-17", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"],
        "themes": [{"id": "art", "weight": 3}, {"id": "food", "weight": 2}],
        "arrival_time": "2026-10-14 10:30", "departure_time": "2026-10-17 18:00"})
    if r.status_code != 201:
        pytest.skip("混选的请求格式不同")
    it = store.load(r.json()["plan_id"])
    model = Scripted(ok())
    evaluate(it, model, load_library("paris"))
    assert len(it.generation.themes) >= 2
    lib = load_library("paris")
    for tid in it.generation.themes:
        assert lib.themes[tid].name_zh in model.calls[0][1]


# --------------------------------------------------------------------------- #
# 不在产品里
# --------------------------------------------------------------------------- #
def test_there_is_no_judge_endpoint_in_the_product(store):
    c = make_client(store, Scripted(ok()))
    pid = make_plan(c)
    assert c.get(f"/plans/{pid}/judge").status_code in (404, 405)
    assert c.post(f"/plans/{pid}/judge").status_code in (404, 405)


def test_generation_never_calls_the_judge(store):
    model = Scripted(ok())
    make_plan(make_client(store, model))
    assert model.calls == []


def test_the_judge_table_from_the_short_lived_v12_is_dropped_on_open(tmp_path):
    import sqlite3

    from app.db import Database

    path = tmp_path / "v12.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE judge_results (plan_id TEXT, version INTEGER, score INTEGER)")
    con.commit()
    con.close()
    with Database(path).connect() as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "judge_results" not in tables


def test_the_prompt_tells_the_judge_when_and_where_so_light_based_themes_can_be_judged(store):
    it = itinerary(store, "photography")
    lib = load_library("paris")
    model = Scripted(ok())
    evaluate(it, model, lib)
    user = model.calls[0][1]
    assert "室外" in user and ("黄昏最佳" in user or "上午最佳" in user)               # 摄影要看光线和时段
    placed = [i for d in it.days for i in d.items if i.place_id]
    assert all(i.title in user for i in placed)
