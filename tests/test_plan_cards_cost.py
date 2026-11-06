"""「我的行程」卡片接口的开销（规格 M01「数据与接口」）：打开数据库的次数不随行程数增长。

背景：手机底栏靠 /plan-cards 判断「今天」标签要不要出现，每次进页面都会请求；
行程一多，每份各开两次数据库连接（每次约 1.6ms）就成了接口耗时的大头。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.db import Database
from app.llm import LLMError
from app.versions import PlanStore


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


BODY = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-05", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art"}


@pytest.fixture
def env(tmp_path):
    store = PlanStore(root=tmp_path / "plans")
    return TestClient(create_app(store=store, client_factory=DeadLLM)), store


def _connects_during_cards(client: TestClient, monkeypatch) -> tuple[int, int]:
    calls: list[int] = []
    original = Database.connect

    def counting(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Database, "connect", counting)
        cards = client.get("/plan-cards").json()
    return len(calls), len(cards)


def test_plan_cards_open_the_database_a_constant_number_of_times(env, monkeypatch):
    client, store = env
    pid = client.post("/plans", json=BODY).json()["plan_id"]
    few_calls, few = _connects_during_cards(client, monkeypatch)
    for _ in range(7):
        store.duplicate(pid)
    many_calls, many = _connects_during_cards(client, monkeypatch)
    assert (few, many) == (1, 8)
    assert many_calls == few_calls, f"1 份行程开 {few_calls} 次连接，8 份开 {many_calls} 次：每份行程都在多开连接"
