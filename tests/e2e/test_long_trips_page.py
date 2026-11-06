"""长行程的前端验收（规格 07 Q5，场景 G2 / G4；G1 在 test_q3_form.py）。

    .venv\Scripts\python -m pytest -m e2e tests/e2e/test_long_trips_page.py
"""

from __future__ import annotations

import os
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
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def make_trip(theme: str, days: int) -> str:
    start = date(2026, 12, 12)
    end = start + timedelta(days=days - 1)
    payload = {"destination": "Paris", "start_date": start.isoformat(), "end_date": end.isoformat(),
               "budget_level": "medium", "travel_style": "balanced", "interests": [theme], "theme": theme,
               "arrival_time": f"{start} 10:00", "departure_time": f"{end} 18:00"}
    r = httpx.post(f"{API}/plans", json=payload, timeout=300)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def open_plan(page: Page, plan_id: str) -> list[dict]:
    page.goto(f"{WEB}/plans/{plan_id}")
    page.locator(".day").first.wait_for()
    return httpx.get(f"{API}/plans/{plan_id}", timeout=30).json()["itinerary"]["days"]


def open_new(page: Page) -> None:
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()


def test_g2_a_trip_longer_than_a_week_defaults_to_the_relaxed_pace_until_you_choose_one(page: Page):
    open_new(page)
    style = page.get_by_label("节奏")
    page.get_by_label("开始日期").fill("2026-11-10")
    page.get_by_label("离开日期").fill("2026-11-13")
    expect(style).to_have_value("balanced")
    page.get_by_label("离开日期").fill("2026-11-20")                      # 11 天
    expect(style).to_have_value("relaxed")
    page.get_by_label("离开日期").fill("2026-11-12")
    expect(style).to_have_value("balanced")                               # 缩短回去，默认也跟着回来
    style.select_option("intense")                                         # 自己选了就不再自动换
    page.get_by_label("离开日期").fill("2026-11-25")
    expect(style).to_have_value("intense")


def test_g2_the_cover_cards_show_the_whole_place_name(page: Page):
    plan_id = make_trip("art", 6)
    page.set_viewport_size({"width": 1280, "height": 1000})
    page.goto(f"{WEB}/plans/{plan_id}")
    page.locator(".day").first.wait_for()
    cut = page.evaluate("""() => [...document.querySelectorAll('.cover-card h3')]
        .filter(h => h.scrollHeight > h.clientHeight + 8).map(h => h.textContent)""")
    assert not cut, f"封面卡片的标题被截掉了：{cut}"


def test_g4_a_trip_longer_than_the_library_can_fill_still_opens_and_shows_free_days(page: Page):
    days = open_plan(page, make_trip("entertainment", 30))
    free = [d for d in days if d["theme"] == "自由安排"]
    assert page.locator(".day").count() == 30
    if free:
        expect(page.locator(".day h3", has_text="自由安排")).to_have_count(len(free))
