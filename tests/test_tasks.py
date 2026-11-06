"""后台生成任务与真实进度（规格 M01 S1 / S3 / S6）。"""

from __future__ import annotations

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.generator import STEPS
from app.llm import LLMError
from app.schemas import TravelRequest
from app.tasks import TaskRunner, TaskStore
from app.versions import PlanStore


def make_request(**kw) -> TravelRequest:
    base = dict(destination="Paris", start_date=date(2026, 10, 3), end_date=date(2026, 10, 6),
                budget_level="medium", travel_style="balanced", interests=["art"])
    base.update(kw)
    return TravelRequest(**base)


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


def dead_client():
    return DeadLLM()


@pytest.fixture
def stores(tmp_path):
    plans = PlanStore(root=tmp_path / "plans")
    return plans, TaskStore(plans.database)


# --------------------------------------------------------------------------- #
# 任务状态机
# --------------------------------------------------------------------------- #
def test_task_goes_through_the_four_real_steps(stores):
    plans, tasks = stores
    runner = TaskRunner(plans, tasks, client_factory=dead_client, inline=True)
    tid = runner.submit(make_request(), "art")
    t = tasks.get(tid)
    assert t.status == "succeeded" and t.error is None
    assert [s.name for s in t.steps] == list(STEPS)
    assert all(s.state == "done" for s in t.steps)
    stamps = [(s.started_at, s.finished_at) for s in t.steps]
    assert all(a and b and a <= b for a, b in stamps)
    assert [a for a, _ in stamps] == sorted(a for a, _ in stamps)       # 步骤是按顺序执行的
    assert t.plan_id and plans.load(t.plan_id).generation.theme_id == "art"


def test_progress_is_recorded_while_the_task_runs(stores, monkeypatch):
    """进度来自后台任务本身：在「排程」这一步执行时，数据库里已经是 library=done / schedule=running。"""
    plans, tasks = stores
    seen = {}

    import app.generator as gen
    real = gen.schedule

    def spy(*a, **kw):
        t = tasks.get(tid_box[0])
        seen["during_schedule"] = [(s.name, s.state) for s in t.steps]
        seen["status"] = t.status
        return real(*a, **kw)

    monkeypatch.setattr(gen, "schedule", spy)
    runner = TaskRunner(plans, tasks, client_factory=dead_client, inline=True)
    tid_box = [None]
    orig_submit = tasks.create

    def create_and_remember(*a, **kw):
        tid_box[0] = orig_submit(*a, **kw)
        return tid_box[0]

    monkeypatch.setattr(tasks, "create", create_and_remember)
    runner.submit(make_request(), "art")
    assert seen["status"] == "running"
    assert seen["during_schedule"] == [("library", "done"), ("schedule", "running"),
                                       ("validate", "pending"), ("narrate", "pending")]


def test_failed_task_keeps_the_reason_and_creates_no_plan(stores, monkeypatch):
    plans, tasks = stores
    import app.generator as gen

    def boom(*a, **kw):
        raise RuntimeError("排程炸了")

    monkeypatch.setattr(gen, "schedule", boom)
    tid = TaskRunner(plans, tasks, client_factory=dead_client, inline=True).submit(make_request(), "art")
    t = tasks.get(tid)
    assert t.status == "failed" and "排程炸了" in t.error and t.plan_id is None
    assert [s.state for s in t.steps] == ["done", "failed", "pending", "pending"]
    assert plans.list_plans() == []


def test_tasks_left_running_by_a_restart_are_marked_interrupted(stores):
    plans, tasks = stores
    tid = tasks.create(make_request(), "art")
    tasks.mark_running(tid)
    tasks.step_started(tid, "library")
    assert tasks.get(tid).status == "running"

    TaskRunner(plans, TaskStore(plans.database), client_factory=dead_client, inline=True)   # 服务重启
    t = tasks.get(tid)
    assert t.status == "failed" and "中断" in t.error
    assert [s.state for s in t.steps][0] == "failed"


def test_unknown_task(stores):
    from app.tasks import TaskNotFound
    with pytest.raises(TaskNotFound):
        stores[1].get("nope")


# --------------------------------------------------------------------------- #
# HTTP 接口
# --------------------------------------------------------------------------- #
def payload(**over) -> dict:
    body = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06",
            "budget_level": "medium", "travel_style": "balanced", "interests": ["art"], "theme": "art"}
    body.update(over)
    return body


@pytest.fixture
def client(tmp_path) -> TestClient:
    app = create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=dead_client)
    return TestClient(app)


def wait_done(client, tid, timeout=20.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        body = client.get(f"/tasks/{tid}").json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("任务超时")


def test_post_tasks_returns_202_and_the_task_finishes_in_the_background(client):
    r = client.post("/tasks", json=payload())
    assert r.status_code == 202
    tid = r.json()["task_id"]
    assert r.json()["status"] in ("queued", "running", "succeeded")
    body = wait_done(client, tid)
    assert body["status"] == "succeeded"
    assert [s["name"] for s in body["steps"]] == list(STEPS)
    assert all(s["state"] == "done" for s in body["steps"])
    plan = client.get(f"/plans/{body['plan_id']}").json()
    assert plan["itinerary"]["generation"]["theme_id"] == "art"
    assert plan["report"]["findings"] == []


def test_task_can_be_read_again_after_it_finished(client):
    """刷新页面 = 再 GET 一次同一个任务号，结果不变。"""
    tid = client.post("/tasks", json=payload()).json()["task_id"]
    first = wait_done(client, tid)
    second = client.get(f"/tasks/{tid}").json()
    assert second["plan_id"] == first["plan_id"] and second["status"] == "succeeded"


def test_post_tasks_validates_before_queueing(client):
    assert client.post("/tasks", json={"destination": "Paris"}).status_code == 422          # 缺信息 -> 追问
    r = client.post("/tasks", json=payload(theme="no-such-theme"))
    assert r.status_code == 422 and "themes" in r.json()["detail"]
    r = client.post("/tasks", json={k: v for k, v in payload().items() if k != "theme"})
    assert r.status_code == 422 and "主题" in r.json()["detail"]["message"]


def test_unknown_task_is_404(client):
    assert client.get("/tasks/nope").status_code == 404


def test_plan_cards_list_my_plans_newest_first(client):
    a = wait_done(client, client.post("/tasks", json=payload(theme="art", interests=["art"])).json()["task_id"])
    b = wait_done(client, client.post("/tasks", json=payload(theme="family", interests=["family"])).json()["task_id"])
    cards = client.get("/plan-cards").json()
    assert [c["plan_id"] for c in cards][:2] == [b["plan_id"], a["plan_id"]]
    c = cards[0]
    assert c["theme_id"] == "family" and c["theme_name"] == "亲子"
    assert c["start_date"] == "2026-10-03" and c["end_date"] == "2026-10-06" and c["days"] == 4
    assert c["latest_version"] == 1 and c["title"].startswith("巴黎")
    assert c["errors"] == 0 and c["warnings"] == 0 and c["narration"] == "template"


def test_plan_cards_empty(client):
    assert client.get("/plan-cards").json() == []


def test_old_synchronous_endpoint_still_works(client):
    r = client.post("/plans", json=payload())
    assert r.status_code == 201 and r.json()["itinerary"]["generation"]["theme_id"] == "art"
