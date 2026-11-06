"""Q2 信息架构的前端验收（specs/07-polish-backlog.md P01–P05）：首页 / 、新建行程 /new、我的行程 /plans。

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_q2_pages.py

行程列表用固定数据（拦截 /plan-cards），不依赖库里已有的行程；日期都相对「今天」。
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
SENTENCE = "10月28日去巴黎玩4天，两个人，喜欢艺术，想去奥赛博物馆"


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
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def paris_hero() -> dict:
    return next(c for c in httpx.get(f"{API}/cities", timeout=10).json() if c["id"] == "paris")["hero"]


def card(pid: str, start: date, days: int = 4, *, hero: dict | None = None, theme: str = "art") -> dict:
    return {"plan_id": pid, "title": "巴黎 · 艺术", "theme_id": theme, "theme_name": "艺术",
            "start_date": start.isoformat(), "end_date": (start + timedelta(days=days - 1)).isoformat(), "days": days,
            "latest_version": 1, "created_at": "2026-10-01T00:00:00+00:00", "errors": 0, "warnings": 0,
            "narration": "template", "archived": False, "copied_from": None, "city_id": "paris", "hero": hero}


def serve_cards(page: Page, cards: list[dict], archived: list[dict] | None = None) -> None:
    def handler(route):
        route.fulfill(json=(archived or []) if "archived=true" in route.request.url else cards)

    page.route(re.compile(r".*/plan-cards.*"), handler)


TODAY = date.today()


# ---- P01 / P02：首页 ----
def test_the_landing_page_is_not_tied_to_paris_text_and_has_no_form(page: Page):
    serve_cards(page, [])
    page.goto(WEB)
    page.get_by_test_id("home-hero").wait_for()
    page.evaluate("document.querySelector('[data-testid=hero-credit]')?.remove()")        # 摄影师的名字里可能有 Paris
    text = page.locator("body").inner_text()
    for word in ("巴黎", "Paris", "PARIS"):
        # 没有行程时页面上没有写死的城市字样
        assert word not in text, (word, text[:300])
    expect(page.locator(".step-card")).to_have_count(0)                              # 首页没有表单了
    expect(page.get_by_test_id("nl-card")).to_be_visible()
    expect(page.get_by_test_id("next-trip")).to_have_count(0)                        # 没有行程：英雄下面什么都没有
    expect(page.get_by_test_id("upcoming-trips")).to_have_count(0)


def test_with_no_trips_the_hero_is_a_city_landmark_with_a_credit(page: Page):
    serve_cards(page, [])
    page.goto(WEB)
    photo = page.get_by_test_id("hero-photo")
    photo.wait_for()
    expect(page.get_by_test_id("hero-credit")).to_contain_text("摄影：")
    assert paris_hero()["url"] in photo.evaluate("e => getComputedStyle(e).backgroundImage")


def test_with_a_next_trip_the_hero_is_that_trips_city_photo(page: Page):
    other = {"place_id": "louvre", **httpx.get(f"{API}/places/louvre", timeout=10).json()["media"]["hero"]}
    # 和「没有行程时」的默认大图不是同一张
    assert other["url"] != paris_hero()["url"]
    serve_cards(page, [card("p-next", TODAY + timedelta(days=5), hero=other)])
    page.goto(WEB)
    page.get_by_test_id("hero-photo").wait_for()
    assert other["url"] in page.get_by_test_id("hero-photo").evaluate("e => getComputedStyle(e).backgroundImage")
    expect(page.get_by_test_id("next-trip")).to_be_visible()
    link = page.get_by_test_id("next-trip").get_by_role("link", name="查看行程")
    expect(link).to_have_attribute("href", "/plans/p-next")


def test_the_landing_page_shows_the_nearest_trip_and_at_most_three_upcoming_cards(page: Page):
    cards = [card(f"p{n}", TODAY + timedelta(days=n)) for n in (40, 3, 12, 25, 7)]
    serve_cards(page, cards)
    page.goto(WEB)
    section = page.get_by_test_id("upcoming-trips")
    section.wait_for()
    hrefs = section.locator("a.plan-card").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert hrefs == ["/plans/p3", "/plans/p7", "/plans/p12"]                           # 最近的三个，近的在前
    expect(page.get_by_test_id("next-trip").get_by_role("link", name="查看行程")).to_have_attribute("href", "/plans/p3")


def test_city_entries_only_show_when_more_than_one_city_is_supported(page: Page):
    serve_cards(page, [card("p1", TODAY + timedelta(days=5))])
    page.goto(WEB)
    page.get_by_test_id("upcoming-trips").wait_for()
    expect(page.get_by_test_id("city-entries")).to_have_count(0)            # 完整支持的只有巴黎（一日游目的地不算）
    cities = httpx.get(f"{API}/cities", timeout=10).json()
    paris = next(c for c in cities if c["id"] == "paris")
    two = [*cities, {**paris, "id": "beijing", "name_zh": "北京"}]
    page.route(re.compile(r".*/cities$"), lambda r: r.fulfill(json=two))
    page.reload()
    entries = page.get_by_test_id("city-entries")
    entries.wait_for()
    expect(entries).to_contain_text("北京")


# ---- P04：首页 → 新建行程 ----
def test_a_sentence_on_the_landing_page_opens_the_new_plan_page_with_the_form_filled(page: Page):
    serve_cards(page, [])
    page.goto(WEB)
    page.get_by_test_id("nl-text").fill(SENTENCE)
    page.get_by_test_id("nl-go").click()
    page.wait_for_url(re.compile(r"/new$"))
    expect(page.get_by_test_id("nl-result")).to_be_visible(timeout=60_000)           # 带着这句话自动解析
    expect(page.get_by_test_id("nl-understood")).to_contain_text("必去：奥赛博物馆")
    expect(page.get_by_test_id("nl-text")).to_have_value(SENTENCE)
    assert page.get_by_label("开始日期").input_value().endswith("-10-28")
    expect(page.get_by_role("button", name="生成行程")).to_be_visible()               # 不会自动生成：仍在新建页
    assert re.search(r"/new$", page.url)
    page.go_back()                                                                    # 后退回首页
    expect(page).to_have_url(re.compile(rf"{re.escape(WEB)}/?$"))
    page.reload()                                                                     # 刷新新建页不会重复解析
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    expect(page.get_by_test_id("nl-result")).to_have_count(0)


def test_the_import_button_on_the_landing_page_opens_the_import_dialog_on_the_new_page(page: Page):
    serve_cards(page, [])
    page.goto(WEB)
    page.get_by_test_id("nl-import").click()
    page.wait_for_url(re.compile(r"/new$"))
    expect(page.get_by_role("dialog", name="从笔记导入")).to_be_visible()


def test_the_new_plan_page_is_a_plain_form_with_its_own_sentence_card(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    expect(page.get_by_test_id("home-hero")).to_have_count(0)                         # 没有英雄大图
    expect(page.get_by_test_id("nl-card")).to_be_visible()
    expect(page.get_by_role("navigation", name="主导航").locator("a.active")).to_have_text(re.compile("新建行程"))
    expect(page.get_by_test_id("nl-result")).to_have_count(0)                         # 直接打开：空白（默认值）表单


# ---- P03 / P05：我的行程 ----
def test_my_plans_are_grouped_and_sorted_by_time_with_countdown_covers(page: Page):
    cards = [
        card("up-far", TODAY + timedelta(days=30)), card("up-near", TODAY + timedelta(days=3)),
        card("tomorrow", TODAY + timedelta(days=1)), card("on-now", TODAY - timedelta(days=1)),
        card("past-old", TODAY - timedelta(days=60)), card("past-new", TODAY - timedelta(days=10)),
    ]
    serve_cards(page, cards)
    page.goto(f"{WEB}/plans")
    page.get_by_test_id("group-upcoming").wait_for()
    up = page.get_by_test_id("group-upcoming").locator("a.plan-card").evaluate_all(
        "els => els.map(e => e.getAttribute('href').split('/').pop())")
    assert up == ["on-now", "tomorrow", "up-near", "up-far"]                         # 进行中最前，其余按出发日从近到远
    past = page.get_by_test_id("group-past").locator("a.plan-card").evaluate_all(
        "els => els.map(e => e.getAttribute('href').split('/').pop())")
    assert past == ["past-new", "past-old"]                                           # 已结束：从新到旧
    def label(pid: str) -> str:
        return page.locator(f'a.plan-card[href$="/{pid}"] [data-testid=plan-countdown]').inner_text()

    assert label("on-now") == "进行中 · 第 2 天" and label("tomorrow") == "明天出发"
    assert label("up-near") == "还有 3 天" and label("up-far") == "还有 30 天"
    assert label("past-new") == "已结束 7 天前" and label("past-old") == "已结束 57 天前"


def test_old_trips_collapse_after_six_and_cards_hide_developer_labels(page: Page):
    cards = [card(f"old{n}", TODAY - timedelta(days=20 + n)) for n in range(8)]
    cards[0] = {**cards[0], "warnings": 2}
    serve_cards(page, cards)
    page.goto(f"{WEB}/plans")
    past = page.get_by_test_id("group-past")
    past.wait_for()
    assert past.locator(":scope > .plan-grid a.plan-card").count() == 6                # 默认只展开最近 6 条
    more = past.locator("details.past-more")
    expect(more.locator("summary")).to_contain_text("更多已结束的行程（2）")
    assert more.get_attribute("open") is None
    more.locator("summary").click()
    assert more.locator("a.plan-card").count() == 2
    body = page.locator("body").inner_text()
    for dev_label in ("质检通过", "模板说明"):
        assert dev_label not in body, dev_label
    expect(page.locator("a.plan-card").filter(has_text="2 条提醒")).to_have_count(1)  # 有问题才显示提醒


def test_a_plan_card_cover_is_the_trips_landmark_photo_with_a_dark_scrim(page: Page):
    serve_cards(page, [card("with-photo", TODAY + timedelta(days=9), hero=paris_hero()),
                       card("no-photo", TODAY + timedelta(days=10))])
    page.goto(f"{WEB}/plans")
    page.get_by_test_id("group-upcoming").wait_for()
    with_photo = page.locator('a.plan-card[href$="/with-photo"] .plan-cover')
    page.wait_for_function(
        "() => document.querySelector('a.plan-card .plan-cover').classList.contains('has-photo')")
    bg = with_photo.evaluate("e => getComputedStyle(e).backgroundImage")
    assert "/media/hero/" in bg and "linear-gradient" in bg                           # 大图 + 压暗
    plain = page.locator('a.plan-card[href$="/no-photo"] .plan-cover')
    # 没有大图：主题渐变
    assert "/media/hero/" not in plain.evaluate("e => getComputedStyle(e).backgroundImage")
    expect(plain.locator(".cover-icon")).to_have_count(1)
    expect(with_photo.locator("[data-testid=plan-countdown]")).to_have_text("还有 9 天")


def test_the_new_button_goes_to_the_new_plan_page_and_an_empty_list_says_so(page: Page):
    serve_cards(page, [])
    page.goto(f"{WEB}/plans")
    expect(page.get_by_text("还没有行程")).to_be_visible()
    page.get_by_role("link", name="新建行程").first.click()
    expect(page).to_have_url(f"{WEB}/new")


def test_on_a_phone_the_landing_and_plans_pages_do_not_scroll_sideways(browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN", is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    try:
        serve_cards(pg, [card("a", TODAY + timedelta(days=4)), card("b", TODAY - timedelta(days=30))])
        for path in ("/", "/plans", "/new"):
            pg.goto(WEB + path)
            pg.wait_for_timeout(1200)
            assert pg.evaluate("document.documentElement.scrollWidth") <= 390, path
    finally:
        ctx.close()


def test_on_a_phone_the_tab_bar_has_a_home_tab_that_goes_to_the_landing_page(browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN", is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    try:
        serve_cards(pg, [])
        pg.goto(f"{WEB}/plans")
        bar = pg.get_by_role("navigation", name="底部导航")
        labels = [t.strip() for t in bar.locator("a").all_inner_texts()]
        assert labels[0] == "首页" and "新建" in labels and "我的行程" in labels, labels
        small = bar.locator("a").evaluate_all("els => els.filter(e => e.getBoundingClientRect().height < 43.5).length")
        assert small == 0                                                            # 点击区域够大
        bar.get_by_role("link", name="首页").click()
        expect(pg).to_have_url(re.compile(rf"{re.escape(WEB)}/?$"))
        expect(bar.locator("a.active")).to_have_text(re.compile("首页"))
        bar.get_by_role("link", name="新建").click()
        expect(pg).to_have_url(f"{WEB}/new")
        # 在 /new 时高亮的是「新建」，不是「首页」
        expect(bar.locator("a.active")).to_have_text(re.compile("新建"))
        assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    finally:
        ctx.close()
