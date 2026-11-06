"""汇率（规格 M09 S1c / S1e）：Frankfurter（欧洲央行参考汇率），免费、不需要 Key。

- 缓存 24 小时；成功拿到的汇率同时存进数据库，重启后接口失败也还能用「上一次的值」并标明日期。
- 接口失败 / 超时 / 数据异常一律不抛给页面：有旧值用旧值（stale=True），一次都没成功过就返回 None（页面只显示欧元）。
- 只发币种代码，不带任何个人信息。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.db import Database

URL = "https://api.frankfurter.dev/v1/latest"
CACHE_TTL_SECONDS = 24 * 3600
TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class FxRate:
    base: str
    quote: str
    rate: float
    date: str                 # 欧洲央行公布这个汇率的日期
    stale: bool = False       # True = 这次没拉到新的，用的是上一次成功的值


class FxService:
    """`client` 可注入（测试用 httpx.MockTransport）；`database` 给就把最后一次成功值落库。"""

    def __init__(self, database: Database | None = None, client: httpx.Client | None = None, *,
                 ttl: float = CACHE_TTL_SECONDS, clock: Callable[[], float] = time.time) -> None:
        self._db = database
        self._client = client or httpx.Client(headers={"User-Agent": "ai-travel-planner-agent/0.3"})
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, FxRate]] = {}

    def get(self, base: str = "EUR", quote: str = "CNY") -> FxRate | None:
        key = (base.upper(), quote.upper())
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        fresh = self._fetch(*key)
        if fresh is not None:
            with self._lock:
                self._cache[key] = (now, fresh)
            self._save(fresh)
            return fresh
        old = (hit[1] if hit else None) or self._load(*key)
        return FxRate(old.base, old.quote, old.rate, old.date, stale=True) if old else None

    def rate(self, src: str, dst: str) -> FxRate | None:
        """任意两种货币的汇率，经欧元换算（Frankfurter 的基准是欧元）；相同币种 = 1。"""
        src, dst = src.upper(), dst.upper()
        if src == dst:
            return FxRate(src, dst, 1.0, datetime.now(UTC).date().isoformat())
        a = None if src == "EUR" else self.get("EUR", src)
        b = None if dst == "EUR" else self.get("EUR", dst)
        if (src != "EUR" and a is None) or (dst != "EUR" and b is None):
            return None
        r_src = 1.0 if a is None else a.rate
        r_dst = 1.0 if b is None else b.rate
        parts = [x for x in (a, b) if x is not None]
        return FxRate(src, dst, r_dst / r_src, min(p.date for p in parts), stale=any(p.stale for p in parts))

    # ------------------------------------------------------------------ #
    def _fetch(self, base: str, quote: str) -> FxRate | None:
        try:
            r = self._client.get(URL, params={"base": base, "symbols": quote}, timeout=TIMEOUT_SECONDS)
            r.raise_for_status()
            body = r.json()
            rate = float(body["rates"][quote])
            date = str(body["date"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        if rate <= 0 or not date:
            return None
        return FxRate(base, quote, rate, date)

    def _save(self, fx: FxRate) -> None:
        if self._db is None:
            return
        with self._db.connect(write=True) as con:
            con.execute(
                "INSERT INTO fx_rates (base, quote, rate, rate_date, fetched_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(base, quote) DO UPDATE SET rate = excluded.rate, rate_date = excluded.rate_date, "
                "fetched_at = excluded.fetched_at",
                (fx.base, fx.quote, fx.rate, fx.date, datetime.now(UTC).isoformat(timespec="seconds")))

    def _load(self, base: str, quote: str) -> FxRate | None:
        if self._db is None:
            return None
        with self._db.connect() as con:
            row = con.execute("SELECT rate, rate_date FROM fx_rates WHERE base = ? AND quote = ?",
                              (base, quote)).fetchone()
        return FxRate(base, quote, float(row["rate"]), row["rate_date"]) if row else None
