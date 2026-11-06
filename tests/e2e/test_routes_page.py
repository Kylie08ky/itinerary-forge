"""P17 深度游学路线的前端验收（规格 09 第八节）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_routes_page.py
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
ART = "art-royal-to-contemporary"


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


def card(page: Page):
    return page.get_by_test_id(f"route-card-{ART}")


def open_new(page: Page) -> None:
    page.goto(f"{WEB}/new?destination=Paris")
    card(page).wait_for()


def test_the_new_plan_page_offers_the_route_with_its_thesis_days_and_examples(page: Page):
    open_new(page)
    c = card(page)
    expect(c).to_contain_text("艺术：从王室收藏到当代")
    expect(c).to_contain_text("建议 3 天")
    expect(c).to_contain_text("3 章")
    expect(c).to_contain_text("12 站")
    expect(c.locator(".route-chips li").first).to_be_visible()


def test_the_route_detail_shows_chapters_stops_and_reading_before_you_generate(page: Page):
    open_new(page)
    page.get_by_test_id(f"route-more-{ART}").click()
    detail = page.get_by_test_id(f"route-detail-{ART}")
    expect(detail).to_contain_text("第 1 章 · 王室收藏与私人藏家")
    expect(detail).to_contain_text("卢浮宫 · Musée du Louvre")
    expect(detail).to_contain_text("第 3 章 · 从现代到当代")
    expect(detail).to_contain_text("延伸阅读 / 观影")
    expect(detail.get_by_role("link", name="L'Œuvre")).to_have_attribute("href", re.compile("^https://"))
    expect(detail).to_contain_text("蓬皮杜中心整修闭馆")
    page.get_by_test_id(f"route-more-{ART}").click()
    expect(detail).to_have_count(0)


def test_a_route_and_a_theme_are_either_or(page: Page):
    open_new(page)
    page.get_by_test_id(f"route-select-{ART}").click()
    expect(card(page)).to_have_attribute("data-selected", "true")
    page.get_by_role("button", name=re.compile("时尚")).first.click()                       # 点主题：取消路线
    expect(card(page)).to_have_attribute("data-selected", "false")


def test_the_day_count_is_never_blocked_only_a_hint_says_how_it_will_be_adapted(page: Page):
    open_new(page)
    expect(card(page)).to_contain_text("推荐最少 2 天")
    fill_trip(page, "2026-12-17", "2026-12-17")                                          # 1 天：少于推荐
    page.get_by_test_id(f"route-select-{ART}").click()
    warn = page.get_by_test_id("route-days-warn")
    expect(warn).to_contain_text("少于推荐的 2 天")
    expect(warn).to_contain_text("先拿掉可选站")
    fill_trip(page, "2026-12-17", "2026-12-23")                                          # 7 天：多于路线内容
    expect(page.get_by_test_id("route-days-warn")).to_contain_text("多出来的 2 天用路线对应的主题补充")
    fill_trip(page, "2026-12-17", "2026-12-19")                                          # 3 天：不提示
    expect(page.get_by_test_id("route-days-warn")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("生成行程"))).to_be_enabled()      # 从头到尾没有被拦住


def test_you_can_pick_which_stops_to_visit_and_the_plan_only_has_those(page: Page):
    open_new(page)
    page.get_by_test_id(f"route-select-{ART}").click()
    stops = page.get_by_test_id("route-stops")
    stops.wait_for()
    expect(page.get_by_test_id("route-stops-count")).to_contain_text("已选 12 / 12")
    page.get_by_test_id("route-stop-picasso").uncheck()
    expect(page.get_by_test_id("route-stops-count")).to_contain_text("已选 11 / 12")
    page.get_by_test_id("route-stops-core").click()
    expect(page.get_by_test_id("route-stops-count")).to_contain_text("已选 9 / 12")
    expect(page.get_by_test_id("route-stop-tuileries-garden")).not_to_be_checked()          # 可选站被拿掉
    expect(page.get_by_test_id("route-stop-orsay")).to_be_checked()
    page.get_by_test_id("route-stops-all").click()
    expect(page.get_by_test_id("route-stops-count")).to_contain_text("已选 12 / 12")
    for pid in ("tuileries-garden", "palais-de-tokyo", "fondation-louis-vuitton", "picasso"):
        page.get_by_test_id(f"route-stop-{pid}").uncheck()
    fill_trip(page, "2026-12-17", "2026-12-19", arrive="09:00", leave="20:00")
    page.get_by_role("button", name=re.compile("生成行程")).click()
    page.wait_for_url(re.compile(r"/plans/paris-"), timeout=240_000)
    page.locator(".day").first.wait_for()
    titles = " ".join(page.locator(".timeline .item h4").all_inner_texts())
    assert "毕加索" not in titles and "杜乐丽" not in titles and "东京宫" not in titles
    assert "卢浮宫" in titles and "奥赛" in titles


def test_the_cover_shows_every_stop_of_the_route_not_one_card_per_day(page: Page):
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-19", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["art"], "theme": "art", "route": ART,
               "arrival_time": "2026-12-17 09:00", "departure_time": "2026-12-19 20:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    detail = r.json()["itinerary"]
    stops = {i["place_id"] for d in detail["days"] for i in d["items"] if i.get("place_id")}
    page.goto(f"{WEB}/plans/{r.json()['plan_id']}")
    page.locator(".day").first.wait_for()
    page.wait_for_timeout(1000)
    assert page.locator("[data-testid=cover-card]").count() == len(stops)


def test_a_long_trip_gets_theme_filler_days_after_the_route(page: Page):
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-23", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["art"], "theme": "art", "route": ART,
               "arrival_time": "2026-12-17 09:00", "departure_time": "2026-12-23 20:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    page.goto(f"{WEB}/plans/{r.json()['plan_id']}")
    page.locator(".day").first.wait_for()
    expect(page.locator(".day")).to_have_count(7)
    titles = page.locator(".day h3").all_inner_texts()
    assert sum("自由日" in t for t in titles) == 2
    expect(page.get_by_test_id("day-chapter")).to_have_count(5)             # 自由日整行就是标题，不再画一遍小标题


def test_generating_a_route_plan_shows_chapters_guides_look_for_reading_and_a_route_link(page: Page):
    open_new(page)
    start, end = fill_trip(page, "2026-12-17", "2026-12-19", arrive="09:00", leave="20:00")
    page.get_by_test_id(f"route-select-{ART}").click()
    page.get_by_role("button", name=re.compile("生成行程")).click()
    page.wait_for_url(re.compile(r"/tasks/"), timeout=15_000)
    page.wait_for_url(re.compile(r"/plans/paris-"), timeout=240_000)
    page.locator(".day").first.wait_for()
    expect(page.locator(".day")).to_have_count(3)
    chapters = page.get_by_test_id("day-chapter")
    expect(chapters.first).to_have_text(re.compile(r"^第 1(–2)? 章$"))  # 只写章号，章节名已经是这一天的标题
    assert chapters.count() == 3
    assert "今天的顺序" not in page.get_by_test_id("day-guide").first.inner_text()    # 顺序就是下面的时间轴
    assert page.get_by_test_id("why-here").count() >= 6                                  # 每一站的理由写在它自己下面
    assert page.get_by_test_id("look-for").count() >= 6                                  # 路线上的站都有「看什么」
    expect(page.get_by_test_id("route-reading")).to_contain_text("延伸阅读 / 观影")
    expect(page.get_by_test_id("route-reading")).to_contain_text("The Judgment of Paris")
    expect(page.get_by_test_id("route-deviated")).to_have_count(0)                        # 没改过：不提示偏离
    expect(page.locator("#sec-route")).to_be_visible()
    expect(page.locator("h1, h2").filter(has_text="艺术：从王室收藏到当代").first).to_be_visible()


def test_a_normal_plan_has_none_of_the_route_pieces(page: Page):
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-19", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["art"], "theme": "art",
               "arrival_time": "2026-12-17 10:30", "departure_time": "2026-12-19 18:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    page.goto(f"{WEB}/plans/{r.json()['plan_id']}")
    page.locator(".day").first.wait_for()
    for gone in ("day-chapter", "day-guide", "look-for", "route-reading", "route-deviated"):
        expect(page.get_by_test_id(gone)).to_have_count(0)


def test_editing_a_route_plan_shows_the_deviation_banner(page: Page):
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-19", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["art"], "theme": "art", "route": ART,
               "arrival_time": "2026-12-17 09:00", "departure_time": "2026-12-19 20:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    pid = r.json()["plan_id"]
    it = httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]
    victim = next(i for i in it["days"][0]["items"] if i.get("place_id"))
    it["days"][0]["items"] = [i for i in it["days"][0]["items"] if i is not victim]
    saved = httpx.post(f"{API}/plans/{pid}/versions", json={"itinerary": it, "name": "删一站", "note": ""}, timeout=60)
    assert saved.status_code == 201, saved.text
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    expect(page.get_by_test_id("route-deviated")).to_contain_text("偏离了路线")


def test_all_the_routes_are_offered_and_the_choice_is_exclusive(page: Page):
    open_new(page)
    expect(page.locator("[data-testid^=route-card-]")).to_have_count(6)
    others = (("fashion-couture-to-street", "时尚"), ("history-thought-medieval-to-existentialism", "历史与思想"),
              ("heritage-royal-to-iron", "古建筑与名胜"), ("gastronomy-market-to-cafe", "法式餐桌"),
              ("christmas-markets-notre-dame-to-alsace", "圣诞集市"))
    for rid, kind in others:
        expect(page.get_by_test_id(f"route-card-{rid}")).to_contain_text(kind)
        page.get_by_test_id(f"route-select-{rid}").click()
        expect(page.get_by_test_id(f"route-card-{rid}")).to_have_attribute("data-selected", "true")
        expect(page.get_by_test_id(f"route-card-{ART}")).to_have_attribute("data-selected", "false")   # 一次只选一条
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-19", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["fashion"], "theme": "fashion",
               "route": "fashion-couture-to-street", "arrival_time": "2026-12-17 09:00",
               "departure_time": "2026-12-19 20:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    page.goto(f"{WEB}/plans/{r.json()['plan_id']}")
    page.locator(".day").first.wait_for()
    expect(page.get_by_test_id("day-chapter").first).to_contain_text("第 1")
    expect(page.get_by_test_id("route-reading")).to_contain_text("The Ladies' Paradise")


def test_routes_are_grouped_by_interest_track_with_headings(page: Page):
    open_new(page)
    history = page.get_by_test_id("route-track-history")
    other = page.get_by_test_id("route-track-other")
    expect(history.locator(".route-track-title")).to_have_text("历史")
    expect(other.locator(".route-track-title")).to_have_text("其他")
    expect(history.locator("[data-testid^=route-card-]")).to_have_count(2)
    expect(history).to_contain_text("历史与思想")
    expect(history).to_contain_text("古建筑与名胜")
    expect(other.locator("[data-testid^=route-card-]")).to_have_count(2)
    folk = page.get_by_test_id("route-track-folk")
    expect(folk.locator(".route-track-title")).to_have_text("民俗体验")
    expect(folk.locator("[data-testid^=route-card-]")).to_have_count(1)
    food = page.get_by_test_id("route-track-food")
    expect(food.locator(".route-track-title")).to_have_text("美食")
    expect(food.locator("[data-testid^=route-card-]")).to_have_count(1)


def test_the_heritage_route_gives_versailles_a_day_of_its_own(page: Page):
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-20", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["history"], "theme": "museums",
               "route": "heritage-royal-to-iron", "arrival_time": "2026-12-17 09:00",
               "departure_time": "2026-12-20 20:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    page.goto(f"{WEB}/plans/{r.json()['plan_id']}")
    page.locator(".day").first.wait_for()
    expect(page.locator(".day")).to_have_count(4)
    trip_day = page.locator(".day").filter(has=page.locator(".item[data-pid='versailles']"))
    expect(trip_day).to_have_count(1)
    assert trip_day.locator(".item[data-pid]").count() == 1                      # 凡尔赛独占一天
    expect(trip_day.get_by_test_id("why-here")).to_contain_text("路易十四")


def test_the_gastronomy_route_opens_with_four_chapters_and_uses_lunch_places(page: Page):
    open_new(page)
    page.get_by_test_id("route-select-gastronomy-market-to-cafe").click()
    card = page.get_by_test_id("route-card-gastronomy-market-to-cafe")
    expect(card).to_contain_text("法式餐桌")
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-20", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["food"], "theme": "food",
               "route": "gastronomy-market-to-cafe", "arrival_time": "2026-12-17 10:00",
               "departure_time": "2026-12-20 18:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    page.goto(f"{WEB}/plans/{r.json()['plan_id']}")
    page.locator(".day").first.wait_for()
    expect(page.locator(".day")).to_have_count(4)
    lunch_item = page.locator(".item[data-pid='cafe-procope']").first
    expect(lunch_item).to_contain_text("这一站就是午餐")
    expect(lunch_item.locator("xpath=ancestor::*[contains(@class,'day')][1]")).not_to_contain_text("午餐（自行选餐厅）")
    expect(page.locator(".item[data-pid='enfants-rouges']")).to_have_count(0)           # 被大火烧毁的市场不排


def test_cafes_whose_hours_are_not_official_say_to_check_the_website(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-19", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["food"], "theme": "food", "must_go_ids": ["cafe-de-la-paix"],
               "arrival_time": "2026-12-17 10:00", "departure_time": "2026-12-19 18:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    place = httpx.get(f"{API}/places/cafe-de-la-paix", timeout=30).json()
    assert place["verification"]["hours_verified"] is False
    assert "官网" in place["note"]


# --------------------------------------------------------------------------- #
# F12–F14 民俗体验（规格 09 十八）：有日期的圣诞市场
# --------------------------------------------------------------------------- #
def test_a_december_shopping_trip_meets_the_notre_dame_market_with_an_honest_date_note(page: Page):
    found = None
    for start in ("2026-12-08", "2026-12-10", "2026-12-12"):
        payload = {"destination": "Paris", "start_date": start, "end_date": start[:-2] + f"{int(start[-2:]) + 2:02d}",
                   "budget_level": "medium", "travel_style": "balanced", "interests": ["shopping"], "theme": "shopping",
                   "arrival_time": f"{start} 10:00", "departure_time": f"{start[:-2]}{int(start[-2:]) + 2:02d} 18:00"}
        r = httpx.post(f"{API}/plans", json=payload, timeout=240)
        assert r.status_code == 201, r.text
        pid = r.json()["plan_id"]
        items = [i for d in httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]["days"] for i in d["items"]]
        if any(i.get("place_id") == "paris-notre-dame-christmas-market" for i in items):
            found = pid
            break
    assert found, "12 月上旬的购物行程里应该会在晚上遇到圣母院圣诞市场"
    page.goto(f"{WEB}/plans/{found}")
    page.locator(".day").first.wait_for()
    market = page.locator(".item[data-pid='paris-notre-dame-christmas-market']").first
    expect(market).to_be_visible()
    market.get_by_test_id("item-detail").click()
    expect(page.get_by_test_id("place-season")).to_contain_text("2026-11-26 至 2026-12-27")
    expect(page.get_by_test_id("place-season")).to_contain_text("出发前请确认")


def test_the_same_trip_in_march_has_no_christmas_market(page: Page):
    payload = {"destination": "Paris", "start_date": "2027-03-08", "end_date": "2027-03-10", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["shopping"], "theme": "shopping",
               "arrival_time": "2027-03-08 10:00", "departure_time": "2027-03-10 18:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    page.goto(f"{WEB}/plans/{r.json()['plan_id']}")
    page.locator(".day").first.wait_for()
    expect(page.locator(".item[data-pid$='christmas-market']")).to_have_count(0)


def test_the_christmas_markets_route_is_offered_under_folk_and_fills_a_december_day(page: Page):
    open_new(page)
    expect(page.get_by_test_id("route-track-folk")).to_contain_text("巴黎的圣诞集市")
    payload = {"destination": "Paris", "start_date": "2026-12-16", "end_date": "2026-12-18", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["shopping"], "theme": "shopping",
               "route": "christmas-markets-notre-dame-to-alsace", "arrival_time": "2026-12-16 10:00",
               "departure_time": "2026-12-18 18:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    page.goto(f"{WEB}/plans/{r.json()['plan_id']}")
    page.locator(".day").first.wait_for()
    expect(page.locator(".item[data-pid='paris-alsace-christmas-market']")).to_have_count(1)
    expect(page.locator(".item[data-pid='paris-notre-dame-christmas-market']")).to_have_count(1)


# --------------------------------------------------------------------------- #
# F15 / F16 限定路线：卡片标「限定」，日期不在举办期就提示
# --------------------------------------------------------------------------- #
def test_a_limited_route_is_marked_and_warns_only_when_the_dates_are_outside_its_window(page: Page):
    open_new(page)
    rid = "christmas-markets-notre-dame-to-alsace"
    expect(page.get_by_test_id(f"route-limited-{rid}")).to_have_text("限定")
    expect(page.get_by_test_id(f"route-season-{rid}")).to_contain_text("2026 年 11 月 26 日 – 12 月 27 日")
    for other in ("heritage-royal-to-iron", "gastronomy-market-to-cafe", ART):
        expect(page.get_by_test_id(f"route-limited-{other}")).to_have_count(0)
        expect(page.get_by_test_id(f"route-season-warn-{other}")).to_have_count(0)
    page.get_by_label("开始日期").fill("2026-12-16")
    page.get_by_label("离开日期").fill("2026-12-18")
    expect(page.get_by_test_id(f"route-season-warn-{rid}")).to_have_count(0)               # 在举办期里：不提示
    page.get_by_label("开始日期").fill("2027-03-08")
    page.get_by_label("离开日期").fill("2027-03-10")
    warn = page.get_by_test_id(f"route-season-warn-{rid}")
    expect(warn).to_contain_text("当前日期没有这个活动")
    expect(warn).to_contain_text("2026 年 11 月 26 日 – 12 月 27 日")
    expect(page.get_by_test_id(f"route-select-{rid}")).to_be_enabled()                     # 只是提示，不拦
