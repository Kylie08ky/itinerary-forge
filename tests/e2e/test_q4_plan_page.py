"""Q4 行程页减负与重排的前端验收（specs/07-polish-backlog.md P10–P14）。

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_q4_plan_page.py
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


def make_page(browser, width=1440, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height}, locale="zh-CN",
                              is_mobile=width < 500, has_touch=width < 500)
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    page = ctx.new_page()
    page.clock.set_fixed_time(datetime(2026, 9, 1, 12, 0))
    return ctx, page, natives


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


# ---- P10：顶部只留结论 ----
def test_the_top_of_a_clean_plan_has_no_quality_or_generation_noise(browser, pid):
    ctx, page, natives = make_page(browser)
    try:
        open_plan(page, pid)
        hero = page.locator(".plan-hero")
        text = hero.inner_text()
        for noise in ("质检", "error", "warning", "生成过程", "耗时", "模型调用", "日期平移后", "说明文字由模板生成"):
            assert noise not in text, (noise, text)
        expect(page.locator(".qa")).to_have_count(0)
        expect(page.get_by_test_id("plan-reminders")).to_have_count(0)                  # 没问题：什么提醒都没有
    finally:
        ctx.close()
    assert not natives


def test_a_real_problem_becomes_a_plain_reminder_not_a_quality_badge(browser, pid):
    import json as _json

    def inject(route):
        resp = route.fetch()
        data = resp.json()
        data["report"]["findings"] = [{
            "code": "closed_day", "severity": "warning", "day": "2026-10-21", "item_index": 1,
            "message": "卢浮宫周二闭馆，这一天最好换个安排"}]
        route.fulfill(response=resp, body=_json.dumps(data, ensure_ascii=False))

    ctx, page, natives = make_page(browser)
    try:
        page.route(re.compile(re.escape(f"{API}/plans/{pid}") + r"(\?.*)?$"), inject)
        open_plan(page, pid)
        box = page.get_by_test_id("plan-reminders")
        expect(box).to_contain_text("有 1 条提醒")
        box.locator("summary").click()
        expect(box).to_contain_text("卢浮宫周二闭馆")
        assert "质检" not in page.locator(".plan-hero").inner_text()
    finally:
        ctx.close()
    assert not natives


def test_generation_details_live_in_the_more_menu(browser, pid):
    ctx, page, natives = make_page(browser)
    try:
        open_plan(page, pid)
        page.get_by_role("button", name="更多").click()
        page.get_by_test_id("open-gen-details").click()
        dialog = page.get_by_role("dialog", name="生成详情")
        expect(dialog).to_be_visible()
        expect(dialog.locator(".qa")).to_contain_text("0 error")
        expect(dialog.locator("details.trace")).to_have_count(1)
        expect(dialog).to_contain_text("主题：")
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
    finally:
        ctx.close()
    assert not natives


# ---- P11：目录在页面最左侧 ----
def test_on_a_wide_screen_the_directory_is_a_vertical_rail_on_the_far_left(browser, pid):
    ctx, page, natives = make_page(browser, 1440, 900)
    try:
        open_plan(page, pid)
        nav = page.get_by_test_id("section-nav")
        expect(nav).to_have_class(re.compile(r"\brail\b"))
        nav_box, main_box = nav.bounding_box(), page.locator(".plan-main").bounding_box()
        aside_box = page.locator(".plan-aside").bounding_box()
        assert nav_box["x"] < main_box["x"] and nav_box["x"] + nav_box["width"] <= main_box["x"]       # 目录在日程左边
        assert aside_box["x"] >= main_box["x"] + main_box["width"] - 1                                  # 地图仍在右边
        tops = nav.locator("button").evaluate_all("els => els.map(e => Math.round(e.getBoundingClientRect().top))")
        assert tops == sorted(tops) and len(set(tops)) == len(tops)                                      # 竖着排
        top_before = nav.bounding_box()["y"]
        page.evaluate(
            "window.scrollTo(0, document.getElementById('cover-end').getBoundingClientRect().top + scrollY + 700)")
        page.wait_for_timeout(500)
        # 滚动时固定在左边
        assert 0 <= nav.bounding_box()["y"] <= 120, (top_before, nav.bounding_box())
        nav.get_by_role("button", name="第 3 天").click()
        page.wait_for_timeout(1100)
        expect(nav.get_by_role("button", name="第 3 天")).to_have_attribute("aria-current", "true")
        top = page.locator(".day").nth(2).bounding_box()["y"]
        # 没被导航条遮住
        assert 40 <= top <= 220, top
    finally:
        ctx.close()
    assert not natives


def test_on_a_narrower_window_the_directory_becomes_the_top_bar_again(browser, pid):
    ctx, page, natives = make_page(browser, 1150, 800)
    try:
        open_plan(page, pid)
        nav = page.get_by_test_id("section-nav")
        expect(nav).not_to_have_class(re.compile(r"\brail\b"))
        boxes = nav.locator("button").evaluate_all("els => els.map(e => e.getBoundingClientRect().top)")
        assert max(boxes) - min(boxes) < 4, boxes                                                         # 横着排
    finally:
        ctx.close()
    assert not natives


# ---- P13：随行清单在日程之后 ----
def test_companion_budget_and_stays_come_after_the_itinerary(browser, pid):
    ctx, page, natives = make_page(browser)
    try:
        open_plan(page, pid)
        expect(page.get_by_test_id("section-nav").locator("button")).to_have_count(12)
        labels = [t.strip() for t in page.get_by_test_id("section-nav").locator("button").all_inner_texts()]
        assert labels == ["概览", "第 1 天 · 10/20", "第 2 天 · 10/21", "第 3 天 · 10/22", "第 4 天 · 10/23",
                          "购票与风险", "住宿", "预算", "随行",
                          "作品", "修改意见", "版本历史"]
        ids = ["sec-top", "sec-booking", "sec-stays", "sec-budget", "sec-companion", "sec-culture", "sec-feedback",
               "sec-history"]
        ys = page.evaluate(
            "ids => ids.map(id => document.getElementById(id).getBoundingClientRect().top + scrollY)", ids)
        last_day_y = page.locator(".day").last.evaluate("e => e.getBoundingClientRect().top + scrollY")
        assert ys == sorted(ys) and last_day_y < ys[2], (ys, last_day_y)
    finally:
        ctx.close()
    assert not natives


# ---- P12：说明收起来 ----
def test_budget_rows_show_only_the_conclusion_until_you_open_the_basis(browser, pid):
    ctx, page, natives = make_page(browser)
    try:
        open_plan(page, pid)
        card = page.get_by_test_id("budget-card")
        tickets = page.get_by_test_id("budget-tickets")
        expect(tickets).to_contain_text("¥")
        info = tickets.locator("details.b-info")
        expect(info).to_have_count(1)
        expect(info.locator(".b-note")).to_be_hidden()                                    # 默认看不到计算规则
        expect(card.locator(".b-detail:visible")).to_have_count(0)
        info.locator("summary").click()
        expect(info.locator(".b-note")).to_be_visible()
    finally:
        ctx.close()
    assert not natives


# ---- P14：地点条目精简 ----
def test_an_item_shows_title_meta_and_key_badges_and_a_detail_tag_opens_the_place_card(browser, pid):
    ctx, page, natives = make_page(browser)
    try:
        open_plan(page, pid)
        item = page.locator(".item[data-pid]").filter(has=page.get_by_test_id("item-detail")).first
        expect(item.locator("h4")).to_be_visible()
        expect(item.locator(".meta")).to_be_visible()
        # 条目里不再有行内展开的详情：参观重点、购票提示、地址、核对记录都在地点浮窗里
        for gone in ("details", ".highlights", ".ticket-note", ".item-addr", ".verified"):
            expect(item.locator(gone)).to_have_count(0)
        assert item.locator(".tags .tag").count() <= 4
        # 「详情」标签就在名字旁边（同一行），点开是地点浮窗
        tag, title = item.get_by_test_id("item-detail"), item.locator("h4 .title-link")
        tag_box, title_box = tag.bounding_box(), title.bounding_box()
        assert abs((tag_box["y"] + tag_box["height"] / 2) - (title_box["y"] + title_box["height"] / 2)) < 20
        assert tag_box["x"] >= title_box["x"] + title_box["width"] - 1
        name = title.inner_text().strip()
        tag.click()
        dialog = page.get_by_role("dialog", name="地点详情")
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text(name)
        expect(dialog).to_contain_text("地址")
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        # 一天的时间轴一屏能放下至少 4 个条目
        visible_items = page.locator(".day").first.locator(".item").evaluate_all(
            "els => els.filter(e => e.getBoundingClientRect().height < 230).length")
        assert visible_items >= 4, visible_items
    finally:
        ctx.close()
    assert not natives


def test_the_place_card_is_laid_out_in_clear_sections(browser, pid):
    ctx, page, natives = make_page(browser)
    try:
        open_plan(page, pid)
        page.locator(".item[data-pid]").filter(has=page.get_by_test_id("item-detail")).nth(1).get_by_test_id(
            "item-detail").click()
        card = page.get_by_role("dialog", name="地点详情")
        expect(card.locator(".pc-head h2")).to_be_visible()
        titles = card.locator(".pc-section h3").all_inner_texts()
        assert "基本信息" in titles, titles
        # 地址 / 交通 / 停留 / 票价 / 闭馆日
        expect(card.locator(".facts dt")).to_have_count(5, timeout=3000)
        if card.locator(".highlights").count():
            # 参观重点是清单，不是一堆标签
            assert card.locator(".highlights .pc-list li").count() >= 1
            assert card.locator(".highlights .hchip").count() == 0
        details = card.locator("details.verification")
        assert details.get_attribute("open") is None                                        # 核对记录默认收起
        details.locator("summary").click()
        expect(card.locator(".verification-list .vstat").first).to_be_visible()
    finally:
        ctx.close()
    assert not natives


def test_on_a_phone_the_plan_page_has_no_sideways_scroll_and_big_enough_taps(browser, pid):
    ctx, page, natives = make_page(browser, 390, 844)
    try:
        open_plan(page, pid)
        size = page.evaluate("({sw: document.documentElement.scrollWidth, iw: window.innerWidth})")
        assert size["sw"] <= size["iw"], size
        small = page.locator(".detail-tag, .b-info summary, .note-add").evaluate_all(
            "els => els.filter(e => { const h = e.getBoundingClientRect().height; return h > 0 && h < 43.5 }).length")
        assert small == 0, small
    finally:
        ctx.close()
    assert not natives
