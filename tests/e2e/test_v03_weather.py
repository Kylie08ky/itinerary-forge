"""v0.3 批次 C 天气的前端验收（specs/modules/M10 S1a–S1f）。

天气接口在浏览器里被拦截成固定数据（不依赖外网和当天的真实天气）；后端取数逻辑见 tests/test_weather.py。
默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_weather.py
"""

from __future__ import annotations

import json
import os
import re

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

from app.library import Setting, load_library  # noqa: E402

pytestmark = pytest.mark.e2e

WEB = os.getenv("E2E_WEB", "http://127.0.0.1:5173").rstrip("/")
API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")
LIB = load_library("paris")


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
    ctx = browser.new_context(viewport={"width": 1280, "height": 2200}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def new_plan(theme="photography") -> dict:
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
        "travel_style": "balanced", "interests": [theme], "theme": theme,
        "arrival_time": "2026-10-20 10:30", "departure_time": "2026-10-23 18:00"}, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()


def weather_payload(plan: dict, *, rainy_day: int | None = None, history_from: int | None = None) -> dict:
    """按行程的真实地点造天气：outdoor_places / mixed_places 取自地点库的 setting（和后端同一份策展数据）。"""
    days = []
    for i, d in enumerate(plan["itinerary"]["days"]):
        items = [x for x in d["items"] if x.get("place_id") in LIB.places]
        hist = history_from is not None and i >= history_from
        rainy = rainy_day == i
        days.append({
            "date": d["date"], "source": "history" if hist else "forecast", "code": 63 if rainy else 1,
            "label": "中雨" if rainy else "大部晴朗", "icon": "rain" if rainy else "sun",
            "t_max": 14.0, "t_min": 7.0, "rain_prob": None if hist else (85 if rainy else 10), "rain_mm": 0.4,
            "rainy": rainy,
            "outdoor_places": [{"place_id": x["place_id"], "title": x["title"]} for x in items
                               if LIB.places[x["place_id"]].setting == Setting.outdoor],
            "mixed_places": [{"place_id": x["place_id"], "title": x["title"]} for x in items
                             if LIB.places[x["place_id"]].setting == Setting.mixed],
        })
    return {"available": True, "days": days,
            "note": "天气来自 Open-Meteo；15 天以外的日期显示去年同一天的实际天气，仅作参考。"}


def serve_weather(page: Page, payload: dict) -> None:
    page.route(re.compile(r".*/plans/[^/]+/weather.*"), lambda r: r.fulfill(json=payload))


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()


# --------------------------------------------------------------------------- #
# S1a / S1b 每天的天气在这一天的标题行里
# --------------------------------------------------------------------------- #
def test_each_day_shows_its_own_weather_in_the_day_header_and_there_is_no_separate_card(page):
    plan = new_plan()
    serve_weather(page, weather_payload(plan, rainy_day=1))
    open_plan(page, plan["plan_id"])
    expect(page.get_by_test_id("weather-card")).to_have_count(0)                      # 没有单独的天气卡
    expect(page.locator(".day-tools")).to_contain_text("Open-Meteo")                  # 数据来源写在日程上方
    heads = page.locator(".day .day-head")
    expect(heads).to_have_count(4)
    chips = page.get_by_test_id("day-weather")
    expect(chips).to_have_count(4)
    for i in range(4):
        expect(heads.nth(i).get_by_test_id("day-weather")).to_have_count(1)           # 天气就在这一天的标题行里
    expect(chips.nth(0)).to_contain_text("大部晴朗")
    expect(chips.nth(0)).to_contain_text("14° / 7°")
    expect(chips.nth(0)).to_contain_text("降雨 10%")
    expect(chips.nth(1)).to_contain_text("中雨")
    expect(chips.nth(1)).to_contain_text("降雨 85%")
    assert "rainy" in chips.nth(1).get_attribute("class")
    assert "rainy" not in chips.nth(0).get_attribute("class")


def test_days_beyond_the_forecast_are_marked_last_year_and_never_flagged_as_rainy(page):
    plan = new_plan()
    payload = weather_payload(plan, history_from=2)
    payload["days"][3].update({"label": "小雨", "icon": "rain", "rainy": False})       # 去年那天下过雨，但不是预报
    serve_weather(page, payload)
    open_plan(page, plan["plan_id"])
    chips = page.get_by_test_id("day-weather")
    for i in (0, 1):
        expect(chips.nth(i)).not_to_contain_text("去年同期")
    for i in (2, 3):
        expect(chips.nth(i)).to_contain_text("去年同期")
        expect(chips.nth(i)).not_to_contain_text("降雨")
    expect(page.get_by_test_id("rain-banner")).to_have_count(0)                  # 历史数据不触发雨天联动
    expect(page.locator(".tag.rain")).to_have_count(0)


