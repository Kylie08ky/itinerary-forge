"""v0.2 候选池与混选的前端验收（specs/modules/M01 S9、M02 S5–S7）。

像真人一样在首页混选主题、标「必去 / 不去」、搜索地点库、点开地点详情卡。
默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v02_pool.py
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
    ctx = browser.new_context(viewport={"width": 1200, "height": 1600}, locale="zh-CN")
    pg = ctx.new_page()
    yield pg
    ctx.close()


def open_home(page: Page) -> None:
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    theme_btn(page, "艺术").click()           # 默认是「均衡」（没有候选池）；选了具体主题，候选池面板才出现
    page.locator(".pool").wait_for()


def theme_btn(page: Page, name: str):
    return page.locator(".theme-btn").filter(has=page.locator(".theme-label", has_text=re.compile(f"^{name}$")))


def generate(page: Page) -> str:
    fill_trip(page)
    page.get_by_role("button", name="生成行程").click()
    page.wait_for_url(re.compile(r"/tasks/"), timeout=15_000)
    page.wait_for_url(re.compile(r"/plans/paris-"), timeout=240_000)
    page.locator(".day").first.wait_for()
    return page.url.rsplit("/", 1)[1].split("?")[0]


def plan_place_ids(pid: str) -> list[str]:
    it = httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]
    return [i["place_id"] for d in it["days"] for i in d["items"] if i.get("place_id")]


def pool_row(page: Page, pid: str):
    return page.locator(f'.pool-row[data-pid="{pid}"]')


# --------------------------------------------------------------------------- #
def test_mixing_two_themes_shows_weights_and_generates_a_mixed_plan(page: Page):
    open_home(page)
    expect(page.locator(".mix-weights")).to_have_count(0)              # 只选了一个主题，没有权重
    theme_btn(page, "美食").click()
    expect(page.locator(".mix-weights select")).to_have_count(2)
    # 不设上限（规格 07 P15）：想选几个选几个；取消两个后回到两个
    theme_btn(page, "购物").click()
    theme_btn(page, "摄影").click()
    expect(page.locator(".theme-btn.active")).to_have_count(4)
    theme_btn(page, "购物").click()
    theme_btn(page, "摄影").click()
    expect(page.locator(".theme-btn.active")).to_have_count(2)

    pid = generate(page)
    page.get_by_role("button", name="更多").click()
    page.get_by_test_id("open-gen-details").click()
    expect(page.locator(".gen-theme")).to_contain_text("艺术 + 美食")
    page.keyboard.press("Escape")
    ids = plan_place_ids(pid)
    assert ids, "混选行程里应该有地点"
    # 美食主题的点真的出现了（不只是艺术）
    food = {m["place_id"] for m in httpx.get(f"{API}/themes/food", timeout=10).json()["members"]}
    assert food & set(ids), "混选的美食主题一个点都没有排进去"


def test_must_go_and_avoid_in_the_pool_change_the_plan(page: Page):
    open_home(page)
    members = httpx.get(f"{API}/themes/art", timeout=10).json()["members"]
    anchors = [m["place_id"] for m in members if m["role"] == "anchor"]
    avoid_id, must_id = anchors[0], anchors[-1]

    page.locator(".pool > summary").click()
    pool_row(page, avoid_id).get_by_role("button", name="不去").click()
    pool_row(page, must_id).get_by_role("button", name="必去").click()
    expect(page.locator(".pool-count")).to_contain_text("已设置 2 项")
    expect(page.get_by_role("group", name=re.compile("候选设置")).first).to_be_visible()

    pid = generate(page)
    ids = plan_place_ids(pid)
    assert avoid_id not in ids, "标了「不去」的地点还是排进了行程"
    assert must_id in ids, "标了「必去」的地点没有排进行程"


def test_search_adds_an_out_of_theme_place_as_must_go(page: Page):
    open_home(page)
    page.locator(".pool > summary").click()
    page.get_by_label("搜索地点库").fill("Orsay")
    hit = page.locator(".pool-hit").first
    expect(hit).to_contain_text("奥赛")
    hit.get_by_role("button", name="加入并标为必去").click()
    expect(page.locator(".pool-row.must")).to_have_count(1)
    expect(page.get_by_label("搜索地点库")).to_have_value("")

    # 搜不到的东西要说清楚为什么
    page.get_by_label("搜索地点库").fill("这个地方不存在xyz")
    expect(page.locator(".pool-hits")).to_contain_text("地点库里没有找到")


def test_place_card_opens_from_pool_and_from_the_plan(page: Page):
    open_home(page)
    page.locator(".pool > summary").click()
    page.locator(".pool-row .linklike").first.click()
    card = page.get_by_role("dialog", name="地点详情")
    expect(card).to_be_visible()
    expect(card).to_contain_text("地址")
    expect(card).to_contain_text("闭馆日")
    expect(card).to_contain_text("购票")
    expect(card).to_contain_text("已于")                               # 至少有一项事实带了核对日期
    page.keyboard.press("Escape")
    expect(card).to_have_count(0)

    pid = generate(page)
    page.locator(".item .title-link").first.click()
    expect(page.get_by_role("dialog", name="地点详情")).to_be_visible()
    page.get_by_role("button", name="关闭").click()
    expect(page.get_by_role("dialog", name="地点详情")).to_have_count(0)
    assert pid


def test_place_card_hides_the_link_for_places_without_online_ticketing(page: Page):
    open_home(page)
    free = next(
        p for p in httpx.get(f"{API}/places", timeout=10).json()
        if p["ticketing"] == "none"
    )
    page.locator(".pool > summary").click()
    page.get_by_label("搜索地点库").fill(free["name_zh"])
    page.locator(".pool-hit .tool").first.click()
    page.locator(".pool-row.must .linklike").first.click()
    card = page.get_by_role("dialog", name="地点详情")
    expect(card).to_contain_text("无需购票")
    expect(card.get_by_role("link", name=re.compile("官网购票"))).to_have_count(0)
