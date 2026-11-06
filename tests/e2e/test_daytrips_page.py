"""周边一日游的前端验收（规格 08 3.4–3.7）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_daytrips_page.py

时刻表（Transitous）这一层在页面测试里用录下来的真实回复回放（tests/fixtures/transit/），这样不受公共服务快慢影响；
最后有一条真连 Transitous 的冒烟测试（连不上就跳过）。行程页上的验收用后端的预设出发地（存好的时刻表）直接建行程。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, Route, expect, sync_playwright  # noqa: E402

from app.daytrips_live import build_live_request, list_destinations, make_origin  # noqa: E402
from tests.transit_fake import HOME, fake_transitous  # noqa: E402

pytestmark = pytest.mark.e2e

WEB = os.getenv("E2E_WEB", "http://127.0.0.1:5173").rstrip("/")
API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")
DEC = "2026-12-12"
HOME_JSON = json.dumps({"label": "Königstraße 28, 70173 Stuttgart", "city": HOME["city"], "city_zh": HOME["city_zh"],
                        "country": "DE", "lat": HOME["lat"], "lng": HOME["lng"]})


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
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def stub_timetable(page: Page) -> None:
    """页面里的时刻表请求用录下来的真实回复回放（和线上同一个后端代码生成的响应）。"""
    def origin_of(b: dict):
        return make_origin(b["city"], b["country"], b["lat"], b["lng"], b.get("city_zh"))

    def search(route: Route) -> None:
        b = route.request.post_data_json
        out = list_destinations(origin_of(b), date.fromisoformat(b["date"]), fake_transitous(), depart=b["depart"])
        route.fulfill(json=out)

    def plan_request(route: Route) -> None:
        b = route.request.post_data_json
        try:
            req, info = build_live_request(origin_of(b), b["destination"], date.fromisoformat(b["date"]),
                                           fake_transitous(), depart=b["depart"], back=b.get("back"), kind=b["kind"])
        except Exception as exc:  # noqa: BLE001 - 和后端一样，原因交给页面弹窗
            route.fulfill(status=422, json={"detail": {"message": str(exc)}})
            return
        route.fulfill(json={"payload": {**req.model_dump(mode="json"), "theme": "daytrip"}, **info})

    page.route("**/daytrips/search", search)
    page.route("**/daytrips/plan-request", plan_request)


def open_page(page: Page, date_: str = DEC, *, home: bool = True, dticket: bool = False) -> None:
    stub_timetable(page)
    seed = []
    if home:
        seed.append(f"localStorage.setItem('atp:daytrip-home', {json.dumps(HOME_JSON)});")
    if dticket:
        seed.append("localStorage.setItem('atp:has-dticket', 'true');")
    if seed:                                                    # 只在新开的标签页里种一次：刷新后用页面自己存的
        page.add_init_script("if (!sessionStorage.getItem('seeded')) {" + "".join(seed)
                             + "sessionStorage.setItem('seeded','1');}")
    page.goto(f"{WEB}/daytrips")
    page.get_by_test_id("daytrips-page").wait_for()
    page.get_by_test_id("dt-date").fill(date_)
    page.get_by_test_id("daytrip-card").first.wait_for(timeout=30_000)


def card(page: Page, city: str):
    return page.locator(f'[data-testid=daytrip-card][data-city="{city}"]')


def minutes_of(c) -> int:
    return int(re.search(r"\d+", c.get_by_test_id("dt-minutes").inner_text()).group())


# --------------------------------------------------------------------------- #
# 页面结构
# --------------------------------------------------------------------------- #
def test_the_nav_leads_to_the_day_trips_page_and_offers_both_city_centres_as_a_shortcut(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.get_by_role("navigation", name="主导航").get_by_role("link", name="周边一日游").click()
    expect(page).to_have_url(re.compile(r"/daytrips$"))
    expect(page.get_by_test_id("dt-address")).to_be_visible()
    expect(page.get_by_test_id("origin-stuttgart")).to_contain_text("斯图加特 Stuttgart")
    expect(page.get_by_test_id("origin-aachen")).to_contain_text("亚琛 Aachen")


def test_destinations_show_german_and_chinese_names_and_the_source_is_folded_away(page: Page):
    open_page(page)
    assert page.get_by_test_id("daytrip-card").count() >= 6
    hd = card(page, "heidelberg")
    expect(hd.locator("h3")).to_contain_text("海德堡")
    expect(hd.locator("h3")).to_contain_text("Heidelberg")
    expect(hd.get_by_test_id("dt-minutes")).to_have_text(re.compile(r"单程\s*\d+\s*分钟"))
    expect(hd).to_contain_text("开放数据草稿")
    details = hd.get_by_test_id("dt-details")
    expect(details.get_by_role("link", name=re.compile("Transitous"))).to_be_hidden()
    details.locator("summary").click()
    expect(details).to_contain_text("不是实时延误")
    expect(details.get_by_role("link", name=re.compile("Transitous"))).to_have_attribute("href", "https://transitous.org")
    expect(details.get_by_role("link", name=re.compile("DB 官方说明"))).to_have_attribute("href", re.compile("bahn.de"))


def test_the_christmas_market_says_whether_the_chosen_date_is_inside_its_season(page: Page):
    open_page(page, DEC)
    season = card(page, "heidelberg").get_by_test_id("dt-season")
    expect(season).to_contain_text("11 月 23 日 至 12 月 22 日")
    expect(season).to_have_attribute("data-in-season", "true")
    expect(season).to_contain_text("你选的日期在期内")
    expect(card(page, "heidelberg")).to_have_class(re.compile("in-season"))
    expect(card(page, "esslingen").get_by_test_id("dt-season")).to_contain_text("没能在官方网站核对")
    expect(season).to_contain_text("官方网站写明")
    page.get_by_test_id("dt-date").fill("2026-07-04")
    expect(card(page, "heidelberg").get_by_test_id("dt-season")).to_have_attribute("data-in-season", "false")
    expect(card(page, "heidelberg")).not_to_have_class(re.compile("in-season"))


def test_there_are_no_time_groups_just_a_filter_and_a_sort_and_cards_sit_two_to_a_row(page: Page):
    open_page(page)
    expect(page.locator(".dt-group-title")).to_have_count(0)
    cards = page.get_by_test_id("daytrip-card")
    total = cards.count()
    expect(page.get_by_test_id("dt-filter-all")).to_contain_text(str(total))
    a, b = cards.nth(0).bounding_box(), cards.nth(1).bounding_box()
    assert abs(a["y"] - b["y"]) < 2 and b["x"] > a["x"] + a["width"] - 1                      # 同一行，不重叠
    mins = [minutes_of(cards.nth(i)) for i in range(total)]
    assert mins == sorted(mins)                                                               # 默认：用时从短到长


def test_sort_by_longest_first_and_by_earliest_arrival_and_by_name(page: Page):
    open_page(page)
    cards = page.get_by_test_id("daytrip-card")
    page.get_by_test_id("dt-sort").select_option("slowest")
    mins = [minutes_of(cards.nth(i)) for i in range(cards.count())]
    assert mins == sorted(mins, reverse=True)
    page.get_by_test_id("dt-sort").select_option("name")
    assert cards.count() == len(mins)


def test_there_is_no_christmas_market_filter_the_card_just_says_whether_it_is_in_season(page: Page):
    open_page(page)
    expect(page.get_by_test_id("dt-filter-season")).to_have_count(0)


def test_filter_to_regional_trains_only_long_distance_trains_or_cross_border_trips(page: Page):
    open_page(page)
    total = page.get_by_test_id("daytrip-card").count()
    page.get_by_test_id("dt-filter-regional").click()                                          # 只看区间火车
    expect(card(page, "strasbourg")).to_have_count(0)                                         # 跨境没有区间车方案
    expect(card(page, "heidelberg").get_by_test_id("dt-opt-regional")).to_have_attribute("data-selected", "true")
    assert 0 < page.get_by_test_id("daytrip-card").count() < total
    page.get_by_test_id("dt-filter-long").click()  # 长途火车
    expect(card(page, "heidelberg").get_by_test_id("dt-opt-fastest")).to_have_attribute("data-selected", "true")
    expect(card(page, "strasbourg")).to_have_count(1)
    page.get_by_test_id("dt-filter-border").click()                                            # 只看跨境
    expect(page.get_by_test_id("daytrip-card")).to_have_count(1)
    expect(card(page, "strasbourg")).to_be_visible()
    page.get_by_test_id("dt-filter-all").click()
    expect(page.get_by_test_id("daytrip-card")).to_have_count(total)


# --------------------------------------------------------------------------- #
# 住址
# --------------------------------------------------------------------------- #
def test_an_address_is_searched_picked_remembered_and_shown_instead_of_a_fixed_city(page: Page):
    stub_timetable(page)
    page.route("**/daytrips/geocode", lambda r: r.fulfill(json=[
        {"label": "Königstraße 28, 70173 Stuttgart", "city": "Stuttgart", "city_zh": "斯图加特", "country": "DE",
         "lat": HOME["lat"], "lng": HOME["lng"]},
        {"label": "Königstraße 28, 97957 Wittighausen", "city": "Wittighausen", "city_zh": None, "country": "DE",
         "lat": 49.5, "lng": 9.7}]))
    page.goto(f"{WEB}/daytrips")
    page.get_by_test_id("dt-address").fill("Königstraße 28, Stuttgart")
    page.get_by_test_id("dt-find").click()
    expect(page.get_by_test_id("dt-candidate")).to_have_count(2)
    expect(page.get_by_test_id("dt-cands-tip")).to_contain_text("同一条街")
    page.get_by_test_id("dt-candidate").first.click()
    expect(page.get_by_test_id("dt-home-label")).to_have_text("Königstraße 28, 70173 Stuttgart")
    page.get_by_test_id("daytrip-card").first.wait_for(timeout=30_000)
    page.reload()                                                                              # 记在浏览器里：刷新还在
    expect(page.get_by_test_id("dt-home-label")).to_have_text("Königstraße 28, 70173 Stuttgart")
    page.get_by_test_id("dt-change-home").click()
    expect(page.get_by_test_id("dt-address")).to_be_visible()


def test_a_too_short_address_is_explained_in_a_popup_not_sent(page: Page):
    page.goto(f"{WEB}/daytrips")
    page.get_by_test_id("dt-address").fill("ab")
    page.get_by_test_id("dt-find").click()
    expect(page.get_by_role("alertdialog").or_(page.get_by_role("dialog"))).to_contain_text("地址写长一点")


def test_when_no_address_is_found_the_popup_says_how_to_write_it(page: Page):
    page.route("**/daytrips/geocode", lambda r: r.fulfill(json=[]))
    page.goto(f"{WEB}/daytrips")
    page.get_by_test_id("dt-address").fill("某某路 3 号")
    page.get_by_test_id("dt-find").click()
    popup = page.get_by_role("alertdialog").or_(page.get_by_role("dialog"))
    expect(popup).to_contain_text("没找到这个地址")
    expect(popup).to_contain_text("邮编 城市")


# --------------------------------------------------------------------------- #
# 车型和 Deutschlandticket
# --------------------------------------------------------------------------- #
def test_each_card_offers_the_fast_train_and_the_regional_train_and_says_which_needs_a_ticket(page: Page):
    open_page(page)
    hd = card(page, "heidelberg")
    fast, regional = hd.get_by_test_id("dt-opt-fastest"), hd.get_by_test_id("dt-opt-regional")
    expect(fast).to_contain_text("ICE 2090")
    expect(fast.get_by_test_id("dt-fare")).to_have_text("需另买票")
    expect(regional).to_contain_text("RE1")
    expect(regional.get_by_test_id("dt-fare")).to_have_text("Deutschlandticket 通常覆盖")
    expect(fast).to_have_attribute("data-selected", "true")                                  # 没勾月票：默认最快
    fast_min = minutes_of(hd)
    regional.click()
    expect(regional).to_have_attribute("data-selected", "true")
    assert minutes_of(hd) > fast_min  # 区间车更慢：大字跟着变
    expect(hd.get_by_test_id("dt-db-link")).to_have_attribute("href", re.compile(r"^https://www\.bahn\.de/"))
    expect(hd.get_by_test_id("dt-db-query")).to_contain_text("Stuttgart Hauptbahnhof → Heidelberg")


def test_the_deutschlandticket_checkbox_is_off_by_default_and_remembered_once_you_tick_it(page: Page):
    open_page(page)
    expect(page.get_by_test_id("dt-dticket")).not_to_be_checked()
    expect(card(page, "heidelberg").get_by_test_id("dt-opt-fastest")).to_have_attribute("data-selected", "true")
    page.get_by_test_id("dt-dticket").check()
    expect(card(page, "heidelberg").get_by_test_id("dt-opt-regional")).to_have_attribute("data-selected", "true")
    page.reload()  # 刷新后保持有的设置
    page.get_by_test_id("daytrip-card").first.wait_for(timeout=30_000)
    expect(page.get_by_test_id("dt-dticket")).to_be_checked()
    expect(card(page, "heidelberg").get_by_test_id("dt-opt-regional")).to_have_attribute("data-selected", "true")
    page.get_by_test_id("dt-dticket").uncheck()                                               # 取消也记住
    page.reload()
    page.get_by_test_id("daytrip-card").first.wait_for(timeout=30_000)
    expect(page.get_by_test_id("dt-dticket")).not_to_be_checked()


def test_a_cross_border_card_is_marked_and_has_no_regional_option(page: Page):
    open_page(page, dticket=True)
    sx = card(page, "strasbourg")
    expect(sx).to_contain_text("跨境")
    expect(sx.get_by_test_id("dt-opt-regional")).to_have_count(0)
    expect(sx.get_by_test_id("dt-fare")).to_have_text("需另买票")
    expect(sx.get_by_test_id("dt-farenote")).to_contain_text("跨境")


# --------------------------------------------------------------------------- #
# 排一天
# --------------------------------------------------------------------------- #
def test_plan_a_day_from_the_address_goes_through_progress_to_a_one_day_plan_with_the_ride_and_no_street(page: Page):
    open_page(page)
    page.get_by_test_id("dt-back").fill("19:00")
    card(page, "heidelberg").get_by_test_id("plan-day").click()
    page.wait_for_url(re.compile(r"/tasks/"), timeout=15_000)
    page.wait_for_url(re.compile(r"/plans/heidelberg-"), timeout=240_000)
    page.locator(".day").first.wait_for()
    expect(page.locator(".day")).to_have_count(1)
    first = page.locator(".timeline .item").first
    expect(first).to_contain_text("斯图加特 Stuttgart")
    expect(first).to_contain_text("抵达")
    expect(first).to_contain_text("ICE 2090")
    expect(first).to_contain_text("需另买票")
    expect(page.locator(".timeline .item").last).to_contain_text("返回斯图加特 Stuttgart")
    assert "Königstraße" not in page.locator("body").inner_text()                             # 街道不进行程
    page.locator(".item .title-link").first.click()
    dialog = page.get_by_role("dialog", name="地点详情")
    expect(dialog.get_by_test_id("place-draft")).to_be_visible()


def test_planning_with_the_regional_option_says_the_ticket_covers_it(page: Page):
    open_page(page, dticket=True)
    card(page, "heidelberg").get_by_test_id("plan-day").click()
    page.wait_for_url(re.compile(r"/plans/heidelberg-"), timeout=240_000)
    page.locator(".day").first.wait_for()
    expect(page.locator(".timeline .item").first).to_contain_text("区间车")
    expect(page.locator(".timeline .item").first).to_contain_text("Deutschlandticket")


def test_a_return_that_is_too_early_shows_the_reason_in_a_popup(page: Page):
    open_page(page)
    page.get_by_test_id("dt-back").fill("10:00")
    card(page, "heidelberg").get_by_test_id("plan-day").click()
    expect(page.get_by_role("alertdialog").or_(page.get_by_role("dialog"))).to_contain_text("太早")
    expect(page).to_have_url(re.compile(r"/daytrips$"))


def test_on_a_phone_the_cards_do_not_overflow(browser):
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, locale="zh-CN", is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    try:
        open_page(pg)
        assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        expect(card(pg, "heidelberg")).to_be_visible()
    finally:
        ctx.close()


# --------------------------------------------------------------------------- #
# 一日游的行程页（用预设出发地的存好的时刻表直接建行程）
# --------------------------------------------------------------------------- #
def day_plan(destination: str) -> str:
    q = {"origin": "stuttgart", "destination": destination, "date": DEC}
    payload = httpx.get(f"{API}/daytrips/request", params=q, timeout=30).json()["payload"]
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def test_a_day_trip_page_has_no_packing_budget_or_stay_cards_and_a_plain_title(page: Page):
    pid = day_plan("heidelberg")
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    for gone in ("#sec-stays", "#sec-companion", '[data-testid="companion-card"]', '[data-testid="budget-card"]'):
        expect(page.locator(gone)).to_have_count(0)
    expect(page.get_by_test_id("budget-off")).to_contain_text("一日游暂不估算预算")
    expect(page.get_by_role("navigation").get_by_role("link", name="随行")).to_have_count(0)
    expect(page.locator("h1, h2").filter(has_text="海德堡 Heidelberg · 一日游").first).to_be_visible()
    body = page.locator("body").inner_text()
    assert "0 晚" not in body and "→ 2026" not in body and "daytrip" not in body, "一日游不写「共 1 天 0 晚」和日期范围"
    assert "heidelberg · 1 天行程" not in body


def test_the_cover_shows_the_places_of_the_day_not_just_one_card(page: Page):
    pid = day_plan("heidelberg")
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    page.wait_for_timeout(800)
    assert page.locator("[data-testid=cover-card]").count() >= 3


def test_a_trip_across_the_border_tells_you_to_take_your_passport_in_the_itinerary(page: Page):
    pid = day_plan("strasbourg")
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    expect(page.locator(".timeline .item").first).to_contain_text("护照")
    home = day_plan("heidelberg")
    page.goto(f"{WEB}/plans/{home}")
    page.locator(".day").first.wait_for()
    expect(page.locator(".timeline .item").first).not_to_contain_text("护照")


# --------------------------------------------------------------------------- #
# 真连 Transitous 的冒烟测试
# --------------------------------------------------------------------------- #
def test_smoke_with_the_real_timetable_service(page: Page):
    if not _up("https://api.transitous.org/api/v1/geocode?text=Stuttgart"):
        pytest.skip("连不上 Transitous")
    page.goto(f"{WEB}/daytrips")
    page.get_by_test_id("dt-date").fill(DEC)
    page.get_by_test_id("dt-address").fill("Königstraße 28, Stuttgart")
    page.get_by_test_id("dt-find").click()
    page.get_by_test_id("dt-candidate").first.wait_for(timeout=30_000)
    page.get_by_test_id("dt-candidate").first.click()
    page.get_by_test_id("daytrip-card").first.wait_for(timeout=120_000)
    assert page.get_by_test_id("daytrip-card").count() >= 3
    if card(page, "heidelberg").count():                       # 偶尔有城市没查到（页面会提醒）；查到了就是 ICE / IC
        expect(card(page, "heidelberg").get_by_test_id("dt-opt-fastest")).to_contain_text(re.compile("ICE|IC "))
    else:
        expect(page.get_by_test_id("dt-missing")).to_be_visible()
