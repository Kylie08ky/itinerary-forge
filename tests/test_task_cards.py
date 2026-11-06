"""生成中的行程要能找回（规格 07 P19）：GET /tasks?unfinished=true、DELETE /tasks/{id}。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.library import load_library
from app.llm import LLMError
from app.schemas import TravelRequest
from app.tasks import TaskStore
from app.versions import PlanStore


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


def request(**kw) -> TravelRequest:
    base = {"destination": "Paris", "start_date": date(2026, 11, 14), "end_date": date(2026, 11, 18),
            "budget_level": "medium", "travel_style": "balanced", "interests": ["art"]}
    return TravelRequest.model_validate({**base, **kw})


@pytest.fixture
def env(tmp_path):
    plans = PlanStore(root=tmp_path / "plans")
    app = create_app(store=plans, client_factory=DeadLLM)
    return TestClient(app), TaskStore(plans.database)


def test_queued_and_running_tasks_are_listed_with_what_the_user_asked_for(env):
    client, tasks = env
    tid = tasks.create(request(), "art")
    tasks.mark_running(tid)
    tasks.step_done(tid, "library")
    tasks.step_started(tid, "schedule")
    got = client.get("/tasks", params={"unfinished": True}).json()
    assert len(got) == 1
    c = got[0]
    assert c["id"] == tid and c["status"] == "running"
    assert (c["destination"], c["start_date"], c["end_date"], c["days"]) == ("巴黎", "2026-11-14", "2026-11-18", 5)
    assert c["theme_name"] == "艺术"
    assert c["current_step"] == "schedule" and c["steps_done"] == 1 and c["steps_total"] == 4
    assert c["error"] is None and c["plan_id"] is None


def test_an_all_theme_mix_is_shown_as_balanced(env):
    client, tasks = env
    all_ids = sorted(load_library("paris").themes)
    tasks.create(request(themes=[{"id": t, "weight": 3} for t in all_ids]), all_ids[0])
    assert client.get("/tasks", params={"unfinished": True}).json()[0]["theme_name"] == "均衡"


def test_finished_tasks_are_not_listed_and_the_newest_comes_first(env):
    client, tasks = env
    done = tasks.create(request(), "art")
    tasks.succeed(done, "paris-x")
    first = tasks.create(request(), "art")
    second = tasks.create(request(), "food")
    got = [c["id"] for c in client.get("/tasks", params={"unfinished": True}).json()]
    assert got == [second, first]


def test_a_failed_task_stays_for_a_day_then_drops_off(env):
    client, tasks = env
    recent = tasks.create(request(), "art")
    tasks.fail(recent, "生成失败：模型超时")
    old = tasks.create(request(), "art")
    tasks.fail(old, "生成失败：别的原因")
    long_ago = (datetime.now(UTC) - timedelta(hours=30)).isoformat(timespec="milliseconds")
    with tasks.database.connect(write=True) as con:
        con.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (long_ago, old))
    got = client.get("/tasks", params={"unfinished": True}).json()
    assert [c["id"] for c in got] == [recent]
    assert got[0]["status"] == "failed" and got[0]["error"] == "生成失败：模型超时"


def test_listing_without_the_filter_is_refused_so_the_whole_history_is_never_dumped(env):
    client, _tasks = env
    assert client.get("/tasks").status_code == 422


def test_only_a_failed_task_can_be_removed(env):
    client, tasks = env
    failed = tasks.create(request(), "art")
    tasks.fail(failed, "生成失败")
    running = tasks.create(request(), "art")
    tasks.mark_running(running)
    assert client.delete(f"/tasks/{running}").status_code == 409                       # 还在跑：不能移除
    assert client.delete(f"/tasks/{failed}").status_code == 204
    assert client.get(f"/tasks/{failed}").status_code == 404
    assert client.delete("/tasks/nope").status_code == 404
    assert [c["id"] for c in client.get("/tasks", params={"unfinished": True}).json()] == [running]


def test_a_task_interrupted_by_a_restart_is_listed_as_failed_with_the_reason(tmp_path):
    plans = PlanStore(root=tmp_path / "plans")
    tasks = TaskStore(plans.database)
    tid = tasks.create(request(), "art")
    tasks.mark_running(tid)
    client = TestClient(create_app(store=plans, client_factory=DeadLLM))                # 重启：启动时把没跑完的标成中断
    got = client.get("/tasks", params={"unfinished": True}).json()
    assert got[0]["id"] == tid and got[0]["status"] == "failed" and "中断" in got[0]["error"]
