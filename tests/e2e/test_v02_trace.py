"""v0.2 模型调用追踪的前端验收（specs/modules/M07 S6）。

默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v02_trace.py
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e

WEB = os.getenv("E2E_WEB", "http://127.0.0.1:5173").rstrip("/")
API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")

BODY = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-05", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art"}


def _up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module", autouse=True)
def servers_running():
    if not (_up(WEB) and _up(f"{API}/health")):
        pytest.skip(f"前后端没起来（{WEB} / {API}），先启动再跑 e2e")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser) -> Page:
    ctx = browser.new_context(viewport={"width": 1100, "height": 1500}, locale="zh-CN")
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture(scope="module")
def task_plan() -> str:
    """走后台任务生成一份行程（有模型就用模型写说明，没有就降级为模板），返回行程号。"""
    tid = httpx.post(f"{API}/tasks", json=BODY, timeout=30).json()["task_id"]
    for _ in range(600):
        t = httpx.get(f"{API}/tasks/{tid}", timeout=10).json()
        if t["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.5)
    assert t["status"] == "succeeded", t
    return t["plan_id"]


def test_plan_page_shows_the_generation_trace(page: Page, task_plan):
    page.goto(f"{WEB}/plans/{task_plan}")
    page.locator(".day").first.wait_for()
    page.get_by_role("button", name="更多").click()
    page.get_by_test_id("open-gen-details").click()
    page.locator("details.trace summary").click()
    trace = page.locator("details.trace")
    for step in ("读取地点库", "选点并排程", "质检", "生成说明"):
        expect(trace).to_contain_text(step)
    expect(trace).to_contain_text("模型调用")
    expect(trace).to_contain_text("费用 $")
    expect(trace.locator(".trace-call").first).to_contain_text("写每天的说明")      # 至少一次叙述调用（成功或降级）


def test_a_plan_made_without_a_task_explains_there_is_no_trace(page: Page):
    pid = httpx.post(f"{API}/plans", json=BODY, timeout=120).json()["plan_id"]
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    page.get_by_role("button", name="更多").click()
    page.get_by_test_id("open-gen-details").click()
    page.locator("details.trace summary").click()
    expect(page.locator("details.trace")).to_contain_text("没有生成轨迹")


def test_usage_page_totals_and_per_purpose_rows(page: Page, task_plan):
    page.goto(f"{WEB}/usage")
    page.get_by_text("模型调用", exact=True).wait_for()
    expect(page.locator(".stat-card")).to_have_count(4)
    expect(page.locator(".usage-table").first).to_contain_text("写每天的说明")
    assert int(page.locator(".stat-card b").first.inner_text()) >= 1
    expect(page.get_by_text("不保存你输入的原话")).to_be_visible()


def test_my_plans_links_to_the_usage_page(page: Page):
    page.goto(f"{WEB}/plans")
    page.get_by_role("link", name="模型用量").click()
    expect(page).to_have_url(f"{WEB}/usage")