# --------------------------------------------------------------------------- #
# S1c 雨天提示 + 室外标签
# --------------------------------------------------------------------------- #
def test_a_rainy_day_gets_a_banner_and_its_outdoor_places_are_tagged(page):
    plan = new_plan()
    payload = weather_payload(plan, rainy_day=1)
    outdoor = [p["title"] for p in payload["days"][1]["outdoor_places"]]
    assert outdoor, "摄影主题第 2 天应该有室外点，否则这个测试没意义"
    serve_weather(page, payload)
    open_plan(page, plan["plan_id"])
    banners = page.get_by_test_id("rain-banner")
    expect(banners).to_have_count(1)                                              # 只有雨天那一天
    expect(banners).to_contain_text("预报降雨概率 85%")
    for title in outdoor:
        expect(banners).to_contain_text(title)
    day2 = page.locator(".day").nth(1)
    expect(day2.locator(".tag.rain", has_text=re.compile(r"^室外 · 可能淋雨$"))).to_have_count(len(outdoor))
    expect(page.locator(".day").nth(0).locator(".tag.rain")).to_have_count(0)    # 不下雨的天没有标签
    expect(banners.get_by_role("button", name="编辑行程，换成室内备选")).to_be_visible()


def test_a_rainy_day_with_only_indoor_places_says_there_is_no_problem(page):
    plan = new_plan("art")                                      # 艺术主题基本都在室内
    payload = weather_payload(plan, rainy_day=0)
    payload["days"][0]["outdoor_places"] = []
    serve_weather(page, payload)
    open_plan(page, plan["plan_id"])
    banner = page.get_by_test_id("rain-banner")
    expect(banner).to_contain_text("基本都在室内")
    expect(banner.get_by_role("button", name="编辑行程，换成室内备选")).to_have_count(0)


# --------------------------------------------------------------------------- #
# S1d 换成室内备选
# --------------------------------------------------------------------------- #
def test_swapping_a_rained_on_outdoor_place_offers_only_indoor_alternatives(page):
    plan = new_plan()
    payload = weather_payload(plan, rainy_day=1)
    victim = payload["days"][1]["outdoor_places"][0]
    serve_weather(page, payload)
    open_plan(page, plan["plan_id"])

    page.get_by_test_id("rain-banner").get_by_role("button", name="编辑行程，换成室内备选").click()
    expect(page.get_by_role("button", name=re.compile("保存为新版本"))).to_be_visible()   # 进入了编辑模式
    day2 = page.locator(".day").nth(1)
    item = day2.locator(f'.item[data-pid="{victim["place_id"]}"]')
    swap_btn = item.get_by_role("button", name="换成室内备选")
    expect(swap_btn).to_be_visible()
    # 不下雨的天，同样的按钮还叫「换一个」
    expect(page.locator(".day").nth(0).get_by_role("button", name="换一个").first).to_be_visible()
    swap_btn.click()

    panel = item.locator(".swap-panel")
    panel.wait_for()
    expect(panel.get_by_role("button", name="换成它").first).to_be_visible()
    names = [n.strip() for n in panel.locator("b, strong").all_inner_texts()]
    assert names, "备选列表是空的"
    by_name = {p.name_zh: p for p in LIB.places.values()}
    for n in names:
        match = next((p for k, p in by_name.items() if k in n or n in k), None)
        assert match is not None, f"备选「{n}」不在地点库里"
        assert match.setting == Setting.indoor, f"备选「{n}」不是室内地点：{match.setting}"

    panel.get_by_role("button", name="换成它").first.click()
    expect(day2.locator(f'.item[data-pid="{victim["place_id"]}"]')).to_have_count(0)     # 室外点被换掉了


# --------------------------------------------------------------------------- #
# S1e 失败不报错
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["unavailable", "server-error", "aborted"])
def test_when_the_weather_is_unavailable_the_header_just_has_no_weather_and_nothing_complains(page, mode):
    plan = new_plan("art")
    pattern = re.compile(r".*/plans/[^/]+/weather.*")
    if mode == "unavailable":
        page.route(pattern, lambda r: r.fulfill(json={"available": False, "days": []}))
    elif mode == "server-error":
        page.route(pattern, lambda r: r.fulfill(status=500, body=json.dumps({"detail": "boom"}),
                                                content_type="application/json"))
    else:
        page.route(pattern, lambda r: r.abort())
    open_plan(page, plan["plan_id"])
    page.wait_for_timeout(600)
    expect(page.get_by_test_id("day-weather")).to_have_count(0)
    expect(page.get_by_test_id("rain-banner")).to_have_count(0)
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    expect(page.locator(".banner.err")).to_have_count(0)
    expect(page.locator(".day")).to_have_count(4)                                    # 行程本身照常显示
    expect(page.get_by_text("离线")).to_have_count(0)                                 # 天气失败不会把页面切成只读
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_be_enabled()


