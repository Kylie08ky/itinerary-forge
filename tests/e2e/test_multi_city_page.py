"""多城市行程的前端验收（规格 11，场景 M1–M10）：几座城市是**一份**行程。
独立后端里有巴黎和一座已建好的小城市（里昂，tests/city_fixtures.py）。

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_multi_city_page.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, timedelta
from urllib.parse import unquote

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
    if not any(c["id"] == "lyon" for c in httpx.get(f"{API}/cities", timeout=10).json()):
        pytest.skip("后端里没有第二座城市（里昂）：用 pytest 自动起的独立后端跑，或先在应用里建好它")


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


def add_second_city(page: Page, name: str = "里昂") -> None:
    page.get_by_test_id("add-leg").click()
    page.get_by_label("第 2 座城市", exact=True).fill(name)


def plus_days(iso: str, n: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=n)).isoformat()


def _generate(page: Page) -> str:
    """点「生成行程」，等进度页跳到行程页；返回行程号。"""
    page.get_by_role("button", name="生成行程").click()
    page.wait_for_url("**/plans/**", timeout=120_000)
    return unquote(page.url.rsplit("/", 1)[1])


def _plan(pid: str) -> dict:
    return httpx.get(f"{API}/plans/{pid}", timeout=10).json()


def test_m1_two_cities_become_one_plan_on_one_card(page: Page):
    open_new(page)
    add_second_city(page)
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 6 天")        # 默认 4 天 + 3 天，换城市那天算一次
    # 第一座城市晚一天离开：后面的城市整体晚一天，总共 7 天
    first_end = page.locator("#end_date").input_value()
    page.locator("#end_date").fill(plus_days(first_end, 1))
    expect(page.get_by_label("第 2 座城市的开始日期")).to_have_value(plus_days(first_end, 1))
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 7 天")
    pid = _generate(page)
    expect(page.get_by_role("heading", name="巴黎 → 里昂 · 7 天行程")).to_be_visible()
    days = page.get_by_test_id("day-city")
    expect(days).to_have_count(7)
    expect(days.first).to_have_text("巴黎")
    expect(days.last).to_have_text("里昂")
    assert [c["name_zh"] for c in _plan(pid)["cities"]] == ["巴黎", "里昂"]
    # 「我的行程」里只有一张卡片
    page.goto(f"{WEB}/plans")
    card = page.locator(f'a.plan-card[href$="{pid[-16:]}"]')                # 行程号末尾是日期和随机码
    expect(card).to_have_count(1)
    expect(card).to_contain_text("巴黎 → 里昂")


def test_m2_m4_the_transfer_sits_between_the_cities_and_marks_the_estimate(page: Page):
    open_new(page)
    add_second_city(page)
    _generate(page)
    transfer = page.get_by_test_id("plan-transfer-0")
    expect(transfer).to_contain_text("巴黎 → 里昂")
    expect(transfer).to_contain_text("高铁")
    expect(transfer).to_contain_text("估算")


def test_m3_the_transfer_day_holds_both_cities(page: Page):
    open_new(page)
    add_second_city(page)
    pid = _generate(page)
    detail = _plan(pid)
    shared = [d for d in detail["itinerary"]["days"] if d.get("theme_by_city")]
    assert len(shared) == 1                                       # 换城市那天仍是一天
    assert {i["city_id"] for i in shared[0]["items"]} == {"paris", "lyon"}
    expect(page.get_by_test_id("day-city").filter(has_text="巴黎 → 里昂")).to_have_count(1)


def test_a_city_can_be_a_single_day_and_the_next_one_may_start_the_next_day(page: Page):
    open_new(page)
    add_second_city(page)
    first_end = page.locator("#end_date").input_value()
    page.get_by_label("第 2 座城市的离开日期").fill(first_end)                    # 里昂只待一天
    page.get_by_label("第 2 座城市的开始日期").fill(plus_days(first_end, 1))      # 夜车：第二天才到
    expect(page.get_by_test_id("transfer-0")).to_contain_text("出发")
    _generate(page)
    expect(page.get_by_test_id("plan-transfer-0")).to_contain_text("估算")
    expect(page.get_by_test_id("day-city").last).to_have_text("里昂")


def test_m5_limits_are_explained_before_anything_is_generated(page: Page):
    open_new(page)
    add_second_city(page)
    first_end = page.locator("#end_date").input_value()
    page.get_by_label("第 2 座城市的开始日期").fill(plus_days(first_end, 3))      # 中间隔了两天
    page.get_by_role("button", name="生成行程").click()
    expect(page.get_by_test_id("legs-editor")).to_contain_text("当天或第二天开始")
    expect(page).to_have_url(f"{WEB}/new?destination=Paris")
    # 相邻两座不能是同一座
    page.get_by_label("第 2 座城市的开始日期").fill(first_end)
    page.get_by_label("第 2 座城市", exact=True).fill("巴黎")
    page.get_by_role("button", name="生成行程").click()
    expect(page.get_by_test_id("legs-editor")).to_contain_text("相邻的两座城市不能是同一座")
    # 最多八座城市：一直加到第八座，「添加」就没有了
    for _ in range(6):
        page.get_by_test_id("add-leg").click()
    expect(page.get_by_test_id("add-leg")).to_have_count(0)
    expect(page.get_by_label("第 8 座城市", exact=True)).to_be_visible()
    expect(page.get_by_label("第 9 座城市", exact=True)).to_have_count(0)


def test_typing_a_date_by_hand_still_moves_the_next_city(page: Page):
    """手敲日期时中间状态是空的（年月日没敲完）：后面的城市仍然要跟着第一座城市的离开日期走。"""
    open_new(page)
    add_second_city(page)
    first_end = page.locator("#end_date").input_value()
    typed = plus_days(first_end, 1)
    page.locator("#end_date").click(position={"x": 12, "y": 12})                  # 日期框最左边：年
    year, month, day = typed.split("-")
    page.keyboard.type(year)
    page.keyboard.press("Tab")
    page.keyboard.type(month + day)                                               # 月敲满两位自动跳到日
    expect(page.locator("#end_date")).to_have_value(typed)
    expect(page.get_by_label("第 2 座城市的开始日期")).to_have_value(typed)
    expect(page.get_by_test_id("transfer-0")).to_contain_text("当天换城市")


def test_m7_each_city_has_its_own_must_go_and_stay(page: Page):
    open_new(page)
    add_second_city(page)
    leg = page.get_by_test_id("leg-1")
    leg.locator("summary", has_text="路线、必去与住宿").click()
    leg.locator("details.pool summary").click()
    row = leg.locator(".pool-row").first
    place_id = row.get_attribute("data-pid")
    row.get_by_role("button", name="必去").click()
    leg.get_by_role("button", name="＋ 添加住宿").click()
    leg.locator("[id^=stay_area_]").fill("市中心")
    leg.locator("[id^=stay_name_]").fill("里昂的旅馆")
    detail = _plan(_generate(page))
    assert [s["name"] for s in detail["stays"]] == ["里昂的旅馆"]
    lyon_items = [i for d in detail["itinerary"]["days"] for i in d["items"] if i.get("city_id") == "lyon"]
    assert place_id in {i.get("place_id") for i in lyon_items}


def test_m7_a_stay_outside_its_city_dates_is_refused_before_generating(page: Page):
    open_new(page)
    add_second_city(page)
    leg = page.get_by_test_id("leg-1")
    leg.locator("summary", has_text="路线、必去与住宿").click()
    leg.get_by_role("button", name="＋ 添加住宿").click()
    leg.locator("[id^=stay_name_]").fill("没填地区的旅馆")
    page.get_by_role("button", name="生成行程").click()
    expect(page.get_by_test_id("legs-editor")).to_contain_text("请填所在区或地址")
    expect(page).to_have_url(f"{WEB}/new?destination=Paris")


def test_m9_everything_reads_from_the_one_plan(page: Page):
    open_new(page)
    add_second_city(page)
    pid = _generate(page)
    assert httpx.get(f"{API}/plans/{pid}/route", timeout=10).status_code == 200
    assert httpx.get(f"{API}/plans/{pid}/reservations", timeout=10).status_code == 200
    ics = httpx.get(f"{API}/plans/{pid}/itinerary.ics", timeout=10)
    assert ics.status_code == 200 and "BEGIN:VEVENT" in ics.text
    assert httpx.get(f"{API}/plans/{pid}/budget", timeout=10).status_code in (200, 404)


def test_m10_direct_editing_is_open_and_the_one_sentence_revision_is_explained(page: Page):
    open_new(page)
    add_second_city(page)
    pid = _generate(page)
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_be_visible()
    expect(page.get_by_text("多城市行程的一句话修改还没做")).to_be_visible()
    first = _plan(pid)["itinerary"]["days"][0]["date"]
    assert httpx.post(f"{API}/plans/{pid}/shift", json={"start_date": plus_days(first, 1)},
                      timeout=60).status_code == 201


def test_m11_the_cover_cards_cover_every_city_not_just_the_first(page: Page):
    open_new(page)
    add_second_city(page)
    _generate(page)
    kickers = page.locator("[data-testid=cover-card] .cover-card-kicker")
    kickers.first.wait_for(state="attached")
    texts = kickers.all_text_contents()
    assert any("巴黎" in t for t in texts) and any("里昂" in t for t in texts), texts


# --------------------------------------------------------------------------- #
# 页内直接编辑（第 ② 批）
# --------------------------------------------------------------------------- #
def _open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    page.get_by_test_id("day-city").first.wait_for()


def _start_editing(page: Page, pid: str) -> None:
    _open_plan(page, pid)
    page.get_by_role("button", name="编辑行程", exact=True).click()
    expect(page.locator(".edit-bar")).to_be_visible()


def _day_city_names(page: Page, city: str) -> list[int]:
    """哪几天（从 0 起）标着这座城市（换城市那天两座都算）。"""
    tags = page.get_by_test_id("day-city").all_inner_texts()
    return [i for i, t in enumerate(tags) if city in t]


def test_m12_moving_a_place_is_limited_to_its_own_citys_days(page: Page):
    open_new(page)
    add_second_city(page)
    pid = _generate(page)
    _start_editing(page, pid)
    paris_days, lyon_days = _day_city_names(page, "巴黎"), _day_city_names(page, "里昂")
    day = page.locator(".day").nth(lyon_days[-1])
    item = day.locator(".item[data-pid]").first
    options = item.locator("select.tool").first.locator("option").all_inner_texts()
    offered = {int(o.split(" ")[1]) - 1 for o in options if o.startswith("第 ")}
    assert offered <= set(lyon_days) and not offered & (set(paris_days) - set(lyon_days)), options
    # 给里昂的某一天加地点：下拉里只有里昂的地点
    add = page.get_by_label(f"给第 {lyon_days[-1] + 1} 天加入地点", exact=True)
    from_lyon = {a for a in add.locator("option").all_inner_texts() if not a.startswith("＋")}
    paris_select = page.get_by_label(f"给第 {paris_days[0] + 1} 天加入地点", exact=True)
    paris_add = set(paris_select.locator("option").all_inner_texts())
    assert from_lyon and from_lyon.isdisjoint(paris_add)


def test_m12_edit_one_city_and_save_a_new_version_without_touching_the_other(page: Page):
    open_new(page)
    add_second_city(page)
    pid = _generate(page)
    before = _plan(pid)["itinerary"]
    paris_before = [(d["date"], i["place_id"], i["time"]) for d in before["days"] for i in d["items"]
                    if i.get("city_id") == "paris" and i.get("place_id")]
    _start_editing(page, pid)
    lyon_days = _day_city_names(page, "里昂")
    day = page.locator(".day").nth(lyon_days[-1])
    target = day.locator(".item[data-pid]").first
    gone = target.get_attribute("data-pid")
    target.get_by_role("button", name="删除").click()
    page.wait_for_timeout(800)
    page.get_by_role("button", name=re.compile("保存为新版本")).click()
    page.get_by_label("版本名字").fill("删一个里昂的点")
    page.get_by_role("button", name="保存", exact=True).click()
    expect(page.locator(".edit-bar")).to_have_count(0)
    after = _plan(pid)
    assert after["version"] == 2
    paris_after = [(d["date"], i["place_id"], i["time"]) for d in after["itinerary"]["days"] for i in d["items"]
                   if i.get("city_id") == "paris" and i.get("place_id")]
    assert paris_after == paris_before                                   # 巴黎一点没动
    placed = {i.get("place_id") for d in after["itinerary"]["days"] for i in d["items"]}
    assert gone not in placed
    assert [c["name_zh"] for c in after["cities"]] == ["巴黎", "里昂"]


def test_m13_the_transfer_between_cities_is_edited_on_the_plan_page(page: Page):
    open_new(page)
    add_second_city(page)
    pid = _generate(page)
    transfer = page.get_by_test_id("plan-transfer-0")
    expect(transfer).to_contain_text("估算")
    transfer.get_by_test_id("plan-transfer-edit-0").click()
    dialog = page.get_by_test_id("transfer-dialog")
    dialog.get_by_label("交通方式").select_option("high_speed_rail")
    dialog.get_by_label("出发", exact=False).fill("15:00")           # 到达早于出发：报错，交通不动
    dialog.get_by_label("到达", exact=False).fill("11:30")
    dialog.get_by_label("车次 / 航班号").fill("G123")
    page.get_by_test_id("transfer-save").click()
    expect(page.get_by_test_id("notice-ok")).to_be_visible(timeout=30_000)
    page.get_by_test_id("notice-ok").click()
    dialog.get_by_label("出发", exact=False).fill("11:30")
    dialog.get_by_label("到达", exact=False).fill("15:00")
    page.get_by_test_id("transfer-save").click()
    expect(page.get_by_test_id("transfer-dialog")).to_have_count(0, timeout=60_000)
    expect(transfer).to_contain_text("G123")
    expect(transfer).to_contain_text("11:30 → 15:00")
    expect(transfer).not_to_contain_text("估算")
    assert _plan(pid)["version"] == 2


def test_m14_shifting_the_plan_moves_every_city_together(page: Page):
    open_new(page)
    add_second_city(page)
    pid = _generate(page)
    before = _plan(pid)
    start = before["cities"][0]["start_date"]
    _open_plan(page, pid)
    page.get_by_role("button", name="更多").click()
    page.get_by_role("button", name="平移日期").click()
    page.get_by_label("新的开始日期").fill(plus_days(start, 2))
    page.get_by_role("button", name="确认平移").click()
    later = page.get_by_role("button", name="稍后再处理")
    try:
        later.wait_for(timeout=2000)
        later.click()
    except Exception:  # noqa: BLE001 - 没有新的闭馆日冲突就没有这个弹窗
        pass
    expect(page.get_by_role("button", name="v2")).to_have_class(re.compile("active"), timeout=30_000)
    after = _plan(pid)
    for a, b in zip(before["cities"], after["cities"], strict=True):
        assert b["start_date"] == plus_days(a["start_date"], 2) and b["end_date"] == plus_days(a["end_date"], 2)
    assert after["transfers"][0]["depart_at"][:10] == plus_days(before["transfers"][0]["depart_at"][:10], 2)
    assert after["version"] == 2


def test_m15_the_overview_and_contents_list_which_days_belong_to_which_city(page: Page):
    open_new(page)
    add_second_city(page)
    pid = _generate(page)
    cities = _plan(pid)["cities"]
    strip = page.get_by_test_id("city-strip-item")
    expect(strip).to_have_count(2)
    expect(strip.nth(0)).to_contain_text("巴黎")
    expect(strip.nth(1)).to_contain_text("里昂")
    expect(strip.nth(1)).to_contain_text(cities[1]["start_date"][5:].replace("-", "/"))
    # 目录里每座城市一个标题（写明日期范围），下面是它的天（第 N 天 · 月/日）；换城市那天写「→ 里昂」
    headings = page.get_by_test_id("nav-city")
    expect(headings).to_have_count(2)
    expect(headings.nth(1)).to_contain_text(re.compile(r"里昂 \d{2}/\d{2}–\d{2}/\d{2}"))
    first_day = re.compile(r"^第 1 天 · \d{2}.\d{2}")         # 斜杠放进选择器会被解析成正则边界，用 . 代替
    expect(page.get_by_test_id("section-nav").get_by_role("button", name=first_day)).to_have_count(1)
    expect(page.get_by_test_id("section-nav").get_by_role("button", name=re.compile("→ 里昂"))).to_have_count(1)
    # 点里昂：滚到里昂的第一天
    lyon_first = [i for i, t in enumerate(page.get_by_test_id("day-city").all_inner_texts()) if "里昂" in t][0]
    strip.nth(1).click()
    page.wait_for_timeout(1200)
    top = page.locator(".day").nth(lyon_first).bounding_box()["y"]
    assert -5 <= top <= 400, top


def test_m16_the_version_history_says_which_places_were_changed(page: Page):
    open_new(page)
    add_second_city(page)
    pid = _generate(page)
    _start_editing(page, pid)
    lyon_days = _day_city_names(page, "里昂")
    target = page.locator(".day").nth(lyon_days[-1]).locator(".item[data-pid]").first
    name = target.locator("h4").inner_text().replace("🔒", "").strip()
    target.get_by_role("button", name="删除").click()
    page.wait_for_timeout(800)
    page.get_by_role("button", name=re.compile("保存为新版本")).click()
    page.get_by_label("版本名字").fill("删一个")
    page.get_by_role("button", name="保存", exact=True).click()
    expect(page.locator(".edit-bar")).to_have_count(0)
    summary = page.get_by_test_id("history-summary").last
    expect(summary).to_contain_text("天有改动")
    expect(summary).to_contain_text(f"移除 {name}")
    expect(summary).to_contain_text(re.compile(r"第 \d+ 天："))


@pytest.mark.parametrize("width", [1280, 800, 390])
def test_m17_the_cover_title_shrinks_to_fit_on_one_line(page: Page, width: int):
    open_new(page)
    add_second_city(page, "里昂")
    pid = _generate(page)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{WEB}/plans/{pid}")
    h1 = page.get_by_test_id("cover-title").locator("h1")
    expect(h1).to_have_text("巴黎 → 里昂")
    fits = h1.evaluate("""e => {
        const lh = parseFloat(getComputedStyle(e).lineHeight) || parseFloat(getComputedStyle(e).fontSize);
        return {over: e.scrollWidth - e.clientWidth, lines: Math.round(e.getBoundingClientRect().height / lh)};
    }""")
    assert fits["over"] <= 1 and fits["lines"] == 1, fits


def _read_sentence(page: Page, text: str) -> None:
    """在新建页的一句话卡里写一句话，等「我是这样理解的」出来（只填表，不会自动生成）。"""
    open_new(page)
    page.get_by_test_id("nl-text").fill(text)
    page.get_by_test_id("nl-go").click()
    expect(page.get_by_test_id("nl-result")).to_be_visible(timeout=30_000)


def test_m18_one_sentence_with_two_cities_fills_both_cities(page: Page):
    _read_sentence(page, "10月28日去巴黎玩3天，然后去里昂玩3天，两个人")
    expect(page.locator("#destination")).to_have_value("巴黎")
    expect(page.get_by_label("第 2 座城市", exact=True)).to_have_value("里昂")
    expect(page.locator("#start_date")).to_have_value(re.compile(r"-10-28$"))
    expect(page.locator("#end_date")).to_have_value(re.compile(r"-10-30$"))                    # 巴黎玩到 30 号
    expect(page.get_by_label("第 2 座城市的开始日期")).to_have_value(re.compile(r"-10-31$"))   # 里昂第二天开始
    expect(page.get_by_label("第 2 座城市的离开日期")).to_have_value(re.compile(r"-11-02$"))
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 6 天")
    expect(page.get_by_test_id("nl-understood")).to_contain_text("巴黎 3 天 → 里昂 3 天")
    expect(page.get_by_test_id("nl-understood")).to_contain_text("2 位成人")
    expect(page.get_by_test_id("nl-notes")).to_contain_text("第二天")
    assert "/plans/" not in page.url                                                         # 只填表，没有自动生成


def test_m19_cities_without_days_split_the_date_range_evenly(page: Page):
    _read_sentence(page, "去巴黎然后去里昂，10月28日到11月2日")
    expect(page.get_by_label("第 2 座城市", exact=True)).to_have_value("里昂")
    expect(page.get_by_label("第 2 座城市的开始日期")).to_have_value(re.compile(r"-10-31$"))
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 6 天")
    expect(page.get_by_test_id("nl-notes")).to_contain_text("平均分")


def test_m21_a_sentence_with_one_city_reads_exactly_as_before(page: Page):
    _read_sentence(page, "10月20号去巴黎玩4天，然后回国")
    expect(page.locator("#destination")).to_have_value("巴黎")
    expect(page.get_by_label("第 2 座城市", exact=True)).to_have_count(0)
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 4 天")


def test_m21b_a_one_city_sentence_after_a_two_city_one_replaces_the_extra_cities(page: Page):
    """你的截图：先读了「巴黎 → 里昂」，再写「去巴黎玩4天」，旧的第二座城市和离开日期还留着。"""
    _read_sentence(page, "10月28日去巴黎玩3天，然后去里昂玩3天")
    expect(page.get_by_label("第 2 座城市", exact=True)).to_have_value("里昂")
    page.get_by_test_id("nl-text").fill("10月20号去巴黎玩4天，然后回国")
    page.get_by_test_id("nl-go").click()
    expect(page.get_by_test_id("nl-understood")).to_contain_text("共 4 天", timeout=30_000)
    expect(page.get_by_label("第 2 座城市", exact=True)).to_have_count(0)
    expect(page.locator("#end_date")).to_have_value(re.compile(r"-10-23$"))
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 4 天")


def test_m21c_a_sentence_with_only_a_date_moves_the_whole_multi_city_trip(page: Page):
    _read_sentence(page, "10月28日去巴黎玩3天，然后去里昂玩3天")
    page.get_by_test_id("nl-text").fill("11月10日出发")
    page.get_by_test_id("nl-go").click()
    expect(page.locator("#start_date")).to_have_value(re.compile(r"-11-10$"), timeout=30_000)
    expect(page.get_by_label("第 2 座城市", exact=True)).to_have_value("里昂")
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 6 天")
    expect(page.get_by_label("第 2 座城市的开始日期")).to_have_value(re.compile(r"-11-13$"))


def test_m22_the_export_cover_has_a_route_map_per_city(page: Page):
    open_new(page)
    add_second_city(page, "里昂")
    pid = _generate(page)
    page.goto(f"{API}/plans/{pid}/itinerary.html")                              # PDF / 长图用的就是这份 HTML
    expect(page.locator(".routemap")).to_have_count(2)
    titles = page.locator(".city-map-title")
    expect(titles.nth(0)).to_contain_text("巴黎 · 第 1–")
    expect(titles.nth(1)).to_contain_text("里昂 · 第 ")
    pdf = httpx.get(f"{API}/plans/{pid}/itinerary.pdf", timeout=120)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_m23_the_export_shows_a_photo_intro_and_credit_for_places_that_have_one(page: Page):
    open_new(page)
    add_second_city(page, "里昂")
    pid = _generate(page)
    page.goto(f"{API}/plans/{pid}/itinerary.html")
    thumbs = page.locator("img.thumb")
    assert thumbs.count() >= 3                                                  # 巴黎那半有核对过的图
    page.wait_for_load_state("load")
    assert all(thumbs.nth(i).evaluate("e => e.naturalWidth") > 0 for i in range(thumbs.count()))
    expect(page.locator(".hl.credit").first).to_contain_text("图片：")
    expect(page.locator(".hl.intro").first).not_to_be_empty()
    pdf = httpx.get(f"{API}/plans/{pid}/itinerary.pdf", timeout=180)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and len(pdf.content) > 200_000   # 图片嵌进去了


def test_m24_the_downloaded_files_are_named_after_every_destination_in_order(page: Page):
    open_new(page)
    add_second_city(page, "里昂")
    pid = _generate(page)
    with page.expect_download() as dl:
        page.get_by_role("button", name="下载 PDF").click()
    name = dl.value.suggested_filename
    assert name.startswith("巴黎-里昂-") and name.endswith(".pdf"), name
    page.get_by_role("button", name=re.compile("更多")).click()
    with page.expect_download() as dl2:
        page.get_by_role("link", name="导出日历").click()
    assert dl2.value.suggested_filename.startswith("巴黎-里昂-"), dl2.value.suggested_filename
    assert pid.startswith("巴黎-")


def test_m25_a_mainland_day_map_falls_back_to_the_basic_map_and_says_so_without_an_amap_key(page: Page):
    if not any(c["id"] == "kunming" for c in httpx.get(f"{API}/cities", timeout=10).json()):
        pytest.skip("后端里没有境内的测试城市（昆明）：用 pytest 自动起的独立后端跑")
    page.goto(f"{WEB}/new?destination=昆明")
    page.locator(".theme-btn").first.wait_for()
    _generate(page)
    expect(page.locator(".map-fallback-note").first).to_have_text("当前使用基础地图（没配高德 Key）")
    expect(page.locator(".daymap .leaflet-tile").first).to_be_attached()           # 退回 OpenStreetMap，位置仍是对的
    cfg = httpx.get(f"{API}/map/config", timeout=10).json()
    assert cfg == {"provider": "osm", "reason": "no_key"}
    # 导航链接跟着目的地走：境内的一天是高德，不是 Google（M25b）
    links = page.locator(".day-nav a")
    expect(links.first).to_contain_text("在高德地图中查看当天地点")
    assert links.first.get_attribute("href").startswith("https://uri.amap.com/marker?markers=")
    expect(page.get_by_text("在 Google 地图中打开当天路线")).to_have_count(0)


def test_m25b_a_city_outside_china_never_asks_for_amap(page: Page):
    open_new(page)
    _generate(page)
    expect(page.get_by_text("在 Google 地图中打开当天路线").first).to_be_attached()
    expect(page.locator(".daymap .leaflet-tile").first).to_be_attached()
    expect(page.locator(".map-fallback-note")).to_have_count(0)


def _open_kunming(page: Page) -> None:
    if not any(c["id"] == "kunming" for c in httpx.get(f"{API}/cities", timeout=10).json()):
        pytest.skip("后端里没有境内的测试城市（昆明）：用 pytest 自动起的独立后端跑")
    page.goto(f"{WEB}/new?destination=昆明")
    page.locator(".theme-btn").first.wait_for()
    page.locator(".pool > summary").click()


def test_m26_a_mainland_pool_search_offers_amap_and_says_why_when_it_cannot(page: Page):
    _open_kunming(page)
    page.get_by_label("搜索地点库").fill("解放碑")
    expect(page.locator(".pool-hits")).to_contain_text("地点库里没有找到")
    expect(page.locator(".pool-hits")).not_to_contain_text("暂不支持")
    page.get_by_test_id("amap-lookup").click()
    expect(page.get_by_test_id("amap-lookup-note")).to_have_text("没配高德 Key，没法在高德里搜。")   # e2e 后端没配 Key
    expect(page.locator(".pool-pending-row")).to_have_count(0)
    page.get_by_label("搜索地点库").fill("磁器口")                                             # 换个词，按钮回来
    expect(page.get_by_test_id("amap-lookup")).to_have_text("在高德里搜「磁器口」")


def test_m26b_an_amap_place_is_added_straight_to_the_pool_and_the_plan_with_a_check_your_own_hours_note(page: Page):
    _open_kunming(page)
    seen: list[str] = []

    def fake(route):
        seen.append(route.request.url)
        route.fulfill(json={"hits": [{"id": "B0E2E00001", "name": "金马碧鸡坊", "address": "金碧路", "area": "五华区",
                                      "type": "名胜古迹", "category": "sight", "lat": 25.0405, "lng": 102.7190}]})

    page.route("**/places/lookup**", fake)
    page.get_by_label("搜索地点库").fill("金马碧鸡坊")
    expect(page.get_by_test_id("amap-lookup")).to_be_visible()
    assert seen == []                                                     # 输入时的联想不调高德
    page.get_by_test_id("amap-lookup").click()
    hit = page.get_by_test_id("amap-hit")
    expect(hit).to_contain_text("金马碧鸡坊")
    expect(hit).to_contain_text("金碧路")
    hit.get_by_role("button", name="加入并标为必去").click()
    row = page.locator(".pool-row.must")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("金马碧鸡坊")
    expect(row.get_by_test_id("unchecked-hours")).to_have_text("营业时间没核对，出发前请自己查")
    expect(page.get_by_test_id("pool-pending-row")).to_have_count(0)      # 不再先进「待核实」
    pid = _generate(page)
    amap_id = "amap-" + hashlib.sha1("金马碧鸡坊|25.0405|102.7190".encode()).hexdigest()[:10]     # 不存高德的编号
    items = [i for d in _plan(pid)["itinerary"]["days"] for i in d["items"] if i.get("place_id") == amap_id]
    assert len(items) == 1 and "营业时间没核对" in items[0]["notes"]
    expect(page.get_by_text("营业时间没核对，出发前请自己查").first).to_be_visible()


def test_m27_a_chinese_city_name_works_with_or_without_shi(page: Page):
    if not any(c["id"] == "kunming" for c in httpx.get(f"{API}/cities", timeout=10).json()):
        pytest.skip("后端里没有境内的测试城市（昆明市）：用 pytest 自动起的独立后端跑")
    page.goto(f"{WEB}/new?destination=昆明")                                # 城市叫「昆明市」，不用写全
    expect(page.locator(".theme-btn").first).to_be_visible()


def test_m26c_a_city_outside_china_has_no_amap_button(page: Page):
    open_new(page)
    page.locator(".pool > summary").click()
    page.get_by_label("搜索地点库").fill("这个地方不存在xyz")
    expect(page.locator(".pool-hits")).to_contain_text("地点库里没有找到")
    expect(page.get_by_test_id("amap-lookup")).to_have_count(0)


def _generate_kunming(page: Page) -> None:
    page.goto(f"{WEB}/new?destination=昆明")
    page.locator(".theme-btn").first.wait_for()
    _generate(page)


def test_m28_a_mainland_day_shows_real_commute_times_only_after_the_button_is_clicked(page: Page):
    if not any(c["id"] == "kunming" for c in httpx.get(f"{API}/cities", timeout=10).json()):
        pytest.skip("后端里没有境内的测试城市（昆明）：用 pytest 自动起的独立后端跑")
    asked: list[dict] = []

    def fake(route):
        body = json.loads(route.request.post_data)
        asked.append(body)
        steps = ["步行 300 米", "乘地铁1号线：北京路 → 昆明火车站（4 站）", "步行 150 米"]
        route.fulfill(json={"legs": [{"minutes": 45, "mode": "公交 / 地铁", "distance_m": 6200, "steps": steps}
                                     for _ in body["legs"]]})

    page.route("**/commute", fake)
    _generate_kunming(page)
    assert asked == []                                                     # 打开行程不调高德
    expect(page.get_by_test_id("commute-leg")).to_have_count(0)
    page.get_by_test_id("commute-load").first.click()
    legs = page.get_by_test_id("commute-leg")
    expect(legs.first).to_contain_text("实际公交 / 地铁约 45 分钟")
    assert len(asked) == 1 and asked[0]["city"] == "kunming"
    first_day_items = len(asked[0]["legs"])
    expect(page.locator(".day").first.get_by_test_id("commute-leg")).to_have_count(first_day_items)
    expect(legs.first).to_contain_text("比排程估的多")                          # 45 分钟比估算多出 ≥ 10 分钟
    legs.first.locator("summary").click()
    expect(legs.first.locator("li")).to_have_count(3)
    expect(page.get_by_test_id("commute-load").first).to_have_text("🚶 重新查询通勤时间")


def test_m28b_without_a_key_the_button_says_why_and_the_estimate_stays(page: Page):
    if not any(c["id"] == "kunming" for c in httpx.get(f"{API}/cities", timeout=10).json()):
        pytest.skip("后端里没有境内的测试城市（昆明）：用 pytest 自动起的独立后端跑")
    _generate_kunming(page)
    page.get_by_test_id("commute-load").first.click()
    expect(page.get_by_test_id("commute-reason").first).to_have_text("没配高德 Key，先用排程的估算")
    expect(page.get_by_test_id("commute-leg")).to_have_count(0)
    expect(page.locator(".day").first.get_by_text("（估算）").first).to_be_visible()


def test_m29_a_city_outside_china_has_no_commute_button(page: Page):
    open_new(page)
    _generate(page)
    expect(page.get_by_test_id("commute-load")).to_have_count(0)
