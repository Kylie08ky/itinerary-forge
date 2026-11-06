"""行程页「本页目录」的前端验收（specs/06-ui-design.md S10）。

默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_nav.py
"""

from __future__ import annotations

import os
import re
from datetime import datetime

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e

WEB = os.getenv("E2E_WEB", "http://127.0.0.1:5173").rstrip("/")
API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")


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


# 1150：目录还是顶部横栏（≥ 1280 变成左侧竖栏，见 test_q4_plan_page.py）
def make_page(browser, width=1150, height=800):
    ctx = browser.new_context(viewport={"width": width, "height": height}, locale="zh-CN",
                              is_mobile=width < 500, has_touch=width < 500)
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    page = ctx.new_page()
    page.clock.set_fixed_time(datetime(2026, 9, 1, 12, 0))   # 行程开始之前：不管真实日期是哪天，都没有「今天」
    return ctx, page, natives


@pytest.fixture
def page(browser) -> Page:
    ctx, pg, natives = make_page(browser)
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


@pytest.fixture(scope="module")
def pid() -> str:
    body = {"destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["museums"], "theme": "museums",
            "arrival_time": "2026-10-20 10:30", "departure_time": "2026-10-23 18:00"}
    r = httpx.post(f"{API}/plans", json=body, timeout=120)
    assert r.status_code == 201, r.text
    plan_id = r.json()["plan_id"]
    yield plan_id
    httpx.delete(f"{API}/plans/{plan_id}", timeout=30)


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    page.get_by_test_id("companion-card").wait_for()
    # 预算是异步出现的，目录要等它出来再读
    page.get_by_test_id("budget-card").wait_for()
    page.get_by_test_id("section-nav").wait_for()


def nav(page: Page):
    return page.get_by_test_id("section-nav")


def settle(page: Page) -> None:
    # 平滑滚动结束 + 加载把目标顶下去后的校正
    page.wait_for_timeout(1800)


def test_the_directory_lists_only_the_sections_this_page_really_has(page, pid):
    open_plan(page, pid)
    # 目录是跟着页面异步更新的，等它齐了再读
    expect(nav(page).locator("button")).to_have_count(12)  # 行程里有地点出现在作品里：多一个「作品」
    labels = [t.strip() for t in nav(page).locator("button").all_inner_texts()]
    assert labels == ["概览", "第 1 天 · 10/20", "第 2 天 · 10/21", "第 3 天 · 10/22", "第 4 天 · 10/23",
                      "购票与风险", "住宿", "预算", "随行",
                      "作品", "修改意见", "版本历史"]
    expect(nav(page)).to_have_attribute("aria-label", "本页目录")


def test_clicking_a_section_scrolls_to_it_without_the_bar_covering_its_title(page, pid):
    open_plan(page, pid)
    nav(page).get_by_role("button", name="预算").click()
    settle(page)
    top = page.get_by_test_id("budget-card").evaluate("e => e.getBoundingClientRect().top")
    bar_bottom = nav(page).evaluate("e => e.getBoundingClientRect().bottom")
    assert bar_bottom + 2 <= top <= bar_bottom + 40, (top, bar_bottom)                       # 紧贴目录条下面，没被盖住
    expect(nav(page).get_by_role("button", name="预算")).to_have_attribute("aria-current", "true")
    nav(page).get_by_role("button", name="版本历史").click()
    settle(page)
    expect(page.locator("#sec-history")).to_be_in_viewport()
    expect(nav(page).get_by_role("button", name="版本历史")).to_have_attribute("aria-current", "true")


def test_the_bar_sticks_to_the_top_and_the_current_section_follows_scrolling(page, pid):
    open_plan(page, pid)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    expect(nav(page).get_by_role("button", name="概览")).to_have_attribute("aria-current", "true")
    page.locator("#sec-feedback").scroll_into_view_if_needed()
    page.evaluate("window.scrollBy(0, -20)")
    page.wait_for_timeout(500)
    top = nav(page).evaluate("e => e.getBoundingClientRect().top")
    assert 0 <= top < 80, top                                                  # 滚到很下面了，目录条还在顶部
    expect(nav(page).locator("button[aria-current=true]")).to_have_count(1)
    expect(nav(page).locator("button[aria-current=true]")).not_to_have_text("概览")


def test_jumping_to_a_collapsed_day_expands_it(page, pid):
    open_plan(page, pid)
    page.get_by_role("button", name="收起第 3 天").click()
    expect(page.get_by_role("button", name="展开第 3 天")).to_have_attribute("aria-expanded", "false")
    nav(page).get_by_role("button", name="第 3 天").click()
    settle(page)
    expect(page.get_by_role("button", name="收起第 3 天")).to_have_attribute("aria-expanded", "true")
    top = page.locator(".day").nth(2).evaluate("e => e.getBoundingClientRect().top")
    bar_bottom = nav(page).evaluate("e => e.getBoundingClientRect().bottom")
    assert bar_bottom + 2 <= top <= bar_bottom + 60, (top, bar_bottom)
    page.get_by_role("button", name="收起第 3 天").click()                                      # 还原：别影响别的测试
    page.get_by_role("button", name="展开第 3 天").click()


def test_while_editing_the_stays_budget_and_companion_entries_are_gone(page, pid):
    open_plan(page, pid)
    page.get_by_role("button", name="编辑行程", exact=True).click()
    page.get_by_role("button", name=re.compile("保存为新版本")).wait_for()
    page.wait_for_timeout(400)
    labels = [t.strip() for t in nav(page).locator("button").all_inner_texts()]
    assert "预算" not in labels and "住宿" not in labels and "随行" not in labels
    assert "第 1 天 · 10/20" in labels and "修改意见" in labels


def test_no_directory_while_comparing_versions(page, pid):
    plan = httpx.get(f"{API}/plans/{pid}", timeout=30).json()
    if plan["latest_version"] < 2:
        r = httpx.post(f"{API}/plans/{pid}/shift", json={"start_date": "2026-10-21"}, timeout=60)
        assert r.status_code == 201
    open_plan(page, pid)
    page.get_by_role("button", name="对比").click()
    page.wait_for_timeout(500)
    expect(page.get_by_test_id("section-nav")).to_have_count(0)


def test_on_a_phone_the_bar_scrolls_sideways_by_itself_and_the_page_does_not(browser, pid):
    ctx, pg, natives = make_page(browser, 390, 844)
    try:
        open_plan(pg, pid)
        size = pg.evaluate("({sw: document.documentElement.scrollWidth, iw: window.innerWidth})")
        assert size["sw"] <= size["iw"], size
        small = pg.get_by_test_id("section-nav").locator("button").evaluate_all(
            "els => els.filter(e => e.getBoundingClientRect().height < 43.5).map(e => e.innerText)")
        assert not small, small
        sl = pg.locator(".section-nav-list")
        assert sl.evaluate("e => e.scrollWidth > e.clientWidth")                             # 条目多：它自己横向滚
        pg.get_by_test_id("section-nav").get_by_role("button", name="版本历史").click()
        pg.wait_for_timeout(1000)
        expect(pg.locator("#sec-history")).to_be_in_viewport()
        top = pg.get_by_test_id("section-nav").evaluate("e => e.getBoundingClientRect().top")
        assert top < 80, top
    finally:
        ctx.close()
    assert not natives
