"""后台生成任务与真实进度（规格 M01 S1 / S3）。

POST /tasks 立刻返回任务号，生成在后台线程里跑；任务和每一步的状态都写进 SQLite，所以：
- 前端轮询 GET /tasks/{id} 看到的是真实进度，不是动画；
- 关掉页面再回来、刷新页面，凭任务号都能看到进度或结果；
- 服务重启时，还停在「进行中」的任务会被标成「中断」，不会永远转圈。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from app import multicity
from app.db import Database
from app.generator import STEPS, generate_from_theme
from app.llm import LLMClient, LLMError
from app.llm_trace import trace_context
from app.schemas import Itinerary, TravelRequest
from app.versions import PlanStore

log = logging.getLogger(__name__)

StepState = Literal["pending", "running", "done", "failed"]
TaskStatus = Literal["queued", "running", "succeeded", "failed"]

INTERRUPTED = "生成被中断（服务重启了），请重新生成"


class TaskNotFound(RuntimeError):
    pass


class TaskNotRemovable(RuntimeError):
    pass


class Step(BaseModel):
    name: str
    state: StepState = "pending"
    started_at: str | None = None
    finished_at: str | None = None


class Task(BaseModel):
    id: str
    status: TaskStatus
    theme_id: str
    steps: list[Step]
    plan_id: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


FAILED_KEEP = timedelta(hours=24)      # 失败的任务在「正在生成」区留多久，之后不再显示


class TaskCard(BaseModel):
    """「我的行程」里「正在生成」区的一张卡（规格 07 P19）：没生成完的任务，加上 24 小时内失败的。"""

    id: str
    status: TaskStatus
    theme_id: str
    theme_name: str
    destination: str
    start_date: date
    end_date: date
    days: int
    steps: list[Step]
    current_step: str | None          # 正在跑的那一步；还没开始是第一步，都做完了是 None
    steps_done: int
    steps_total: int
    plan_id: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


def _theme_label(req: TravelRequest, theme_id: str) -> str:
    """卡片上的主题名：全选且等权重叫「均衡」，否则用「主题 + 主题」。主题库读不出来就退回主题 id。"""
    from app.library import load_library
    from app.scheduler.engine import BALANCED_NAME, is_balanced, selected_themes

    try:
        if req.legs:                             # 多城市：每座城市各自的主题
            return " · ".join(_theme_label(leg.request, leg.theme) for leg in req.legs)
        if req.route:                            # 深度路线：卡片上写路线名
            from app.routes import get_route

            return get_route(req.city_id, req.route).name_zh
        lib = load_library(req.city_id)
        sel = selected_themes(req, theme_id)
        return BALANCED_NAME if is_balanced(sel, lib) else " + ".join(lib.get_theme(t).name_zh for t, _w in sel)
    except Exception:  # noqa: BLE001 —— 老任务的主题可能已不在库里，卡片照样要能显示
        return theme_id


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class TaskStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    # -- 写 -------------------------------------------------------------- #
    def create(self, request: TravelRequest, theme_id: str) -> str:
        tid = uuid4().hex[:12]
        now = _now()
        names = ([f"{multicity.city_name(leg.request.city_id)}：{n}" for leg in request.legs for n in STEPS]
                 if request.legs else list(STEPS))                # 多城市：每座城市各走一遍步骤
        steps = [Step(name=n).model_dump() for n in names]
        with self.database.connect(write=True) as con:
            con.execute(
                "INSERT INTO tasks (id, status, theme_id, request_json, steps_json, created_at, updated_at) "
                "VALUES (?, 'queued', ?, ?, ?, ?, ?)",
                (tid, theme_id, request.model_dump_json(), json.dumps(steps), now, now))
        return tid

    def mark_running(self, tid: str) -> None:
        self._update(tid, status="running")

    def step_started(self, tid: str, name: str) -> None:
        self._update_step(tid, name, state="running", started_at=_now())

    def step_done(self, tid: str, name: str) -> None:
        self._update_step(tid, name, state="done", finished_at=_now())

    def step_failed(self, tid: str, name: str) -> None:
        self._update_step(tid, name, state="failed", finished_at=_now())

    def succeed(self, tid: str, plan_id: str) -> None:
        self._update(tid, status="succeeded", plan_id=plan_id)

    def fail(self, tid: str, error: str) -> None:
        self._update(tid, status="failed", error=error)

    def interrupt_unfinished(self) -> int:
        """服务（重新）启动时调用：没跑完的任务不可能再继续了，标成中断。"""
        with self.database.connect() as con:
            ids = [r["id"] for r in con.execute("SELECT id FROM tasks WHERE status IN ('queued', 'running')")]
        for tid in ids:
            for s in self.get(tid).steps:
                if s.state == "running":
                    self.step_failed(tid, s.name)
            self.fail(tid, INTERRUPTED)
        return len(ids)

    def remove_failed(self, tid: str) -> None:
        """把一个失败的任务从「正在生成」区移除。只有失败的能移除：没跑完的移掉会让用户以为它没了、其实还在后台跑。"""
        with self.database.connect(write=True) as con:
            row = con.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None:
                raise TaskNotFound(f"找不到任务：{tid}")
            if row["status"] != "failed":
                raise TaskNotRemovable("这个任务还没有失败，不能移除")
            con.execute("DELETE FROM tasks WHERE id = ?", (tid,))

    # -- 读 -------------------------------------------------------------- #
    def unfinished_cards(self, now: datetime | None = None) -> list[TaskCard]:
        """没生成完的任务（排队 / 进行中）加 24 小时内失败的，新的在前。"""
        cutoff = ((now or datetime.now(UTC)) - FAILED_KEEP).isoformat(timespec="milliseconds")
        with self.database.connect() as con:
            rows = con.execute(
                "SELECT * FROM tasks WHERE status IN ('queued', 'running') OR (status = 'failed' AND updated_at >= ?) "
                "ORDER BY seq DESC", (cutoff,)).fetchall()
        cards: list[TaskCard] = []
        for row in rows:
            req = TravelRequest.model_validate_json(row["request_json"])
            steps = [Step(**s) for s in json.loads(row["steps_json"])]
            running = next((s.name for s in steps if s.state in ("running", "failed")), None)
            current = running or next((s.name for s in steps if s.state == "pending"), None)
            cards.append(TaskCard(
                id=row["id"], status=row["status"], theme_id=row["theme_id"],
                theme_name=_theme_label(req, row["theme_id"]),
                destination=multicity.title_of(req) if req.legs else req.city.name_zh,
                start_date=req.start_date, end_date=req.end_date, days=req.trip_days, steps=steps,
                current_step=current, steps_done=sum(s.state == "done" for s in steps), steps_total=len(steps),
                plan_id=row["plan_id"], error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"]))
        return cards

    def get(self, tid: str) -> Task:
        with self.database.connect() as con:
            row = con.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if row is None:
            raise TaskNotFound(f"找不到任务：{tid}")
        return Task(id=row["id"], status=row["status"], theme_id=row["theme_id"],
                    steps=[Step(**s) for s in json.loads(row["steps_json"])],
                    plan_id=row["plan_id"], error=row["error"],
                    created_at=row["created_at"], updated_at=row["updated_at"])

    def for_plan(self, plan_id: str) -> Task:
        """生成了这份行程的任务（同步接口生成的行程没有任务）。"""
        with self.database.connect() as con:
            row = con.execute("SELECT id FROM tasks WHERE plan_id = ? ORDER BY seq DESC LIMIT 1",
                              (plan_id,)).fetchone()
        if row is None:
            raise TaskNotFound(f"这份行程不是后台任务生成的：{plan_id}")
        return self.get(row["id"])

    # -- 内部 ---------------------------------------------------------- #
    def _update(self, tid: str, **cols: str) -> None:
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self.database.connect(write=True) as con:
            cur = con.execute(f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?",
                              (*cols.values(), _now(), tid))
            if cur.rowcount == 0:
                raise TaskNotFound(f"找不到任务：{tid}")

    def _update_step(self, tid: str, name: str, **fields: str) -> None:
        with self.database.connect(write=True) as con:
            row = con.execute("SELECT steps_json FROM tasks WHERE id = ?", (tid,)).fetchone()
            if row is None:
                raise TaskNotFound(f"找不到任务：{tid}")
            steps = json.loads(row["steps_json"])
            for s in steps:
                if s["name"] == name:
                    s.update(fields)
            con.execute("UPDATE tasks SET steps_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(steps), _now(), tid))


class TaskRunner:
    """把生成放到后台线程里跑。inline=True 时在调用线程里同步跑完（测试用）。"""

    def __init__(
        self,
        plans: PlanStore,
        tasks: TaskStore,
        *,
        client_factory: Callable[[], LLMClient],
        inline: bool = False,
        workers: int = 2,
        post_generate: Callable[[TravelRequest, Itinerary], Itinerary] | None = None,
    ) -> None:
        self.plans, self.tasks, self._make_client, self.inline = plans, tasks, client_factory, inline
        self._post_generate = post_generate          # 生成之后、保存之前的一步（按预算调整行程）
        self._pool = None if inline else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="generate")
        self.tasks.interrupt_unfinished()

    def submit(self, request: TravelRequest, theme_id: str) -> str:
        tid = self.tasks.create(request, theme_id)
        if self._pool is None:
            self._run(tid, request, theme_id)
        else:
            self._pool.submit(self._run, tid, request, theme_id)
        return tid

    def _run(self, tid: str, request: TravelRequest, theme_id: str) -> None:
        with trace_context(task_id=tid):               # 这次生成里所有模型调用都记在这个任务名下
            self._run_traced(tid, request, theme_id)

    def _run_traced(self, tid: str, request: TravelRequest, theme_id: str) -> None:
        current: list[str] = []

        def on_step(name: str) -> None:
            if current:
                self.tasks.step_done(tid, current[-1])
            self.tasks.step_started(tid, name)
            current.append(name)

        try:
            self.tasks.mark_running(tid)
            try:
                client: LLMClient | None = self._make_client()
            except LLMError:
                client = None                      # 连客户端都建不起来：直接用模板文案
            if request.legs:                                      # 多城市：每座城市各排一份，合成一份行程
                itinerary = multicity.generate(request, client=client, on_step=on_step, post=self._post_generate)
                if current:
                    self.tasks.step_done(tid, current[-1])
                plan_id = self.plans.create(request, itinerary, note="via POST /tasks multi-city")
                self.tasks.succeed(tid, plan_id)
                return
            trip = generate_from_theme(request, theme_id, client=client, on_step=on_step)
            if current:
                self.tasks.step_done(tid, current[-1])
            itinerary = self._post_generate(request, trip.itinerary) if self._post_generate else trip.itinerary
            plan_id = self.plans.create(request, itinerary, note=f"via POST /tasks theme={theme_id}")
            self.tasks.succeed(tid, plan_id)
        except Exception as exc:  # noqa: BLE001 - 后台线程里任何错误都要落到任务上，不能悄悄死掉
            log.exception("生成任务 %s 失败", tid)
            if current:
                self.tasks.step_failed(tid, current[-1])
            self.tasks.fail(tid, f"生成失败：{exc}")
