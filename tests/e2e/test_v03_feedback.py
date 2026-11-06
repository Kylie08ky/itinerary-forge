"""你的四条反馈的前端验收：
1. 首页填开始 / 离开日期，到达 / 离开时间在来回交通里（M01 S4a、07 P07）；
2. 错误以弹窗显示在当前画面里；
3. 勾选「已订」→ 填入馆时间 → 行程锁定；时段票按时段调整（M06 S4）；
4. 分享是弹窗，点了一定看得见（M08 S5）。

默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_feedback.py
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

pytest.importorskip("playwright")
from helpers_form import fill_trip  # noqa: E402
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


@pytest.fixture
def page(browser) -> Page:
    # 窗口高度只有 900：弹窗是不是「在当前画面里」要在真实尺寸下才看得出来
    ctx = browser.new_context(viewport={"width": 1200, "height": 900}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def new_plan(start="2026-10-20", end="2026-10-23") -> str:
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": start, "end_date": end, "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art",
        "arrival_time": f"{start} 10:30", "departure_time": f"{end} 18:00"}, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()


def in_viewport(page: Page, locator) -> bool:
    box = locator.bounding_box()
    vh = page.viewport_size["height"]
    return bool(box) and box["y"] >= 0 and box["y"] + box["height"] <= vh


# --------------------------------------------------------------------------- #
# 1. 首页填开始 / 离开日期 + 来回交通
# --------------------------------------------------------------------------- #
def test_home_asks_for_start_and_end_dates_and_derives_the_days(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    start = page.get_by_label("开始日期")
    end = page.get_by_label("离开日期")
    assert start.input_value() and end.input_value()                          # 有合理的默认值
    start.fill("2026-11-10")
    end.fill("2026-11-13")
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 4 天（2026-11-10 → 2026-11-13）")


def test_dates_and_transport_are_validated_on_the_form_and_long_trips_are_only_advised(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    page.get_by_label("开始日期").fill("2026-11-10")
    page.get_by_label("离开日期").fill("2026-11-10")
    page.get_by_role("button", name="生成行程").click()
    expect(page.locator(".ferr").first).to_contain_text("离开日期要晚于开始日期")
    assert "/tasks/" not in page.url                                          # 校验不通过：没发起生成
    page.get_by_label("离开日期").fill("2026-11-20")                           # 11 天：只提醒，不拦
    expect(page.get_by_test_id("duration-verdict")).to_contain_text("偏长")
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 11 天")


def test_the_generated_plan_uses_the_dates_and_the_arrival_time_from_the_transport(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    fill_trip(page, "2026-11-10", "2026-11-13", arrive="15:30", leave="18:00")
    page.get_by_role("button", name="生成行程").click()
    page.wait_for_url(re.compile(r"/plans/paris-"), timeout=240_000)
    page.locator(".day").first.wait_for()
    expect(page.get_by_test_id("plan-title").locator(".sub")).to_contain_text("2026-11-10 → 2026-11-13")
    expect(page.locator(".day")).to_have_count(4)


# --------------------------------------------------------------------------- #
# 2. 错误弹窗
# --------------------------------------------------------------------------- #
def test_errors_pop_up_in_the_current_view_even_when_the_page_is_scrolled_down(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    row = page.locator(".rowc", has_text="卢浮宫").first
    row.get_by_role("checkbox", name="卢浮宫已订好了").check()
    row.scroll_into_view_if_needed()
    assert page.evaluate("window.scrollY") > 400                              # 已经滚到页面下半部分
    start = row.get_by_label("卢浮宫的入馆时间")
    start.fill("16:00")
    start.blur()
    expect(row.locator(".tag.locked-tag")).to_be_visible()
    end = row.get_by_label("卢浮宫的时段结束")
    end.fill("15:00")                                                         # 时段结束早于开始：后端拒绝
    end.blur()
    dialog = page.get_by_role("alertdialog", name="出错了")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("时段结束必须晚于入馆时间")
    assert in_viewport(page, dialog), "错误弹窗不在当前画面里"
    page.get_by_test_id("notice-ok").click()
    expect(dialog).to_have_count(0)


def test_the_error_popup_closes_with_escape(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="＋ 添加住宿").click()
    form = page.locator(".stay-form")
    form.get_by_label("名称").fill("A")
    form.get_by_label("所在区", exact=True).fill("1区")
    form.get_by_label("入住", exact=True).fill("2026-10-20")
    form.get_by_label("退房", exact=True).fill("2026-10-22")
    page.get_by_role("button", name="保存住宿").click()
    page.get_by_role("button", name="＋ 添加住宿").click()
    form.get_by_label("名称").fill("B")
    form.get_by_label("所在区", exact=True).fill("9区")
    form.get_by_label("入住", exact=True).fill("2026-10-21")
    form.get_by_label("退房", exact=True).fill("2026-10-23")
    page.get_by_role("button", name="保存住宿").click()
    expect(page.get_by_role("alertdialog", name="出错了")).to_contain_text("重叠")
    page.keyboard.press("Escape")
    expect(page.get_by_role("alertdialog", name="出错了")).to_have_count(0)


# --------------------------------------------------------------------------- #
# 3. 勾选已订 + 入馆时间 -> 锁定行程
# --------------------------------------------------------------------------- #
def louvre_item(page: Page):
    return page.locator('.item[data-pid="louvre"]')


def test_ticking_booked_and_entering_the_time_locks_the_itinerary(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    row = page.locator(".rowc", has_text="卢浮宫").first
    row.get_by_role("checkbox", name="卢浮宫已订好了").check()
    expect(row.locator(".tag.booked-ok")).to_be_visible()
    assert httpx.get(f"{API}/plans/{pid}", timeout=10).json()["latest_version"] == 1      # 只勾选：不动行程

    entry = row.get_by_label("卢浮宫的入馆时间")
    entry.fill("15:30")
    entry.blur()
    expect(row.locator(".tag.locked-tag")).to_contain_text("已锁定在 15:30")
    expect(page.locator(".history")).to_contain_text("预订锁定")
    expect(page.locator(".history")).to_contain_text("预订：卢浮宫 15:30 入场，已锁定")
    expect(louvre_item(page).locator(".t")).to_have_text("15:30")
    expect(louvre_item(page).locator(".lock-badge")).to_be_visible()                      # 行程里带 🔒

    row.get_by_role("checkbox", name="卢浮宫已订好了").uncheck()                          # 取消勾选：解锁，时间不动
    expect(page.locator(".history")).to_contain_text("取消预订")
    expect(louvre_item(page).locator(".lock-badge")).to_have_count(0)
    expect(louvre_item(page).locator(".t")).to_have_text("15:30")


def test_a_time_window_ticket_is_scheduled_inside_the_window(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    row = page.locator(".rowc", has_text="卢浮宫").first
    row.get_by_role("checkbox", name="卢浮宫已订好了").check()
    start = row.get_by_label("卢浮宫的入馆时间")
    start.fill("16:00")
    start.blur()
    expect(row.locator(".tag.locked-tag")).to_be_visible()
    end = row.get_by_label("卢浮宫的时段结束")
    end.fill("16:30")
    end.blur()
    expect(row.locator(".tag.locked-tag")).to_contain_text("时段 16:00–16:30")
    shown = louvre_item(page).locator(".t").inner_text()
    assert "16:00" <= shown <= "16:30"


def test_the_locked_time_survives_a_reload_and_the_exports_show_it(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    row = page.locator(".rowc", has_text="卢浮宫").first
    row.get_by_role("checkbox", name="卢浮宫已订好了").check()
    code = row.get_by_label("卢浮宫的确认码")
    code.fill("LV-424242")
    code.blur()
    entry = row.get_by_label("卢浮宫的入馆时间")
    entry.fill("14:15")
    entry.blur()
    expect(row.locator(".tag.locked-tag")).to_be_visible()
    page.reload()
    page.locator(".day").first.wait_for()
    row = page.locator(".rowc", has_text="卢浮宫").first
    expect(row.get_by_label("卢浮宫的入馆时间")).to_have_value("14:15")
    expect(row.get_by_label("卢浮宫的确认码")).to_have_value("LV-424242")
    html = httpx.get(f"{API}/plans/{pid}/itinerary.html", timeout=60).text
    assert "14:15" in html and "LV-424242" in html


def test_the_old_segmented_control_is_gone_in_favour_of_the_checkbox(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    expect(page.get_by_role("group", name=re.compile("订票状态"))).to_have_count(0)
    expect(page.get_by_role("checkbox", name=re.compile("已订好了")).first).to_be_visible()   # 清单异步加载


# --------------------------------------------------------------------------- #
# 4. 分享是弹窗
# --------------------------------------------------------------------------- #
def test_share_opens_a_popup_in_view_even_when_the_stays_card_is_tall(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="住哪个区？").click()   # 把住宿卡片撑高（旧版面板会被挤出画面）
    page.locator(".rec-row").first.wait_for()
    page.get_by_role("button", name="分享", exact=True).click()
    dialog = page.get_by_role("dialog", name="分享给朋友")
    expect(dialog).to_be_visible()
    assert in_viewport(page, dialog), "分享弹窗不在当前画面里"
    page.get_by_role("button", name="生成分享链接").click()
    expect(page.get_by_label("分享链接")).to_have_value(re.compile(r"/s/[A-Za-z0-9_-]{32,}$"))
    page.get_by_role("button", name="关闭", exact=True).click()
    expect(dialog).to_have_count(0)


def test_the_share_popup_also_closes_with_escape(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="分享", exact=True).click()
    expect(page.get_by_role("dialog", name="分享给朋友")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="分享给朋友")).to_have_count(0)
