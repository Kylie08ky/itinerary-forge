"""v0.3 批次 G 地点图片与简介的前端验收（specs/modules/M02 S7b）。

默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_media.py
"""

from __future__ import annotations

import os
import re
from datetime import datetime

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


def make_page(browser, width=1400, height=1800):
    ctx = browser.new_context(viewport={"width": width, "height": height}, locale="zh-CN",
                              is_mobile=width < 500, has_touch=width < 500)
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    page = ctx.new_page()
    # 行程之前：不出现「今天」卡片。用 install 而不是 set_fixed_time：Leaflet 的平移动画靠时间推进，时间冻住动画就不动了
    page.clock.install(time=datetime(2026, 9, 1, 12, 0))
    return ctx, page, natives


@pytest.fixture
def page(browser) -> Page:
    ctx, pg, natives = make_page(browser)
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


@pytest.fixture(scope="module")
def places() -> dict[str, dict]:
    return {p["id"]: p for p in httpx.get(f"{API}/places", timeout=30).json()}


@pytest.fixture(scope="module")
def pid() -> str:
    body = {"destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["museums"], "theme": "museums",
            "arrival_time": "2026-10-20 10:30", "departure_time": "2026-10-23 18:00"}
    r = httpx.post(f"{API}/plans", json=body, timeout=120)
    assert r.status_code == 201, r.text
    plan_id = r.json()["plan_id"]
    yield plan_id
    httpx.delete(f"{API}/plans/{plan_id}", timeout=30)


@pytest.fixture(scope="module")
def items(pid, places) -> dict:
    """行程里的地点条目：with = 有图片；without = 没有图片（可能有简介）；none = 图片和简介都没有。"""
    plan = httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]
    seen, with_img, without, none = set(), [], [], []
    for d in plan["days"]:
        for it in d["items"]:
            p = it.get("place_id")
            if not p or p in seen:
                continue
            seen.add(p)
            m = places[p].get("media") or {}
            (with_img if m.get("image") else without).append((it["title"], p))
            if not m:
                none.append((it["title"], p))
    return {"with": with_img, "without": without, "none": none}


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()


def card(page: Page):
    return page.get_by_role("dialog", name="地点详情")


def test_the_museums_plan_has_places_with_and_without_photos(items):
    assert len(items["with"]) >= 3, items


# --------------------------------------------------------------------------- #
# S7b-1 详情卡
# --------------------------------------------------------------------------- #
def test_the_place_card_shows_the_photo_with_its_credit_and_the_intro_with_its_source(page, pid, items, places):
    title, place_id = next((t, p) for t, p in items["with"] if places[p]["media"].get("intro"))
    media = places[place_id]["media"]
    open_plan(page, pid)
    page.get_by_role("button", name=title, exact=True).first.click()
    c = card(page)
    photo = c.get_by_test_id("place-photo")
    expect(photo).to_be_visible()
    img = photo.locator("img")
    expect(img).to_have_attribute("alt", places[place_id]["name_zh"])
    assert img.evaluate("e => e.complete && e.naturalWidth > 0")                       # 真的加载出来了
    credit = c.get_by_test_id("photo-credit")
    expect(credit).to_contain_text(f"摄影：{media['image']['author']}")
    expect(credit).to_contain_text(media["image"]["license"])
    expect(credit.get_by_role("link", name=re.compile("来源"))).to_have_attribute("href", media["image"]["source_url"])
    assert media["image"]["source_url"].startswith("https://commons.wikimedia.org/")
    if media["image"]["license_url"]:
        expect(credit.get_by_role("link", name=media["image"]["license"])).to_have_attribute(
            "href", media["image"]["license_url"])
    intro = c.get_by_test_id("place-intro")
    expect(intro).to_contain_text(media["intro"]["text"])
    expect(intro).to_contain_text("来源：中文维基百科")
    src_link = intro.get_by_role("link", name=re.compile("中文维基百科"))
    expect(src_link).to_have_attribute("href", media["intro"]["source_url"])
    expect(intro.get_by_role("link", name="CC BY-SA 4.0")).to_have_attribute("href", re.compile("creativecommons.org"))


