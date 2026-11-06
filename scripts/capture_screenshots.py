"""给 README 和演示脚本截图：起一套独立的前后端（临时数据库，不碰你平时的服务和数据），
生成两份示例行程（艺术 + 美食 4 天；一条深度路线 3 天），用 Playwright 截图存到 docs/images/。

用法：python scripts/capture_screenshots.py

说明文字一律是模板（模型指向一个连不上的地址），所以每次截图的文字稳定，不依赖你本机有没有开 Ollama。
地图瓦片来自 OpenStreetMap，需要联网；没网时地图是灰底，其余照常。
截图是 JPEG（比 PNG 小得多），宽 1280 的桌面图和 375 宽的手机图。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from e2e_stack import IsolatedStack  # noqa: E402
from playwright.sync_api import Page, sync_playwright  # noqa: E402

OUT = ROOT / "docs" / "images"
ROUTE = "art-royal-to-contemporary"
SHOT = {"type": "jpeg", "quality": 82}


def make_plans(api: str) -> tuple[str, str]:
    start = date.today() + timedelta(days=30)
    end = start + timedelta(days=3)
    first = {"destination": "Paris", "start_date": start.isoformat(), "end_date": end.isoformat(),
             "budget_level": "medium", "travel_style": "balanced", "interests": ["艺术", "美食"],
             "theme": "art", "theme_weights": {"art": 3, "food": 2},
             "arrival_time": f"{start} 09:00", "departure_time": f"{end} 18:00"}
    s2 = start + timedelta(days=45)
    second = {"destination": "Paris", "start_date": s2.isoformat(), "end_date": (s2 + timedelta(days=2)).isoformat(),
              "budget_level": "medium", "travel_style": "balanced", "interests": ["艺术"], "theme": "art",
              "route": ROUTE, "arrival_time": f"{s2} 09:00", "departure_time": f"{s2 + timedelta(days=2)} 20:00"}
    ids = []
    for payload in (first, second):
        r = httpx.post(f"{api}/plans", json=payload, timeout=300)
        if r.status_code == 422 and "theme_weights" in payload:     # 旧接口不收权重：去掉重试
            payload.pop("theme_weights")
            r = httpx.post(f"{api}/plans", json=payload, timeout=300)
        r.raise_for_status()
        ids.append(r.json()["plan_id"])
    return ids[0], ids[1]


TILES: dict[int, int] = {}


def watch_tiles(page: Page) -> None:
    page.on("response", lambda r: TILES.__setitem__(r.status, TILES.get(r.status, 0) + 1)
            if "tile.openstreetmap.org" in r.url else None)


def shot(page: Page, name: str, full: bool = False) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:                            # noqa: BLE001 - 地图一直在请求时别卡住
        pass
    page.wait_for_timeout(1500)                  # 图片和地图瓦片
    path = OUT / f"{name}.jpg"
    page.screenshot(path=str(path), full_page=full, **SHOT)
    print(f"  {path.relative_to(ROOT)}  {path.stat().st_size // 1024} KB")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    stack = IsolatedStack()
    stack.start()
    try:
        plan, route_plan = make_plans(stack.api_url)
        web = stack.web_url
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
            page = ctx.new_page()
            watch_tiles(page)

            page.goto(f"{web}/")
            shot(page, "01-home")

            page.goto(f"{web}/new?destination=Paris")
            page.wait_for_selector("text=深度路线", timeout=30_000)
            shot(page, "02-new-plan")

            page.goto(f"{web}/plans")
            page.wait_for_selector("a[href^='/plans/']", timeout=30_000)
            shot(page, "03-my-plans")

            page.goto(f"{web}/plans/{plan}")
            page.locator(".day").first.wait_for()
            shot(page, "04-plan-cover")
            page.locator(".day").first.scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, 40)")
            shot(page, "05-plan-day")
            budget = page.locator("#sec-budget")
            if budget.count():
                budget.scroll_into_view_if_needed()
                shot(page, "06-plan-budget")

            page.goto(f"{web}/plans/{route_plan}")
            page.locator(".day").first.wait_for()
            page.locator(".day").first.scroll_into_view_if_needed()
            shot(page, "07-route-plan")

            m = browser.new_context(viewport={"width": 375, "height": 812}, locale="zh-CN", device_scale_factor=2)
            mp = m.new_page()
            mp.goto(f"{web}/plans/{plan}")
            mp.locator(".day").first.wait_for()
            shot(mp, "08-mobile-plan")
            browser.close()
    finally:
        stack.stop()
    print(f"地图瓦片响应：{TILES or '没有请求'}（200 = 成功）")
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
