"""P19 生成中的行程要能找回（specs/07-polish-backlog.md P19）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_p19_generating.py

真实生成在没有模型的测试栈里不到一秒就完成，赶不上看「进行中」，所以这里拦截 GET /tasks?unfinished=true 造出各种任务；
后端本身的行为（只列没完成的、24 小时内失败的、只能移除失败的）见 tests/test_task_cards.py。
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e

WEB = os.getenv("E2E_WEB", "http://127.0.0.1:5173").rstrip("/")
API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")

STEPS = ["library", "schedule", "validate", "narrate"]


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
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def task_card(tid="abc123def456", status="running", done=1, error=None, theme="均衡") -> dict:
    states = ["done"] * done + ["running" if status != "failed" else "failed"] + ["pending"] * (3 - done)
    steps = [{"name": n, "state": s, "started_at": None, "finished_at": None}
             for n, s in zip(STEPS, states, strict=True)]
    current = next((s["name"] for s in steps if s["state"] in ("running", "failed")), None)
    return {"id": tid, "status": status, "theme_id": "art", "theme_name": theme, "destination": "巴黎",
            "start_date": "2026-11-14", "end_date": "2026-11-18", "days": 5, "steps": steps, "current_step": current,
            "steps_done": done, "steps_total": 4, "plan_id": None, "error": error,
            "created_at": "2026-10-24T18:30:00.000+00:00", "updated_at": "2026-10-24T18:31:00.000+00:00"}


def serve_tasks(page: Page, cards: list[dict]) -> list[dict]:
    """拦截任务列表：返回 holder（改它就是改接口返回的内容）。"""
    holder = list(cards)
    page.route(re.compile(r".*/tasks\?unfinished=true$"), lambda r: r.fulfill(json=holder))
    return holder


def test_a_running_task_shows_in_my_plans_with_its_step_and_opens_the_progress_page(page: Page):
    serve_tasks(page, [task_card(done=1)])
    page.goto(f"{WEB}/plans")
    tile = page.get_by_test_id("gen-tile")
    expect(tile).to_have_count(1)
    expect(page.get_by_test_id("group-generating")).to_contain_text("正在生成")
    expect(tile).to_contain_text("巴黎 · 均衡")
    expect(tile).to_contain_text("2026-11-14 → 2026-11-18 · 5 天")
    expect(tile).to_contain_text("第 2 / 4 步 · 选点并排程")
    expect(tile.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "25")
    page.route(re.compile(r".*/tasks/abc123def456$"), lambda r: r.fulfill(json={
        "id": "abc123def456", "status": "running", "theme_id": "art", "plan_id": None, "error": None,
        "steps": task_card()["steps"], "created_at": "x", "updated_at": "x"}))
    tile.click()
    expect(page).to_have_url(re.compile(r"/tasks/abc123def456$"))
    expect(page.get_by_test_id("task-leave-hint")).to_contain_text("可以离开这个页面")
    page.get_by_test_id("task-leave-hint").get_by_role("link", name=re.compile("我的行程")).click()
    expect(page).to_have_url(re.compile(r"/plans$"))


def test_the_nav_shows_how_many_are_still_generating_from_any_page(page: Page):
    serve_tasks(page, [task_card(), task_card(tid="second000001", done=2, theme="艺术")])
    page.goto(f"{WEB}/new?destination=Paris")
    badge = page.get_by_role("navigation", name="主导航").get_by_test_id("gen-badge")
    expect(badge).to_have_text("2")
    expect(badge).to_have_attribute("title", "2 份正在生成")


def test_a_failed_task_stays_with_its_reason_and_can_be_removed(page: Page):
    reason = "生成被中断（服务重启了），请重新生成"
    holder = serve_tasks(page, [task_card(status="failed", error=reason)])
    removed: list[str] = []

    def delete(route):
        removed.append(route.request.url.rsplit("/", 1)[1])
        holder.clear()
        route.fulfill(status=204)

    page.route(re.compile(r".*/tasks/abc123def456$"),
               lambda r: delete(r) if r.request.method == "DELETE" else r.continue_())
    page.goto(f"{WEB}/plans")
    tile = page.get_by_test_id("gen-tile")
    expect(tile).to_have_attribute("data-status", "failed")
    expect(tile).to_contain_text("生成没有完成")
    expect(tile).to_contain_text(reason)
    expect(tile.get_by_role("link", name="重新生成")).to_have_attribute("href", "/new")
    nav_badge = page.get_by_role("navigation", name="主导航").get_by_test_id("gen-badge")
    expect(nav_badge).to_have_text("!")                                                 # 只剩失败的：感叹号
    tile.get_by_role("button", name="移除").click()
    expect(page.get_by_test_id("gen-tile")).to_have_count(0)
    assert removed == ["abc123def456"]
    expect(page.get_by_test_id("gen-badge")).to_have_count(0)


def test_when_a_task_finishes_the_list_reloads_and_the_tile_goes_away(page: Page):
    holder = serve_tasks(page, [task_card()])
    loads: list[str] = []
    page.on("request", lambda q: "/plan-cards" in q.url and "archived=true" not in q.url and loads.append(q.url))
    page.goto(f"{WEB}/plans")
    expect(page.get_by_test_id("gen-tile")).to_have_count(1)
    before = len(loads)
    holder.clear()                                                                     # 任务完成：从未完成列表里消失
    expect(page.get_by_test_id("gen-tile")).to_have_count(0, timeout=15_000)
    page.wait_for_timeout(500)
    assert len(loads) > before                                              # 重新读了行程列表，新行程会出现在下面


def test_with_no_plans_yet_a_running_task_replaces_the_empty_state(page: Page):
    serve_tasks(page, [task_card()])
    page.route(re.compile(r".*/plan-cards.*"), lambda r: r.fulfill(json=[]))
    page.goto(f"{WEB}/plans")
    expect(page.get_by_test_id("gen-tile")).to_have_count(1)
    expect(page.get_by_text("还没有行程")).to_have_count(0)


def test_on_a_phone_the_tab_bar_carries_the_badge(browser):
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, locale="zh-CN", is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    try:
        serve_tasks(pg, [task_card()])
        pg.goto(f"{WEB}/plans")
        expect(pg.get_by_role("navigation", name="底部导航").get_by_test_id("gen-badge")).to_have_text("1")
        expect(pg.get_by_test_id("gen-tile")).to_be_visible()
        assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")        # 没有横向溢出
    finally:
        ctx.close()
