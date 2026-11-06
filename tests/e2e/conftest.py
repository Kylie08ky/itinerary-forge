"""浏览器验收（e2e）共用：跑完自动删掉这一轮测试生成的行程。

默认（`pytest -m e2e`）用独立的前后端和临时数据库（tests/e2e_stack.py），
不碰你平时的服务，跑完整个删掉，这里什么也不用清。

只有对着已经在跑的服务测（设了 E2E_API / E2E_WEB / E2E_USE_RUNNING）时才会连到你的真实数据库：
不清理的话每跑一遍「我的行程」里就多出上百份测试行程，所以跑之前记下已有的行程号（含已归档），跑完把新增的全部删掉。
设 E2E_KEEP=1 可以保留（想肉眼看测试留下的东西时）。
"""

from __future__ import annotations

import os

import httpx
import pytest

API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")


def _plan_ids() -> set[str] | None:
    try:
        ids: set[str] = set()
        for archived in ("false", "true"):
            r = httpx.get(f"{API}/plan-cards", params={"archived": archived}, timeout=30)
            r.raise_for_status()
            ids |= {c["plan_id"] for c in r.json()}
        return ids
    except httpx.HTTPError:
        return None            # 后端没起来：e2e 自己会跳过，这里什么也不做


@pytest.fixture(scope="session", autouse=True)
def _delete_plans_created_by_the_tests():
    if os.getenv("E2E_ISOLATED") == "1":          # 独立的临时数据库，跑完整个删掉，不用一份份清
        yield
        return
    before = _plan_ids()
    yield
    if before is None or os.getenv("E2E_KEEP") == "1":
        return
    after = _plan_ids() or set()
    for pid in after - before:
        try:
            httpx.delete(f"{API}/plans/{pid}", timeout=30)
        except httpx.HTTPError:
            pass
