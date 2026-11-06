"""界面设计的前端验收（specs/06-ui-design.md S1–S7）：导航、封面 + 统计、更多菜单、并排地图、窄屏、首页、列表、深色。

默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_ui.py
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


def make_page(browser, *, width=1440, height=900, scheme="light", mobile=False):
    ctx = browser.new_context(viewport={"width": width, "height": height}, locale="zh-CN", color_scheme=scheme,
                              is_mobile=mobile, has_touch=mobile)
    return ctx, ctx.new_page()


def new_plan(theme="art", start="2026-10-20", end="2026-10-23") -> str:
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": start, "end_date": end, "budget_level": "medium",
        "travel_style": "balanced", "interests": [theme], "theme": theme,
        "arrival_time": f"{start} 10:30", "departure_time": f"{end} 18:00"}, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()


# --------------------------------------------------------------------------- #
# S1 导航
# --------------------------------------------------------------------------- #
def test_the_top_navigation_is_on_every_internal_page_and_highlights_the_current_one(browser):
    ctx, page = make_page(browser)
    nav = page.get_by_role("navigation", name="主导航")
    for path, current in (("/new", "新建行程"), ("/plans", "我的行程"), ("/usage", "模型用量")):
        page.goto(WEB + path)
        expect(nav).to_be_visible()
        expect(nav.locator("a.active")).to_have_text(re.compile(current))
    page.get_by_role("link", name="我的行程").first.click()
    expect(page).to_have_url(f"{WEB}/plans")
    page.get_by_role("link", name="新建行程").first.click()
    expect(page).to_have_url(f"{WEB}/new")
    page.get_by_role("link", name="AI 旅行规划 首页").click()                       # 品牌图标回首页（Q2）
    expect(page).to_have_url(f"{WEB}/")
    ctx.close()


def test_on_a_phone_the_top_links_are_replaced_by_a_bottom_tab_bar(browser):
    ctx, page = make_page(browser, width=390, height=844, mobile=True)
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    expect(page.get_by_role("navigation", name="主导航")).to_be_hidden()           # 顶部链接收起
    bar = page.get_by_role("navigation", name="底部导航")
    expect(bar).to_be_visible()
    box = bar.bounding_box()
    assert box["y"] + box["height"] <= 844 and box["y"] > 844 - 120, "标签栏应该悬在屏幕底部"
    expect(bar.locator("a.active")).to_have_text(re.compile("新建"))
    bar.get_by_role("link", name="我的行程").click()
    expect(page).to_have_url(f"{WEB}/plans")
    expect(bar.locator("a.active")).to_have_text(re.compile("我的行程"))
    bar.get_by_role("link", name="用量").click()
    expect(page).to_have_url(f"{WEB}/usage")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    page.goto(WEB + "/plans")                                                      # 内容底部不被标签栏遮住
    pad = page.evaluate("parseFloat(getComputedStyle(document.body).paddingBottom)")
    assert pad >= box["height"], f"页面底部留白 {pad}px 小于标签栏高度 {box['height']}px，内容会被遮住"
    ctx.close()


def test_on_desktop_there_is_no_bottom_tab_bar(browser):
    ctx, page = make_page(browser)
    page.goto(WEB)
    expect(page.get_by_role("navigation", name="底部导航")).to_be_hidden()
    ctx.close()


def test_the_public_share_page_has_no_internal_navigation(browser):
    pid = new_plan()
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=10).json()["token"]
    ctx, page = make_page(browser)
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    expect(page.get_by_role("navigation", name="主导航")).to_have_count(0)
    ctx.close()


# --------------------------------------------------------------------------- #
# S2 封面 + 统计 + 更多菜单
# --------------------------------------------------------------------------- #
def test_plan_page_has_a_cover_four_stat_tiles_and_a_more_menu(browser):
    pid = new_plan()
    route = httpx.get(f"{API}/plans/{pid}/route", timeout=30).json()
    ctx, page = make_page(browser)
    open_plan(page, pid)
    hero = page.locator(".plan-hero")
    expect(hero).to_be_visible()
    expect(page.get_by_test_id("trip-cover")).to_be_visible()                # UI2：全屏封面舞台（见 test_ui2_cover.py）
    expect(page.get_by_test_id("plan-title")).to_be_visible()                      # 原来的封面卡变成一行紧凑标题
    tiles = page.locator(".stat-tile")
    expect(tiles).to_have_count(4)
    expect(tiles.nth(0)).to_contain_text("4")
    expect(tiles.nth(1)).to_contain_text(str(route["place_count"]))
    expect(tiles.nth(2)).to_contain_text(f"{route['total_km']:g}")
    expect(tiles.nth(3)).to_contain_text("€")
    for primary in ("编辑行程", "分享", "下载 PDF"):
        found = hero.get_by_role("button", name=primary).or_(hero.get_by_role("link", name=primary))
        expect(found.first).to_be_visible()

    page.get_by_role("button", name="更多").click()
    menu = page.locator(".more-menu")
    for item in ("复制链接", "导出日历", "导出 KML", "每天一页", "平移日期", "复制行程", "归档", "删除"):
        expect(menu).to_contain_text(item)
    assert menu.locator(".menu-item.danger").inner_text().strip() == "删除"          # 删除在最下面、红色
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    ctx.close()


def test_the_more_menu_closes_when_clicking_elsewhere(browser):
    pid = new_plan()
    ctx, page = make_page(browser)
    open_plan(page, pid)
    page.get_by_role("button", name="更多").click()
    expect(page.locator(".more-menu")).to_be_visible()
    page.get_by_test_id("plan-title").locator("h2").click()
    expect(page.locator(".more-menu")).to_have_count(0)
    ctx.close()


# --------------------------------------------------------------------------- #
# S3 桌面端并排地图
# --------------------------------------------------------------------------- #
def test_on_desktop_the_map_sits_beside_the_itinerary_and_stays_while_scrolling(browser):
    pid = new_plan()
    ctx, page = make_page(browser, width=1440, height=900)
    open_plan(page, pid)
    aside = page.locator(".plan-aside")
    expect(aside).to_be_visible()
    expect(page.get_by_test_id("full-map")).to_be_visible()
    expect(page.locator("details.routecard")).to_have_count(0)                # 宽屏没有折叠卡片
    main_box, side_box = page.locator(".plan-main").bounding_box(), aside.bounding_box()
    assert side_box["x"] > main_box["x"] + main_box["width"] - 1                # 地图在日程右边
    top_before = aside.bounding_box()["y"]
    page.evaluate(
        "window.scrollTo(0, document.getElementById('cover-end').getBoundingClientRect().top + scrollY + 900)")
    page.wait_for_timeout(300)
    box = aside.bounding_box()
    assert 0 <= box["y"] <= 120, f"滚动后地图跑出画面了（y={box['y']}，滚动前 {top_before}）"   # 固定在上方
    ctx.close()


def test_each_day_badge_has_the_same_color_as_its_line_on_the_map(browser):
    pid = new_plan()
    route = httpx.get(f"{API}/plans/{pid}/route", timeout=30).json()
    ctx, page = make_page(browser)
    open_plan(page, pid)
    page.get_by_test_id("full-map").wait_for()

    def rgb(hex_color: str) -> str:
        h = hex_color.lstrip("#")
        return f"rgb({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)})"

    for i, d in enumerate(route["days"]):
        bg = page.locator(".day").nth(i).locator(".dnum").evaluate("e => getComputedStyle(e).backgroundColor")
        assert bg == rgb(d["color"]), f"第 {i + 1} 天徽标颜色和地图上的线不同"
    ctx.close()


# --------------------------------------------------------------------------- #
# S4 窄屏
# --------------------------------------------------------------------------- #
def test_on_a_narrow_screen_the_map_folds_and_nothing_scrolls_sideways(browser):
    pid = new_plan()
    ctx, page = make_page(browser, width=390, height=844, mobile=True)
    open_plan(page, pid)
    expect(page.locator(".plan-aside")).to_have_count(0)
    expect(page.locator("details.routecard")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    expect(page.locator(".stat-tile")).to_have_count(4)
    ctx.close()


# --------------------------------------------------------------------------- #
# S5 首页
# --------------------------------------------------------------------------- #
def test_home_has_theme_cards_with_icons_three_numbered_steps_and_one_generate_button(browser):
    ctx, page = make_page(browser)
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    assert page.locator(".theme-btn").count() >= 8
    expect(page.locator(".theme-btn .theme-icon").first).to_be_visible()
    steps = page.locator(".step-no")
    expect(steps).to_have_count(5)
    assert [s.strip() for s in steps.all_inner_texts()] == ["1", "2", "3", "4", "5"]
    heading_font = page.locator(".new-head h1").evaluate("e => getComputedStyle(e).fontFamily")
    assert heading_font.startswith("Georgia") and "serif" not in heading_font.split(",")[0], heading_font
    expect(page.get_by_role("button", name="生成行程")).to_have_count(1)

    active = page.locator(".theme-btn.active")
    expect(active).to_have_count(1)
    expect(active.locator(".theme-check")).to_be_visible()                      # 选中有对勾和描边
    page.locator(".theme-btn:not(.balanced)", has_text="艺术").click()          # 从「均衡」切到具体主题
    page.locator(".theme-btn", has_text="美食").click()
    page.locator(".theme-btn", has_text="购物").click()
    expect(page.locator(".theme-btn.active")).to_have_count(3)
    page.locator(".theme-btn", has_text="摄影").click()                           # 不设上限：第四个也能选
    expect(page.locator(".theme-btn.active")).to_have_count(4)
    expect(page.locator(".submit-summary")).to_contain_text("艺术 + 美食 + 购物 + 摄影")
    ctx.close()


def fake_card(pid: str, start: date, days: int, title: str = "巴黎 · 艺术") -> dict:
    return {"plan_id": pid, "title": title, "theme_id": "art", "theme_name": "艺术", "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=days - 1)).isoformat(), "days": days, "latest_version": 1,
            "created_at": "2026-10-01T00:00:00+00:00", "errors": 0, "warnings": 0, "narration": "llm",
            "archived": False, "copied_from": None, "city_id": "paris", "hero": None}


def test_the_next_trip_countdown_ticks_every_second_and_names_the_nearest_upcoming_plan(browser):
    # 行程列表用固定数据（不依赖库里已有的行程）：一份已经出发、一份 10 天后、一份 3 天后、一份 30 天后，故意乱序
    today = date.today()
    cards = [fake_card("p-later", today + timedelta(days=30), 4), fake_card("p-past", today - timedelta(days=2), 4),
             fake_card("p-soon", today + timedelta(days=3), 4), fake_card("p-mid", today + timedelta(days=10), 4)]
    ctx, page = make_page(browser)
    page.route(re.compile(r".*/plan-cards.*"), lambda r: r.fulfill(json=cards))
    page.goto(WEB)                                                                   # 首页 /（Q2 起倒计时在首页）
    card = page.get_by_test_id("next-trip")
    expect(card).to_be_visible()
    expect(card).to_contain_text("下一站，巴黎")
    expect(card.get_by_role("link", name="查看行程")).to_have_attribute("href", "/plans/p-soon")   # 最近的、还没出发的
    days = int(card.locator(".cd-cell b").first.inner_text())
    assert days in (2, 3), f"距离出发应该是 2–3 天，实际 {days}"
    seconds = card.get_by_test_id("cd-seconds")
    first = seconds.inner_text()
    page.wait_for_function(
        "t => document.querySelector('[data-testid=cd-seconds]').innerText !== t", arg=first, timeout=4000)
    ctx.close()


def test_the_next_trip_card_ignores_plans_that_already_started(browser):
    today = date.today()
    ctx, page = make_page(browser)
    started = [fake_card("p-past", today - timedelta(days=1), 4)]
    page.route(re.compile(r".*/plan-cards.*"), lambda r: r.fulfill(json=started))
    page.goto(WEB)
    page.get_by_test_id("home-hero").wait_for()
    page.wait_for_timeout(500)
    expect(page.get_by_test_id("next-trip")).to_have_count(0)
    ctx.close()


def test_the_next_trip_card_is_absent_when_no_plan_is_upcoming(browser):
    ctx, page = make_page(browser)
    page.route(re.compile(r".*/plan-cards.*"), lambda r: r.fulfill(json=[]))
    page.goto(WEB)
    page.get_by_test_id("home-hero").wait_for()
    page.wait_for_timeout(500)
    expect(page.get_by_test_id("next-trip")).to_have_count(0)
    ctx.close()


# --------------------------------------------------------------------------- #
# S6 我的行程
# --------------------------------------------------------------------------- #
def test_plan_cards_have_covers_whose_gradient_follows_the_theme(browser):
    # 用还没出发的行程：已结束的默认只展开最近 6 条，库里行程多了（整套一起跑）这张卡会被收进「更多」里看不见
    soon = date.today() + timedelta(days=30)
    span = dict(start=soon.isoformat(), end=(soon + timedelta(days=3)).isoformat())
    art, food = new_plan("art", **span), new_plan("food", **span)
    ctx, page = make_page(browser)
    page.goto(f"{WEB}/plans")
    page.locator(".plan-card").first.wait_for()
    cover = lambda pid: page.locator(f'a.plan-card[href$="{pid}"] .plan-cover')  # noqa: E731
    expect(cover(art)).to_be_visible()
    # 封面是这趟行程里标志性地点的大图 + 倒计时（Q2）；没有大图的行程才是主题渐变 + 主题图标
    for pid in (art, food):
        expect(page.locator(f'a.plan-card[href$="{pid}"] [data-testid=plan-countdown]')).to_be_visible()
        if cover(pid).evaluate("e => e.classList.contains('has-photo')"):
            assert "/media/hero/" in cover(pid).evaluate("e => getComputedStyle(e).backgroundImage")
        else:
            assert cover(pid).locator(".cover-icon").count() == 1
    bg_art = cover(art).evaluate("e => getComputedStyle(e).backgroundImage")
    bg_food = cover(food).evaluate("e => getComputedStyle(e).backgroundImage")
    assert bg_art != bg_food, "不同主题（不同行程）的封面应该不一样"
    expect(page.locator(f'a.plan-card[href$="{art}"]')).to_contain_text("4 天")
    ctx.close()


# --------------------------------------------------------------------------- #
# S7 深色
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/", "/plans", "/usage"])
def test_dark_mode_has_dark_backgrounds_and_readable_text_on_every_page(browser, path):
    ctx, page = make_page(browser, scheme="dark")
    page.goto(WEB + path)
    page.wait_for_timeout(800)

    def lum(css: str) -> float:
        nums = [float(x) for x in re.findall(r"\d*\.?\d+", css)[:3]]
        r, g, b = nums if css.startswith("color(") else [n / 255 for n in nums]      # color-mix 给的是 0–1 的 srgb
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    assert lum(page.evaluate("getComputedStyle(document.body).backgroundColor")) < 0.15
    assert lum(page.evaluate("getComputedStyle(document.body).color")) > 0.7
    nav_bg = page.locator(".appnav").evaluate("e => getComputedStyle(e).backgroundColor")
    assert lum(nav_bg) < 0.25
    ctx.close()


def test_dark_mode_plan_page_cards_are_not_white(browser):
    pid = new_plan()
    ctx, page = make_page(browser, scheme="dark")
    open_plan(page, pid)
    for sel in (".stat-tile", ".plan-aside .card", ".day .timeline"):
        bg = page.locator(sel).first.evaluate("e => getComputedStyle(e).backgroundColor")
        r, g, b = (int(x) for x in re.findall(r"\d+", bg)[:3])
        assert (r + g + b) / 3 < 80, f"{sel} 在深色下是亮色背景：{bg}"
    ctx.close()


# --------------------------------------------------------------------------- #
# S8 城市配色
# --------------------------------------------------------------------------- #
def _rgb(css: str) -> tuple[int, int, int]:
    return tuple(int(x) for x in re.findall(r"\d+", css)[:3])  # type: ignore[return-value]


def test_paris_pages_carry_the_paris_palette_with_no_green_and_unknown_cities_fall_back(browser):
    pid = new_plan()
    ctx, page = make_page(browser)
    for url in (f"{WEB}/new", f"{WEB}/plans/{pid}"):
        page.goto(url)
        page.locator("#destination, .day").first.wait_for()
        assert page.evaluate("document.documentElement.dataset.city") == "paris"
        hexval = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent')").strip()
        accent = tuple(int(hexval[i:i + 2], 16) for i in (1, 3, 5))
        r, g, b = accent
        assert b > r and b >= g, f"巴黎主色应该是蓝灰，实际 {accent}"          # 不是绿色
    # 首页英雄：没有大图时是带屋顶接缝纹理的渐变
    page.goto(WEB)
    page.route(re.compile(r".*/media/hero/.*"), lambda r: r.abort())
    page.reload()
    page.get_by_test_id("home-hero").wait_for()
    page.wait_for_timeout(800)
    cover = page.locator(".home-hero").first.evaluate("e => getComputedStyle(e).backgroundImage")
    assert "repeating-linear-gradient" in cover, "封面应带屋顶接缝纹理"
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for(state="attached")
    page.evaluate("delete document.documentElement.dataset.city")               # 没有专属配色：中性色仍可读
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert _rgb(bg) != (0, 0, 0) and page.locator(".day").first.is_visible()
    ctx.close()
