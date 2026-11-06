"""P18 每个景点「中文名 · 当地语言名」的前端验收（规格 07 P18）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_p18_local_names.py
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
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def new_plan(theme="museums") -> str:
    body = {"destination": "Paris", "start_date": "2026-10-28", "end_date": "2026-10-31", "budget_level": "medium",
            "travel_style": "balanced", "interests": [theme], "theme": theme,
            "arrival_time": "2026-10-28 10:30", "departure_time": "2026-10-31 18:00"}
    r = httpx.post(f"{API}/plans", json=body, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def skip_cover(page: Page) -> None:
    page.get_by_role("button", name=re.compile("跳过封面")).click()


def test_the_place_card_title_has_the_chinese_name_then_the_local_name_on_one_line(page: Page):
    pid = new_plan()
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for(state="attached")
    skip_cover(page)
    page.locator(".item .title-link").first.click()
    card = page.get_by_role("dialog", name="地点详情")
    expect(card).to_be_visible()
    title = card.locator("h2")
    local = card.get_by_test_id("place-local-name")
    expect(local).to_have_count(1)
    expect(title).to_contain_text(re.compile(r"\S+ · \S+"))
    assert local.inner_text().strip().startswith("·")


def test_the_second_line_of_a_timeline_item_is_the_local_name_not_english(page: Page):
    pid = new_plan("art")
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for(state="attached")
    skip_cover(page)
    plan = httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]
    placed = [i for d in plan["days"] for i in d["items"] if i.get("place_id")]
    assert placed
    places = {p["id"]: p for p in httpx.get(f"{API}/places", timeout=30).json()}
    item = placed[0]
    local = places[item["place_id"]]["name_local"]
    assert local and item["location"].startswith(local)
    expect(page.locator(".timeline .item").filter(has_text=item["title"]).first).to_contain_text(local)


def test_search_finds_a_place_by_its_french_name_and_shows_both_names(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    page.locator(".theme-btn").filter(has=page.locator(".theme-label", has_text=re.compile("^艺术$"))).click()
    page.locator(".pool > summary").click()
    page.get_by_label("搜索地点库").fill("musée d'orsay")
    hit = page.locator(".pool-hit").first
    expect(hit).to_contain_text("奥赛")
    expect(hit).to_contain_text("Orsay")


def test_the_map_bubble_has_the_local_name_on_a_second_line(page: Page):
    pid = new_plan()
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for(state="attached")
    skip_cover(page)
    map_ = page.locator(".plan-aside .fullmap")
    map_.wait_for()
    page.locator("button.lg", has_text=re.compile("第 2 天")).click()                  # 只看一天，标记不会被聚合成圈
    pin = map_.locator(".map-pin").first
    pin.wait_for()
    pin.hover()
    bubble = page.locator(".leaflet-tooltip .map-bubble")
    expect(bubble).to_be_visible()
    expect(bubble.locator(".bubble-local")).to_have_count(1)