def test_the_real_weather_endpoint_answers_without_crashing_and_never_errors(page):
    """不拦截：真实后端（连不连得上 Open-Meteo 都行）只会返回 available 真 / 假，不会 5xx。"""
    plan = new_plan("art")
    r = httpx.get(f"{API}/plans/{plan['plan_id']}/weather", timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["available"], bool) and isinstance(body["days"], list)
    if body["available"]:
        assert len(body["days"]) == 4
        assert all(d["source"] in ("forecast", "history") for d in body["days"])


# --------------------------------------------------------------------------- #
# 06-ui-design S9 每天可以展开 / 收起
# --------------------------------------------------------------------------- #
def test_each_day_can_be_collapsed_and_expanded_and_keeps_its_header_and_weather(page):
    plan = new_plan()
    serve_weather(page, weather_payload(plan, rainy_day=1))
    open_plan(page, plan["plan_id"])
    day1 = page.locator(".day").nth(0)
    day2 = page.locator(".day").nth(1)
    toggle = day1.get_by_role("button", name="收起第 1 天")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(day1.locator(".timeline")).to_be_visible()

    toggle.click()
    expect(day1.get_by_role("button", name="展开第 1 天")).to_have_attribute("aria-expanded", "false")
    expect(day1.locator(".timeline")).to_have_count(0)
    expect(day1.get_by_test_id("day-weather")).to_be_visible()                       # 标题行和天气还在
    expect(day1.locator(".day-count")).to_contain_text("个地点")
    expect(day2.locator(".timeline")).to_be_visible()                                # 别的天不受影响

    day1.get_by_role("button", name="展开第 1 天").click()
    expect(day1.locator(".timeline")).to_be_visible()
    expect(day1.locator(".day-count")).to_have_count(0)

    day2.locator(".day-head h3").click()                                             # 点标题行本身也能收起
    expect(day2.locator(".timeline")).to_have_count(0)
    expect(day2.get_by_test_id("rain-banner")).to_have_count(0)                      # 雨天提示一起收起，天气胶囊仍高亮
    assert "rainy" in day2.get_by_test_id("day-weather").get_attribute("class")


def test_collapse_all_and_expand_all(page):
    plan = new_plan()
    serve_weather(page, weather_payload(plan))
    open_plan(page, plan["plan_id"])
    page.get_by_role("button", name="全部收起").click()
    expect(page.locator(".day .timeline")).to_have_count(0)
    expect(page.locator(".day-count")).to_have_count(4)
    page.get_by_role("button", name="全部展开").click()
    expect(page.locator(".day .timeline")).to_have_count(4)


def test_a_collapsed_day_still_works_in_edit_mode_and_clicking_its_buttons_does_not_toggle_it(page):
    plan = new_plan()
    serve_weather(page, weather_payload(plan))
    open_plan(page, plan["plan_id"])
    page.get_by_role("button", name="编辑行程", exact=True).click()
    page.get_by_role("button", name=re.compile("保存为新版本")).wait_for()
    day1 = page.locator(".day").nth(0)
    day1.get_by_role("button", name="收起第 1 天").click()
    expect(day1.locator(".timeline")).to_have_count(0)
    expect(day1.locator(".add-row")).to_have_count(0)                                # 收起时加地点的入口也收起
    day1.get_by_role("button", name="展开第 1 天").click()
    expect(day1.locator(".add-row")).to_have_count(1)
    first_item_lock = day1.locator(".item .tool", has_text="锁定").first
    first_item_lock.click()                                                          # 点日程里的按钮不会误触收起
    expect(day1.locator(".timeline")).to_be_visible()


# --------------------------------------------------------------------------- #
# 记住上次的收起状态（06-ui-design S9）
# --------------------------------------------------------------------------- #
def test_the_collapsed_days_are_remembered_per_plan_across_reloads(page):
    a, b = new_plan(), new_plan()
    for p in (a, b):
        serve_weather(page, weather_payload(p))
    open_plan(page, a["plan_id"])
    page.get_by_role("button", name="收起第 2 天").click()
    page.get_by_role("button", name="收起第 3 天").click()
    expect(page.locator(".day .timeline")).to_have_count(2)

    page.reload()
    page.locator(".day").first.wait_for()
    expect(page.locator(".day .timeline")).to_have_count(2)                          # 刷新后还是收起的
    expect(page.get_by_role("button", name="展开第 2 天")).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_role("button", name="展开第 3 天")).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_role("button", name="收起第 1 天")).to_have_attribute("aria-expanded", "true")

    open_plan(page, b["plan_id"])                                                    # 另一份行程各记各的
    expect(page.locator(".day .timeline")).to_have_count(4)

    open_plan(page, a["plan_id"])
    page.get_by_role("button", name="全部展开").click()
    expect(page.locator(".day .timeline")).to_have_count(4)
    page.reload()
    page.locator(".day").first.wait_for()
    expect(page.locator(".day .timeline")).to_have_count(4)                          # 全部展开也被记住
    assert page.evaluate(f"localStorage.getItem('atp:collapsed-days:{a['plan_id']}')") is None


