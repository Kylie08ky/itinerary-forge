"""v0.1 前端验收（specs/04-versions-and-roadmap.md 的「v0.1 主题生成 MVP」）。

像真人一样在真实浏览器里点一遍：选主题 → 看真实进度 → 看行程页（地图、闭馆日、链接规则、质检）→ 下载 PDF →
手机宽度。需要后端和前端已经在跑，所以默认不跑：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e

地址可用环境变量改：E2E_WEB（默认 http://127.0.0.1:5173）、E2E_API（默认 http://127.0.0.1:8137）。
服务没起来时整组测试自动跳过。
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
NO_FORM_BUTTON = re.compile(r"生成行程")


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
    ctx = browser.new_context(viewport={"width": 1100, "height": 900}, locale="zh-CN")
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture(scope="module")
def generated(browser) -> dict:
    """走一遍真实的生成流程，返回 {plan_id, steps_seen}。后面的验收都基于这份行程。"""
    ctx = browser.new_context(viewport={"width": 1100, "height": 900}, locale="zh-CN")
    page = ctx.new_page()
    page.goto(f"{WEB}/new?destination=Paris")
    page.get_by_role("button", name=re.compile("艺术")).first.click()
    # 下面的断言（周一 / 周二闭馆、10:30 抵达、18:00 离开的缓冲）依赖这组固定的时间；首页默认值是「两周后」，所以显式填
    fill_trip(page, "2026-10-03", "2026-10-06")
    page.get_by_role("button", name=NO_FORM_BUTTON).click()
    page.wait_for_url(re.compile(r"/tasks/"), timeout=15_000)
    page.locator(".pstep").first.wait_for(timeout=10_000)
    steps_seen = page.locator(".pstep b").all_inner_texts()
    page.wait_for_url(re.compile(r"/plans/paris-"), timeout=180_000)
    plan_id = page.url.rsplit("/", 1)[1]
    ctx.close()
    return {"plan_id": plan_id, "steps_seen": steps_seen}


def itinerary(plan_id: str) -> dict:
    return httpx.get(f"{API}/plans/{plan_id}", timeout=30).json()["itinerary"]


# --------------------------------------------------------------------------- #
def test_home_offers_the_eight_themes_and_a_default_balanced_choice(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    assert page.locator(".theme-btn:not(.balanced)").count() == 8
    expect(page.get_by_test_id("theme-balanced")).to_have_class(re.compile("active"))     # 默认「均衡 · 让我来推荐」


def test_generation_shows_four_real_steps(generated):
    assert generated["steps_seen"] == ["读取地点库", "选点并排程", "质检", "生成说明"]


def test_plan_page_header_and_quality_badge(page: Page, generated):
    page.goto(f"{WEB}/plans/{generated['plan_id']}")
    page.locator(".day").first.wait_for()
    # 质检结果不再摆在行程页顶部（规格 07 P10）：没问题时页面上看不到；完整质检在「更多 → 生成详情」里
    assert page.locator(".qa").count() == 0 and "质检" not in page.inner_text(".plan-hero")
    page.get_by_role("button", name="更多").click()
    page.get_by_test_id("open-gen-details").click()
    dialog = page.get_by_role("dialog", name="生成详情")
    assert "主题：艺术" in dialog.locator(".gen-theme").inner_text()
    badge = dialog.locator(".qa").inner_text()
    assert "0 error" in badge and "可交付" in badge
    page.keyboard.press("Escape")
    assert page.locator(".day").count() == 4


def test_every_day_map_numbers_every_library_place(page: Page, generated):
    page.goto(f"{WEB}/plans/{generated['plan_id']}")
    page.locator(".day").first.wait_for()
    page.wait_for_timeout(1500)                       # 等地图渲染
    expected = [sum(1 for i in d["items"] if i.get("place_id")) for d in itinerary(generated["plan_id"])["days"]]
    pins = [page.locator(".day").nth(k).locator(".map-pin").count() for k in range(4)]
    assert pins == expected and all(n >= 1 for n in expected)


def test_closed_days_are_respected(generated):
    it = itinerary(generated["plan_id"])
    by_date = {d["date"]: {i.get("place_id") for i in d["items"]} for d in it["days"]}
    assert "orsay" not in by_date["2026-10-05"] and "rodin" not in by_date["2026-10-05"]       # 周一
    assert "louvre" not in by_date["2026-10-06"] and "orangerie" not in by_date["2026-10-06"]  # 周二


def test_arrival_and_departure_buffers(generated):
    days = itinerary(generated["plan_id"])["days"]
    first_real = next(i for i in days[0]["items"] if i["type"] not in ("logistics",))
    assert first_real["time"] >= "11:30"                           # 10:30 抵达 + 1 小时缓冲
    last = days[-1]["items"][-1]
    assert last["type"] == "logistics" and last["time"] == "15:00"          # 18:00 离开 - 3 小时


def test_links_follow_the_ticket_rules(page: Page, generated):
    """有线上购票渠道的地点才有官网链接；其余只有地址。"""
    page.goto(f"{WEB}/plans/{generated['plan_id']}")
    page.locator(".day").first.wait_for()
    page.wait_for_timeout(800)
    places = {p["id"]: p for p in httpx.get(f"{API}/places", timeout=30).json()}
    by_name = {p["name_zh"]: p for p in places.values()}
    checked = 0
    for item in page.locator(".item").all():
        title = item.locator("h4").inner_text().strip()
        place = by_name.get(title)
        if place is None:
            continue
        links = item.locator("a.tag.link")
        if place["ticketing"] == "none":
            assert links.count() == 0, title
        else:
            assert links.count() == 1 and links.first.get_attribute("href") == place["official_url"], title
        # 地址不再摆在条目里（Q4）：名字旁边有「详情」标签，点开浮窗里有地址
        detail = item.get_by_test_id("item-detail")
        assert detail.count() == 1, title
        if checked == 0:
            detail.click()
            assert place["address"] in page.get_by_role("dialog", name="地点详情").inner_text(), title
            page.keyboard.press("Escape")
        checked += 1
    assert checked >= 6


def test_ticket_checklist_has_two_tiers_when_both_kinds_are_present(page: Page, generated):
    page.goto(f"{WEB}/plans/{generated['plan_id']}")
    page.locator(".day").first.wait_for()
    it = itinerary(generated["plan_id"])
    if it["booking_checklist"]:
        titles = page.locator(".check-group h3").all_inner_texts()
        assert titles and titles[0].startswith("必须提前线上购票") or titles[0].startswith("建议线上购票")


def test_pdf_downloads(generated):
    r = httpx.get(f"{API}/plans/{generated['plan_id']}/itinerary.pdf", timeout=120)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-" and len(r.content) > 20_000


def test_my_plans_lists_the_new_plan(page: Page, generated):
    page.goto(f"{WEB}/plans")
    page.locator(".plan-card").first.wait_for()
    # 已结束超过 6 条会折叠，先展开
    page.evaluate("document.querySelectorAll('details.past-more').forEach(d => { d.open = true; })")
    hrefs = [a.get_attribute("href") for a in page.locator("a.plan-card").all()]
    assert f"/plans/{generated['plan_id']}" in hrefs
    mine = page.locator(f'a.plan-card[href$="{generated["plan_id"]}"]')        # 列表按时间分组排序，不一定排第一
    assert "艺术" in mine.inner_text()


@pytest.mark.parametrize("path", ["/", "/plans", "PLAN"])
def test_phone_width_has_no_horizontal_scroll(browser, generated, path):
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    page.goto(WEB + (f"/plans/{generated['plan_id']}" if path == "PLAN" else path))
    page.wait_for_timeout(1500)
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
    ctx.close()
