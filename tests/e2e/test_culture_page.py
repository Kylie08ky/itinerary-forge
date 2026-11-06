"""作品与故事的前端验收（规格 09 十五，场景 C1–C8）：故事织进行程，「了解巴黎」降级成参考页。

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_culture_page.py
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
HISTORY = "history-thought-medieval-to-existentialism"
PARIS_WINTER = timezone(timedelta(hours=1))


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


@pytest.fixture(scope="module")
def plan() -> dict:
    """一份历史与思想路线的行程（圣母院、先贤祠等都有故事）。"""
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-19", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["history"], "theme": "museums", "route": HISTORY,
               "arrival_time": "2026-12-17 09:00", "departure_time": "2026-12-19 20:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    pid = r.json()["plan_id"]
    return {"pid": pid, **httpx.get(f"{API}/plans/{pid}", timeout=30).json()}


def go_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()


def item_with(page: Page, place_id: str):
    return page.locator(f".item[data-pid='{place_id}']").first


# --------------------------------------------------------------------------- #
# C1 / C2 行程条目和地点卡
# --------------------------------------------------------------------------- #
def test_an_item_whose_place_appears_in_a_work_shows_the_story_without_any_choice(page: Page, plan):
    go_plan(page, plan["pid"])
    stories = item_with(page, "notre-dame").get_by_test_id("item-stories")
    expect(stories).to_be_visible()
    expect(stories).to_contain_text("巴黎圣母院")
    assert stories.locator("li").count() <= 3                                     # 最多 2 条 + 一行「还有 N 条」
    # 没有任何故事的地点不出现空块
    plain = [i for i in plan["itinerary"]["days"][0]["items"]
             if i.get("place_id") and i["place_id"] not in plan["stories"]]
    for it in plain[:1]:
        expect(item_with(page, it["place_id"]).get_by_test_id("item-stories")).to_have_count(0)


def test_the_place_card_lists_every_story_with_its_creator_and_a_source(page: Page, plan):
    go_plan(page, plan["pid"])
    item_with(page, "notre-dame").get_by_test_id("item-detail").click()
    card = page.get_by_test_id("card-stories")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Victor Hugo")
    expect(card).to_contain_text("午夜巴黎")
    href = card.get_by_role("link", name=re.compile("来源")).first.get_attribute("href")
    assert href and href.startswith("https://")


# --------------------------------------------------------------------------- #
# C3 这趟旅行里的作品
# --------------------------------------------------------------------------- #
def test_the_recap_is_collapsed_lists_the_works_on_the_route_and_links_to_the_reference_page(page: Page, plan):
    go_plan(page, plan["pid"])
    recap = page.get_by_test_id("culture-recap")
    expect(recap).to_be_visible()
    assert recap.get_attribute("open") is None                                  # 默认收起，不打扰
    recap.locator("summary").click()
    work = page.get_by_test_id("recap-work-hunchback")
    expect(work).to_contain_text("你会经过")
    expect(work).to_contain_text("圣母院")
    page.get_by_test_id("culture-more").click()
    expect(page).to_have_url(re.compile(r"/culture$"))


def test_a_plan_without_stories_has_no_recap_and_no_story_blocks(page: Page):
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-17", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["shopping"], "theme": "shopping",
               "arrival_time": "2026-12-17 09:00", "departure_time": "2026-12-17 20:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    detail = httpx.get(f"{API}/plans/{r.json()['plan_id']}", timeout=30).json()
    if detail.get("stories"):
        pytest.skip("这份购物行程刚好经过有故事的地点")
    go_plan(page, r.json()["plan_id"])
    expect(page.get_by_test_id("culture-recap")).to_have_count(0)
    expect(page.get_by_test_id("item-stories")).to_have_count(0)


# --------------------------------------------------------------------------- #
# C4 今日页
# --------------------------------------------------------------------------- #
def test_today_shows_the_story_of_the_current_stop(browser, plan):
    chosen = next(((d["date"], i) for d in plan["itinerary"]["days"] for i in d["items"]
                   if i.get("place_id") in plan["stories"]), None)
    if chosen is None:
        pytest.skip("行程里没有有故事的地点")
    date, item = chosen
    h, m = map(int, item["time"].split(":"))
    when = datetime.fromisoformat(date).replace(hour=h, minute=m, tzinfo=PARIS_WINTER) + timedelta(minutes=5)
    ctx = browser.new_context(viewport={"width": 1280, "height": 1400}, locale="zh-CN", timezone_id="Asia/Shanghai")
    page = ctx.new_page()
    page.clock.set_fixed_time(when)
    try:
        go_plan(page, plan["pid"])
        expect(page.get_by_test_id("today-now")).to_contain_text(item["title"])
        expect(page.get_by_test_id("today-now").get_by_test_id("item-stories")).to_be_visible()
    finally:
        ctx.close()


# --------------------------------------------------------------------------- #
# C6 分享页
# --------------------------------------------------------------------------- #
def test_the_shared_read_only_page_shows_the_stories_too(page: Page, plan):
    token = httpx.post(f"{API}/plans/{plan['pid']}/share", timeout=30).json()["token"]
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    expect(page.get_by_test_id("item-stories").first).to_be_visible()


# --------------------------------------------------------------------------- #
# C7 降级：没有导航入口、新建页不再有「带进行程」
# --------------------------------------------------------------------------- #
def test_the_culture_page_is_a_reference_page_not_a_step(page: Page):
    page.goto(f"{WEB}/")
    page.get_by_test_id("home-hero").wait_for()
    expect(page.get_by_role("link", name="文化入门")).to_have_count(0)
    expect(page.get_by_role("link", name="了解巴黎")).to_have_count(0)
    page.goto(f"{WEB}/new?work=midnight-in-paris")                                # 老链接：什么都不会发生
    page.get_by_label("开始日期").wait_for()
    expect(page.get_by_test_id("work-banner")).to_have_count(0)
    expect(page.get_by_test_id("to-culture")).to_have_count(0)
    page.goto(f"{WEB}/culture")
    page.get_by_test_id("culture-work-midnight-in-paris").wait_for()
    expect(page.get_by_test_id("culture-intro")).to_contain_text("自动提示")
    expect(page.get_by_test_id("culture-plan-midnight-in-paris")).to_have_count(0)


def test_the_reference_page_shows_works_with_places_notes_and_checkable_sources(page: Page):
    page.goto(f"{WEB}/culture")
    w = page.get_by_test_id("culture-work-midnight-in-paris")
    w.wait_for()
    expect(w).to_contain_text("午夜巴黎")
    expect(w).to_contain_text("Woody Allen · 2011")
    expect(page.get_by_test_id("culture-place-midnight-in-paris-notre-dame")).to_contain_text("约翰二十三世广场")
    expect(page.get_by_test_id("culture-routes-midnight-in-paris")).to_contain_text("历史与思想")
    href = w.get_by_role("link", name=re.compile("来源")).get_attribute("href")
    assert href and href.startswith("https://en.wikipedia.org/")
    expect(page.get_by_test_id("culture-podcast-note")).to_have_count(0)  # 没有的类型不写说明，筛选里也没有
    expect(page.get_by_test_id("culture-kind-podcast")).to_have_count(0)
    page.get_by_test_id("culture-kind-book").click()
    expect(page.get_by_test_id("culture-work-hunchback")).to_be_visible()
    expect(page.get_by_test_id("culture-work-midnight-in-paris")).to_have_count(0)
