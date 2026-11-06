"""v0.3 行程管理的前端验收（specs/modules/M04 S8 / S13 / S14）：平移日期、复制行程、归档与恢复。

默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_manage.py
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta

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
    ctx = browser.new_context(viewport={"width": 1200, "height": 2000}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框（应用内嵌浏览器里它会瞬间返回「取消」，操作会静默失效）：{natives}"


def new_plan() -> str:
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art",
        "arrival_time": "2026-10-03 10:30", "departure_time": "2026-10-06 18:00"}, timeout=120)
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



def first_day_date(page: Page) -> str:
    return page.locator(".day .date").first.inner_text()


def conflicting_start() -> str:
    """找一个平移后会让某天撞上闭馆日的开始日期（用后端试：平移 -> 看冲突 -> 丢掉这个试验用的行程）。"""
    probe = new_plan()
    for k in range(1, 8):
        start = (date(2026, 10, 3) + timedelta(days=k)).isoformat()
        r = httpx.post(f"{API}/plans/{probe}/shift", json={"start_date": start}, timeout=60).json()
        if r["conflicts"]:
            return start
        httpx.post(f"{API}/plans/{probe}/versions/1/restore", timeout=60)
    raise AssertionError("艺术主题里有周二 / 周一闭馆的馆，总有一个平移量会撞上")


# --------------------------------------------------------------------------- #
def test_shift_dates_creates_a_labelled_version_and_keeps_the_old_one(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    assert first_day_date(page).startswith("2026-10-03")
    open_more(page)
    page.get_by_role("button", name="平移日期").click()
    page.get_by_label("新的开始日期").fill("2026-10-08")
    expect(page.get_by_text("整体推迟 5 天")).to_be_visible()
    page.get_by_role("button", name="确认平移").click()
    dismiss_conflicts(page)

    expect(page.get_by_role("button", name="v2")).to_have_class(re.compile("active"))
    page.wait_for_function("document.querySelector('.day .date')?.innerText.startsWith('2026-10-08')")
    history = page.locator(".history")
    expect(history).to_contain_text("平移日期")
    expect(history).to_contain_text("整体平移 +5 天")
    # 旧版本不动，切回去仍是原来的日期，质检也没有因为日期对不上而冒错
    page.get_by_role("button", name="v1").click()
    page.wait_for_function("document.querySelector('.day .date')?.innerText.startsWith('2026-10-03')")
    page.get_by_role("button", name="更多").click()
    page.get_by_test_id("open-gen-details").click()
    expect(page.locator(".qa")).to_contain_text("0 error")
    page.keyboard.press("Escape")


def test_same_date_is_not_allowed(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    open_more(page)
    page.get_by_role("button", name="平移日期").click()
    expect(page.get_by_text("日期没变")).to_be_visible()
    expect(page.get_by_role("button", name="确认平移")).to_be_disabled()


def test_shift_lists_new_closed_day_conflicts_and_reschedule_clears_them(page: Page):
    start = conflicting_start()
    pid = new_plan()
    open_plan(page, pid)
    open_more(page)
    page.get_by_role("button", name="平移日期").click()
    page.get_by_label("新的开始日期").fill(start)
    page.get_by_role("button", name="确认平移").click()

    banner = page.locator(".shift-conflicts")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("新的冲突")
    expect(banner).to_contain_text("闭馆")
    page.get_by_role("button", name="重新排程以避开闭馆日").click()
    expect(page.get_by_role("button", name="v3")).to_have_class(re.compile("active"), timeout=30000)
    expect(page.locator(".shift-conflicts")).to_have_count(0)
    report = httpx.get(f"{API}/plans/{pid}", timeout=30).json()["report"]
    assert not [f for f in report["findings"] if f["code"] == "closed_day"], "重新排程后还有闭馆日冲突"
    expect(page.locator(".history")).to_contain_text("按新日期重新排程")


def test_duplicate_opens_an_independent_copy_marked_as_copy(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    open_more(page)
    page.get_by_role("button", name="复制行程").click()
    page.wait_for_function(f"!location.pathname.endsWith('{pid}')")
    page.locator(".day").first.wait_for()
    copy_id = page.url.rsplit("/", 1)[1]
    assert copy_id != pid
    expect(page.get_by_text("副本 · 复制自")).to_be_visible()
    expect(page.get_by_role("button", name="v1")).to_have_class(re.compile("active"))
    assert httpx.get(f"{API}/plans/{pid}", timeout=30).json()["latest_version"] == 1

    page.goto(f"{WEB}/plans")
    page.locator(".plan-card").first.wait_for()
    card = page.locator(f'a.plan-card[href$="{copy_id}"]')
    expect(card).to_contain_text("副本")


def show_all_cards(page: Page) -> None:
    """已结束的行程超过 6 条时，多的折叠在「更多已结束的行程」里（规格 07 P05）；测试里把它们展开再找卡片。"""
    page.evaluate("document.querySelectorAll('details.past-more').forEach(d => { d.open = true; })")


def test_archive_hides_the_plan_from_the_list_and_restore_brings_it_back(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    open_more(page)
    page.get_by_role("button", name="归档").click()
    expect(page.get_by_text("这份行程已归档")).to_be_visible()
    open_more(page)
    expect(page.get_by_role("button", name="恢复")).to_be_visible()
    page.keyboard.press("Escape")

    page.goto(f"{WEB}/plans")
    page.get_by_role("button", name=re.compile(r"已归档 \(\d+\)")).wait_for()
    assert page.locator(f'a.plan-card[href$="{pid}"]').count() == 0          # 默认列表里没有
    page.get_by_role("button", name=re.compile(r"已归档 \(\d+\)")).click()
    show_all_cards(page)
    card = page.locator(f'a.plan-card[href$="{pid}"]')
    expect(card).to_be_visible()
    card.get_by_role("button", name="恢复").click()
    expect(card).to_have_count(0)                                              # 从已归档里消失
    page.get_by_role("button", name=re.compile(r"进行中 \(\d+\)")).click()
    show_all_cards(page)
    expect(page.locator(f'a.plan-card[href$="{pid}"]')).to_be_visible()


def test_an_archived_plan_still_opens_by_link_and_its_share_link_still_works(page: Page):
    pid = new_plan()
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=10).json()["token"]
    httpx.put(f"{API}/plans/{pid}/archive", json={"archived": True}, timeout=10)
    open_plan(page, pid)
    expect(page.get_by_text("这份行程已归档")).to_be_visible()
    stranger = page.context.new_page()
    stranger.goto(f"{WEB}/s/{token}")
    stranger.locator(".day").first.wait_for()


def test_delete_from_the_plan_page_asks_first_and_removes_everything(page: Page):
    pid = new_plan()
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=10).json()["token"]
    open_plan(page, pid)
    open_more(page)
    page.get_by_role("button", name="删除").click()
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_contain_text("永久删除")
    expect(dialog).to_contain_text("分享链接")
    expect(dialog).to_contain_text("不能恢复")
    page.get_by_test_id("confirm-ok").click()
    page.wait_for_url(re.compile(r"/plans$"))
    assert httpx.get(f"{API}/plans/{pid}", timeout=10).status_code == 404
    assert httpx.get(f"{API}/share/{token}", timeout=10).status_code == 404
    page.locator(".card, .plan-card").first.wait_for()
    assert page.locator(f'a.plan-card[href$="{pid}"]').count() == 0


def test_declining_the_confirmation_deletes_nothing(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    open_more(page)
    page.get_by_role("button", name="删除").click()
    page.get_by_test_id("confirm-cancel").click()
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    assert httpx.get(f"{API}/plans/{pid}", timeout=10).status_code == 200
    assert page.url.endswith(pid)
    open_more(page)
    page.get_by_role("button", name="删除").click()                          # Esc 也算取消
    page.keyboard.press("Escape")
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    assert httpx.get(f"{API}/plans/{pid}", timeout=10).status_code == 200


def test_delete_from_a_card_in_the_list_including_archived_ones(page: Page):
    keep, drop_active, drop_archived = new_plan(), new_plan(), new_plan()
    httpx.put(f"{API}/plans/{drop_archived}/archive", json={"archived": True}, timeout=10)
    page.goto(f"{WEB}/plans")
    page.locator(".plan-card").first.wait_for()
    show_all_cards(page)
    card = page.locator(f'a.plan-card[href$="{drop_active}"]')
    card.get_by_role("button", name="删除").click()
    page.get_by_test_id("confirm-ok").click()
    expect(card).to_have_count(0)
    page.get_by_role("button", name=re.compile(r"已归档 \(\d+\)")).click()
    show_all_cards(page)
    arch = page.locator(f'a.plan-card[href$="{drop_archived}"]')
    arch.get_by_role("button", name="删除").click()
    page.get_by_test_id("confirm-ok").click()
    expect(arch).to_have_count(0)
    assert httpx.get(f"{API}/plans/{drop_active}", timeout=10).status_code == 404
    assert httpx.get(f"{API}/plans/{drop_archived}", timeout=10).status_code == 404
    assert httpx.get(f"{API}/plans/{keep}", timeout=10).status_code == 200
