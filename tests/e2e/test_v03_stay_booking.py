"""v0.3 住宿与预订的前端验收（specs/modules/M06 S3 / S4 / S5 / S5b）。

默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_stay_booking.py
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
    ctx = browser.new_context(viewport={"width": 1200, "height": 2400}, locale="zh-CN")
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

def open_more(page: Page) -> None:
    """操作藏在「更多」菜单里（规格 06-ui-design）：先打开菜单。"""
    page.get_by_role("button", name="更多").click()


def dismiss_conflicts(page: Page) -> None:
    """平移后如果有新的闭馆日冲突，会弹窗提示；不关掉它会挡住后面的点击。"""
    later = page.get_by_role("button", name="稍后再处理")
    try:
        later.wait_for(timeout=1500)
        later.click()
    except Exception:  # noqa: BLE001 - 没有冲突弹窗就什么都不用做
        pass


def open_full_map(page: Page) -> None:
    """桌面端全程地图一直在右侧；窄屏是折叠卡片，要点开。"""
    summary = page.locator("details.routecard summary")
    if summary.count():
        summary.click()
    page.get_by_test_id("full-map").wait_for()



def fill_stay(page: Page, name: str, **kw) -> None:
    form = page.locator(".stay-form")
    form.get_by_label("名称").fill(name)
    if "area" in kw:
        form.get_by_label("所在区", exact=True).fill(kw["area"])
    if "address" in kw:
        form.get_by_label("地址").fill(kw["address"])
    if "price" in kw:
        form.get_by_label("每晚价格").fill(str(kw["price"]))
    for label, key in (("入住", "check_in"), ("退房", "check_out")):
        if key in kw:
            form.get_by_label(label, exact=True).fill(kw[key])


# --------------------------------------------------------------------------- #
# 住宿
# --------------------------------------------------------------------------- #
def test_which_area_to_stay_in_gives_ranked_areas_with_reasons_and_limits(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    expect(page.locator(".stays-card")).to_contain_text("还没填住宿")
    page.get_by_role("button", name="住哪个区？").click()
    rows = page.locator(".rec-row")
    expect(rows.first).to_be_visible()
    assert 2 <= rows.count() <= 3
    expect(rows.first).to_contain_text("平均")
    expect(rows.first).to_contain_text("公里")
    expect(page.locator(".rec-caveat")).to_contain_text("不含酒店价格")
    first_area = rows.first.locator("b").inner_text().split(". ")[1]
    rows.first.get_by_role("button", name="用这个区添加住宿").click()
    expect(page.locator(".stay-form").get_by_label("所在区", exact=True)).to_have_value(first_area)


def test_adding_a_stay_shows_it_on_every_night_and_creates_a_labelled_version(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="＋ 添加住宿").click()
    fill_stay(page, "Hôtel Test", area="9区", price=180)
    page.get_by_role("button", name="保存住宿").click()

    row = page.locator(".stay-row")
    expect(row).to_contain_text("Hôtel Test")
    expect(row).to_contain_text("3 晚")
    expect(row).to_contain_text("€180/晚")
    expect(row).to_contain_text("路线按 9区 的中心估算")                       # 没填地址：说清楚是估算
    expect(page.locator(".stay-total")).to_contain_text("€540")
    notes = page.locator('[data-testid="hotel-note"]')
    expect(notes).to_have_count(4)
    expect(notes.nth(0)).to_contain_text("今晚住：Hôtel Test")
    expect(notes.nth(2)).to_contain_text("今晚住：Hôtel Test")
    expect(notes.nth(3)).to_contain_text("退房：Hôtel Test")                 # 最后一天退房
    expect(page.locator(".history")).to_contain_text("住宿更新")
    expect(page.locator(".history")).to_contain_text("新增「Hôtel Test」")
    page.get_by_role("button", name="v1").click()                            # 旧版本没有住宿
    expect(page.locator('[data-testid="hotel-note"]')).to_have_count(0)


def test_overlapping_stays_are_refused_with_a_readable_message(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="＋ 添加住宿").click()
    fill_stay(page, "A", area="1区", check_in="2026-10-20", check_out="2026-10-22")
    page.get_by_role("button", name="保存住宿").click()
    expect(page.locator(".stay-row")).to_have_count(1)
    page.get_by_role("button", name="＋ 添加住宿").click()
    fill_stay(page, "B", area="9区", check_in="2026-10-21", check_out="2026-10-23")
    page.get_by_role("button", name="保存住宿").click()
    expect(page.get_by_role("alertdialog", name="出错了")).to_contain_text("重叠")      # 错误以弹窗显示
    page.get_by_test_id("notice-ok").click()
    assert httpx.get(f"{API}/plans/{pid}", timeout=10).json()["latest_version"] == 2     # 失败没留版本


def test_an_address_that_cannot_be_located_falls_back_to_the_area_and_says_so(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="＋ 添加住宿").click()
    fill_stay(page, "No Such Hotel", area="16区", address="zzzz qqqq 99999 not a real street")
    page.get_by_role("button", name="保存住宿").click()
    expect(page.locator(".stay-row")).to_contain_text("路线按 16区 的中心估算")
    expect(page.locator(".toast")).to_contain_text("没能定位")


def test_edit_and_delete_a_stay_and_reschedule_around_it(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="＋ 添加住宿").click()
    fill_stay(page, "Far Hotel", area="19区")
    page.locator(".stay-form").get_by_label("保存后按新住宿重新排程").check()
    page.get_by_role("button", name="保存住宿").click()
    expect(page.locator(".history")).to_contain_text("按新住宿重新排程")
    page.locator(".stay-row").get_by_role("button", name="编辑").click()
    fill_stay(page, "Renamed Hotel", area="1区")
    page.get_by_role("button", name="保存住宿").click()
    expect(page.locator(".stay-row")).to_contain_text("Renamed Hotel")
    page.locator(".stay-row").get_by_role("button", name="删除").click()
    expect(page.locator(".stay-row")).to_have_count(0)
    expect(page.locator('[data-testid="hotel-note"]')).to_have_count(0)


def test_shifting_the_trip_moves_the_stay_with_it(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="＋ 添加住宿").click()
    fill_stay(page, "Moves With Trip", area="9区")
    page.get_by_role("button", name="保存住宿").click()
    open_more(page)
    page.get_by_role("button", name="平移日期").click()
    page.get_by_label("新的开始日期").fill("2026-11-02")
    page.get_by_role("button", name="确认平移").click()
    dismiss_conflicts(page)
    page.wait_for_function("document.querySelector('.day .date')?.innerText.startsWith('2026-11-02')")
    expect(page.locator(".stay-row")).to_contain_text("11-02 → 11-05")
    expect(page.locator('[data-testid="hotel-note"]').first).to_contain_text("今晚住：Moves With Trip")


def test_the_public_share_page_never_shows_the_hotel(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="＋ 添加住宿").click()
    fill_stay(page, "Private Hotel", area="9区")
    page.get_by_role("button", name="保存住宿").click()
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=10).json()["token"]
    other = page.context.new_page()
    other.goto(f"{WEB}/s/{token}")
    other.locator(".day").first.wait_for()
    assert "Private Hotel" not in other.content()
    expect(other.locator('[data-testid="hotel-note"]')).to_have_count(0)


# --------------------------------------------------------------------------- #
# 预订
# --------------------------------------------------------------------------- #
def reservations(pid: str) -> dict[str, dict]:
    return {i["place_id"]: i for i in httpx.get(f"{API}/plans/{pid}/reservations", timeout=30).json()["items"]}


def test_checklist_shows_deadlines_only_where_a_lead_time_is_stated(page: Page):
    pid = new_plan()
    items = reservations(pid)
    open_plan(page, pid)
    with_lead = [i for i in items.values() if i["lead_days"]]
    without = [i for i in items.values() if not i["lead_days"] and i["urgency"] != "past"]
    assert with_lead, "艺术主题里应该有卢浮宫这类有明确提前量的地点"
    checklist = page.locator(".check")
    for it in with_lead:
        row = checklist.locator(".rowc", has_text=it["title"]).first
        expect(row).to_contain_text("建议最晚")
        expect(row).to_contain_text("经验值")
        expect(row).to_contain_text("非官方规定")
        tag = {"late": "马上订", "soon": "快到了"}.get(it["urgency"])
        if tag:
            expect(row.locator(".tag", has_text=tag)).to_be_visible()
        else:
            expect(row.locator(".tag.urgent, .tag.soon")).to_have_count(0)
    for it in without:
        row = checklist.locator(".rowc", has_text=it["title"]).first
        expect(row).to_contain_text("建议尽早订")
        expect(row).not_to_contain_text("建议最晚")


def test_mark_booked_with_a_code_survives_reload_and_goes_into_the_exports(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    louvre = page.locator(".rowc", has_text="卢浮宫").first
    louvre.get_by_role("checkbox", name="卢浮宫已订好了").check()
    expect(louvre.locator(".tag.booked-ok")).to_be_visible()
    louvre.get_by_label("卢浮宫的确认码").fill("LV-777888")
    louvre.get_by_label("卢浮宫的确认码").blur()
    page.wait_for_timeout(600)

    page.reload()
    page.locator(".day").first.wait_for()
    louvre = page.locator(".rowc", has_text="卢浮宫").first
    expect(louvre.get_by_label("卢浮宫的确认码")).to_have_value("LV-777888")
    expect(louvre.locator(".tag.booked-ok")).to_be_visible()
    assert page.locator(".history .hrow").count() == 1                      # 预订记录不产生行程版本
    html = httpx.get(f"{API}/plans/{pid}/itinerary.html", timeout=60).text
    assert "LV-777888" in html and "已订" in html

    louvre.get_by_role("checkbox", name="卢浮宫已订好了").uncheck()
    expect(louvre.locator(".tag.booked-ok")).to_have_count(0)


def test_confirmation_codes_never_reach_the_public_share_page(page: Page):
    pid = new_plan()
    httpx.put(f"{API}/plans/{pid}/reservations/louvre",
              json={"status": "booked", "confirmation_code": "TOP-SECRET-1"}, timeout=10)
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=10).json()["token"]
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    assert "TOP-SECRET-1" not in page.content()
    expect(page.get_by_role("checkbox", name=re.compile("已订好了"))).to_have_count(0)


def test_shifting_dates_flags_a_booking_made_for_the_old_date(page: Page):
    pid = new_plan()
    httpx.put(f"{API}/plans/{pid}/reservations/louvre",
              json={"status": "booked", "confirmation_code": "OLD-1"}, timeout=10)
    open_plan(page, pid)
    open_more(page)
    page.get_by_role("button", name="平移日期").click()
    page.get_by_label("新的开始日期").fill("2026-11-10")
    page.get_by_role("button", name="确认平移").click()
    dismiss_conflicts(page)
    row = page.locator(".rowc", has_text="卢浮宫").first
    expect(row.locator(".tag", has_text="行程日期变了")).to_be_visible()


# --------------------------------------------------------------------------- #
# 模型用量重置
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.getenv("E2E_RESET_USAGE") != "1", reason="会清掉真实的模型用量统计；需要时设 E2E_RESET_USAGE=1")
def test_usage_can_be_reset_after_confirming(page: Page):
    new_plan()                                                              # 保证至少有一条调用记录
    page.goto(f"{WEB}/usage")
    page.get_by_text("模型调用", exact=True).wait_for()
    assert int(page.locator(".stat-card b").first.inner_text()) >= 1
    page.get_by_role("button", name="清零统计").click()
    page.get_by_test_id("confirm-cancel").click()                           # 先取消：数字不变
    assert int(page.locator(".stat-card b").first.inner_text()) >= 1
    page.get_by_role("button", name="清零统计").click()
    page.get_by_test_id("confirm-ok").click()
    expect(page.locator(".stat-card b").first).to_have_text("0")
    expect(page.get_by_text("还没有模型调用记录")).to_be_visible()
