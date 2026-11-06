"""v0.2 PWA 与离线只读的前端验收（specs/modules/M15 S3 / S4）。

Service Worker 只在生产构建里注册，所以这里自己 `npm run build` 并用 `vite preview` 起一份生产版（端口 4173），
后端用已经在跑的那个。需要后端在跑、本机有 node（默认不跑）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v02_pwa.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e

API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")
PORT = int(os.getenv("E2E_PREVIEW_PORT", "4173"))
PREVIEW = f"http://127.0.0.1:{PORT}"
WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def _up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def preview():
    if not _up(f"{API}/health"):
        pytest.skip(f"后端没起来（{API}），先启动再跑 e2e")
    node, npm = shutil.which("node"), shutil.which("npm")
    if not (node and npm):
        pytest.skip("本机没有 node / npm，没法构建前端")
    # 生产构建里写死后端地址：指到这次测试用的后端；产物放临时目录，不覆盖 web/dist
    out = Path(tempfile.mkdtemp(prefix="atp_dist_"))
    env = {**os.environ, "VITE_API_BASE": API}
    subprocess.run([npm, "run", "build", "--", "--outDir", str(out), "--emptyOutDir"], cwd=WEB_DIR, env=env,
                   check=True, capture_output=True, timeout=240)
    vite = WEB_DIR / "node_modules" / "vite" / "bin" / "vite.js"
    proc = subprocess.Popen([node, str(vite), "preview", "--host", "127.0.0.1", "--port", str(PORT), "--strictPort",
                             "--outDir", str(out)], cwd=WEB_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            if _up(PREVIEW):
                break
            time.sleep(0.5)
        else:
            pytest.fail("vite preview 没起来")
        yield PREVIEW
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        shutil.rmtree(out, ignore_errors=True)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def ctx(browser):
    c = browser.new_context(viewport={"width": 1100, "height": 1600}, locale="zh-CN")
    yield c
    c.close()


def new_plan() -> str:
    r = httpx.post(f"{API}/plans", json={
        "destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art"}, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def sw_ready(page: Page) -> None:
    page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.controller !== null", timeout=15000)

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



def open_online(ctx, base: str, path: str) -> Page:
    """联网打开一遍，并刷新一次，让页面、脚本、接口数据都经过 Service Worker 进了缓存。"""
    page = ctx.new_page()
    page.goto(base + path)
    page.evaluate("navigator.serviceWorker.ready.then(() => true)")
    sw_ready(page)
    page.reload()
    return page


# --------------------------------------------------------------------------- #
def test_manifest_icons_and_installability(preview, ctx):
    page = ctx.new_page()
    page.goto(preview)
    page.get_by_test_id("home-hero").wait_for()                    # 首页（Q2 起首页是入口，没有表单）
    href = page.locator('link[rel="manifest"]').get_attribute("href")
    m = httpx.get(preview + href).json()
    assert m["display"] == "standalone" and m["name"] and m["short_name"]
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    for i in m["icons"]:
        r = httpx.get(preview + i["src"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert page.locator('link[rel="apple-touch-icon"]').count() == 1
    assert page.locator('meta[name="theme-color"]').count() == 1

    sw_ready(page)
    cdp = ctx.new_cdp_session(page)
    errors = cdp.send("Page.getInstallabilityErrors")["installabilityErrors"]
    assert errors == [], f"浏览器认为不可安装：{errors}"


def test_a_plan_opened_online_can_be_read_offline_and_shows_the_read_only_banner(preview, ctx):
    pid = new_plan()
    page = open_online(ctx, preview, f"/plans/{pid}")
    page.locator(".day").first.wait_for()
    n_days = page.locator(".day").count()
    page.get_by_role("checkbox", name=re.compile("已订好了")).first.wait_for()      # 预订状态也加载完了再取基准
    must_book = page.locator(".check-group").first.inner_text()
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_be_visible()
    expect(page.locator(".offline-banner")).to_have_count(0)
    open_full_map(page)                                          # 把全程地图用到的接口数据也走一遍
    page.wait_for_timeout(800)

    ctx.set_offline(True)
    page.reload()
    page.locator(".day").first.wait_for(timeout=15000)
    assert page.locator(".day").count() == n_days
    page.get_by_role("checkbox", name=re.compile("已订好了")).first.wait_for()      # 离线也看得到预订状态（缓存里的）
    assert page.locator(".check-group").first.inner_text() == must_book      # 预约清单还在
    banner = page.locator(".offline-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("离线 · 只读")
    # 只读：不能编辑、不能分享、不能用一句话修改、不能回滚
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="分享")).to_be_disabled()
    expect(page.get_by_placeholder(re.compile("不想早起"))).to_be_hidden()
    expect(page.get_by_role("button", name="回滚到这版")).to_have_count(0)

    ctx.set_offline(False)
    page.reload()
    page.locator(".day").first.wait_for()
    expect(page.locator(".offline-banner")).to_have_count(0)
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_be_visible()


def test_my_plans_list_works_offline_and_generation_is_disabled(preview, ctx):
    new_plan()
    page = open_online(ctx, preview, "/plans")
    page.locator(".plan-card").first.wait_for()
    n = page.locator(".plan-card").count()
    ctx.set_offline(True)
    page.reload()
    page.locator(".plan-card").first.wait_for(timeout=15000)
    assert page.locator(".plan-card").count() == n
    expect(page.locator(".offline-banner")).to_be_visible()

    page.goto(preview + "/new")                                   # 新建行程页（也是缓存的外壳）
    expect(page.get_by_role("button", name="生成行程")).to_be_disabled(timeout=15000)
    expect(page.get_by_text("暂时不能生成")).to_be_visible()


def test_a_plan_never_opened_before_says_it_is_unavailable_instead_of_crashing(preview, ctx):
    fresh = new_plan()
    page = open_online(ctx, preview, "/plans")
    page.locator(".plan-card").first.wait_for()
    ctx.set_offline(True)
    page.goto(f"{preview}/plans/{fresh}")
    expect(page.locator(".banner.err")).to_be_visible(timeout=15000)
    expect(page.locator(".offline-banner")).to_be_visible()
    expect(page.locator(".day")).to_have_count(0)


def test_service_worker_does_not_cache_write_requests_or_downloads(preview, ctx):
    pid = new_plan()
    page = open_online(ctx, preview, f"/plans/{pid}")
    page.locator(".day").first.wait_for()
    keys = page.evaluate("""async () => {
        const out = [];
        for (const name of await caches.keys()) {
            const c = await caches.open(name);
            for (const r of await c.keys()) out.push(r.url);
        }
        return out;
    }""")
    assert any(u.endswith(f"/plans/{pid}") for u in keys)                    # 行程本身进了缓存
    assert not any(re.search(r"\.(pdf|ics|kml)", u) for u in keys)           # 导出文件不缓存
    assert not any("/tasks" in u or "/llm/" in u or "/revisions" in u for u in keys)