def test_a_broken_or_blocked_browser_store_falls_back_to_everything_expanded(page):
    plan = new_plan()
    serve_weather(page, weather_payload(plan))
    page.add_init_script(f"localStorage.setItem('atp:collapsed-days:{plan['plan_id']}', '{{not json');")
    open_plan(page, plan["plan_id"])
    expect(page.locator(".day .timeline")).to_have_count(4)                          # 坏数据不影响页面
    page.get_by_role("button", name="收起第 1 天").click()
    expect(page.locator(".day .timeline")).to_have_count(3)


# --------------------------------------------------------------------------- #
# S1g 分享页也带天气
# --------------------------------------------------------------------------- #
def share_token(plan: dict) -> str:
    return httpx.post(f"{API}/plans/{plan['plan_id']}/share", timeout=10).json()["token"]


def serve_share_with_weather(page: Page, token: str, payload: dict | None) -> None:
    """真实的分享接口 + 把 weather 字段换成指定内容（None = 删掉，模拟天气取不到）。"""
    def handler(route):
        res = route.fetch()
        body = res.json()
        if payload is None:
            body["weather"] = {"available": False, "days": []}
        else:
            body["weather"] = payload
        route.fulfill(response=res, json=body)

    page.route(re.compile(rf".*/share/{re.escape(token)}$"), handler)


def test_the_public_share_page_shows_each_days_weather_and_the_rain_hint_but_cannot_edit(page):
    plan = new_plan()
    token = share_token(plan)
    payload = weather_payload(plan, rainy_day=1)
    outdoor = [p["title"] for p in payload["days"][1]["outdoor_places"]]
    assert outdoor
    serve_share_with_weather(page, token, payload)
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()

    chips = page.get_by_test_id("day-weather")
    expect(chips).to_have_count(4)
    expect(chips.nth(0)).to_contain_text("大部晴朗")
    expect(chips.nth(1)).to_contain_text("降雨 85%")
    banner = page.get_by_test_id("rain-banner")
    expect(banner).to_have_count(1)
    for title in outdoor:
        expect(banner).to_contain_text(title)
    outdoor_tags = page.locator(".day").nth(1).locator(".tag.rain", has_text=re.compile(r"^室外 · 可能淋雨$"))
    expect(outdoor_tags).to_have_count(len(outdoor))
    expect(banner.get_by_role("button", name="编辑行程，换成室内备选")).to_have_count(0)     # 只读：没有编辑入口
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_have_count(0)
    expect(page.get_by_role("navigation", name="主导航")).to_have_count(0)


def test_the_share_page_can_collapse_days_and_remembers_it_per_link(page):
    plan = new_plan()
    token = share_token(plan)
    serve_share_with_weather(page, token, weather_payload(plan))
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    page.get_by_role("button", name="收起第 1 天").click()
    expect(page.locator(".day .timeline")).to_have_count(3)
    expect(page.locator(".day").first.get_by_test_id("day-weather")).to_be_visible()
    page.reload()
    page.locator(".day").first.wait_for()
    expect(page.locator(".day .timeline")).to_have_count(3)
    page.get_by_role("button", name="全部展开").click()
    expect(page.locator(".day .timeline")).to_have_count(4)


def test_the_share_page_without_weather_just_has_no_weather(page):
    plan = new_plan("art")
    token = share_token(plan)
    serve_share_with_weather(page, token, None)
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    expect(page.get_by_test_id("day-weather")).to_have_count(0)
    expect(page.locator(".banner.err")).to_have_count(0)
    expect(page.locator(".day")).to_have_count(4)


# --------------------------------------------------------------------------- #
# S1h 导出（真实后端，不拦截；连不连得上 Open-Meteo 都行）
# --------------------------------------------------------------------------- #
def test_real_exports_succeed_and_carry_weather_lines_only_when_weather_is_available(page):
    plan = new_plan("art")
    pid = plan["plan_id"]
    available = httpx.get(f"{API}/plans/{pid}/weather", timeout=30).json()["available"]
    md = httpx.get(f"{API}/plans/{pid}/itinerary.md", timeout=60)
    html = httpx.get(f"{API}/plans/{pid}/itinerary.html", timeout=60)
    assert md.status_code == 200 and html.status_code == 200
    assert ("天气：" in md.text) == available
    assert ("天气：" in html.text) == available
