"""v0.2 编辑的前端验收（specs/modules/M04-editing-and-versions.md 的 S3–S7）。

像真人一样在真实浏览器里改行程：进入编辑 → 移到另一天 / 拖拽 → 锁定 → 换一个 → 撤销 / 重做 → 保存成命名版本 → 回滚。
默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v02_editing.py

每个测试自己新生成一份行程（模板文案，不依赖模型），互不影响。
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
    # 窗口开得很高：拖拽的起点和终点都要在可视范围内，否则松手时不在任何元素上、浏览器不会发 drop
    ctx = browser.new_context(viewport={"width": 1200, "height": 3200}, locale="zh-CN")
    pg = ctx.new_page()
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))   # 确认框是页面内自带的，不应该再弹原生的
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框（应用内嵌浏览器里它会瞬间返回「取消」）：{natives}"


def new_plan(theme="art", days=5) -> str:
    """直接走接口造一份主题行程（同步接口、不调模型文案），返回行程号。"""
    end = f"2026-10-{2 + days:02d}"
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": "2026-10-03", "end_date": end, "budget_level": "medium",
        "travel_style": "balanced", "interests": [theme], "theme": theme,
    }, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()


def start_editing(page: Page, pid: str) -> None:
    open_plan(page, pid)
    page.get_by_role("button", name="编辑行程", exact=True).click()
    expect(page.locator(".edit-bar")).to_be_visible()


def day_places(page: Page, n: int) -> list[str]:
    """第 n 天（从 0 起）里景点的名字，按显示顺序。"""
    items = page.locator(".day").nth(n).locator(".item[data-pid]")
    return [items.nth(i).locator("h4").inner_text().replace("🔒", "").strip() for i in range(items.count())]


def drag(page: Page, source, target, *, lower_half: bool = False) -> None:
    """用鼠标一步步拖：HTML5 拖拽要求在落点上停一下，浏览器才会确认「这里可以放」并发出 drop。"""
    sb, tb = source.bounding_box(), target.bounding_box()
    page.mouse.move(sb["x"] + 10, sb["y"] + 5)
    page.mouse.down()
    page.mouse.move(sb["x"] + 20, sb["y"] + 20, steps=5)
    y = tb["y"] + (tb["height"] - 8 if lower_half else tb["height"] / 2)
    page.mouse.move(tb["x"] + 40, y, steps=15)
    page.wait_for_timeout(300)
    page.mouse.up()


def wait_idle(page: Page) -> None:
    page.wait_for_function("!document.querySelector('.edit-tools button:disabled, .edit-tools select:disabled') "
                           "|| document.querySelectorAll('.item.locked').length > 0", timeout=15000)
    page.wait_for_timeout(300)


# --------------------------------------------------------------------------- #
def test_edit_button_only_appears_on_the_latest_version_of_a_themed_plan(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_be_visible()
    # 旧流程（没有主题）生成的行程不能编辑：用种子样例
    legacy = next((c["plan_id"] for c in httpx.get(f"{API}/plan-cards").json() if not c["theme_id"]), None)
    if legacy:
        open_plan(page, legacy)
        expect(page.get_by_role("button", name="编辑行程", exact=True)).to_have_count(0)


def test_move_to_another_day_with_the_menu_updates_both_days_and_can_be_undone(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    before0, before3 = day_places(page, 0), day_places(page, 3)
    victim = before0[0]
    page.get_by_label(f"把{victim}移到另一天").first.select_option(index=3)       # 选项 0 是占位，1.. 是别的天
    expect(page.locator(".edit-row")).to_contain_text("1 处未保存的修改")
    wait_idle(page)
    after0, after_days = day_places(page, 0), [day_places(page, i) for i in range(5)]
    assert victim not in after0 and any(victim in d for d in after_days[1:])
    assert len(after0) == len(before0) - 1
    page.get_by_role("button", name="更多").click()
    page.get_by_test_id("open-gen-details").click()
    # 质检结果随编辑刷新（在「生成详情」里）
    assert "0 error" in page.inner_text(".qa") or "error" in page.inner_text(".qa")
    page.keyboard.press("Escape")

    page.get_by_role("button", name=re.compile("撤销")).click()
    page.wait_for_timeout(300)
    assert day_places(page, 0) == before0 and day_places(page, 3) == before3
    page.get_by_role("button", name=re.compile("重做")).click()
    page.wait_for_timeout(300)
    assert victim not in day_places(page, 0)


def test_dragging_a_place_inside_a_day_reorders_it(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    d = next(i for i in range(5) if len(day_places(page, i)) >= 2)
    before = day_places(page, d)
    items = page.locator(".day").nth(d).locator(".item[data-pid]")
    drag(page, items.nth(0).locator("h4"), items.nth(len(before) - 1), lower_half=True)      # 拖到最后一个的下半部分
    page.wait_for_timeout(800)
    after = day_places(page, d)
    assert sorted(after) == sorted(before) and after != before and after[-1] == before[0]


def test_dragging_a_place_onto_another_day_moves_it(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    src = next(i for i in range(5) if len(day_places(page, i)) >= 2)
    dst = (src + 1) % 5
    victim = day_places(page, src)[0]
    drag(page, page.locator(".day").nth(src).locator(".item[data-pid]").first.locator("h4"),
         page.locator(".day").nth(dst).locator(".day-head"))             # 落在这一天的标题行上
    page.wait_for_timeout(800)
    assert victim in day_places(page, dst) and victim not in day_places(page, src)


def test_locked_place_cannot_be_moved_and_keeps_its_time_after_reschedule(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    d = next(i for i in range(5) if len(day_places(page, i)) >= 2)
    item = page.locator(".day").nth(d).locator(".item[data-pid]").last
    name = item.locator("h4").inner_text().strip()
    time_before = item.locator(".t").inner_text()
    item.get_by_role("button", name=re.compile("锁定")).click()
    expect(page.locator(".item.locked")).to_have_count(1)
    locked = page.locator(".item.locked")
    expect(locked.get_by_role("button", name="换一个")).to_be_disabled()
    expect(locked.get_by_role("button", name="删除")).to_be_disabled()
    page.get_by_role("button", name=re.compile("重新排程")).click()
    page.get_by_test_id("confirm-ok").click()
    page.wait_for_timeout(1500)
    again = page.locator(".item.locked")
    expect(again).to_have_count(1)
    assert name in again.locator("h4").inner_text() and again.locator(".t").inner_text() == time_before


def test_swap_offers_alternatives_and_replaces_in_place(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    d = next(i for i in range(5) if len(day_places(page, i)) >= 2)
    before = day_places(page, d)
    page.locator(".day").nth(d).locator(".item[data-pid]").first.get_by_role("button", name="换一个").click()
    panel = page.locator(".swap-panel")
    expect(panel.locator(".alt").first).to_be_visible()
    assert 1 <= panel.locator(".alt").count() <= 3
    new_name = panel.locator(".alt b").first.inner_text()
    panel.get_by_role("button", name="换成它").first.click()
    page.wait_for_timeout(800)
    after = day_places(page, d)
    assert after[0] == new_name and after[1:] == before[1:]


def test_adding_a_place_from_the_library(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    before = day_places(page, 0)
    select = page.get_by_label("给第 1 天加入地点")
    names = select.locator("option").all_inner_texts()
    newcomer = next(n for n in names if n not in before and not n.startswith("＋"))
    select.select_option(label=newcomer)
    page.wait_for_timeout(800)
    assert newcomer in day_places(page, 0)


def test_saving_makes_a_named_version_and_rollback_makes_another(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    d = next(i for i in range(5) if len(day_places(page, i)) >= 2)
    original = day_places(page, d)
    page.locator(".day").nth(d).locator(".item[data-pid]").first.get_by_role("button", name="删除").click()
    page.wait_for_timeout(800)
    page.get_by_role("button", name=re.compile("保存为新版本")).click()
    page.get_by_label("版本名字").fill("朋友确认版")
    page.get_by_role("button", name="保存", exact=True).click()
    expect(page.locator(".edit-bar")).to_have_count(0)                      # 保存后退出编辑
    page.locator(".history .hrow").nth(1).wait_for()
    history = page.locator(".history .hrow").all_inner_texts()
    assert any("朋友确认版" in h and "手动保存" in h and "改动：" in h for h in history)
    assert len(day_places(page, d)) == len(original) - 1

    page.locator(".history .hrow").first.get_by_role("button", name="回滚到这版").click()
    page.get_by_test_id("confirm-ok").click()
    page.locator(".history .hrow").nth(2).wait_for()
    assert any("回滚" in h for h in page.locator(".history .hrow").all_inner_texts())
    assert day_places(page, d) == original                                  # 内容回到 v1，v2 还在


def test_leaving_with_unsaved_changes_asks_first(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    page.locator(".day").first.locator(".item[data-pid]").first.get_by_role("button", name="删除").click()
    page.wait_for_timeout(800)
    page.get_by_role("button", name="退出编辑").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("未保存")
    page.get_by_test_id("confirm-cancel").click()                          # 取消：还在编辑，修改还在
    expect(page.locator(".edit-bar")).to_be_visible()
    page.get_by_role("button", name="退出编辑").click()
    page.get_by_test_id("confirm-ok").click()                              # 确认：退出，修改丢掉
    expect(page.locator(".edit-bar")).to_have_count(0)


def test_phone_width_editing_controls_stay_inside_the_screen(browser):
    pid = new_plan()
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    open_plan(page, pid)
    page.get_by_role("button", name="编辑行程", exact=True).click()
    page.wait_for_timeout(1200)
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
    ctx.close()


# --------------------------------------------------------------------------- #
# 拖拽到很远的地方（真人反馈：每天的内容有一千多像素高，从第 2 天拖到第 1 天拖不上去）
# --------------------------------------------------------------------------- #
@pytest.fixture
def small_page(browser) -> Page:
    """真实笔记本屏幕大小的窗口：一屏装不下一整天，必须滚动才能到另一天。"""
    ctx = browser.new_context(viewport={"width": 1280, "height": 720}, locale="zh-CN")
    ctx.on("dialog", lambda d: d.accept())
    pg = ctx.new_page()
    yield pg
    ctx.close()


def test_dragging_near_the_top_edge_scrolls_the_page_up(small_page: Page):
    page = small_page
    pid = new_plan()
    start_editing(page, pid)
    day2 = page.locator(".day").nth(1)
    item = day2.locator(".item[data-pid]").first
    item.scroll_into_view_if_needed()
    page.evaluate("window.scrollBy(0, 1400)")                # 先往下滚一大截，要回到页面上面得滚很远
    before = page.evaluate("window.scrollY")
    box = item.locator("h4").bounding_box()
    page.mouse.move(box["x"] + 10, box["y"] + 5)
    page.mouse.down()
    page.mouse.move(box["x"] + 20, box["y"] - 40, steps=5)
    page.mouse.move(600, 95, steps=10)                      # 离窗口顶部约 95 像素：浏览器自带的自动滚动只认贴边的几像素
    page.wait_for_timeout(1500)                              # 停在这里：页面应该自己往上滚
    scrolled = page.evaluate("window.scrollY")
    page.mouse.up()
    assert scrolled < before - 700, f"自动滚动太慢或没有滚：{before} -> {scrolled}（1.5 秒内要上滚 700 像素以上）"


def test_the_day_dock_lets_you_drop_on_any_day_without_scrolling(small_page: Page):
    page = small_page
    pid = new_plan()
    start_editing(page, pid)
    dock = page.locator(".drag-dock")
    expect(dock).to_be_hidden()                              # 平时不显示，拖拽时才出现
    src = next(i for i in range(5) if len(day_places(page, i)) >= 2)
    dst = (src + 3) % 5                                       # 隔得很远的一天
    victim = day_places(page, src)[0]
    item = page.locator(".day").nth(src).locator(".item[data-pid]").first
    item.scroll_into_view_if_needed()
    box = item.locator("h4").bounding_box()
    page.mouse.move(box["x"] + 10, box["y"] + 5)
    page.mouse.down()
    page.mouse.move(box["x"] + 30, box["y"] + 30, steps=5)
    expect(dock).to_be_visible()
    chip = dock.locator(".dock-day").nth(dst).bounding_box()
    page.mouse.move(chip["x"] + chip["width"] / 2, chip["y"] + chip["height"] / 2, steps=15)
    page.wait_for_timeout(400)
    page.mouse.up()
    page.wait_for_timeout(1000)
    assert victim in day_places(page, dst) and victim not in day_places(page, src)
    expect(dock).to_be_hidden()
    assert page.evaluate("document.body.classList.contains('is-dragging')") is False   # 拖完后控件不会一直被禁用
    expect(page.locator(".item[data-pid] .edit-tools button").first).to_be_enabled()


def test_the_edit_bar_sits_below_the_top_navigation_and_jumps_leave_room_for_it(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    page.evaluate("window.scrollTo(0, 1500)")
    page.wait_for_timeout(400)
    nav_bottom = page.locator("nav, header").first.bounding_box()
    bar = page.locator(".edit-bar").bounding_box()
    assert bar["y"] >= (nav_bottom["y"] + nav_bottom["height"]) - 1, (nav_bottom, bar)   # 没被顶部导航条盖住
    # 点目录里的某一天：那一天的标题落在编辑条下面，不被它盖住
    page.get_by_test_id("section-nav").get_by_role("button", name="第 3 天").click()
    page.wait_for_timeout(1500)
    head = page.locator(".day").nth(2).bounding_box()
    assert head["y"] >= bar["y"] + bar["height"] - 4, (head, bar)


def test_the_edit_bar_stays_above_the_map_when_scrolling(page: Page):
    pid = new_plan()
    start_editing(page, pid)
    page.locator(".leaflet-container").first.scroll_into_view_if_needed()
    page.evaluate("""() => {
        const m = document.querySelector('.leaflet-container').getBoundingClientRect();
        window.scrollBy(0, m.top - 120);   // 让地图正好滚到编辑条下面
    }""")
    page.wait_for_timeout(400)
    bar = page.locator(".edit-bar").bounding_box()
    top = page.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); return !!e && !!e.closest('.edit-bar'); }",
        [bar["x"] + bar["width"] / 2, bar["y"] + bar["height"] - 6],
    )
    assert top, "编辑条被地图盖住了"
