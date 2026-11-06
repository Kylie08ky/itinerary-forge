"""LLM 调用追踪与成本统计（规格 M07 S6、02-architecture §5）。

每次模型调用记一行：用途、模型、耗时、输入输出字数与 token、成功与否、费用；不存提示词原文（里面有用户的话）。
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.db import Database
from app.llm import LLMConfig, LLMError, OllamaClient
from app.llm_trace import LlmCallStore, TracedClient, trace_context
from app.versions import PlanStore


class Fake:
    model_name = "fake-model"
    backend = "fake"

    def __init__(self, reply: str = '{"ok": true}', usage: tuple[int, int] | None = (120, 30)):
        self.reply, self.usage = reply, usage

    def complete_json_with_usage(self, *, system: str, user: str, schema: dict):
        return self.reply, self.usage

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        return self.complete_json_with_usage(system=system, user=user, schema=schema)[0]


class Boom:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "t.db")


@pytest.fixture
def store(db) -> LlmCallStore:
    return LlmCallStore(db, price_in_per_mtok=1.0, price_out_per_mtok=3.0)


def call(client, system="S" * 10, user="U" * 40):
    return client.complete_json(system=system, user=user, schema={})


# --------------------------------------------------------------------------- #
def test_a_successful_call_is_recorded_with_sizes_tokens_and_latency(store):
    c = TracedClient(Fake(), store)
    assert call(c) == '{"ok": true}'
    [r] = store.recent()
    assert r["ok"] is True and r["error"] is None
    assert r["model"] == "fake-model" and r["backend"] == "fake"
    assert r["prompt_chars"] == 50 and r["response_chars"] == len('{"ok": true}')
    assert (r["prompt_tokens"], r["completion_tokens"]) == (120, 30)
    assert r["latency_ms"] >= 0
    assert r["purpose"] == "other"


def test_prompt_text_is_never_stored(store, db):
    call(TracedClient(Fake(), store), system="SECRET-SYSTEM", user="我的护照号 E12345678")
    with db.connect() as con:
        dump = json.dumps([dict(r) for r in con.execute("SELECT * FROM llm_calls")], ensure_ascii=False)
    assert "SECRET" not in dump and "E12345678" not in dump


def test_a_failed_call_is_recorded_and_the_error_still_propagates(store):
    with pytest.raises(LLMError, match="模型没启动"):
        call(TracedClient(Boom(), store))
    [r] = store.recent()
    assert r["ok"] is False and "模型没启动" in r["error"]
    assert r["prompt_tokens"] is None and r["cost_usd"] == 0


def test_purpose_and_task_come_from_the_surrounding_context(store):
    c = TracedClient(Fake(), store)
    with trace_context(purpose="narration", task_id="t1"):
        call(c)
        with trace_context(purpose="nl_edit"):               # 内层只改用途，任务号沿用外层
            call(c)
    call(c)
    rows = {r["purpose"]: r for r in store.recent()}
    assert rows["narration"]["task_id"] == "t1" and rows["nl_edit"]["task_id"] == "t1"
    assert rows["other"]["task_id"] is None


def test_cost_is_computed_from_token_prices(store):
    call(TracedClient(Fake(usage=(1_000_000, 500_000)), store))
    [r] = store.recent()
    assert r["cost_usd"] == pytest.approx(1.0 * 1 + 0.5 * 3)           # 100 万输入 $1 + 50 万输出 $1.5


def test_local_models_cost_nothing_by_default(db):
    free = LlmCallStore(db)
    call(TracedClient(Fake(usage=(5000, 5000)), free))
    assert free.recent()[0]["cost_usd"] == 0


def test_summary_aggregates_calls_failures_latency_tokens_and_cost(store):
    ok, bad = TracedClient(Fake(usage=(100, 10)), store), TracedClient(Boom(), store)
    with trace_context(purpose="narration"):
        call(ok)
        call(ok)
        with pytest.raises(LLMError):
            call(bad)
    with trace_context(purpose="nl_edit"):
        call(ok)
    s = store.summary()
    assert s["totals"]["calls"] == 4 and s["totals"]["failures"] == 1
    assert s["totals"]["prompt_tokens"] == 300 and s["totals"]["completion_tokens"] == 30
    by = {p["purpose"]: p for p in s["by_purpose"]}
    assert by["narration"]["calls"] == 3 and by["narration"]["failures"] == 1
    assert by["nl_edit"]["calls"] == 1 and by["nl_edit"]["failures"] == 0


def test_summary_can_be_limited_to_one_task(store):
    c = TracedClient(Fake(), store)
    with trace_context(task_id="a"):
        call(c)
    with trace_context(task_id="b"):
        call(c)
        call(c)
    assert store.summary(task_id="b")["totals"]["calls"] == 2
    assert len(store.recent(task_id="a")) == 1


def test_a_broken_recorder_never_breaks_the_actual_call(db):
    store = LlmCallStore(db)
    store.record = lambda **kw: (_ for _ in ()).throw(RuntimeError("磁盘满了"))   # type: ignore[method-assign]
    assert call(TracedClient(Fake(), store)) == '{"ok": true}'


def test_tracing_a_client_without_usage_info_still_works(store):
    class Plain:
        def complete_json(self, *, system: str, user: str, schema: dict) -> str:
            return "{}"

    call(TracedClient(Plain(), store))
    r = store.recent()[0]
    assert r["prompt_tokens"] is None and r["ok"] is True and r["model"] == "unknown"


# --------------------------------------------------------------------------- #
def test_ollama_client_reports_token_counts_from_the_response(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return httpx.Response(200, json={"message": {"content": "{}"}, "prompt_eval_count": 321, "eval_count": 45},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OllamaClient(LLMConfig())
    text, usage = client.complete_json_with_usage(system="s", user="u", schema={})
    assert text == "{}" and usage == (321, 45)
    assert client.complete_json(system="s", user="u", schema={}) == "{}"
    assert client.model_name == LLMConfig().ollama_model and client.backend == "ollama"


# --------------------------------------------------------------------------- #
# 接口：用量总览 + 一次生成的调用轨迹
# --------------------------------------------------------------------------- #
class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


def payload() -> dict:
    return {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["art"], "theme": "art"}


def test_usage_endpoint_starts_empty(client):
    body = client.get("/llm/usage").json()
    assert body["totals"]["calls"] == 0 and body["by_purpose"] == [] and body["recent"] == []


def test_generating_a_plan_shows_up_as_a_failed_narration_call(client):
    assert client.post("/plans", json=payload()).status_code == 201
    body = client.get("/llm/usage").json()
    assert body["totals"]["calls"] >= 1 and body["totals"]["failures"] >= 1
    assert any(p["purpose"] == "narration" for p in body["by_purpose"])
    assert body["recent"][0]["ok"] is False


def test_task_trace_lists_step_timings_model_calls_and_the_degrade_reason(client):
    tid = client.post("/tasks", json=payload()).json()["task_id"]
    import time

    for _ in range(100):
        t = client.get(f"/tasks/{tid}").json()
        if t["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)
    assert t["status"] == "succeeded"
    tr = client.get(f"/tasks/{tid}/trace").json()
    assert [s["name"] for s in tr["steps"]] == ["library", "schedule", "validate", "narrate"]
    assert all(s["seconds"] is not None and s["seconds"] >= 0 for s in tr["steps"])
    assert tr["llm"]["totals"]["calls"] >= 1 and tr["llm"]["totals"]["failures"] >= 1
    assert tr["calls"] and tr["calls"][0]["purpose"] == "narration"
    assert tr["degraded"] is True and "模板" in tr["degraded_reason"]
    # 同一份行程也能凭行程号找到它的生成轨迹
    assert client.get(f"/plans/{t['plan_id']}/trace").json()["task_id"] == tid


def test_trace_404s(client):
    assert client.get("/tasks/nope/trace").status_code == 404
    pid = client.post("/plans", json=payload()).json()["plan_id"]       # 同步生成的行程没有任务
    assert client.get(f"/plans/{pid}/trace").status_code == 404
    assert client.get("/plans/nope/trace").status_code == 404


# --------------------------------------------------------------------------- #
# 重置统计（用户随时可以清零；只清调用记录，不动行程）
# --------------------------------------------------------------------------- #
def test_reset_clears_every_call_record(store):
    c = TracedClient(Fake(), store)
    call(c)
    call(c)
    assert store.reset() == 2
    assert store.recent() == [] and store.summary()["totals"]["calls"] == 0


def test_usage_reset_endpoint_keeps_plans(client):
    pid = client.post("/plans", json=payload()).json()["plan_id"]
    assert client.get("/llm/usage").json()["totals"]["calls"] >= 1
    assert client.delete("/llm/usage").status_code == 204
    assert client.get("/llm/usage").json()["totals"]["calls"] == 0
    assert client.get(f"/plans/{pid}").status_code == 200
