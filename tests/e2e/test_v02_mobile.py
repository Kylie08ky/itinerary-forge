"""v0.2 手机宽度的前端验收（specs/modules/M15 S1）：375px 无横向滚动、可点的控件至少 44px 高。

逐个页面（首页含候选池、我的行程、行程页含分享面板 / 全程地图 / 生成过程 / 编辑模式、公开分享页、模型用量）检查。
默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v02_mobile.py
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
MIN_TAP = 44

# 页面里所有看得见的、可点的控件：高度不够的列出来。地图瓦片 / 标记在地图容器里，只检查缩放按钮。
AUDIT_JS = """(minTap) => {
  const small = [];
  const sel = 'button, select, a.pdf-link, a.tool, a.primary-link, a.btn-link, .icon-btn, .appnav nav a';
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest('.leaflet-container') && !el.closest('.leaflet-control')) continue;
    const b = el.getBoundingClientRect();
    if (b.width === 0 || b.height === 0 || getComputedStyle(el).visibility === 'hidden') continue;
    if (b.height >= minTap - 0.5) continue;
    const label = (el.innerText || '').trim().slice(0, 14);
    small.push(`${el.className || el.tagName} "${label}" ${Math.round(b.height)}px`);
  }
  return { scrollWidth: document.documentElement.scrollWidth, small };
}"""


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
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True, has_touch=True, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


@pytest.fixture(scope="module")
def plan() -> dict:
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art"}, timeout=120)
    assert r.status_code == 201, r.text
    pid = r.json()["plan_id"]
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=10).json()["token"]
    return {"id": pid, "token": token}


def audit(page: Page, where: str) -> None:
    res = page.evaluate(AUDIT_JS, MIN_TAP)
    assert res["scrollWidth"] <= 375, f"{where}：出现横向滚动（页面宽 {res['scrollWidth']}px）"
    assert not res["small"], f"{where}：这些控件的点击区域小于 {MIN_TAP}px：{res['small'][:8]}"


# --------------------------------------------------------------------------- #
def test_home_with_the_candidate_pool_open(page: Page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    page.locator(".theme-btn", has_text="美食").click()              # 混选后出现权重下拉
    page.locator(".pool > summary").click()
    page.locator(".pool-row").first.wait_for()
    audit(page, "首页（候选池展开）")


def test_my_plans_and_usage_pages(page: Page, plan):
    page.goto(f"{WEB}/plans")
    page.locator(".plan-card").first.wait_for()
    audit(page, "我的行程")
    page.goto(f"{WEB}/usage")
    page.get_by_text("模型调用", exact=True).wait_for()
    audit(page, "模型用量")


def test_usage_page_has_an_entry_to_version_footprints(page: Page):
    """手机没有顶部导航：版本足迹从「用量」页进（规格 06 S21）。"""
    page.goto(f"{WEB}/usage")
    page.get_by_test_id("usage-about-link").click()
    expect(page).to_have_url(re.compile(r"/about$"))
    expect(page.get_by_role("heading", name="版本足迹", exact=True)).to_be_visible()
    expect(page.get_by_test_id("footprint-current")).to_be_visible()


def test_plan_page_with_share_map_and_trace_open(page: Page, plan):
    page.goto(f"{WEB}/plans/{plan['id']}")
    page.locator(".day").first.wait_for()
    audit(page, "行程页")
    page.get_by_role("button", name="分享").click()
    page.get_by_label("分享链接").wait_for()
    audit(page, "分享弹窗")
    page.get_by_role("button", name="关闭", exact=True).click()             # 弹窗盖住了页面，先关掉再点别的
    expect(page.locator("details.routecard")).to_have_attribute("open", "")   # 窄屏：地图默认展开（S26）
    page.get_by_test_id("full-map").wait_for()
    page.get_by_role("button", name="更多").click()
    page.get_by_test_id("open-gen-details").click()
    page.locator("details.trace summary").click()
    page.wait_for_timeout(600)
    audit(page, "行程页（全程地图展开 / 生成详情里的生成过程展开）")


def test_plan_page_in_edit_mode(page: Page, plan):
    page.goto(f"{WEB}/plans/{plan['id']}")
    page.locator(".day").first.wait_for()
    page.get_by_role("button", name="编辑行程", exact=True).click()
    page.locator(".edit-bar").wait_for()
    audit(page, "行程页（编辑模式）")


def test_shared_page_for_a_stranger(page: Page, plan):
    page.goto(f"{WEB}/s/{plan['token']}")
    page.locator(".day").first.wait_for()
    page.wait_for_timeout(600)
    audit(page, "公开分享页")
