"""v0.4 批次 I 文件附件的前端验收（specs/modules/M10 S5）。

像真人一样：行程页「随行」卡 →「文件」页签 → 上传 / 预览 / 下载 / 改挂靠 / 回收站；每天、地点、预订上的回形针；
分享页看不到文件。默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v04_attachments.py
"""

from __future__ import annotations

import base64
import os

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e

WEB = os.getenv("E2E_WEB", "http://127.0.0.1:5173").rstrip("/")
API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


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
    ctx = browser.new_context(viewport={"width": 1280, "height": 2600}, locale="zh-CN")
    natives: list[str] = []
    ctx.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    pg = ctx.new_page()
    yield pg
    ctx.close()
    assert not natives, f"页面弹了原生对话框：{natives}"


def new_plan() -> str:
    body = {"destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["museums"], "theme": "museums",
            "arrival_time": "2026-10-20 10:30", "departure_time": "2026-10-23 18:00"}
    r = httpx.post(f"{API}/plans", json=body, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def open_files(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    page.get_by_test_id("companion-card").wait_for()
    page.get_by_test_id("tab-files").click()
    expect(page.get_by_test_id("files-tab")).to_be_visible()


def upload(page: Page, name: str, data: bytes, mime: str = "application/octet-stream") -> None:
    page.get_by_test_id("file-input").set_input_files({"name": name, "mimeType": mime, "buffer": data})


def api_files(pid: str) -> dict:
    return httpx.get(f"{API}/plans/{pid}/attachments", timeout=30).json()


def rows(page: Page):
    return page.get_by_test_id("file-row")


# ---- S5a / S5c：上传、预览、下载 ----
def test_upload_preview_and_download(page: Page):
    pid = new_plan()
    open_files(page, pid)
    expect(page.get_by_test_id("files-empty")).to_contain_text("还没有文件")
    upload(page, "酒店确认单.pdf", PDF, "application/pdf")
    expect(rows(page)).to_have_count(1)
    upload(page, "预订截图.png", PNG, "image/png")
    expect(rows(page)).to_have_count(2)
    pdf, png = rows(page).filter(has_text="酒店确认单.pdf"), rows(page).filter(has_text="预订截图.png")
    expect(pdf).to_contain_text("PDF")
    expect(pdf.get_by_test_id("file-target")).to_have_text("整个行程")
    expect(page.get_by_test_id("tab-files")).to_contain_text("（2）")

    link = pdf.get_by_test_id("file-open")                                        # PDF：新标签页打开
    assert link.get_attribute("target") == "_blank" and "/attachments/" in link.get_attribute("href")
    assert "download=1" in pdf.get_by_test_id("file-download").get_attribute("href")

    png.get_by_test_id("file-open").click()                                       # 图片：应用内弹窗预览
    img = page.get_by_test_id("file-preview-img")
    expect(img).to_be_visible()
    assert page.evaluate("document.querySelector('[data-testid=file-preview-img]').complete") is True
    page.keyboard.press("Escape")
    expect(img).to_be_hidden()

    got = httpx.get(link.get_attribute("href"), timeout=10)                       # 真的能取到文件内容，且带安全头
    assert got.content == PDF and got.headers["x-content-type-options"] == "nosniff"
    assert api_files(pid)["attachments"][0]["name"] in ("酒店确认单.pdf", "预订截图.png")


def test_refreshing_the_page_keeps_the_files(page: Page):
    pid = new_plan()
    open_files(page, pid)
    upload(page, "a.png", PNG, "image/png")
    expect(rows(page)).to_have_count(1)
    page.reload()
    page.get_by_test_id("companion-card").wait_for()
    expect(page.get_by_test_id("tab-files")).to_contain_text("（1）")


# ---- 错误提示 ----
@pytest.mark.parametrize("name, data, words", [
    ("病毒.exe", PDF, ["只支持 PDF / PNG / JPG"]),
    ("伪装.pdf", b"<html><script>alert(1)</script></html>", ["只支持"]),
    ("改了扩展名.pdf", PNG, ["对不上"]),
    ("空的.pdf", b"", ["空"]),
])
def test_bad_files_get_a_popup_with_the_reason(page: Page, name, data, words):
    pid = new_plan()
    open_files(page, pid)
    upload(page, name, data)
    popup = page.get_by_role("alertdialog")
    expect(popup).to_be_visible()
    for w in words:
        expect(popup).to_contain_text(w)
    page.get_by_test_id("notice-ok").click()
    expect(rows(page)).to_have_count(0)
    assert api_files(pid)["attachments"] == []


def test_a_file_over_10mb_is_refused_with_its_size(page: Page):
    pid = new_plan()
    open_files(page, pid)
    upload(page, "大文件.pdf", PDF + b"\0" * (10 * 1024 * 1024 + 10), "application/pdf")
    expect(page.get_by_role("alertdialog")).to_contain_text("超过 10MB")
    page.get_by_test_id("notice-ok").click()
    assert api_files(pid)["attachments"] == []


# ---- S5b / S5i：挂靠与回形针 ----
def test_attach_to_a_day_shows_a_paperclip_that_jumps_to_the_filtered_list(page: Page):
    pid = new_plan()
    days = [d["date"] for d in httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]["days"]]
    open_files(page, pid)
    page.get_by_test_id("file-target-kind").select_option("day")
    page.get_by_test_id("file-target-ref").select_option(days[1])                 # 第 2 天
    upload(page, "第二天的票.pdf", PDF, "application/pdf")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first.get_by_test_id("file-target")).to_contain_text("第 2 天")

    clip = page.locator(".day").nth(1).get_by_test_id("clip")
    expect(clip).to_have_text("📎 1")
    expect(page.locator(".day").nth(0).get_by_test_id("clip")).to_have_count(0)    # 别的天没有

    rows(page).first.get_by_test_id("file-retarget").select_option("plan|")        # 改挂到整个行程
    expect(clip).to_have_count(0)
    rows(page).first.get_by_test_id("file-retarget").select_option(f"day|{days[2]}")
    clip3 = page.locator(".day").nth(2).get_by_test_id("clip")
    expect(clip3).to_have_text("📎 1")

    page.get_by_role("tab", name="待办").click()                                    # 先切走
    clip3.click()                                                                  # 点回形针：跳回「文件」并筛选
    expect(page.get_by_test_id("files-tab")).to_be_visible()
    expect(page.get_by_test_id("file-filter")).to_have_value(f"day|{days[2]}")
    expect(rows(page)).to_have_count(1)


