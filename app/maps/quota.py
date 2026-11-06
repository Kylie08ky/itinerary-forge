"""高德配额保护（规格 M05 S7c）：按服务、按自然月记账，到「自留上限」就当月不再调高德。

自留上限远低于个人认证的免费额度（2026-11-02 高德定价页）；到上限后调用方降级（路线退回直线估算、
搜索退回地点库内搜索、地图退回 OpenStreetMap），不报错、不会走到要付费的那一步。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from app.db import Database

# 个人认证开发者每月免费额度（次）；认证满 1 年后以控制台为准
FREE_ALLOWANCE = {"lbs": 150_000, "search": 5_000, "map_init": 1_500_000}
# 自留上限：搜索 40%、基础 LBS 约 33%、地图初始化 20%
CAPS = {"lbs": 50_000, "search": 2_000, "map_init": 300_000}
LABELS = {"lbs": "路线、地理编码", "search": "地点搜索", "map_init": "地图加载"}
WARN_AT = 0.8


def _this_month() -> str:
    return datetime.now().strftime("%Y-%m")


class Usage:
    def __init__(self, db: Database, *, clock: Callable[[], str] = _this_month) -> None:
        self.db = db
        self._clock = clock

    def used(self, service: str) -> int:
        with self.db.connect() as con:
            row = con.execute("SELECT n FROM map_usage WHERE month = ? AND service = ?",
                              (self._clock(), service)).fetchone()
        return int(row["n"]) if row else 0

    def allow(self, service: str) -> bool:
        return self.used(service) < CAPS[service]

    def record(self, service: str, n: int = 1) -> None:
        with self.db.connect(write=True) as con:
            con.execute("INSERT INTO map_usage(month, service, n) VALUES (?, ?, ?) "
                        "ON CONFLICT(month, service) DO UPDATE SET n = n + excluded.n",
                        (self._clock(), service, n))

    def snapshot(self) -> list[dict]:
        rows = []
        for service, cap in CAPS.items():
            used = self.used(service)
            rows.append({"service": service, "label": LABELS[service], "used": used, "cap": cap,
                         "free_allowance": FREE_ALLOWANCE[service],
                         "warn": used >= cap * WARN_AT, "blocked": used >= cap})
        return rows
