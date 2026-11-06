"""v0.2 交付的前端验收（specs/modules/M08 S2/S4/S5、M05 S3/S5/S6）。

导出日历 / KML、全程分色地图与标记聚类、一键导航链接、公开只读分享（含关闭后失效）。
默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v02_delivery.py
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

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
def ctx(browser):
    c = browser.new_context(viewport={"width": 1200, "height": 2400}, locale="zh-CN", accept_downloads=True)
    natives: list[str] = []
    c.on("page", lambda pg: pg.on("dialog", lambda d: (natives.append(d.message), d.dismiss())))
    yield c
    c.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


@pytest.fixture
def page(ctx) -> Page:
    pg = ctx.new_page()
    return pg


def new_plan(theme="art", days=4) -> str:
    end = f"2026-10-{2 + days:02d}"
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": "2026-10-03", "end_date": end, "budget_level": "medium",
        "travel_style": "balanced", "interests": [theme], "theme": theme}, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()

def open_more(page: Page) -> None:
    """操作藏在「更多」菜单里（规格 06-ui-design）：先打开菜单。"""
    page.get_by_role("button", name="更多").click()


def dismiss_conflicts(page: Page) -> None:
    """平移后如果有新的闭馆日冲突，会弹窗提示；不关掉它会挡住后面的点击。"""
    later = page.get_by_role("button", name="稍后再处理")
    try:
        later.wait_for(timeout=1500)
        later.click()
    except Exception:  # noqa: BLE001 - 没有冲突弹窗就什么都不用做
        pass


def open_full_map(page: Page) -> None:
    """桌面端全程地图一直在右侧；窄屏是折叠卡片，要点开。"""
    summary = page.locator("details.routecard summary")
    if summary.count():
        summary.click()
    page.get_by_test_id("full-map").wait_for()



# --------------------------------------------------------------------------- #
def test_calendar_and_kml_downloads_are_real_files(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    open_more(page)
    with page.expect_download() as dl:
        page.get_by_role("link", name="导出日历").click()
    ics = open(dl.value.path(), encoding="utf-8").read()
    assert dl.value.suggested_filename.endswith(".ics")
    assert ics.startswith("BEGIN:VCALENDAR") and ics.count("BEGIN:VEVENT") >= 8
    assert "TZID=Europe/Paris" in ics

    open_more(page)
    with page.expect_download() as dl2:
        page.get_by_role("link", name="导出 KML").click()
    kml = open(dl2.value.path(), encoding="utf-8").read()
    assert dl2.value.suggested_filename.endswith(".kml") and kml.count("<Folder>") == 4


def test_pdf_button_shows_progress_then_downloads_and_follows_the_one_page_per_day_option(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    button = page.get_by_role("button", name="下载 PDF")
    with page.expect_request(lambda r: "itinerary.pdf" in r.url) as req:
        with page.expect_download() as dl:
            button.click()
            expect(page.get_by_role("button", name="生成中…")).to_be_disabled()              # 立刻有反应
    assert "page_per_day" not in req.value.url
    assert dl.value.suggested_filename.endswith(".pdf")
    assert open(dl.value.path(), "rb").read(5) == b"%PDF-"
    expect(page.get_by_role("button", name="下载 PDF")).to_be_enabled()                     # 下完恢复
    open_more(page)
    page.get_by_label("每天一页").uncheck()
    with page.expect_request(lambda r: "itinerary.pdf" in r.url) as req2:
        with page.expect_download():
            page.get_by_role("button", name="下载 PDF").click()
    assert "page_per_day=false" in req2.value.url


def test_a_failed_pdf_shows_an_error_popup_instead_of_a_blank_tab(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.route(re.compile(r".*/itinerary\.pdf.*"), lambda r: r.fulfill(
        status=503, body='{"detail":"PDF 生成失败（本机缺 Chrome/Edge？）：boom"}', content_type="application/json"))
    page.get_by_role("button", name="下载 PDF").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("PDF 生成失败")
    expect(page.get_by_role("button", name="下载 PDF")).to_be_enabled()
    assert len(page.context.pages) == 1                                                    # 没有新开空白标签页


def test_the_same_pdf_comes_back_from_the_cache(page: Page):
    import time

    pid = new_plan()
    first = httpx.get(f"{API}/plans/{pid}/itinerary.pdf", timeout=120)
    assert first.status_code == 200
    t0 = time.time()
    second = httpx.get(f"{API}/plans/{pid}/itinerary.pdf", timeout=120)
    assert second.content == first.content and time.time() - t0 < 0.5


def test_html_export_has_the_cover_and_route_map(page: Page):
    pid = new_plan()
    html = httpx.get(f"{API}/plans/{pid}/itinerary.html", timeout=60).text
    assert 'class="cover"' in html and "<polyline" in html and "公里" in html


def test_full_map_is_colored_per_day_and_the_legend_filters_one_day(page: Page):
    pid = new_plan()
    route = httpx.get(f"{API}/plans/{pid}/route", timeout=30).json()
    open_plan(page, pid)
    stats = page.get_by_test_id("stat-tiles")
    expect(stats).to_contain_text(f"{route['place_count']}")
    open_full_map(page)
    expect(page.get_by_test_id("full-map")).to_be_visible()

    legend = page.locator(".route-legend .lg")
    expect(legend).to_have_count(len(route["days"]))
    for i, d in enumerate(route["days"]):
        expect(legend.nth(i)).to_contain_text(f"第 {i + 1} 天 · {d['distance_km']:g} 公里")   # 页面按数字显示：1.0 → 1

    strokes = lambda: sorted(page.get_by_test_id("full-map").locator("path.leaflet-interactive").evaluate_all(  # noqa: E731
        "els => els.map(e => e.getAttribute('stroke'))"))
    drawn = sorted(d["color"] for d in route["days"] if len(d["stops"]) >= 2)
    assert strokes() == drawn                                          # 每天一种颜色

    one = next(i for i, d in enumerate(route["days"]) if len(d["stops"]) >= 2)         # 凡尔赛那天只有一个点，画不出线
    legend.nth(one).click()
    expect(legend.nth(one)).to_have_attribute("aria-pressed", "true")
    assert strokes() == [route["days"][one]["color"]]                  # 只看这一天
    legend.nth(one).click()
    assert strokes() == drawn                                          # 再点一下回到全部


def test_zooming_out_clusters_the_markers_into_numbered_circles(page: Page):
    pid = new_plan()
    open_plan(page, pid)
    open_full_map(page)
    zoom_out = page.locator(".leaflet-control-zoom-out")
    for _ in range(9):
        zoom_out.click()
        page.wait_for_timeout(120)
    page.wait_for_timeout(500)
    clusters = page.locator(".marker-cluster")
    expect(clusters.first).to_be_visible()
    assert clusters.first.inner_text().strip().isdigit()
    assert int(clusters.first.inner_text().strip()) >= 2


def test_navigation_links_open_google_maps_routes_without_a_key(page: Page):
    pid = new_plan()
    route = httpx.get(f"{API}/plans/{pid}/route", timeout=30).json()
    open_plan(page, pid)
    day = page.locator(".day").first
    nav = day.get_by_role("link", name=re.compile("在 Google 地图中打开当天路线"))
    expect(nav.first).to_be_visible()
    href = nav.first.get_attribute("href")
    assert href == route["days"][0]["google_urls"][0]
    q = parse_qs(urlparse(href).query)
    assert "origin" in q and "destination" in q and "key" not in q
    assert nav.first.get_attribute("target") == "_blank"

    leg = page.get_by_role("link", name=re.compile("从「.+」导航")).first
    expect(leg).to_be_visible()
    lq = parse_qs(urlparse(leg.get_attribute("href")).query)
    assert lq["travelmode"] == ["transit"] and "origin" in lq and "destination" in lq


def test_share_link_is_read_only_for_a_stranger_and_dies_when_closed(ctx, page: Page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="分享").click()
    page.get_by_role("button", name="生成分享链接").click()
    box = page.get_by_label("分享链接")
    expect(box).to_have_value(re.compile(r"/s/[A-Za-z0-9_-]{32,}$"))
    link = box.input_value()

    stranger = ctx.browser.new_context(viewport={"width": 1200, "height": 1800}, locale="zh-CN").new_page()
    stranger.goto(link)
    stranger.locator(".day").first.wait_for()
    expect(stranger.get_by_text("只读分享")).to_be_visible()
    expect(stranger.locator(".day")).to_have_count(4)
    for forbidden in ("编辑行程", "分享", "我的行程", "下载 PDF"):
        expect(stranger.get_by_role("button", name=forbidden)).to_have_count(0)
        expect(stranger.get_by_role("link", name=forbidden)).to_have_count(0)
    assert pid not in stranger.content()                               # 公开页里没有行程号
    expect(stranger.get_by_test_id("full-map")).to_be_visible()

    page.get_by_role("button", name="关闭链接").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("立刻失效")
    page.get_by_test_id("confirm-ok").click()
    expect(page.get_by_role("button", name="生成分享链接")).to_be_visible()
    stranger.reload()
    expect(stranger.get_by_text("链接已失效")).to_be_visible()
    expect(stranger.locator(".day")).to_have_count(0)
    stranger.context.close()


def test_an_unknown_share_link_says_so(page: Page):
    page.goto(f"{WEB}/s/this-token-does-not-exist-0000000000")
    expect(page.get_by_text("没有这个分享链接")).to_be_visible()
