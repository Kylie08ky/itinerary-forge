"""美食的前端验收（规格 09 十七，场景 F1–F5）：场所的招牌织进行程，参考页多一节「美食」。

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_food_page.py
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
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def make_plan(theme: str, must_go: list[str]) -> str:
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-17", "budget_level": "medium",
               "travel_style": "balanced", "interests": [theme], "theme": theme, "must_go_ids": must_go,
               "arrival_time": "2026-12-17 09:00", "departure_time": "2026-12-17 20:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


@pytest.fixture(scope="module")
def pid() -> str:
    return make_plan("shopping", ["angelina", "laduree-champs"])


def go_plan(page: Page, plan_id: str) -> None:
    page.goto(f"{WEB}/plans/{plan_id}")
    page.locator(".day").first.wait_for()


def item(page: Page, place_id: str):
    return page.locator(f".item[data-pid='{place_id}']").first


# --------------------------------------------------------------------------- #
# F1 / F2 行程条目和地点卡
# --------------------------------------------------------------------------- #
def test_an_item_with_a_signature_shows_it_under_the_item(page: Page, pid):
    go_plan(page, pid)
    stories = item(page, "angelina").get_by_test_id("item-stories")
    expect(stories).to_be_visible()
    expect(stories).to_contain_text("招牌：蒙布朗")
    expect(stories).to_contain_text("招牌：热巧克力")
    assert stories.locator("li").count() <= 3                                    # 最多 2 条 + 一行「还有 N 条」
    expect(item(page, "laduree-champs").get_by_test_id("item-stories")).to_contain_text("招牌：马卡龙")


def test_the_place_card_lists_the_signature_with_a_source(page: Page, pid):
    go_plan(page, pid)
    item(page, "laduree-champs").get_by_test_id("item-detail").click()
    card = page.get_by_test_id("card-stories")
    expect(card).to_be_visible()
    expect(card).to_contain_text("招牌：马卡龙")
    expect(card).to_contain_text("1862")
    href = card.get_by_role("link", name=re.compile("来源")).first.get_attribute("href")
    assert href and href.startswith("https://en.wikipedia.org/")


# --------------------------------------------------------------------------- #
# F3 分享页
# --------------------------------------------------------------------------- #
def test_the_shared_page_shows_the_signature_too(page: Page, pid):
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=30).json()["token"]
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    expect(page.get_by_test_id("item-stories").first).to_contain_text("招牌")


# --------------------------------------------------------------------------- #
# F4 参考页的「美食」一节
# --------------------------------------------------------------------------- #
def test_the_reference_page_has_a_food_section_with_sources_and_only_real_places(page: Page):
    page.goto(f"{WEB}/culture")
    page.get_by_test_id("culture-kind-food").click()
    macaron = page.get_by_test_id("food-dish-macaron")
    expect(macaron).to_contain_text("马卡龙")
    expect(macaron).to_contain_text("Macaron")
    expect(page.get_by_test_id("food-place-macaron-laduree-champs")).to_contain_text("1862")
    href = macaron.get_by_role("link", name=re.compile("来源")).get_attribute("href")
    assert href and href.startswith("https://en.wikipedia.org/")
    expect(page.get_by_test_id("food-dish-croissant")).to_be_visible()
    expect(page.get_by_test_id("food-dish-croissant").locator(".culture-places")).to_have_count(0)  # 没出处不写去哪吃
    expect(page.get_by_test_id("culture-work-midnight-in-paris")).to_have_count(0)                    # 作品不混进来


# --------------------------------------------------------------------------- #
# F5 没有招牌的行程
# --------------------------------------------------------------------------- #
def test_a_plan_without_any_signature_has_no_signature_wording(page: Page):
    plan_id = make_plan("museums", ["louvre", "orsay"])
    detail = httpx.get(f"{API}/plans/{plan_id}", timeout=30).json()
    if any(s["kind"] == "food" for ss in detail["stories"].values() for s in ss):
        pytest.skip("这份博物馆行程刚好经过有招牌的场所")
    go_plan(page, plan_id)
    expect(page.get_by_text("招牌")).to_have_count(0)


# --------------------------------------------------------------------------- #
# F6 / F7 / F8 用餐的站进排程（第 2 步）
# --------------------------------------------------------------------------- #
def test_a_market_that_is_lunch_replaces_the_generic_lunch_and_says_so(page: Page):
    plan_id = make_plan("food", ["cafe-procope"])
    go_plan(page, plan_id)
    market = item(page, "cafe-procope")
    expect(market).to_contain_text("这一站就是午餐")
    expect(page.locator(".day").first).not_to_contain_text("午餐（自行选餐厅）")
    expect(page.locator(".day").first).not_to_contain_text("午餐（简餐）")


def test_the_burnt_down_market_is_not_in_a_food_plan(page: Page):
    # 红孩子市场 2026-10-12 被大火烧毁，地点库标了长期闭馆；美食行程里不排它，午饭另有安排
    plan_id = make_plan("food", [])
    go_plan(page, plan_id)
    expect(page.locator(".item[data-pid='enfants-rouges']")).to_have_count(0)
    expect(page.locator(".day").first).to_contain_text("午餐")


def test_a_tea_room_is_the_afternoon_stop_and_says_so(page: Page):
    plan_id = make_plan("food", ["angelina"])
    go_plan(page, plan_id)
    tea = item(page, "angelina")
    expect(tea).to_contain_text("这一站就是下午茶")
    items = httpx.get(f"{API}/plans/{plan_id}", timeout=30).json()["itinerary"]["days"][0]["items"]
    assert next(i["time"] for i in items if i.get("place_id") == "angelina") >= "14:30"


# --------------------------------------------------------------------------- #
# F17 / F18 午餐建议（规格 09 十七之五；F19 重排后重算由 tests/test_food.py 覆盖）
# --------------------------------------------------------------------------- #
def make_trip(theme: str, must_go: list[str], days: int = 4) -> str:
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": f"2026-12-{16 + days}",
               "budget_level": "medium", "travel_style": "balanced", "interests": [theme], "theme": theme,
               "must_go_ids": must_go, "arrival_time": "2026-12-17 10:00",
               "departure_time": f"2026-12-{16 + days} 18:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=240)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def lunch_row(page: Page, plan_id: str, place_id: str):
    """含指定景点那一天的「午餐（自行选餐厅）」行。"""
    go_plan(page, plan_id)
    day = page.locator(".day", has=page.locator(f".item[data-pid='{place_id}']")).first
    return day.locator(".item", has_text="午餐（自行选餐厅）").first


def test_the_lunch_row_names_a_nearby_checked_place_with_distance_and_hours(page: Page):
    row = lunch_row(page, make_trip("art", ["orsay"]), "orsay")
    expect(row).to_contain_text("附近可以去：")
    expect(row).to_contain_text("普罗可布咖啡馆")
    expect(row).to_contain_text(re.compile(r"约 \d+ 米"))
    expect(row).to_contain_text("12:00–24:00")


def test_the_lunch_row_far_from_every_lunch_place_keeps_the_plain_note(page: Page):
    row = lunch_row(page, make_trip("photography", ["pere-lachaise"]), "pere-lachaise")
    expect(row).to_contain_text("具体餐厅请自行选择")
    expect(row).not_to_contain_text("附近可以去")
