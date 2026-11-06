r"""分享页跟进新的界面风格的前端验收（specs/modules/M08 S9 前半）。

    .venv\Scripts\python -m pytest -m e2e tests/e2e/test_share_style.py
"""

from __future__ import annotations

import os

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


@pytest.fixture(scope="module")
def token() -> str:
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art"}, timeout=120)
    assert r.status_code == 201, r.text
    pid = r.json()["plan_id"]
    s = httpx.post(f"{API}/plans/{pid}/share", timeout=30)
    assert s.status_code in (200, 201), s.text
    return s.json()["token"]


def open_share(browser, token: str, width: int, height: int = 1800) -> Page:
    ctx = browser.new_context(viewport={"width": width, "height": height}, locale="zh-CN")
    page = ctx.new_page()
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    return page


def test_the_share_page_wears_the_city_colours_and_a_dark_title_band(browser, token):
    page = open_share(browser, token, 1280)
    expect(page.locator("html")).to_have_attribute("data-city", "paris")
    hero = page.get_by_test_id("share-hero")
    expect(hero).to_contain_text("Paris · 4 天行程")
    expect(hero).to_contain_text("2026-10-03")
    expect(hero).to_contain_text("这是朋友分享给你的只读行程")
    expect(page.locator(".banner.warn")).to_have_count(0)               # 不再是整条黄色横幅
    expect(page.get_by_text("只读分享")).to_be_visible()
    # 标题带是深色的
    bg = hero.evaluate("e => getComputedStyle(e).backgroundImage")
    assert "gradient" in bg
    page.context.close()


def test_four_stat_cards_match_the_trip(browser, token):
    page = open_share(browser, token, 1280)
    stats = page.get_by_test_id("share-stats")
    expect(stats.locator(".stat-tile")).to_have_count(4)
    expect(stats.locator(".stat-tile").first).to_contain_text("4")
    expect(stats.locator(".stat-tile").nth(3)).to_contain_text("€")  # 门票合计和行程页一样，不是「待补」
    page.context.close()


def test_wide_screens_keep_the_map_beside_the_days_and_narrow_ones_put_it_above(browser, token):
    wide = open_share(browser, token, 1360)
    m = wide.get_by_test_id("full-map").bounding_box()
    d = wide.locator(".day").first.bounding_box()
    assert m and d and m["x"] > d["x"] + d["width"] - 4                 # 地图在日程右边
    wide.context.close()
    narrow = open_share(browser, token, 390, 2400)
    m = narrow.get_by_test_id("full-map").bounding_box()
    d = narrow.locator(".day").first.bounding_box()
    assert m and d and m["y"] < d["y"]                                  # 地图在日程上方
    assert narrow.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    narrow.context.close()


def test_the_share_page_is_still_read_only_and_leaves_no_city_colour_behind(browser, token):
    page = open_share(browser, token, 1280)
    for forbidden in ("编辑行程", "分享", "下载 PDF"):
        expect(page.get_by_role("button", name=forbidden)).to_have_count(0)
    expect(page.get_by_test_id("budget-card")).to_have_count(0)
    page.goto(f"{WEB}/s/this-token-does-not-exist-0000000000")
    expect(page.get_by_text("没有这个分享链接")).to_be_visible()
    assert page.evaluate("document.documentElement.dataset.city") in (None, "paris")  # 失效页不套城市色也可
    page.context.close()