def test_the_credit_and_source_links_open_in_a_new_tab_safely(page, pid, items):
    title, _ = items["with"][0]
    open_plan(page, pid)
    page.get_by_role("button", name=title, exact=True).first.click()
    for link in card(page).get_by_test_id("photo-credit").get_by_role("link").all():
        assert link.get_attribute("target") == "_blank" and "noreferrer" in link.get_attribute("rel")


def test_a_place_without_media_has_no_photo_area_and_no_intro(page, pid, items):
    if not items["none"]:
        pytest.skip("这份行程里的地点都有图片或简介")
    title, _ = items["none"][0]
    open_plan(page, pid)
    page.get_by_role("button", name=title, exact=True).first.click()
    c = card(page)
    c.get_by_role("heading").first.wait_for()
    expect(c.get_by_test_id("place-photo")).to_have_count(0)
    expect(c.get_by_test_id("place-intro")).to_have_count(0)
    expect(c.locator("img")).to_have_count(0)


def test_when_the_photo_file_cannot_be_loaded_the_whole_photo_block_including_the_credit_goes_away(page, pid, items):
    title, _ = items["with"][0]
    open_plan(page, pid)
    page.route(re.compile(r".*/media/places/.*"), lambda r: r.abort())
    page.get_by_role("button", name=title, exact=True).first.click()
    c = card(page)
    c.get_by_role("heading").first.wait_for()
    page.wait_for_timeout(500)
    expect(c.get_by_test_id("place-photo")).to_have_count(0)
    expect(c.get_by_text("摄影：")).to_have_count(0)                                      # 署名不单独留着
    expect(c.get_by_role("heading").first).to_be_visible()                              # 详情卡其余部分照常


# --------------------------------------------------------------------------- #
# S7b-3 时间轴缩略图
# --------------------------------------------------------------------------- #
def test_each_place_with_a_photo_has_a_small_thumbnail_that_opens_the_card(page, pid, items):
    open_plan(page, pid)
    thumbs = page.get_by_test_id("item-thumb")
    expect(thumbs.first).to_be_visible()
    shown = {p for _, p in items["with"]}
    # 每天一个景点最多一张；整份行程里出现过的有图片的地点，数量对得上
    assert thumbs.count() == sum(1 for d in httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]["days"]
                                 for it in d["items"] if it.get("place_id") in shown)
    box = thumbs.first.bounding_box()
    assert 70 <= box["width"] <= 74 and 70 <= box["height"] <= 74
    first = thumbs.first
    assert first.get_attribute("title").startswith("摄影：")
    first.click()
    expect(card(page).get_by_test_id("place-photo")).to_be_visible()


def test_places_without_a_photo_have_no_thumbnail(page, pid, items):
    if not items["without"]:
        pytest.skip("这份行程里的地点都有图片")
    title, _ = items["without"][0]
    open_plan(page, pid)
    item = page.locator(".item").filter(has=page.get_by_role("button", name=title, exact=True)).first
    expect(item.get_by_test_id("item-thumb")).to_have_count(0)


def test_thumbnails_are_lazy_and_a_broken_one_hides_itself(page, pid):
    open_plan(page, pid)
    imgs = page.get_by_test_id("item-thumb").locator("img")
    assert imgs.first.get_attribute("loading") == "lazy"


def test_a_thumbnail_that_fails_to_load_disappears_instead_of_showing_a_broken_image(browser, pid):
    ctx, pg, _ = make_page(browser)
    try:
        pg.route(re.compile(r".*/media/places/.*"), lambda r: r.abort())
        open_plan(pg, pid)
        pg.wait_for_timeout(800)
        thumbs = pg.get_by_test_id("item-thumb")
        assert thumbs.count() >= 3
        for t in thumbs.all():
            t.evaluate("e => e.scrollIntoView({block: 'center'})")           # 懒加载：滚到眼前才开始加载
            pg.wait_for_timeout(300)
            assert t.is_hidden()
    finally:
        ctx.close()


