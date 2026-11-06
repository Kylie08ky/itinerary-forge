# ruff: noqa: E501  （拦截的接口数据是长字面量）
"""任意目的地的前端验收（规格 10，场景 H1–H3）。认城市和建城市的接口用拦截造出来（真的建一座城市要几分钟，
后端那一半由 tests/test_destinations_api.py 和一次真实的里斯本现建覆盖）。

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_destinations_page.py
"""

from __future__ import annotations

import json
import os
import re

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e

WEB = os.getenv("E2E_WEB", "http://127.0.0.1:5173").rstrip("/")
API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")

OSAKA = {"id": "osaka", "name_zh": "大阪", "name_en": "Osaka", "tier": "standard", "country": "JP", "currency": "JPY",
         "timezone": "Asia/Tokyo", "center": [34.69, 135.5], "map_provider": "osm", "residency_options": [], "hero": None,
         "suggested_days": {"min": 3, "max": 5, "quality_max": 7, "fill_max": None, "note": "草稿"}}
OSAKA_THEMES = [{"id": "heritage", "name_zh": "古迹与皇家", "tagline": "大阪的城堡和古建筑", "description": "草稿", "tips": [],
                 "anchors_per_day": {"relaxed": 2, "balanced": 2, "intense": 3}, "place_count": 2}]


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


def open_new(page: Page) -> None:
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()


def submit_with(page: Page, destination: str) -> None:
    page.get_by_label("目的地").fill(destination)
    page.get_by_role("button", name="生成行程").click()


def test_h1_an_unknown_city_is_built_with_progress_and_then_its_themes_replace_the_old_ones(page: Page):
    state = {"polls": 0, "built": False}
    cities = httpx.get(f"{API}/cities", timeout=10).json()

    def resolve(route):
        route.fulfill(json={"status": "build", "choice": {"qid": "Q35765", "label": "大阪", "country": "日本", "description": ""}})

    def build(route):
        route.fulfill(status=202, json={"status": "building", "job_id": "j1", "city_id": "osaka", "name_zh": "大阪"})

    def job(route):
        state["polls"] += 1
        if state["polls"] < 2:
            route.fulfill(json={"status": "running", "city_id": "osaka", "message": "核对 3/25：大阪城", "error": ""})
        else:
            state["built"] = True
            route.fulfill(json={"status": "done", "city_id": "osaka", "name_zh": "大阪", "message": "完成", "error": ""})

    page.route("**/destinations/resolve", resolve)
    page.route("**/destinations/build", build)
    page.route("**/destinations/jobs/j1", job)
    page.route("**/cities", lambda r: r.fulfill(json=[*cities, OSAKA] if state["built"] else cities))
    page.route("**/themes?city=osaka", lambda r: r.fulfill(json=OSAKA_THEMES))
    open_new(page)
    submit_with(page, "大阪")
    flow = page.get_by_test_id("destination-flow")
    expect(flow).to_contain_text("正在建立「大阪」的目的地数据")
    expect(page.get_by_test_id("destination-progress")).to_contain_text("核对 3/25：大阪城")
    expect(page.get_by_test_id("destination-ready-ok")).to_be_visible(timeout=15000)
    page.get_by_test_id("destination-ready-ok").click()
    expect(flow).to_have_count(0)
    expect(page.get_by_label("目的地")).to_have_value("大阪")
    expect(page.locator(".theme-btn", has_text="古迹与皇家")).to_be_visible()          # 主题换成大阪的
    expect(page.locator(".theme-btn", has_text="艺术")).to_have_count(0)


def test_h2_a_country_asks_which_city_and_the_chosen_one_is_built(page: Page):
    asked: list[dict] = []
    page.route("**/destinations/resolve", lambda r: r.fulfill(json={
        "status": "choices", "many": True, "reason": "日本最常去的城市，可以多选",
        "choices": [{"qid": "Q35765", "label": "大阪市", "country": "日本", "description": ""},
                    {"qid": "Q34600", "label": "京都市", "country": "日本", "description": ""}]}))
    page.route("**/destinations/build", lambda r: (asked.append(json.loads(r.request.post_data or "{}")), r.fulfill(
        status=202, json={"status": "ready", "city_id": "kyoto", "name_zh": "京都市"}))[1])
    open_new(page)
    submit_with(page, "日本")
    expect(page.get_by_test_id("destination-flow")).to_contain_text("日本最常去的城市")
    page.get_by_test_id("destination-choice-Q34600").click()
    page.get_by_test_id("destination-pick-ok").click()
    expect(page.get_by_test_id("destination-flow")).to_contain_text("「京都市」准备好了")
    assert asked and asked[0]["qid"] == "Q34600" and asked[0].get("query", "") == ""


