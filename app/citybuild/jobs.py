"""现建城市的后台任务（规格 10）：用户输入新目的地后，在后台建城市，前端轮询进度。

任务放在内存里（服务重启就没了，用户重新输入即可）；同一座城市同时只建一次，重复请求拿到同一个任务。
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

import httpx

from app.citybuild.build import build_and_store
from app.citybuild.fetch import make_client

log = logging.getLogger(__name__)

Builder = Callable[[dict, httpx.Client, Callable[[str], None]], dict]


@dataclass
class BuildJob:
    id: str
    city_id: str
    name_zh: str
    status: str = "running"                  # running / done / failed
    message: str = "准备中"
    error: str = ""
    log: list[str] = field(default_factory=list)

    def public(self) -> dict:
        d = asdict(self)
        d["log"] = self.log[-5:]
        return d


class BuildJobs:
    def __init__(self, *, builder: Builder | None = None, client_factory: Callable[[], httpx.Client] = make_client,
                 inline: bool = False, workers: int = 2) -> None:
        self._builder = builder or (lambda seed, client, say: build_and_store(seed, client, say))
        self._client_factory = client_factory
        self._inline = inline
        self._pool = None if inline else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="citybuild")
        self._jobs: dict[str, BuildJob] = {}
        self._by_city: dict[str, str] = {}
        self._lock = threading.Lock()

    def start(self, seed: dict) -> BuildJob:
        with self._lock:
            existing = self._jobs.get(self._by_city.get(seed["id"], ""))
            if existing is not None and existing.status == "running":
                return existing
            job = BuildJob(id=uuid.uuid4().hex[:12], city_id=seed["id"], name_zh=seed["name_zh"])
            self._jobs[job.id] = job
            self._by_city[seed["id"]] = job.id
        if self._pool is None:
            self._run(job, seed)
        else:
            self._pool.submit(self._run, job, seed)
        return job

    def get(self, job_id: str) -> BuildJob | None:
        return self._jobs.get(job_id)

    def _run(self, job: BuildJob, seed: dict) -> None:
        def say(message: str) -> None:
            job.message = message
            job.log.append(message)

        try:
            with self._client_factory() as client:
                self._builder(seed, client, say)
            job.status, job.message = "done", "完成"
        except Exception as exc:  # noqa: BLE001 - 后台线程里任何错误都要落到任务上
            log.exception("建城市 %s 失败", seed["id"])
            job.status, job.error = "failed", f"建立「{seed['name_zh']}」的数据失败：{exc}"