def test_place_card_and_booking_row_paperclips(page: Page):
    pid = new_plan()
    plan = httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]
    place = next(i for d in plan["days"] for i in d["items"] if i.get("place_id"))
    open_files(page, pid)
    page.get_by_test_id("file-target-kind").select_option("place")
    page.get_by_test_id("file-target-ref").select_option(place["place_id"])
    upload(page, "地点的票.png", PNG, "image/png")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first.get_by_test_id("file-target")).to_contain_text(place["title"])

    page.locator(f'.item[data-pid="{place["place_id"]}"] .title-link').first.click()   # 打开地点详情卡
    clip = page.get_by_role("dialog", name="地点详情").get_by_test_id("clip")
    expect(clip).to_have_text("📎 1")
    clip.click()                                                                   # 卡片关掉，跳到文件页签并筛选
    expect(page.get_by_role("dialog", name="地点详情")).to_have_count(0)
    expect(page.get_by_test_id("file-filter")).to_have_value(f"place|{place['place_id']}")

    reservations = httpx.get(f"{API}/plans/{pid}/reservations", timeout=30).json()["items"]
    if reservations:                                                               # 预订记录那一行
        res = reservations[0]
        page.get_by_test_id("file-target-kind").select_option("reservation")
        page.get_by_test_id("file-target-ref").select_option(res["place_id"])
        upload(page, "预订截图.png", PNG, "image/png")
        expect(page.locator("#sec-booking").get_by_test_id("clip").first).to_have_text("📎 1")


