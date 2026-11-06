"""界面优化的前端验收（specs/06-ui-design.md S24–S27）：首页输入卡 + 登机牌、我的行程封面卡、手机行程页与标签栏。

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_ui3_redesign.py

行程列表用固定数据（拦截 /plan-cards），日期相对「今天」；手机行程页用真生成的一份行程。
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
TODAY = date.today()


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


def _new_page(browser, width: int, height: int, mobile: bool = False):
    ctx = browser.new_context(
        viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    return ctx, ctx.new_page(), natives


@pytest.fixture
def page(browser) -> Page:
    ctx, pg, natives = _new_page(browser, 1280, 800)
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


@pytest.fixture
def phone(browser) -> Page:
    ctx, pg, natives = _new_page(browser, 375, 812, mobile=True)
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def card(pid: str, start: date, days: int = 4, *, title: str = "巴黎 · 艺术") -> dict:
    return {"plan_id": pid, "title": title, "theme_id": "art", "theme_name": "艺术",
            "start_date": start.isoformat(), "end_date": (start + timedelta(days=days - 1)).isoformat(), "days": days,
            "latest_version": 1, "created_at": "2026-10-01T00:00:00+00:00", "errors": 0, "warnings": 0,
            "narration": "template", "archived": False, "copied_from": None, "city_id": "paris", "hero": None}


def serve_cards(page: Page, cards: list[dict], archived: list[dict] | None = None) -> None:
    def handler(route):
        route.fulfill(json=(archived or []) if "archived=true" in route.request.url else cards)

    page.route(re.compile(r".*/plan-cards.*"), handler)


# ---- S24 首页 ----
def test_home_card_is_a_cream_composer_with_four_suggestions_that_only_fill_the_text(page: Page):
    serve_cards(page, [])
    page.goto(WEB)
    page.get_by_test_id("home-hero").wait_for()
    style = page.get_by_test_id("nl-card").evaluate(
        "e => { const s = getComputedStyle(e); return [s.backdropFilter, s.borderTopWidth, s.borderTopLeftRadius] }")
    assert "blur(20px)" in style[0] and style[1] == "0px" and style[2] == "28px", style
    chips = page.get_by_test_id("nl-suggest")
    expect(chips).to_have_count(4)
    first = chips.first.inner_text()
    chips.first.click()
    expect(page.get_by_test_id("nl-text")).to_have_value(first)
    expect(page.get_by_test_id("nl-go")).to_be_enabled()
    assert page.url.rstrip("/") == WEB                                       # 只是填进去，没有提交、没有跳走
    expect(page.get_by_test_id("home-hero").locator("p:not(.hero-credit)")).to_have_count(0)  # 不再有一句话说明


def test_the_next_trip_is_a_boarding_pass_inside_the_hero(page: Page):
    serve_cards(page, [card("p-soon", TODAY + timedelta(days=3))])
    page.goto(WEB)
    hero = page.get_by_test_id("home-hero")
    pas = hero.get_by_test_id("next-trip")
    expect(pas).to_be_visible()
    expect(pas).to_contain_text("下一站，巴黎")
    expect(pas.get_by_role("link", name="查看行程")).to_have_attribute("href", "/plans/p-soon")
    order = [pas.locator(sel).bounding_box()["x"] for sel in ("h2", ".countdown", "a.next-link")]
    assert order == sorted(order), order                                     # 城市 → 数字 → 箭头，从左到右
    box, hb = pas.bounding_box(), hero.bounding_box()
    assert box and hb and box["x"] + box["width"] > hb["x"] + hb["width"] - 60, (box, hb)   # 桌面在右下


def test_the_boarding_pass_sits_under_the_suggestions_on_a_phone(phone: Page):
    serve_cards(phone, [card("p-soon", TODAY + timedelta(days=3))])
    phone.goto(WEB)
    pas = phone.get_by_test_id("home-hero").get_by_test_id("next-trip")
    expect(pas).to_be_visible()
    last_chip = phone.get_by_test_id("nl-suggest").last.bounding_box()
    box = pas.bounding_box()
    assert last_chip and box and box["y"] >= last_chip["y"] + last_chip["height"] - 1
    assert box["x"] >= 0 and box["x"] + box["width"] <= 375
    assert phone.evaluate("document.documentElement.scrollWidth") <= 375


# ---- S25 我的行程 ----
def test_my_plans_has_search_a_filter_that_is_always_there_and_a_big_first_card(page: Page):
    cards = [card("p-a", TODAY + timedelta(days=3), title="泉州市 · 均衡"),
             card("p-b", TODAY + timedelta(days=9), title="罗马 · 艺术"),
             card("p-c", TODAY + timedelta(days=20), title="巴黎 · 艺术"),
             card("p-old", TODAY - timedelta(days=30), title="上海市 · 均衡")]
    serve_cards(page, cards)                                                  # 没有归档的行程，切换也要在
    page.goto(f"{WEB}/plans")
    page.locator("a.plan-card").first.wait_for()
    expect(page.get_by_role("button", name=re.compile(r"进行中 \(4\)"))).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"已归档 \(0\)"))).to_be_visible()
    expect(page.get_by_role("link", name="新建", exact=True)).to_be_visible()
    first, second = (page.locator('[data-testid="group-upcoming"] a.plan-card').nth(i).bounding_box() for i in (0, 1))
    assert first and second and first["width"] > second["width"] * 1.7, (first, second)    # 第一张占两列
    cover = page.locator('a.plan-card[href$="/p-a"] .plan-cover')
    expect(cover.get_by_test_id("plan-countdown")).to_have_text("还有 3 天")
    expect(cover.locator("h2")).to_have_text("泉州市 · 均衡")                    # 城市名在封面上
    expect(cover.locator(".cover-dates")).to_have_text(re.compile(r"\d\d\.\d\d — \d\d\.\d\d"))
    line = cover.evaluate("e => getComputedStyle(e, '::after').height")
    assert line == "3px", line                                                 # 封面底部的黄铜线


def test_searching_filters_the_cards_and_says_when_nothing_matches(page: Page):
    cards = [card("p-a", TODAY + timedelta(days=3), title="泉州市 · 均衡"),
             card("p-b", TODAY + timedelta(days=9), title="罗马 · 艺术"),
             card("p-old", TODAY - timedelta(days=30), title="罗马 · 古迹")]
    serve_cards(page, cards)
    page.goto(f"{WEB}/plans")
    page.locator("a.plan-card").first.wait_for()
    page.get_by_test_id("plans-search").fill("罗马")
    expect(page.locator("a.plan-card")).to_have_count(2)                      # 即将到来 + 已结束里都过滤
    expect(page.locator('a.plan-card[href$="/p-a"]')).to_have_count(0)
    page.get_by_test_id("plans-search").fill("不存在的城市")
    expect(page.get_by_test_id("plans-no-match")).to_have_text("没有匹配的行程")
    expect(page.locator("a.plan-card")).to_have_count(0)
    page.get_by_test_id("plans-search").fill("")
    expect(page.locator("a.plan-card")).to_have_count(3)


def test_my_plans_stacks_one_card_per_row_on_a_phone(phone: Page):
    serve_cards(phone, [card("p-a", TODAY + timedelta(days=3)), card("p-b", TODAY + timedelta(days=9))])
    phone.goto(f"{WEB}/plans")
    phone.locator("a.plan-card").first.wait_for()
    a, b = (phone.locator("a.plan-card").nth(i).bounding_box() for i in (0, 1))
    assert a and b and a["x"] == b["x"] and b["y"] > a["y"], (a, b)
    assert a["width"] > 330
    assert phone.evaluate("document.documentElement.scrollWidth") <= 375


# ---- S26 / S27 手机行程页与标签栏 ----
@pytest.fixture(scope="module")
def plan_id() -> str:
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art"}, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def test_tabbar_active_item_has_a_pill_behind_the_icon(phone: Page):
    serve_cards(phone, [])
    phone.goto(f"{WEB}/plans")
    bar = phone.get_by_role("navigation", name="底部导航")
    active = bar.locator("a.active")
    expect(active).to_have_count(1)
    expect(active).to_contain_text("我的行程")
    bg = active.locator("svg").evaluate("e => getComputedStyle(e).backgroundColor")
    assert bg not in ("rgba(0, 0, 0, 0)", "transparent"), bg                 # 有胶囊底
    idle = bar.locator("a:not(.active)").first.locator("svg").evaluate("e => getComputedStyle(e).backgroundColor")
    assert idle in ("rgba(0, 0, 0, 0)", "transparent"), idle
    assert active.evaluate("e => getComputedStyle(e, '::before').display") == "none"   # 不再有顶部黄铜条
    assert bar.bounding_box()["height"] >= 56


def test_phone_plan_page_opens_the_map_by_default_and_stays_inside_the_screen(phone: Page, plan_id):
    phone.goto(f"{WEB}/plans/{plan_id}")
    phone.locator(".routecard").wait_for()
    expect(phone.locator(".routecard")).to_have_attribute("open", "")
    phone.locator(".routecard .fullmap").wait_for()
    box = phone.locator(".routecard .fullmap").bounding_box()
    assert box and box["width"] >= 374 and 200 <= box["height"] <= 260, box   # 通栏地图，约 240px 高
    assert phone.evaluate("document.documentElement.scrollWidth") <= 375
    nav_btn = phone.get_by_test_id("section-nav").locator("button.active").first
    bg = nav_btn.evaluate("e => getComputedStyle(e).backgroundColor")
    assert bg not in ("rgba(0, 0, 0, 0)", "transparent")                      # 当前栏目是实心胶囊
    assert nav_btn.bounding_box()["height"] >= 44


# ---- S28 新建行程 ----
def test_new_plan_has_a_sticky_summary_card_on_the_right_on_wide_screens(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".step-card").first.wait_for()
    bar = page.locator(".submit-bar")
    first = page.locator(".step-card").first.bounding_box()
    box = bar.bounding_box()
    assert first and box and box["x"] >= first["x"] + first["width"], (first, box)       # 摘要卡在表单右边
    head = page.get_by_test_id("submit-head")
    expect(head).to_contain_text("Paris")
    expect(head).to_contain_text(re.compile(r"\d\d\.\d\d — \d\d\.\d\d"))
    expect(bar.get_by_role("button", name="生成行程")).to_be_visible()
    page.evaluate("window.scrollTo(0, 1400)")
    page.wait_for_timeout(300)
    moved = bar.bounding_box()
    nav = page.locator("header.appnav").bounding_box()
    # 滚动时吸附在导航条下面，不被导航条盖住
    assert moved and nav and nav["y"] + nav["height"] <= moved["y"] <= nav["y"] + nav["height"] + 30, (moved, nav)
    title = page.get_by_role("heading", name="新建行程")
    assert title.evaluate("e => getComputedStyle(e).fontFamily").startswith("Georgia")


def test_new_plan_keeps_the_bottom_bar_on_a_phone(phone: Page):
    phone.goto(f"{WEB}/new?destination=Paris")
    phone.locator(".step-card").first.wait_for()
    expect(phone.get_by_test_id("submit-head")).to_be_hidden()
    box = phone.locator(".submit-bar").bounding_box()
    first = phone.locator(".step-card").first.bounding_box()
    # 和表单同宽、在下面
    assert box and first and abs(box["x"] - first["x"]) <= 1 and box["width"] >= first["width"] - 1, (box, first)
    assert phone.evaluate("document.documentElement.scrollWidth") <= 375


# ---- S29 行程页桌面 ----
def test_desktop_plan_page_has_a_dark_title_band_and_one_card_per_stop(page: Page, plan_id):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{WEB}/plans/{plan_id}")
    band = page.get_by_test_id("plan-title")
    band.wait_for(state="attached")
    assert "linear-gradient" in band.evaluate("e => getComputedStyle(e).backgroundImage")
    assert band.evaluate("e => getComputedStyle(e, '::after').height") == "3px"           # 黄铜线
    item = page.locator(".timeline .item").first
    item.wait_for(state="attached")
    assert item.evaluate("e => getComputedStyle(e).borderTopLeftRadius") == "18px"
    assert item.locator(".rail").evaluate("e => getComputedStyle(e, '::after').display") == "none"
    two = page.locator(".timeline").first.locator(".item")
    assert two.count() >= 2
    a, b = two.nth(0).bounding_box(), two.nth(1).bounding_box()
    assert a and b and b["y"] >= a["y"] + a["height"] + 4, (a, b)  # 卡片之间有空隙


# ---- S30 用量与版本足迹 ----
def test_usage_and_footprints_have_serif_titles(page: Page):
    page.goto(f"{WEB}/usage")
    title = page.get_by_role("heading", name="模型用量", exact=True)
    title.wait_for()
    assert title.evaluate("e => getComputedStyle(e).fontFamily").startswith("Georgia")
    page.goto(f"{WEB}/about")
    h1 = page.get_by_role("heading", name="版本足迹", exact=True)
    h1.wait_for()
    assert h1.evaluate("e => getComputedStyle(e).fontFamily").startswith("Georgia")


def test_usage_tables_become_row_lists_on_a_phone(phone: Page):
    phone.route(re.compile(r".*/llm/usage.*"), lambda r: r.fulfill(json={
        "totals": {"calls": 3, "failures": 1, "avg_latency_ms": 4000, "cost_usd": 0,
                   "prompt_tokens": 10, "completion_tokens": 5},
        "by_purpose": [{"purpose": "intake_nl", "calls": 3, "failures": 1, "avg_latency_ms": 4000,
                        "prompt_tokens": 10, "completion_tokens": 5}],
        "recent": []}))
    phone.goto(f"{WEB}/usage")
    phone.locator(".usage-table").first.wait_for()
    assert phone.locator(".usage-table thead").first.evaluate("e => getComputedStyle(e).display") == "none"
    row = phone.locator(".usage-table tbody tr").first
    assert row.evaluate("e => getComputedStyle(e).display") == "flex"
    assert row.locator("td").nth(1).evaluate("e => getComputedStyle(e, '::before').content") == '"次数 "'
    assert phone.evaluate("document.documentElement.scrollWidth") <= 375


# ---- S31 手机行程页底部抽屉 ----
def open_sheet(phone: Page, plan_id: str) -> None:
    phone.goto(f"{WEB}/plans/{plan_id}")
    phone.get_by_test_id("open-map-sheet").click()
    phone.get_by_test_id("map-sheet").wait_for()
    phone.get_by_test_id("sheet-stop").first.wait_for()
    phone.wait_for_timeout(450)                                              # 等抽屉滑动停稳


def test_the_map_drawer_button_only_exists_on_a_phone(page: Page, phone: Page, plan_id):
    page.goto(f"{WEB}/plans/{plan_id}")
    page.get_by_test_id("plan-title").wait_for(state="attached")
    expect(page.get_by_test_id("open-map-sheet")).to_be_hidden()
    phone.goto(f"{WEB}/plans/{plan_id}")
    fab = phone.get_by_test_id("open-map-sheet")
    expect(fab).to_be_visible()
    assert fab.bounding_box()["height"] >= 44


def test_the_drawer_starts_low_over_a_map_and_the_back_button_closes_it(phone: Page, plan_id):
    open_sheet(phone, plan_id)
    panel = phone.get_by_test_id("sheet-panel")
    expect(panel).to_have_attribute("data-snap", "peek")
    expect(phone.get_by_test_id("open-map-sheet")).to_be_hidden()
    top = panel.bounding_box()["y"]
    assert top >= 812 * 0.4, top                                            # 地图占上半屏
    phone.locator(".sheet-map .leaflet-tile").first.wait_for(state="attached")
    assert phone.locator(".sheet-pin").count() >= 1
    assert phone.evaluate("document.documentElement.scrollWidth") <= 375
    back = phone.get_by_test_id("map-sheet-close").bounding_box()
    assert back and back["height"] >= 44 and back["width"] >= 44, back
    assert phone.get_by_test_id("sheet-day").first.bounding_box()["height"] >= 44
    phone.get_by_test_id("map-sheet-close").click()
    expect(phone.get_by_test_id("map-sheet")).to_have_count(0)
    expect(phone.get_by_test_id("open-map-sheet")).to_be_visible()


def test_dragging_or_clicking_the_grip_moves_the_drawer_between_two_heights(phone: Page, plan_id):
    open_sheet(phone, plan_id)
    panel = phone.get_by_test_id("sheet-panel")
    grip = phone.get_by_test_id("sheet-grip")
    low = panel.bounding_box()["y"]
    box = grip.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    phone.mouse.move(x, y)
    phone.mouse.down()
    phone.mouse.move(x, y - 120, steps=6)
    phone.mouse.move(x, y - 260, steps=6)
    phone.mouse.up()
    expect(panel).to_have_attribute("data-snap", "full")
    phone.wait_for_timeout(400)
    assert panel.bounding_box()["y"] < low - 150
    expect(grip).to_have_attribute("aria-expanded", "true")
    grip.click()                                                            # 点一下也能收回去
    expect(panel).to_have_attribute("data-snap", "peek")
    phone.wait_for_timeout(400)
    assert abs(panel.bounding_box()["y"] - low) <= 2


def test_day_chips_switch_the_list_and_tapping_a_stop_selects_it_on_the_map(phone: Page, plan_id):
    open_sheet(phone, plan_id)
    days = phone.get_by_test_id("sheet-day")
    assert days.count() >= 3
    expect(days.first).to_have_attribute("aria-selected", "true")
    first_title = phone.get_by_test_id("sheet-panel").locator(".sheet-title").inner_text()
    days.nth(1).click()
    expect(days.nth(1)).to_have_attribute("aria-selected", "true")
    assert phone.get_by_test_id("sheet-panel").locator(".sheet-title").inner_text() != first_title
    days.first.click()
    stops = phone.get_by_test_id("sheet-stop")
    numbered = stops.filter(has=phone.locator(".sheet-no")).first
    numbered.locator("button.sheet-stop-main").click()
    expect(numbered).to_have_attribute("data-selected", "true")
    expect(numbered.get_by_role("button", name=re.compile("详情"))).to_be_visible()
    expect(phone.locator(".sheet-pin.sel")).to_have_count(1)
    numbered.get_by_role("button", name=re.compile("详情")).click()
    expect(phone.get_by_role("dialog", name="地点详情")).to_be_visible()


def test_escape_closes_the_drawer(phone: Page, plan_id):
    open_sheet(phone, plan_id)
    phone.keyboard.press("Escape")
    expect(phone.get_by_test_id("map-sheet")).to_have_count(0)


# ---- S31 底图（高德 / 基础地图）与后退键（S31b） ----
FAKE_AMAP = r"""
window.__amap = { maps: 0, markers: [], lines: 0, fit: null, centers: [], zooms: [] };
(function () {
  class Pixel { constructor(x, y) { this.x = x; this.y = y; } }
  class Marker {
    constructor(o) { this.o = o; this.h = {}; this.el = document.createElement("div"); this.el.innerHTML = o.content; }
    on(e, f) { this.h[e] = f; }
    setContent(c) { this.o.content = c; this.el.innerHTML = c; }
    setOffset() {}
    setzIndex() {}
  }
  class Polyline { constructor(o) { this.o = o; } }
  class Map {
    constructor(el) {
      this.el = el; this.z = 12; this.sheet = !!el.closest(".sheet-map");
      if (this.sheet) window.__amap.maps += 1;
      el.setAttribute("data-fake-amap", "1");
    }
    add(o) {
      if (!this.sheet) return;
      if (o instanceof Marker) {
        const left = (40 + 40 * window.__amap.markers.length) + 'px';
        Object.assign(o.el.style, { position: 'absolute', left, top: '250px', zIndex: 3 });
        this.el.appendChild(o.el); window.__amap.markers.push(o);
        o.el.addEventListener("click", () => o.h.click && o.h.click());
      } else window.__amap.lines += 1;
    }
    clearMap() { this.el.innerHTML = ""; if (this.sheet) { window.__amap.markers = []; window.__amap.lines = 0; } }
    setFitView(ov, imm, avoid, max) { if (this.sheet) window.__amap.fit = { n: ov.length, avoid, max }; }
    getZoom() { return this.z; }
    setZoomAndCenter(z) { this.z = z; if (this.sheet) window.__amap.zooms.push(z); }
    lngLatToContainer() { return { x: 187, y: 300 }; }
    containerToLngLat(p) { return [p.x, p.y]; }
    setCenter(c) { if (this.sheet) window.__amap.centers.push(c); }
    destroy() { this.el.innerHTML = ""; }
  }
  window.AMap = { Map, Marker, Polyline, Pixel };
})();
"""


def amap_stub(page: Page) -> None:
    page.route("**/map/config", lambda r: r.fulfill(json={"provider": "amap", "js_key": "k", "security_code": "s"}))
    page.route("https://webapi.amap.com/**", lambda r: r.fulfill(content_type="text/javascript", body=FAKE_AMAP))


@pytest.fixture(scope="module")
def kunming_plan() -> str:
    if not any(c["id"] == "kunming" for c in httpx.get(f"{API}/cities", timeout=10).json()):
        pytest.skip("后端里没有境内的测试城市（昆明）：用 pytest 自动起的独立后端跑")
    r = httpx.post(f"{API}/plans", json={
        "destination": "kunming", "start_date": "2026-10-03", "end_date": "2026-10-05", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["history"], "theme": "museum"}, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def test_a_mainland_route_uses_amap_in_the_drawer_and_the_stops_still_work(phone: Page, kunming_plan):
    amap_stub(phone)
    open_sheet(phone, kunming_plan)
    sheet_map = phone.get_by_test_id("sheet-map")
    expect(sheet_map).to_have_attribute("data-engine", "amap")
    expect(phone.get_by_test_id("sheet-map-note")).to_have_count(0)
    assert phone.locator(".sheet-map .leaflet-tile").count() == 0  # 没有 OpenStreetMap 的图块
    numbered = phone.get_by_test_id("sheet-stop").filter(has=phone.locator(".sheet-no"))
    n = numbered.count()
    assert n >= 1
    state = phone.evaluate("window.__amap")
    assert state["maps"] == 1 and len(state["markers"]) == n, state
    assert state["fit"]["avoid"][0] >= 100 and state["fit"]["avoid"][1] >= 360, state["fit"]  # 避开顶部条和半屏抽屉
    phone.locator(".sheet-map .sheet-pin").first.click()                              # 点圆点 = 选中那一站
    expect(numbered.first).to_have_attribute("data-selected", "true")
    phone.wait_for_timeout(200)
    assert phone.evaluate("window.__amap.centers.length") >= 1
    assert phone.evaluate("window.__amap.zooms.at(-1)") >= 15
    phone.get_by_test_id("sheet-day").nth(1).click()  # 换天：同一张高德地图重画，不新建
    phone.wait_for_timeout(200)
    assert phone.evaluate("window.__amap.maps") == 1
    assert phone.evaluate("window.__amap.markers.length") >= 1


def test_without_an_amap_key_the_drawer_falls_back_and_says_so(phone: Page, kunming_plan):
    open_sheet(phone, kunming_plan)
    expect(phone.get_by_test_id("sheet-map")).to_have_attribute("data-engine", "osm")
    expect(phone.get_by_test_id("sheet-map-note")).to_have_text("当前使用基础地图（没配高德 Key）")
    phone.locator(".sheet-map .leaflet-tile").first.wait_for(state="attached")


def test_a_route_outside_china_never_asks_for_amap_in_the_drawer(phone: Page, plan_id):
    asked: list[str] = []
    phone.on("request", lambda r: asked.append(r.url) if "/map/config" in r.url or "amap.com" in r.url else None)
    open_sheet(phone, plan_id)
    expect(phone.get_by_test_id("sheet-map")).to_have_attribute("data-engine", "osm")
    expect(phone.get_by_test_id("sheet-map-note")).to_have_count(0)
    assert not asked, asked


def test_the_back_key_only_closes_the_drawer(phone: Page, plan_id):
    phone.goto(f"{WEB}/plans")
    phone.goto(f"{WEB}/plans/{plan_id}")
    phone.get_by_test_id("open-map-sheet").click()
    expect(phone.get_by_test_id("map-sheet")).to_be_visible()
    assert phone.url.endswith(f"/plans/{plan_id}#map"), phone.url
    phone.go_back()
    expect(phone.get_by_test_id("map-sheet")).to_have_count(0)
    assert phone.url.endswith(f"/plans/{plan_id}"), phone.url  # 还在行程页，没有退到上一页
    expect(phone.get_by_test_id("open-map-sheet")).to_be_visible()
    phone.go_forward()                                                                  # 前进又回到地图模式
    expect(phone.get_by_test_id("map-sheet")).to_be_visible()


def test_the_back_button_and_escape_leave_no_extra_history_entry(phone: Page, plan_id):
    phone.goto(f"{WEB}/plans")
    phone.goto(f"{WEB}/plans/{plan_id}")
    for how in ("button", "escape"):
        phone.get_by_test_id("open-map-sheet").click()
        expect(phone.get_by_test_id("map-sheet")).to_be_visible()
        if how == "button":
            phone.get_by_test_id("map-sheet-close").click()
        else:
            phone.keyboard.press("Escape")
        expect(phone.get_by_test_id("map-sheet")).to_have_count(0)
        assert phone.url.endswith(f"/plans/{plan_id}"), phone.url
    phone.go_back()  # 再后退一次：直接回到上一页
    phone.wait_for_url(re.compile(r"/plans$"))


def test_opening_the_address_with_map_goes_straight_to_the_drawer_and_close_stays_on_the_page(phone: Page, plan_id):
    phone.goto(f"{WEB}/plans")
    phone.goto(f"{WEB}/plans/{plan_id}#map")
    phone.get_by_test_id("map-sheet").wait_for()
    phone.get_by_test_id("map-sheet-close").click()
    expect(phone.get_by_test_id("map-sheet")).to_have_count(0)
    assert phone.url.endswith(f"/plans/{plan_id}"), phone.url  # 只去掉 #map，没有离开页面
    expect(phone.get_by_test_id("open-map-sheet")).to_be_visible()


def test_the_place_card_opened_from_the_drawer_closes_with_it(phone: Page, plan_id):
    open_sheet(phone, plan_id)
    stop = phone.get_by_test_id("sheet-stop").filter(has=phone.locator(".sheet-no")).first
    stop.locator("button.sheet-stop-main").click()
    stop.get_by_role("button", name=re.compile("详情")).click()
    card_ = phone.get_by_role("dialog", name="地点详情")
    expect(card_).to_be_visible()
    phone.go_back()
    expect(phone.get_by_test_id("map-sheet")).to_have_count(0)
    expect(card_).to_have_count(0)


def test_the_drawer_is_not_opened_by_a_map_hash_on_a_wide_screen(page: Page, plan_id):
    page.goto(f"{WEB}/plans/{plan_id}#map")
    page.get_by_test_id("plan-title").wait_for(state="attached")
    expect(page.get_by_test_id("map-sheet")).to_have_count(0)