# --------------------------------------------------------------------------- #
# S7b-4 地图气泡
# --------------------------------------------------------------------------- #
def test_hovering_a_marker_on_the_map_shows_a_bubble_with_the_photo_and_the_name(page, pid):
    open_plan(page, pid)
    map_ = page.locator(".plan-aside .fullmap")
    map_.wait_for()
    page.locator("button.lg", has_text=re.compile("第 2 天")).click()                  # 只看一天，标记不会被聚合成圈
    pin = map_.locator(".map-pin").first
    pin.wait_for()
    pin.hover()
    bubble = page.locator(".leaflet-tooltip .map-bubble")
    expect(bubble).to_be_visible()
    expect(bubble).to_contain_text("第 2 天")
    expect(bubble.locator("img")).to_have_count(1)


def test_the_day_map_has_the_same_bubbles(browser, pid):
    ctx, page, _ = make_page(browser, 1000, 1800)                           # 窄一点：没有右侧大地图，每天自带小地图
    try:
        _day_map_bubble(page, pid)
    finally:
        ctx.close()


def _day_map_bubble(page, pid):
    open_plan(page, pid)
    page.wait_for_load_state("networkidle")            # 地点库 / 路线数据到了，每天的小地图会重画一次；等它画完再去悬停
    page.wait_for_timeout(800)
    for attempt in range(3):
        try:
            pin = page.locator(".day .daymap .map-pin").first
            pin.scroll_into_view_if_needed()
            pin.hover()
            break
        except Exception:                               # noqa: BLE001 - 重画的瞬间标记被换掉了，再来一次
            if attempt == 2:
                raise
            page.wait_for_timeout(500)
    expect(page.locator(".leaflet-tooltip .map-bubble")).to_be_visible()


# --------------------------------------------------------------------------- #
# 分享页也有缩略图和照片（你验收后改的）
# --------------------------------------------------------------------------- #
def test_the_share_page_also_has_thumbnails_bubble_photos_and_the_place_card_with_its_credit(page, pid, items):
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=10).json()["token"]
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    thumbs = page.get_by_test_id("item-thumb")
    expect(thumbs.first).to_be_visible()
    assert thumbs.count() >= 3
    page.locator(".fullmap .map-pin").first.hover()
    expect(page.locator(".leaflet-tooltip .map-bubble img")).to_have_count(1)
    title, _ = items["with"][0]
    page.get_by_role("button", name=title, exact=True).first.click()
    expect(card(page).get_by_test_id("place-photo")).to_be_visible()
    expect(card(page).get_by_test_id("photo-credit")).to_contain_text("摄影：")


# --------------------------------------------------------------------------- #
# 气泡永远完整显示在地图里（你验收后提的：悬停气泡被地图边缘裁掉）
# --------------------------------------------------------------------------- #
def inside(page: Page, map_selector: str) -> list[str]:
    """对地图上每个标记悬停一次，返回气泡超出地图的描述（空 = 都完整显示）。"""
    problems = []
    n = page.locator(f"{map_selector} .map-pin").count()
    for i in range(n):
        pin = page.locator(f"{map_selector} .map-pin").nth(i)
        pin.hover(force=True)
        page.wait_for_timeout(700)                                        # 平移动画 + 照片加载后再量一次
        tip = page.locator(".leaflet-tooltip").first
        if tip.count() == 0:
            continue
        r = page.evaluate("""([sel]) => {
            const m = document.querySelector(sel).getBoundingClientRect();
            const t = document.querySelector('.leaflet-tooltip').getBoundingClientRect();
            return {l: t.left - m.left, t: t.top - m.top, r: m.right - t.right, b: m.bottom - t.bottom};
        }""", [map_selector])
        if min(r.values()) < -1.5:
            problems.append(f"第 {i + 1} 个标记的气泡超出：{r}")
    return problems


