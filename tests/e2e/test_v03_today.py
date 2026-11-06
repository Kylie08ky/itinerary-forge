"""v0.3 批次 F 今日视图 / 手机底栏 / 长图的前端验收（specs/modules/M15 S2、M08 S6）。

用浏览器的假时钟把「现在」拨到各种时刻；浏览器本身设在上海时区，证明显示的是**巴黎**的今天和现在，
不是设备的。默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_today.py
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e

WEB = os.getenv("E2E_WEB", "http://127.0.0.1:5173").rstrip("/")
API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")
PARIS_SUMMER = timezone(timedelta(hours=2))        # 10 月 25 日前巴黎是夏令时 UTC+2；行程在 10 月 20–23 日


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


@pytest.fixture(scope="module")
def pid() -> str:
    body = {"destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["museums"], "theme": "museums",
            "arrival_time": "2026-10-20 10:30", "departure_time": "2026-10-23 18:00",
            "avoid_ids": ["versailles"]}   # 凡尔赛那天只有一个点
    r = httpx.post(f"{API}/plans", json=body, timeout=120)
    assert r.status_code == 201, r.text
    plan_id = r.json()["plan_id"]
    yield plan_id
    httpx.delete(f"{API}/plans/{plan_id}", timeout=30)


@pytest.fixture(scope="module")
def days(pid) -> list[dict]:
    return httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]["days"]


def paris(date: str, hhmm: str, plus_min: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.fromisoformat(date).replace(hour=h, minute=m, tzinfo=PARIS_SUMMER) + timedelta(minutes=plus_min)


def end_of(item: dict, date: str) -> datetime:
    return paris(date, item["time"], item["duration_minutes"])


def open_at(browser, when: datetime, *, width=1280, height=1400, mobile=False):
    """在假时钟拨到 when 的浏览器里（设备时区 = 上海）新开一页。返回 (context, page)。"""
    ctx = browser.new_context(viewport={"width": width, "height": height}, locale="zh-CN",
                              timezone_id="Asia/Shanghai", is_mobile=mobile, has_touch=mobile)
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    page = ctx.new_page()
    page.clock.set_fixed_time(when)
    page._natives = natives  # type: ignore[attr-defined]
    return ctx, page


def card(page: Page):
    return page.get_by_test_id("today-card")


def states(page: Page) -> list[str]:
    return [e.get_attribute("data-state") for e in page.get_by_test_id("today-item").all()]


def go_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()


# --------------------------------------------------------------------------- #
# S2a / S2b 卡片内容随「现在」变化
# --------------------------------------------------------------------------- #
def test_before_the_first_item_it_says_where_the_day_starts_and_how_long_to_go(browser, pid, days):
    d = days[1]
    first = d["items"][0]
    ctx, page = open_at(browser, paris(d["date"], first["time"], -90))
    try:
        go_plan(page, pid)
        expect(card(page)).to_be_visible()
        expect(page.get_by_test_id("today-meta")).to_contain_text(f"第 2 天 / 共 4 天 · {d['date']}")
        nxt = page.get_by_test_id("today-next")
        expect(nxt).to_contain_text("今天第一站")
        expect(nxt).to_contain_text(first["title"])
        expect(nxt).to_contain_text(f"{first['time']} 开始，还有 1 小时 30 分钟")
        expect(page.get_by_test_id("today-now")).to_have_count(0)
        assert states(page) == ["next"] + ["later"] * (len(d["items"]) - 1)
        expect(page.get_by_test_id("today-remaining")).to_have_text(f"今天还剩 {len(d['items'])} 个安排")
    finally:
        ctx.close()


def test_during_an_item_it_shows_now_and_the_next_stop_with_a_navigation_link(browser, pid, days):
    d = days[1]
    first, second = d["items"][0], d["items"][1]
    ctx, page = open_at(browser, paris(d["date"], first["time"], 5))
    try:
        go_plan(page, pid)
        now = page.get_by_test_id("today-now")
        expect(now).to_contain_text(first["title"])
        expect(now).to_contain_text(re.compile(r"到 \d\d:\d\d 结束，还有 .*(分钟|小时)"))
        nxt = page.get_by_test_id("today-next")
        expect(nxt).to_contain_text("下一站")
        expect(nxt).to_contain_text(second["title"])
        assert states(page)[:2] == ["current", "next"]
        link = nxt.get_by_role("link", name=re.compile("导航"))
        if second["place_id"]:
            href = link.get_attribute("href")
            assert href.startswith("https://www.google.com/maps/dir/?api=1&destination=")
            assert "origin=" not in href                                            # 不写起点 = 用你当前的位置
            assert "travelmode=transit" in href
        else:
            expect(link).to_have_count(0)                                           # 没有坐标的安排不给导航
    finally:
        ctx.close()


def test_between_items_there_is_no_current_and_finished_ones_are_struck_through(browser, pid, days):
    d = days[1]
    items = d["items"]
    gap = next((i for i in range(len(items) - 1)
                if end_of(items[i], d["date"]) < paris(d["date"], items[i + 1]["time"])), None)
    if gap is None:
        pytest.skip("这份行程第 2 天的安排首尾相接，没有空档")
    ctx, page = open_at(browser, end_of(items[gap], d["date"]) + timedelta(minutes=1))
    try:
        go_plan(page, pid)
        expect(page.get_by_test_id("today-now")).to_have_count(0)
        expect(page.get_by_test_id("today-next")).to_contain_text(items[gap + 1]["title"])
        expect(page.get_by_test_id("today-remaining")).to_have_text(f"今天还剩 {len(items) - gap - 1} 个安排")
        assert states(page)[: gap + 1] == ["done"] * (gap + 1) and states(page)[gap + 1] == "next"
        done = page.get_by_test_id("today-item").first
        assert done.evaluate("e => getComputedStyle(e.querySelector('.title')).textDecorationLine") == "line-through"
    finally:
        ctx.close()


def test_after_the_last_item_it_says_done_and_previews_tomorrow(browser, pid, days):
    d, tomorrow = days[1], days[2]
    ctx, page = open_at(browser, end_of(d["items"][-1], d["date"]) + timedelta(minutes=1))
    try:
        go_plan(page, pid)
        done = page.get_by_test_id("today-done")
        expect(done).to_contain_text("今天的安排都完成了")
        expect(done).to_contain_text(f"明天第一站：{tomorrow['items'][0]['title']}（{tomorrow['items'][0]['time']}）")
        expect(page.get_by_test_id("today-remaining")).to_have_text("今天还剩 0 个安排")
        assert set(states(page)) == {"done"}
    finally:
        ctx.close()


def test_on_the_last_day_after_the_last_item_there_is_no_tomorrow_preview(browser, pid, days):
    d = days[3]
    ctx, page = open_at(browser, end_of(d["items"][-1], d["date"]) + timedelta(minutes=1))
    try:
        go_plan(page, pid)
        expect(page.get_by_test_id("today-done")).to_have_text("今天的安排都完成了。")
    finally:
        ctx.close()


def test_it_follows_paris_time_not_the_device_time(browser, pid, days):
    """设备在上海（UTC+8）：同一个时刻，上海的日期可能已经是第二天，卡片必须仍按巴黎算。"""
    d = days[1]
    last = d["items"][-1]
    when = end_of(last, d["date"]) - timedelta(minutes=10)          # 巴黎当天最后一项还没结束，上海已是次日凌晨前后
    ctx, page = open_at(browser, when)
    try:
        shanghai = page.evaluate("new Date().toLocaleString('sv-SE', {timeZone: 'Asia/Shanghai'})")
        assert shanghai[:10] >= d["date"]
        go_plan(page, pid)
        expect(page.get_by_test_id("today-meta")).to_contain_text(f"· {d['date']}（")
        assert states(page)[-1] == "current"
    finally:
        ctx.close()


@pytest.mark.parametrize("when,label", [("2026-10-19T12:00:00+02:00", "行程开始前一天"),
                                         ("2026-10-24T09:00:00+02:00", "行程结束后一天"),
                                         ("2026-12-01T09:00:00+01:00", "很久以后")])
def test_outside_the_trip_there_is_no_today_card_no_chip_no_directory_entry(browser, pid, when, label):
    ctx, page = open_at(browser, datetime.fromisoformat(when))
    try:
        go_plan(page, pid)
        page.get_by_test_id("section-nav").wait_for()
        expect(card(page)).to_have_count(0)
        expect(page.get_by_test_id("today-chip")).to_have_count(0)
        expect(page.get_by_test_id("section-nav").get_by_role("button", name="今天", exact=True)).to_have_count(0)
    finally:
        ctx.close()


# --------------------------------------------------------------------------- #
# 行程页：今天那天的标记、目录入口、自动刷新
# --------------------------------------------------------------------------- #
def test_the_plan_page_marks_today_and_offers_it_in_the_directory(browser, pid, days):
    d = days[1]
    ctx, page = open_at(browser, paris(d["date"], d["items"][0]["time"], 5))
    try:
        go_plan(page, pid)
        page.get_by_test_id("section-nav").wait_for()
        expect(page.get_by_test_id("today-chip")).to_have_count(1)
        expect(page.locator(f"#day-{d['date']}").get_by_test_id("today-chip")).to_be_visible()
        today_btn = page.get_by_test_id("section-nav").get_by_role("button", name="今天", exact=True)
        expect(today_btn).to_have_count(1)
        labels = [t.strip() for t in page.get_by_test_id("section-nav").locator("button").all_inner_texts()]
        assert labels[:2] == ["概览", "今天"]
        page.get_by_role("button", name="编辑行程", exact=True).click()
        page.get_by_role("button", name=re.compile("保存为新版本")).wait_for()
        expect(card(page)).to_have_count(0)                                         # 编辑时让开
    finally:
        ctx.close()


def test_the_card_refreshes_by_itself_as_time_passes(browser, pid, days):
    d = days[1]
    first, second = d["items"][0], d["items"][1]
    start = paris(d["date"], first["time"], -2)
    ctx = browser.new_context(viewport={"width": 1280, "height": 1400}, locale="zh-CN", timezone_id="Asia/Shanghai")
    page = ctx.new_page()
    page.clock.install(time=start)
    try:
        go_plan(page, pid)
        expect(page.get_by_test_id("today-next")).to_contain_text(first["title"])
        expect(page.get_by_test_id("today-now")).to_have_count(0)
        page.clock.run_for(5 * 60 * 1000)                                           # 过 5 分钟：第一项开始了
        expect(page.get_by_test_id("today-now")).to_contain_text(first["title"])
        expect(page.get_by_test_id("today-next")).to_contain_text(second["title"])
    finally:
        ctx.close()


# --------------------------------------------------------------------------- #
# 首页、手机底栏、/today
# --------------------------------------------------------------------------- #
def test_the_home_page_shows_today_on_top_with_a_link_to_the_plan(browser, pid, days):
    d = days[1]
    ctx, page = open_at(browser, paris(d["date"], d["items"][0]["time"], 5))
    try:
        page.goto(WEB)
        card(page).wait_for()
        expect(card(page).get_by_role("link", name="查看行程")).to_have_attribute("href", f"/plans/{pid}")
        hero_bottom = page.get_by_test_id("home-hero").evaluate("e => e.getBoundingClientRect().bottom + scrollY")
        top = card(page).evaluate("e => e.getBoundingClientRect().top + scrollY")
        assert top >= hero_bottom - 1                                              # 在首页英雄下面（首页没有表单了）
        expect(page.locator(".step-card")).to_have_count(0)
    finally:
        ctx.close()


def test_on_a_phone_the_bottom_bar_gets_a_today_tab_only_during_the_trip(browser, pid, days):
    d = days[1]
    ctx, page = open_at(browser, paris(d["date"], d["items"][0]["time"], 5), width=390, height=844, mobile=True)
    try:
        page.goto(f"{WEB}/plans")
        bar = page.get_by_role("navigation", name="底部导航")
        bar.wait_for()
        expect(bar.get_by_role("link")).to_have_text(["首页", "新建", "我的行程", "今天", "用量"])
        bar.get_by_role("link", name="今天").click()
        expect(page).to_have_url(re.compile(r"/today$"))
        expect(page.get_by_test_id("today-card")).to_have_class(re.compile(r"\bbig\b"))
        size = page.evaluate("({sw: document.documentElement.scrollWidth, iw: window.innerWidth})")
        assert size["sw"] <= size["iw"], size
        expect(bar.get_by_role("link", name="今天")).to_have_class(re.compile("active"))
        nav_link = page.get_by_test_id("today-next").get_by_role("link", name=re.compile("导航"))
        if nav_link.count():
            assert nav_link.bounding_box()["height"] >= 43.5                        # 点击区域 ≥ 44px
    finally:
        ctx.close()
    later = datetime.fromisoformat("2026-12-01T09:00:00+01:00")
    ctx2, page2 = open_at(browser, later, width=390, height=844, mobile=True)
    try:
        page2.goto(f"{WEB}/plans")
        bar2 = page2.get_by_role("navigation", name="底部导航")
        bar2.wait_for()
        expect(bar2.get_by_role("link")).to_have_text(["首页", "新建", "我的行程", "用量"])
        page2.goto(f"{WEB}/today")
        expect(page2.get_by_test_id("today-empty")).to_contain_text("今天不在任何行程里")
        expect(page2.get_by_test_id("today-empty").get_by_role("link", name="我的行程")).to_be_visible()
    finally:
        ctx2.close()


def test_the_today_page_also_shows_each_items_note(browser, pid, days):
    d = days[1]
    first = d["items"][0]
    ref = first["place_id"] or f"{d['date']}|{first['title']}"
    put = httpx.put(f"{API}/plans/{pid}/notes", json={"ref": ref, "text": "记得带学生证"}, timeout=10)
    assert put.status_code == 200
    ctx, page = open_at(browser, paris(d["date"], first["time"], 5), width=390, height=844, mobile=True)
    try:
        page.goto(f"{WEB}/today")
        card(page).wait_for()
        expect(card(page)).to_contain_text("记得带学生证")
        expect(card(page).get_by_test_id("today-item").first).to_contain_text(first["location"])
    finally:
        httpx.put(f"{API}/plans/{pid}/notes", json={"ref": ref, "text": ""}, timeout=10)
        ctx.close()


def test_an_archived_plan_is_never_picked_as_today(browser, pid):
    body = {"destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["art"], "theme": "art",
            "arrival_time": "2026-10-20 10:30", "departure_time": "2026-10-23 18:00"}
    other = httpx.post(f"{API}/plans", json=body, timeout=120).json()["plan_id"]      # 比 pid 新：重叠时优先
    ctx, page = open_at(browser, paris("2026-10-21", "11:00"))
    try:
        page.goto(WEB)
        card(page).wait_for()
        expect(card(page).get_by_role("link", name="查看行程")).to_have_attribute("href", f"/plans/{other}")
        assert httpx.put(f"{API}/plans/{other}/archive", json={"archived": True}, timeout=10).status_code == 200
        page.reload()
        card(page).wait_for()
        expect(card(page).get_by_role("link", name="查看行程")).to_have_attribute("href", f"/plans/{pid}")
    finally:
        httpx.delete(f"{API}/plans/{other}", timeout=30)
        ctx.close()


# --------------------------------------------------------------------------- #
# 长图导出
# --------------------------------------------------------------------------- #
def test_the_export_long_image_button_shows_progress_then_downloads_a_phone_width_png(browser, pid):
    ctx = browser.new_context(viewport={"width": 1280, "height": 1400}, locale="zh-CN", accept_downloads=True)
    page = ctx.new_page()
    try:
        go_plan(page, pid)
        page.get_by_role("button", name="更多").click()
        btn = page.get_by_role("button", name="导出长图")
        with page.expect_download() as dl:
            btn.click()
            expect(page.get_by_role("button", name="生成长图中…")).to_be_disabled()
        assert dl.value.suggested_filename.endswith(".png")
        data = open(dl.value.path(), "rb").read()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width = int.from_bytes(data[16:20], "big")
        assert width in (760, 1520)
        expect(page.get_by_role("button", name="导出长图")).to_be_enabled()
    finally:
        ctx.close()


def test_a_failed_long_image_shows_an_error_popup(browser, pid):
    ctx = browser.new_context(viewport={"width": 1280, "height": 1400}, locale="zh-CN")
    page = ctx.new_page()
    try:
        go_plan(page, pid)
        page.route(re.compile(r".*/itinerary\.png.*"), lambda r: r.fulfill(
            status=503, body='{"detail":"长图生成失败（本机缺 Chrome/Edge 或 Playwright？）：boom"}',
            content_type="application/json"))
        page.get_by_role("button", name="更多").click()
        page.get_by_role("button", name="导出长图").click()
        expect(page.get_by_role("alertdialog")).to_contain_text("长图生成失败")
        assert len(ctx.pages) == 1
    finally:
        ctx.close()


# --------------------------------------------------------------------------- #
# 导航：晚饭时间也要能点到导航
# --------------------------------------------------------------------------- #
def dinner_day(days: list[dict]) -> dict:
    day = next(d for d in days if d["items"][-1]["type"] in ("meal", "food") and not d["items"][-1]["place_id"])
    return day


def test_when_the_next_stop_is_dinner_it_offers_a_nearby_restaurant_search(browser, pid, days):
    d = dinner_day(days)
    items = d["items"]
    ctx, page = open_at(browser, end_of(items[-2], d["date"]) + timedelta(minutes=1))
    try:
        go_plan(page, pid)
        nxt = page.get_by_test_id("today-next")
        expect(nxt).to_contain_text(items[-1]["title"])
        expect(nxt.get_by_role("link", name=re.compile("导航"))).to_have_count(0)        # 晚饭没有坐标
        link = nxt.get_by_role("link", name=re.compile("搜附近餐厅"))
        href = link.get_attribute("href")
        assert href.startswith("https://www.google.com/maps/search/?api=1&query=")
        assert "origin" not in href and "destination" not in href                         # 不写位置：用你当前的位置
    finally:
        ctx.close()


def test_every_located_item_has_its_own_navigation_link_at_any_time_of_day(browser, pid, days):
    d = dinner_day(days)
    items = d["items"]
    ctx, page = open_at(browser, paris(d["date"], items[-1]["time"], 5))               # 正在吃晚饭：没有「下一站」
    try:
        go_plan(page, pid)
        expect(page.get_by_test_id("today-now")).to_contain_text(items[-1]["title"])
        expect(page.get_by_test_id("today-next")).to_have_count(0)
        near = page.get_by_test_id("today-now").get_by_role("link", name=re.compile("搜附近餐厅"))
        assert near.get_attribute("href").startswith("https://www.google.com/maps/search/?api=1&query=")
        rows = page.get_by_test_id("today-item")
        last = rows.nth(len(items) - 1).get_by_role("link", name=re.compile("附近的餐厅"))
        assert last.get_attribute("href") == near.get_attribute("href")
        for i, it in enumerate(items):
            link = rows.nth(i).get_by_role("link", name=f"导航到{it['title']}")
            if it["place_id"]:
                href = link.get_attribute("href")                                         # 已结束的也能点
                assert href.startswith("https://www.google.com/maps/dir/?api=1&destination=")
                assert "origin=" not in href and "travelmode=transit" in href
            else:
                expect(link).to_have_count(0)
    finally:
        ctx.close()


def test_the_row_navigation_links_are_big_enough_to_tap_on_a_phone(browser, pid, days):
    d = days[1]
    ctx, page = open_at(browser, paris(d["date"], d["items"][0]["time"], 5), width=390, height=844, mobile=True)
    try:
        page.goto(f"{WEB}/today")
        card(page).wait_for()
        links = card(page).locator("a.today-row-nav")
        assert links.count() >= 1
        for box in [e.bounding_box() for e in links.all()]:
            assert box["height"] >= 43.5 and box["width"] >= 43.5
        size = page.evaluate("({sw: document.documentElement.scrollWidth, iw: window.innerWidth})")
        assert size["sw"] <= size["iw"], size
    finally:
        ctx.close()