def test_h2b_a_country_lets_you_pick_several_cities_in_order_and_each_gets_its_own_themes(page: Page):
    by_qid = {"Q490": ("milan", "米兰"), "Q220": ("rome", "罗马"), "Q2044": ("florence", "佛罗伦萨")}
    page.route("**/destinations/resolve", lambda r: r.fulfill(json={
        "status": "choices", "many": True, "reason": "意大利最常去的城市，可以多选",
        "choices": [{"qid": q, "label": n, "country": "意大利", "description": ""} for q, (_i, n) in by_qid.items()]}))

    queries: list[str] = []

    def build(route):
        body = json.loads(route.request.post_data or "{}")
        queries.append(body.get("query", ""))
        qid = body["qid"]
        cid, name = by_qid[qid]
        route.fulfill(status=202, json={"status": "ready", "city_id": cid, "name_zh": name})

    page.route("**/destinations/build", build)
    cities = httpx.get(f"{API}/cities", timeout=10).json()
    italy = [{**OSAKA, "id": cid, "name_zh": name, "name_en": cid.title(), "country": "IT"} for cid, name in by_qid.values()]
    page.route("**/cities", lambda r: r.fulfill(json=[*cities, *italy]))
    page.route("**/themes?city=*", lambda r: r.fulfill(json=OSAKA_THEMES))
    open_new(page)
    submit_with(page, "意大利")
    flow = page.get_by_test_id("destination-flow")
    expect(flow).to_contain_text("可以多选")
    for qid in ("Q490", "Q220", "Q2044"):                         # 米兰 → 罗马 → 佛罗伦萨：点选的顺序就是走的顺序
        page.get_by_test_id(f"destination-choice-{qid}").click()
    expect(page.get_by_test_id("destination-choice-Q220").locator(".pick-no")).to_have_text("2")
    page.get_by_test_id("destination-pick-ok").click()
    page.get_by_test_id("destination-ready-ok").click()
    assert queries == ["", "", ""], "国家名不能当成城市的别名存下来（否则以后写「意大利」会直接变成米兰）"
    expect(page.get_by_label("目的地")).to_have_value("米兰")
    editor = page.get_by_test_id("legs-editor")
    expect(editor).to_contain_text("罗马")
    expect(editor).to_contain_text("佛罗伦萨")
    assert editor.inner_text().index("罗马") < editor.inner_text().index("佛罗伦萨")
    # 第二步：每一座城市都有自己的主题卡片，不再只有第一座
    expect(page.get_by_test_id("leg-themes-0")).to_contain_text("罗马")
    expect(page.get_by_test_id("leg-themes-1")).to_contain_text("佛罗伦萨")
    first_leg_theme = page.get_by_test_id("leg-themes-0").locator(".theme-btn:not(.balanced)").first
    first_leg_theme.click()
    expect(first_leg_theme).to_have_class(re.compile("active"))
    expect(page.get_by_test_id("leg-theme-balanced-0")).not_to_have_class(re.compile("active"))
    expect(page.get_by_test_id("leg-theme-balanced-1")).to_have_class(re.compile("active"))