def test_on_the_big_desktop_map_every_bubble_fits_inside_the_map(page, pid):
    open_plan(page, pid)
    page.locator(".plan-aside .fullmap").wait_for()
    for day in (1, 2, 3, 4):
        page.locator("button.lg", has_text=re.compile(f"第 {day} 天")).click()        # 只看一天：标记不聚合
        page.wait_for_timeout(500)
        assert not inside(page, ".plan-aside .fullmap"), f"第 {day} 天"
        page.locator("button.lg", has_text=re.compile(f"第 {day} 天")).click()


def test_on_the_per_day_map_every_bubble_fits_inside_the_map(browser, pid):
    ctx, pg, _ = make_page(browser, 1000, 1800)
    try:
        open_plan(pg, pid)
        for k in range(2):
            day_map = f".day:nth-of-type({k + 1}) .daymap"
            sel = ".daymap"
            maps = pg.locator(sel)
            maps.nth(k).scroll_into_view_if_needed()
            problems = []
            checked = 0
            pins = maps.nth(k).locator(".map-pin")
            for i in range(pins.count()):
                pins.nth(i).hover(force=True)
                pg.wait_for_timeout(700)
                if pg.locator(".leaflet-tooltip").count() == 0:     # 两个标记叠在一起时，鼠标下的是另一个
                    continue
                checked += 1
                r = pg.evaluate("""([idx]) => {
                    const m = document.querySelectorAll('.daymap')[idx].getBoundingClientRect();
                    const t = document.querySelector('.leaflet-tooltip').getBoundingClientRect();
                    return {l: t.left - m.left, t: t.top - m.top, r: m.right - t.right, b: m.bottom - t.bottom};
                }""", [k])
                if min(r.values()) < -1.5:
                    problems.append((day_map, i, r))
            assert not problems, problems
            assert checked >= 1, "一个气泡都没出现"
    finally:
        ctx.close()


def test_on_a_phone_the_bubble_fits_inside_the_map_too(browser, pid):
    ctx, pg, natives = make_page(browser, 390, 844)
    try:
        open_plan(pg, pid)
        maps = pg.locator(".daymap")
        assert maps.count() >= 1
        maps.first.scroll_into_view_if_needed()
        pins = maps.first.locator(".map-pin")
        checked = 0
        for i in range(pins.count()):
            pins.nth(i).hover(force=True)
            pg.wait_for_timeout(700)
            if pg.locator(".leaflet-tooltip").count() == 0:
                continue
            checked += 1
            r = pg.evaluate("""() => {
                const m = document.querySelector('.daymap').getBoundingClientRect();
                const t = document.querySelector('.leaflet-tooltip').getBoundingClientRect();
                return {l: t.left - m.left, t: t.top - m.top, r: m.right - t.right, b: m.bottom - t.bottom};
            }""")
            assert min(r.values()) >= -1.5, (i, r)
        assert checked >= 1, "一个气泡都没出现"
    finally:
        ctx.close()
    assert not natives


# --------------------------------------------------------------------------- #
# 手机
# --------------------------------------------------------------------------- #
def test_on_a_phone_thumbnails_do_not_cause_sideways_scrolling_and_the_card_fits(browser, pid, items):
    ctx, pg, natives = make_page(browser, 390, 844)
    try:
        open_plan(pg, pid)
        size = pg.evaluate("({sw: document.documentElement.scrollWidth, iw: window.innerWidth})")
        assert size["sw"] <= size["iw"], size
        widths = pg.locator(".item > div:last-child").evaluate_all(
            "els => els.map(e => e.getBoundingClientRect().width)")
        assert widths and min(widths) >= 220, widths                    # 时间轴每一条的内容列不能被挤成窄条
        title, _ = items["with"][0]
        pg.get_by_role("button", name=title, exact=True).first.click()
        c = card(pg)
        c.get_by_test_id("place-photo").wait_for()
        box = c.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 390 + 1
        size = pg.evaluate("({sw: document.documentElement.scrollWidth, iw: window.innerWidth})")
        assert size["sw"] <= size["iw"], size
    finally:
        ctx.close()
    assert not natives