# ---- S5e：回收站 ----
def test_trash_restore_and_permanent_delete_with_a_confirm_dialog(page: Page):
    pid = new_plan()
    open_files(page, pid)
    upload(page, "要删的.pdf", PDF, "application/pdf")
    expect(rows(page)).to_have_count(1)
    rows(page).first.get_by_test_id("file-delete").click()
    expect(rows(page)).to_have_count(0)
    expect(page.get_by_test_id("trash-toggle")).to_have_text("回收站 1")
    assert api_files(pid)["attachments"] == [] and len(api_files(pid)["trash"]) == 1

    page.get_by_test_id("trash-toggle").click()
    page.get_by_test_id("trash-restore").click()                                   # 恢复
    expect(rows(page)).to_have_count(1)
    expect(page.get_by_test_id("files-trash")).to_have_count(0)

    rows(page).first.get_by_test_id("file-delete").click()
    expect(page.get_by_test_id("trash-row")).to_have_count(1)                      # 回收站保持展开
    page.get_by_test_id("trash-purge").click()                                     # 永久删除要先确认
    expect(page.get_by_test_id("confirm-ok")).to_be_visible()
    page.get_by_test_id("confirm-cancel").click()
    expect(page.get_by_test_id("trash-row")).to_have_count(1)                      # 取消了就还在
    page.get_by_test_id("trash-purge").click()
    page.get_by_test_id("confirm-ok").click()
    expect(page.get_by_test_id("files-trash")).to_have_count(0)
    assert api_files(pid)["attachments"] == [] and api_files(pid)["trash"] == []


# ---- S5f：不外露 ----
def test_files_are_not_on_the_share_page(browser):
    pid = new_plan()
    httpx.post(f"{API}/plans/{pid}/attachments", params={"name": "ZZ9-确认单.pdf", "target_kind": "plan"},
               content=PDF, timeout=30).raise_for_status()
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=30).json()["token"]
    ctx = browser.new_context(viewport={"width": 1280, "height": 2000}, locale="zh-CN")
    pg = ctx.new_page()
    try:
        pg.goto(f"{WEB}/s/{token}")
        pg.locator(".day").first.wait_for()
        assert "ZZ9" not in pg.content() and "📎" not in pg.content()
        expect(pg.get_by_test_id("clip")).to_have_count(0)
    finally:
        ctx.close()


# ---- 平移日期 ----
def test_shifting_the_dates_moves_a_day_attachment_with_it(page: Page):
    pid = new_plan()
    days = [d["date"] for d in httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]["days"]]
    httpx.post(f"{API}/plans/{pid}/attachments", params={"name": "d.png", "target_kind": "day", "target_ref": days[1]},
               content=PNG, timeout=30).raise_for_status()
    assert httpx.post(f"{API}/plans/{pid}/shift", json={"start_date": "2026-11-10"}, timeout=60).status_code == 201
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    expect(page.locator(".day").nth(1).get_by_test_id("clip")).to_have_text("📎 1")
    assert api_files(pid)["attachments"][0]["target_ref"] == "2026-11-11"


# ---- 手机 ----
def test_files_tab_fits_a_phone_screen(browser):
    pid = new_plan()
    for name, data in (("一个名字特别特别特别特别特别特别长的酒店确认单文件.pdf", PDF), ("b.png", PNG)):
        httpx.post(f"{API}/plans/{pid}/attachments", params={"name": name}, content=data, timeout=30).raise_for_status()
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, locale="zh-CN")
    pg = ctx.new_page()
    try:
        open_files(pg, pid)
        expect(rows(pg)).to_have_count(2)
        assert pg.evaluate("document.documentElement.scrollWidth") <= 376
    finally:
        ctx.close()