def test_h2c_a_country_picks_at_most_five_and_more_cities_are_added_by_hand(page: Page):
    names = ["罗马", "佛罗伦萨", "米兰", "威尼斯", "那不勒斯", "博洛尼亚"]
    by_qid = {f"Q9{i}": (f"it{i}", n) for i, n in enumerate(names)}
    page.route("**/destinations/resolve", lambda r: r.fulfill(json={
        "status": "choices", "many": True, "reason": "意大利最常去的城市，可以多选",
        "choices": [{"qid": q, "label": n, "country": "意大利", "description": ""} for q, (_i, n) in by_qid.items()]}))
    page.route("**/destinations/build", lambda r: (lambda qid: r.fulfill(status=202, json={
        "status": "ready", "city_id": by_qid[qid][0], "name_zh": by_qid[qid][1]}))(json.loads(r.request.post_data)["qid"]))
    cities = httpx.get(f"{API}/cities", timeout=10).json()
    fake = [{**OSAKA, "id": cid, "name_zh": n, "name_en": cid.upper(), "country": "IT"} for cid, n in by_qid.values()]
    page.route("**/cities", lambda r: r.fulfill(json=[*cities, *fake]))
    page.route("**/themes?city=*", lambda r: r.fulfill(json=OSAKA_THEMES))
    open_new(page)
    submit_with(page, "意大利")
    expect(page.get_by_test_id("destination-flow")).to_contain_text("最多 5 个")
    for qid in by_qid:                                              # 点 6 座：第 6 座点不上
        page.get_by_test_id(f"destination-choice-{qid}").click()
    expect(page.get_by_test_id("destination-choice-Q95").locator(".pick-no")).to_have_count(0)
    page.get_by_test_id("destination-pick-ok").click()
    page.get_by_test_id("destination-ready-ok").click()
    expect(page.get_by_test_id("leg-themes-3")).to_be_visible()      # 后面 4 座 = 共 5 座
    expect(page.get_by_test_id("leg-themes-4")).to_have_count(0)
    page.get_by_test_id("add-leg").click()                           # 超过 5 座：手动再加一座
    expect(page.get_by_test_id("leg-themes-4")).to_be_visible()


def test_h3_a_name_that_is_not_a_place_says_so_and_a_known_city_does_not_open_the_dialog(page: Page):
    page.route("**/destinations/resolve", lambda r: r.fulfill(json={"status": "unknown", "reason": "没找到叫这个名字的目的地"}))
    open_new(page)
    submit_with(page, "xyzabc")
    expect(page.get_by_test_id("destination-error")).to_contain_text("没找到叫这个名字的目的地")
    page.get_by_test_id("destination-error-ok").click()
    expect(page.get_by_test_id("destination-flow")).to_have_count(0)
    submit_with(page, "Paris")                                     # 库里有的：不走这个流程，直接生成
    expect(page.get_by_test_id("destination-flow")).to_have_count(0)
    page.wait_for_url("**/tasks/**", timeout=30000)


def test_h5_the_destination_comes_first_and_nothing_of_another_city_shows_until_it_is_filled(page: Page):
    page.goto(f"{WEB}/new")
    page.get_by_label("目的地").wait_for()
    headings = page.locator(".step-card h2").all_inner_texts()
    assert headings.index("行程信息") < headings.index("选主题") < headings.index("来回交通与住宿")
    expect(page.get_by_label("目的地")).to_have_value("")
    expect(page.get_by_test_id("theme-need-destination")).to_be_visible()
    expect(page.locator(".theme-btn")).to_have_count(0)                 # 没有巴黎的主题
    expect(page.get_by_test_id("route-picker")).to_have_count(0)
    page.get_by_label("目的地").fill("Paris")
    page.get_by_label("目的地").press("Tab")
    expect(page.locator(".theme-btn").first).to_be_visible()
    expect(page.get_by_test_id("theme-need-destination")).to_have_count(0)


def test_h6_changing_the_dates_anywhere_updates_the_transport_and_the_stays_together(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    page.get_by_role("button", name="添加住宿").click()
    start0, end0 = page.get_by_label("开始日期").input_value(), page.get_by_label("离开日期").input_value()
    page.get_by_label("开始日期").fill("2026-12-10")
    page.get_by_label("离开日期").fill("2026-12-14")
    expect(page.get_by_label("去程到达时间")).to_have_value("2026-12-10T10:00")
    expect(page.get_by_label("去程出发时间")).to_have_value("2026-12-10T07:00")          # 出发也跟着移，不会比到达早很多天
    expect(page.get_by_label("返程出发时间")).to_have_value("2026-12-14T18:00")
    stay = page.get_by_test_id("stay-block")
    expect(stay.locator("input[type=date]").first).to_have_value("2026-12-10")           # 入住 = 新的开始日
    expect(stay.locator("input[type=date]").nth(1)).to_have_value("2026-12-14")          # 退房 = 新的离开日
    assert (start0, end0) != ("2026-12-10", "2026-12-14")
    # 反过来：在来回交通里改到达日，上面的开始日期和去程出发一起变
    page.get_by_label("去程到达时间").fill("2026-12-12T09:00")
    expect(page.get_by_label("开始日期")).to_have_value("2026-12-12")
    expect(page.get_by_label("去程出发时间")).to_have_value("2026-12-12T07:00")
