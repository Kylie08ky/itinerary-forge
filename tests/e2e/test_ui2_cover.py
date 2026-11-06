"""UI2 批次 J3 行程页封面（Mostar 结构，紧凑版）的前端验收（specs/06-ui-design.md S15–S20）。

像真人一样：进入行程页 → 全屏封面（大图 + 大标题）→ 往下滚（统计面板、行程亮点卡片飞入）→ 点卡片开详情 / 跳过封面；
手机、「减少动态效果」、没有大图时是静态一屏；历史版本 / 编辑中不出现封面。

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_ui2_cover.py
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
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
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


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.get_by_test_id("trip-cover").wait_for()
    page.locator(".day").first.wait_for(state="attached")


CTL_READY = "() => document.querySelector('[data-testid=trip-cover]').classList.contains('ctl-ready')"


def cover_var(page: Page, name: str) -> str:
    return page.evaluate(
        "n => getComputedStyle(document.querySelector('[data-testid=trip-cover]')).getPropertyValue(n).trim()", name)


def scroll_to(page: Page, y: int, until: str | None = None) -> None:
    """滚到封面里的第 y 像素，等惯性走完（滚动位置算出的变量不再变化）。"""
    page.evaluate("y => window.scrollTo(0, y)", y)
    if until:
        page.wait_for_function(until, timeout=15_000)
    else:
        page.wait_for_timeout(1500)


# ---- S15：首屏 ----
def test_cover_is_a_full_screen_photo_stage_with_title_dates_and_credit(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    cover = page.get_by_test_id("trip-cover")
    expect(cover).to_have_attribute("data-mode", "story")
    stage = page.locator(".cover-stage")
    box = stage.bounding_box()
    assert box and box["height"] >= 800 - 56 - 2, box                              # 占满导航条下面的整屏
    page.get_by_test_id("cover-photo").wait_for()
    title = page.get_by_test_id("cover-title")
    expect(title.locator("h1")).to_have_text("巴黎")
    expect(title).to_contain_text("博物馆之旅")
    expect(title.locator(".big-dates")).to_have_text(re.compile(r"^\d\d\.\d\d—\d\d\.\d\d$"))
    expect(title).to_contain_text("共 4 天 3 晚")
    expect(page.get_by_test_id("cover-skip")).to_be_visible()
    credit = page.get_by_test_id("hero-credit")
    expect(credit).to_contain_text("摄影：")
    assert "commons.wikimedia.org" in credit.locator("a").nth(1).get_attribute("href")
    url = page.get_by_test_id("cover-photo").evaluate("e => getComputedStyle(e).backgroundImage")
    assert "/media/hero/" in url


def test_the_cover_photo_is_a_landmark_from_this_trip(page: Page):
    pid = new_plan("art")
    plan = httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]
    in_trip = {i["place_id"] for d in plan["days"] for i in d["items"] if i.get("place_id")}
    open_plan(page, pid)
    url = page.get_by_test_id("cover-photo").evaluate("e => getComputedStyle(e).backgroundImage")
    pid_in_url = re.search(r"/media/hero/([a-z0-9-]+)\.jpg", url).group(1)
    assert pid_in_url in in_trip, (pid_in_url, in_trip)                             # 大图是这趟行程里的地点


# ---- S16：滚动 ----
def test_scrolling_plays_title_stats_and_highlight_cards_in_order(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    assert float(cover_var(page, "--title-o")) > 0.99 and float(cover_var(page, "--stats-o")) < 0.01
    assert cover_var(page, "--cards-vis") == "hidden"
    expect(page.get_by_test_id("cover-cards")).to_be_hidden()                       # 还没飞进来

    scroll_to(page, 300, "() => parseFloat(getComputedStyle(document.querySelector('[data-testid=trip-cover]'))"
                         ".getPropertyValue('--title-o')) < 0.05")
    assert float(cover_var(page, "--photo-blur").removesuffix("px")) > 1                # 照片已经开始模糊

    scroll_to(page, 760 + 56, "() => parseFloat(getComputedStyle(document.querySelector('[data-testid=trip-cover]'))"
                              ".getPropertyValue('--stats-o')) > 0.98")
    stats = page.get_by_test_id("cover-stats")
    expect(stats).to_be_visible()
    texts = stats.inner_text()
    assert "天" in texts and "个地点" in texts and "门票合计" in texts and "公里" in texts

    scroll_to(page, 1100 + 56, CTL_READY)
    cards = page.get_by_test_id("cover-card")
    pids = cards.evaluate_all("els => els.map(e => e.dataset.pid)")
    # 每天一张亮点（4 天 = 4 张），每张只出现一次：不循环、不复制
    assert len(pids) >= 3 and len(set(pids)) == len(pids), pids
    expect(page.get_by_test_id("cover-cards")).to_be_visible()
    expect(page.get_by_test_id("cover-next")).to_be_visible()


def scroll_left(page: Page) -> float:
    return page.get_by_test_id("cover-track").evaluate("e => e.scrollLeft")


def test_cards_open_the_place_card_and_the_arrows_scroll_the_row(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    scroll_to(page, 1150 + 56, CTL_READY)
    expect(page.get_by_test_id("cover-prev")).to_be_disabled()                        # 在最左边：不能再往左
    page.get_by_test_id("cover-next").click()
    page.wait_for_function("() => document.querySelector('[data-testid=cover-track]').scrollLeft > 100")
    expect(page.get_by_test_id("cover-prev")).to_be_enabled()
    page.get_by_test_id("cover-prev").click()
    page.wait_for_function("() => document.querySelector('[data-testid=cover-track]').scrollLeft < 5")
    # 一直点 →：滚到头就变灰，不会循环回来
    for _ in range(8):
        if page.get_by_test_id("cover-next").is_disabled():
            break
        page.get_by_test_id("cover-next").click()
        page.wait_for_timeout(700)
    expect(page.get_by_test_id("cover-next")).to_be_disabled()
    card = page.get_by_test_id("cover-card").last
    name = card.locator("h3").inner_text()
    card.click()
    dialog = page.get_by_role("dialog", name="地点详情")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text(name)
    page.keyboard.press("Escape")


def drag(page: Page, x0: float, y: float, dx: float) -> None:
    page.mouse.move(x0, y)
    page.mouse.down()
    page.mouse.move(x0 + dx, y, steps=12)
    page.mouse.up()


def test_the_cards_scroll_with_a_mouse_drag_and_a_drag_is_not_a_click(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    scroll_to(page, 1150 + 56, CTL_READY)
    first = page.get_by_test_id("cover-card").first.bounding_box()
    x, y = first["x"] + first["width"] / 2, first["y"] + first["height"] / 2
    assert scroll_left(page) < 5
    drag(page, x, y, -300)                                                            # 按住向左拖：卡片向左走
    page.wait_for_timeout(500)
    assert scroll_left(page) > 100
    expect(page.get_by_role("dialog", name="地点详情")).to_have_count(0)               # 拖动不会顺手点开卡片
    drag(page, x, y, 300)                                                             # 向右拖回来
    page.wait_for_timeout(500)
    assert scroll_left(page) < 40
    page.get_by_test_id("cover-card").first.click()                                   # 单纯点一下照样打开
    expect(page.get_by_role("dialog", name="地点详情")).to_be_visible()
    page.keyboard.press("Escape")


def test_the_cards_always_leave_something_to_slide_on_common_screen_widths(browser):
    # 用户反馈「没办法滑动」：4 张卡在 1700 宽的屏幕上刚好全放下，没有多出来的内容可滑。
    # 现在卡片宽度按张数算，最后一张总会露出一截。
    pid = new_plan()
    for width in (1280, 1702, 1920):
        ctx = browser.new_context(viewport={"width": width, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        try:
            open_plan(page, pid)
            scroll_to(page, 1150 + 56, CTL_READY)
            room = page.get_by_test_id("cover-track").evaluate("e => e.scrollWidth - e.clientWidth")
            assert room >= 150, (width, room)
            last = page.get_by_test_id("cover-card").last.bounding_box()
            assert last["x"] < width - 40 and last["x"] + last["width"] > width, (width, last)      # 露出一截
            expect(page.get_by_test_id("cover-next")).to_be_enabled()
        finally:
            ctx.close()


def test_two_finger_trackpad_and_touch_swipes_scroll_the_cards_too(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN", has_touch=True)
    page = ctx.new_page()
    try:
        open_plan(page, new_plan())
        scroll_to(page, 1150 + 56, CTL_READY)
        box = page.get_by_test_id("cover-card").first.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.wheel(250, 0)                                                      # 触控板双指左右划 = 横向滚轮
        page.wait_for_timeout(700)
        after_wheel = scroll_left(page)
        assert after_wheel > 100, after_wheel
        page.mouse.wheel(-250, 0)
        page.wait_for_timeout(700)
        assert scroll_left(page) < after_wheel - 100
        cdp = ctx.new_cdp_session(page)                                               # 触屏：手指按下 → 向左划 → 抬起
        before = scroll_left(page)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for i in range(1, 11):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x - i * 20, "y": y}]})
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(900)
        assert scroll_left(page) > before + 100, (before, scroll_left(page))
        expect(page.get_by_role("dialog", name="地点详情")).to_have_count(0)
    finally:
        ctx.close()


def test_a_slightly_diagonal_two_finger_swipe_still_moves_the_cards_not_the_page(browser):
    # 用户反馈「又不滚动了」：触控板双指左右划总会带一点上下分量，浏览器按主方向把整个手势交给页面纵向滚动，卡片不动
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN", has_touch=True)
    page = ctx.new_page()
    try:
        open_plan(page, new_plan())
        scroll_to(page, 1150 + 56, CTL_READY)
        box = page.get_by_test_id("cover-card").first.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        y0 = page.evaluate("window.scrollY")
        page.mouse.wheel(120, 90)                                                     # 横向 120、纵向 90：带点斜
        page.wait_for_timeout(700)
        assert scroll_left(page) > 60, scroll_left(page)
        assert abs(page.evaluate("window.scrollY") - y0) < 30                         # 页面没有被带着上下滚
    finally:
        ctx.close()


def test_a_two_finger_touchscreen_swipe_scrolls_the_cards(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN", has_touch=True)
    page = ctx.new_page()
    try:
        open_plan(page, new_plan())
        scroll_to(page, 1150 + 56, CTL_READY)
        box = page.get_by_test_id("cover-card").first.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        cdp = ctx.new_cdp_session(page)
        before = scroll_left(page)
        two = lambda dx: [{"x": x + dx, "y": y}, {"x": x + dx + 60, "y": y + 8}]       # noqa: E731  两根手指一起平移
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": two(0)})
        for i in range(1, 11):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": two(-i * 20)})
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(900)
        assert scroll_left(page) > before + 100, (before, scroll_left(page))
        expect(page.get_by_role("dialog", name="地点详情")).to_have_count(0)
    finally:
        ctx.close()


def test_swiping_on_the_blank_stage_beside_the_cards_also_moves_them_and_vertical_swipes_still_scroll_the_page(browser):
    # 手指不一定刚好落在卡片上：舞台空白处左右划 / 横向滚轮也要带动卡片；上下划仍然是滚页面
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN", has_touch=True)
    page = ctx.new_page()
    try:
        open_plan(page, new_plan())
        scroll_to(page, 1150 + 56, CTL_READY)
        box = page.get_by_test_id("cover-card").first.bounding_box()
        x, y = 640, max(box["y"] - 120, 120)                                           # 卡片行上方的空白
        assert page.evaluate("([x, y]) => !document.elementFromPoint(x, y).closest('.cover-track')", [x, y])
        page.mouse.move(x, y)
        page.mouse.wheel(250, 0)
        page.wait_for_timeout(600)
        assert scroll_left(page) > 100, scroll_left(page)
        page.mouse.wheel(-300, 0)
        page.wait_for_timeout(600)
        cdp = ctx.new_cdp_session(page)
        before = scroll_left(page)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for i in range(1, 11):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x - i * 20, "y": y + i}]})
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(500)
        assert scroll_left(page) > before + 100, (before, scroll_left(page))
        y0 = page.evaluate("window.scrollY")                                            # 上下划：照样滚页面
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for i in range(1, 11):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x + i, "y": y + i * 20}]})
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(700)
        assert abs(page.evaluate("window.scrollY") - y0) > 40
    finally:
        ctx.close()


def test_two_fingers_with_only_one_on_a_card_still_move_the_cards(browser):
    # 用户反馈：电脑触屏双指左右滑动没反应、上下可以——两根手指不一定都落在卡片上，浏览器按第一根手指所在的元素决定谁滚
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN", has_touch=True)
    page = ctx.new_page()
    try:
        open_plan(page, new_plan())
        scroll_to(page, 1150 + 56, CTL_READY)
        box = page.get_by_test_id("cover-card").first.bounding_box()
        blank = (640, max(box["y"] - 100, 120))                                       # 第一根手指在卡片行上方的空白
        card = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)             # 第二根手指在卡片上
        cdp = ctx.new_cdp_session(page)
        before = scroll_left(page)
        pts = lambda dx: [{"x": blank[0] + dx, "y": blank[1], "id": 1}, {"x": card[0] + dx, "y": card[1], "id": 2}]  # noqa: E731
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pts(0)[:1]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pts(0)})
        for i in range(1, 11):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": pts(-i * 20)})
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(900)
        assert scroll_left(page) > before + 100, (before, scroll_left(page))
        expect(page.get_by_role("dialog", name="地点详情")).to_have_count(0)
    finally:
        ctx.close()


def test_the_stage_leaves_only_vertical_panning_to_the_browser(browser):
    # Chrome 把电脑触屏上的双指当成「捏合」，横向的双指划到不了卡片行（Claude 自带浏览器没有这个行为）。
    # 所以舞台上只让浏览器接管上下划（touch-action: pan-y），左右划由页面自己的指针事件带动卡片。
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN", has_touch=True)
    page = ctx.new_page()
    try:
        open_plan(page, new_plan())
        for sel in ("[data-testid=cover-track]", "[data-testid=cover-card]", ".cover-stage"):
            assert page.locator(sel).first.evaluate("e => getComputedStyle(e).touchAction") == "pan-y", sel
        scroll_to(page, 1150 + 56, CTL_READY)
        box = page.get_by_test_id("cover-card").first.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        cdp = ctx.new_cdp_session(page)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for i in range(1, 6):                                                          # 快速甩一下
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x - i * 30, "y": y}]})
            page.wait_for_timeout(8)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(900)
        assert scroll_left(page) > 100, scroll_left(page)
        expect(page.get_by_role("dialog", name="地点详情")).to_have_count(0)           # 甩完不算点击
    finally:
        ctx.close()


def test_in_a_short_window_the_cover_is_the_static_one_so_no_card_is_cut_off(browser):
    # 用户反馈：窗口不高时卡片左边被截断、下半截掉到屏幕外。原因：钉住的舞台最矮 560px，窗口矮时进场动画走不完、
    # 卡片也在屏幕下面；所以窗口高度不够（< 616px）就用静态版（一排可横划的卡片）。
    ctx = browser.new_context(viewport={"width": 760, "height": 330}, locale="zh-CN", has_touch=True)
    pg = ctx.new_page()
    try:
        open_plan(pg, new_plan())
        assert pg.get_by_test_id("trip-cover").get_attribute("data-mode") == "static"
        row = pg.get_by_test_id("cover-strip-row")
        row.scroll_into_view_if_needed()
        box = row.bounding_box()
        assert box["x"] == 0 and box["width"] <= 760
        first = pg.get_by_test_id("cover-card").first.bounding_box()
        assert first["x"] >= 0 and first["y"] + first["height"] <= 330 + pg.evaluate("window.scrollY") + 400
        assert row.evaluate("e => e.scrollWidth > e.clientWidth")
    finally:
        ctx.close()


def test_a_real_touchpad_swipe_of_many_tiny_wheel_steps_moves_the_story_cards(browser):
    # 用户反馈：触控板双指左右划不动。触控板的一划是几十个几像素的小滚轮事件，不是一个大滚轮；
    # 每个小增量都被卡片行的吸附（scroll-snap）拉回原位，累计就是 0。
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
    pg = ctx.new_page()
    try:
        open_plan(pg, new_plan())
        scroll_to(pg, 1150 + 56, CTL_READY)
        box = pg.get_by_test_id("cover-card").first.bounding_box()
        pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(40):
            pg.mouse.wheel(6, 0)
            pg.wait_for_timeout(16)
        pg.wait_for_timeout(600)
        assert scroll_left(pg) > 150, scroll_left(pg)
    finally:
        ctx.close()


def test_a_real_touchpad_swipe_of_many_tiny_wheel_steps_moves_the_static_row(browser):
    ctx = browser.new_context(viewport={"width": 520, "height": 800}, locale="zh-CN")
    pg = ctx.new_page()
    try:
        open_plan(pg, new_plan())
        row = pg.get_by_test_id("cover-strip-row")
        row.scroll_into_view_if_needed()
        box = row.bounding_box()
        pg.mouse.move(box["x"] + 250, box["y"] + 60)
        for _ in range(40):
            pg.mouse.wheel(6, 0)
            pg.wait_for_timeout(16)
        pg.wait_for_timeout(600)
        assert row.evaluate("e => e.scrollLeft") > 150
    finally:
        ctx.close()


def test_the_static_card_row_also_drags_with_a_mouse(browser):
    pid = new_plan()
    ctx = browser.new_context(viewport={"width": 520, "height": 800}, locale="zh-CN")
    pg = ctx.new_page()
    try:
        open_plan(pg, pid)
        row = pg.get_by_test_id("cover-strip-row")
        row.scroll_into_view_if_needed()
        box = row.bounding_box()
        assert row.evaluate("e => e.scrollWidth > e.clientWidth")
        drag(pg, box["x"] + 300, box["y"] + 60, -220)
        pg.wait_for_timeout(300)
        assert row.evaluate("e => e.scrollLeft") > 100                                 # 横排被拖着走了
        expect(pg.get_by_role("dialog", name="地点详情")).to_have_count(0)
    finally:
        ctx.close()


@pytest.mark.parametrize("ctx_kw", [
    {"viewport": {"width": 520, "height": 800}},                                       # 窄窗口 / 手机宽度
    {"viewport": {"width": 900, "height": 800}, "reduced_motion": "reduce"},           # 系统关了动画
], ids=["narrow", "reduced-motion"])
def test_the_static_card_row_moves_with_a_two_finger_touch_swipe_too(browser, ctx_kw):
    # 用户的 Chrome 不一定走桌面故事版：窄窗口或系统「减少动态效果」时用的是静态卡片行，它也得能双指横划
    pid = new_plan()
    ctx = browser.new_context(locale="zh-CN", has_touch=True, **ctx_kw)
    pg = ctx.new_page()
    try:
        open_plan(pg, pid)
        row = pg.get_by_test_id("cover-strip-row")
        row.scroll_into_view_if_needed()
        assert row.evaluate("e => getComputedStyle(e).touchAction") == "pan-y"
        box = row.bounding_box()
        x, y = box["x"] + 200, box["y"] + 40
        cdp = ctx.new_cdp_session(pg)
        two = lambda dx: [{"x": x + dx, "y": y}, {"x": x + dx + 60, "y": y + 8}]       # noqa: E731
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": two(0)})
        for i in range(1, 9):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": two(-i * 20)})
            pg.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        pg.wait_for_timeout(800)
        assert row.evaluate("e => e.scrollLeft") > 100
        expect(pg.get_by_role("dialog", name="地点详情")).to_have_count(0)
    finally:
        ctx.close()


# ---- S20：手机上行程页顶部不挤 ----
def test_on_a_phone_the_top_card_keeps_its_content_off_the_edges(browser):
    pid = new_plan()
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, locale="zh-CN")
    pg = ctx.new_page()
    try:
        open_plan(pg, pid)
        card = pg.locator(".plan-hero").bounding_box()
        assert pg.evaluate("document.documentElement.scrollWidth") <= 376
        assert card["x"] <= 14, card                                                      # 页面外边距别太宽
        title = '[data-testid="plan-title"]'
        for sel in (f"{title} .crumb", f"{title} h2", ".stat-tile", ".hero-actions .vbtn"):
            b = pg.locator(sel).first.bounding_box()
            assert b["x"] - card["x"] >= 14, (sel, b, card)                                # 卡片里的东西不贴边
        for sel in (".stat-tile", ".hero-actions button"):
            for i in range(pg.locator(sel).count()):
                b = pg.locator(sel).nth(i).bounding_box()
                if b:
                    assert b["x"] + b["width"] <= card["x"] + card["width"] - 12, (sel, i, b)   # 右边也不挤出去
        tiles = pg.locator(".stat-tile")
        assert tiles.first.bounding_box()["height"] <= 66                                  # 统计小卡片不再大块头
    finally:
        ctx.close()


# ---- S18：封面不挡路 ----
def test_skip_goes_straight_to_the_plan_and_the_plan_is_intact(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_test_id("cover-skip").click()
    page.wait_for_function(
        "() => document.getElementById('cover-end').getBoundingClientRect().top < 200", timeout=15_000)
    expect(page.locator(".stat-tile")).to_have_count(4)                              # 原来的统计小卡片还在
    for primary in ("编辑行程", "分享", "下载 PDF"):
        found = page.get_by_role("button", name=primary).or_(page.get_by_role("link", name=primary))
        expect(found.first).to_be_visible()
    expect(page.get_by_test_id("plan-title")).to_be_visible()
    expect(page.locator(".hero-cover")).to_be_hidden()                               # 旧封面卡不重复出现（S20）


def test_the_contents_bar_jumps_past_the_cover_to_the_day(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    day3 = re.compile("^第 3 天")
    page.get_by_role("link", name=day3).or_(page.get_by_role("button", name=day3)).first.click()
    page.wait_for_timeout(1200)
    day = page.locator(".day").nth(2)
    top = day.bounding_box()["y"]
    assert 50 <= top <= 400, top                                                     # 停在导航 / 目录条下面，没被遮住
    assert page.get_by_test_id("trip-cover").bounding_box()["y"] < -100              # 封面已经滚过去了


# ---- S17：降级 ----
def test_on_a_phone_the_cover_is_a_static_screen_with_a_swipeable_row(browser):
    pid = new_plan()
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, locale="zh-CN")
    pg = ctx.new_page()
    try:
        open_plan(pg, pid)
        assert pg.get_by_test_id("trip-cover").get_attribute("data-mode") == "static"
        stage = pg.locator(".cover-stage")
        assert stage.evaluate("e => getComputedStyle(e).position") != "sticky"
        expect(pg.get_by_test_id("cover-title").locator("h1")).to_have_text("巴黎")
        expect(pg.get_by_test_id("cover-skip")).to_have_count(0)
        strip = pg.get_by_test_id("cover-strip")
        expect(strip).to_be_visible()
        assert strip.get_by_test_id("cover-card").count() >= 3
        card = strip.get_by_test_id("cover-card").first.bounding_box()
        assert card["height"] <= 160 and card["width"] <= 260, card                       # 手机上的亮点卡片小一号
        row = pg.get_by_test_id("cover-strip-row")
        assert row.evaluate("e => e.offsetHeight - e.clientHeight") == 0                   # 没有原生滚动条占地方
        assert pg.evaluate("document.documentElement.scrollWidth") <= 376
        expect(pg.get_by_test_id("hero-credit")).to_be_visible()
        strip.get_by_test_id("cover-card").first.click()
        expect(pg.get_by_role("dialog", name="地点详情")).to_be_visible()
    finally:
        ctx.close()


def test_reduced_motion_gives_a_static_cover(browser):
    pid = new_plan()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="reduce", locale="zh-CN")
    pg = ctx.new_page()
    try:
        open_plan(pg, pid)
        assert pg.get_by_test_id("trip-cover").get_attribute("data-mode") == "static"
        expect(pg.get_by_test_id("cover-strip")).to_be_visible()
    finally:
        ctx.close()


def test_without_a_photo_the_cover_falls_back_to_the_city_gradient_and_is_static(page: Page):
    pid = new_plan()
    page.route("**/media/hero/*", lambda r: r.abort())
    page.goto(f"{WEB}/plans/{pid}")
    page.get_by_test_id("trip-cover").wait_for()
    assert page.get_by_test_id("trip-cover").get_attribute("data-mode") == "static"
    expect(page.get_by_test_id("cover-photo")).to_have_count(0)
    expect(page.get_by_test_id("hero-credit")).to_have_count(0)                      # 没有图就没有署名
    assert "gradient" in page.locator(".cover-stage").evaluate("e => getComputedStyle(e).backgroundImage")
    expect(page.get_by_test_id("cover-title").locator("h1")).to_have_text("巴黎")
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_be_visible()


# ---- S17 / S20：历史版本、编辑中不出现封面 ----
def test_old_versions_and_editing_use_the_old_cover_card(page: Page):
    pid = new_plan()
    r = httpx.post(f"{API}/plans/{pid}/shift", json={"start_date": "2026-11-10"}, timeout=60)
    assert r.status_code == 201, r.text
    open_plan(page, pid)
    page.get_by_role("button", name="v1", exact=True).click()                       # 历史版本
    page.locator(".hero-cover").wait_for(state="visible")
    expect(page.get_by_test_id("trip-cover")).to_have_count(0)
    expect(page.locator(".hero-cover h1")).to_be_visible()
    page.get_by_role("button", name="v2", exact=True).click()                       # 回到最新版本：封面回来
    page.get_by_test_id("trip-cover").wait_for()
    page.get_by_role("button", name="编辑行程", exact=True).first.click()                          # 编辑中
    expect(page.get_by_test_id("trip-cover")).to_have_count(0)
    expect(page.locator(".hero-cover")).to_be_visible()


def test_the_dark_theme_keeps_the_cover_readable(browser):
    pid = new_plan()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="dark", locale="zh-CN")
    pg = ctx.new_page()
    try:
        open_plan(pg, pid)
        color = pg.get_by_test_id("cover-title").locator("h1").evaluate("e => getComputedStyle(e).color")
        assert color in ("rgb(255, 255, 255)", "rgb(253, 241, 225)"), color
        expect(pg.get_by_test_id("cover-skip")).to_be_visible()
    finally:
        ctx.close()
