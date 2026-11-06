"""LLM 调用追踪与成本统计（规格 M07 S6、02-architecture §5）。

`TracedClient` 包在任意 `LLMClient` 外面，每次调用记一行到 SQLite 的 `llm_calls` 表：
用途、模型、耗时、输入输出字数、token（后端给得出就记）、成功与否、费用。

- 不存提示词和返回原文：里面有用户的原话，追踪只需要「发生了什么、花了多久」。
- 记录失败绝不能影响真正的调用（磁盘满了、库被锁了都只写日志）。
- 用途和任务号靠 contextvar 传递：调用方用 `with trace_context(purpose="narration")` 标一下，不用改函数签名。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.db import Database
from app.llm import LLMClient

log = logging.getLogger(__name__)

_purpose: ContextVar[str] = ContextVar("llm_purpose", default="other")
_task_id: ContextVar[str | None] = ContextVar("llm_task_id", default=None)


@contextmanager
def trace_context(*, purpose: str | None = None, task_id: str | None = None) -> Iterator[None]:
    """给这段代码里的模型调用打标签。只传 purpose 时任务号沿用外层的。"""
    tokens = []
    if purpose is not None:
        tokens.append((_purpose, _purpose.set(purpose)))
    if task_id is not None:
        tokens.append((_task_id, _task_id.set(task_id)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


_COLS = ("at", "task_id", "purpose", "backend", "model", "ok", "error", "latency_ms", "prompt_chars",
         "response_chars", "prompt_tokens", "completion_tokens", "cost_usd")


class LlmCallStore:
    def __init__(self, database: Database, *, price_in_per_mtok: float = 0.0, price_out_per_mtok: float = 0.0) -> None:
        self.database = database
        self.price_in, self.price_out = price_in_per_mtok, price_out_per_mtok

    def cost(self, prompt_tokens: int | None, completion_tokens: int | None) -> float:
        return ((prompt_tokens or 0) * self.price_in + (completion_tokens or 0) * self.price_out) / 1_000_000

    def record(self, *, task_id: str | None, purpose: str, backend: str, model: str, ok: bool, error: str | None,
               latency_ms: int, prompt_chars: int, response_chars: int, prompt_tokens: int | None,
               completion_tokens: int | None) -> None:
        with self.database.connect(write=True) as con:
            con.execute(
                f"INSERT INTO llm_calls ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
                (datetime.now(UTC).isoformat(timespec="milliseconds"), task_id, purpose, backend, model, int(ok),
                 error, latency_ms, prompt_chars, response_chars, prompt_tokens, completion_tokens,
                 self.cost(prompt_tokens, completion_tokens)))

    def reset(self) -> int:
        """清零统计：删掉所有调用记录，返回删了多少条。"""
        with self.database.connect(write=True) as con:
            return con.execute("DELETE FROM llm_calls").rowcount

    # -- 读 -------------------------------------------------------------- #
    def recent(self, limit: int = 20, *, task_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM llm_calls", []
        if task_id is not None:
            sql, args = sql + " WHERE task_id = ?", [task_id]
        with self.database.connect() as con:
            rows = con.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**{c: r[c] for c in _COLS}, "ok": bool(r["ok"])} for r in rows]

    def summary(self, *, task_id: str | None = None) -> dict[str, Any]:
        where, args = ("WHERE task_id = ?", [task_id]) if task_id is not None else ("", [])
        agg = ("COUNT(*) AS calls, SUM(1 - ok) AS failures, AVG(latency_ms) AS avg_ms, "
               "SUM(COALESCE(prompt_tokens, 0)) AS pt, SUM(COALESCE(completion_tokens, 0)) AS ct, "
               "SUM(cost_usd) AS cost")

        def shape(r) -> dict[str, Any]:
            return {"calls": r["calls"], "failures": int(r["failures"] or 0), "avg_latency_ms": round(r["avg_ms"] or 0),
                    "prompt_tokens": int(r["pt"] or 0), "completion_tokens": int(r["ct"] or 0),
                    "cost_usd": round(r["cost"] or 0, 6)}

        with self.database.connect() as con:
            total = con.execute(f"SELECT {agg} FROM llm_calls {where}", args).fetchone()
            by = con.execute(f"SELECT purpose, {agg} FROM llm_calls {where} GROUP BY purpose ORDER BY calls DESC",
                             args).fetchall()
        return {"totals": shape(total), "by_purpose": [{"purpose": r["purpose"], **shape(r)} for r in by]}


class TracedClient:
    """透明包装：行为和被包装的客户端完全一样，只是多记一行。"""

    def __init__(self, inner: LLMClient, store: LlmCallStore) -> None:
        self._inner, self._store = inner, store

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        started = time.perf_counter()
        usage: tuple[int, int] | None = None
        text = ""
        error: str | None = None
        try:
            with_usage = getattr(self._inner, "complete_json_with_usage", None)
            if with_usage is not None:
                text, usage = with_usage(system=system, user=user, schema=schema)
            else:
                text = self._inner.complete_json(system=system, user=user, schema=schema)
            return text
        except Exception as exc:
            error = str(exc)[:300] or type(exc).__name__
            raise
        finally:
            self._record(started, system, user, text, usage, error)

    def _record(self, started: float, system: str, user: str, text: str, usage: tuple[int, int] | None,
                error: str | None) -> None:
        try:
            self._store.record(
                task_id=_task_id.get(), purpose=_purpose.get(),
                backend=getattr(self._inner, "backend", "unknown"), model=getattr(self._inner, "model_name", "unknown"),
                ok=error is None, error=error, latency_ms=round((time.perf_counter() - started) * 1000),
                prompt_chars=len(system) + len(user), response_chars=len(text),
                prompt_tokens=usage[0] if usage else None, completion_tokens=usage[1] if usage else None)
        except Exception:  # noqa: BLE001 - 追踪是旁路，出任何问题都不能连累真正的调用
            log.exception("记录 LLM 调用失败（已忽略）")
