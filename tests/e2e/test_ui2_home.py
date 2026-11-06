"""UI2 批次 J2 首页（Wandor 结构）的前端验收（specs/06-ui-design.md S11–S14、M01 S10–S13）。

像真人一样：首屏大图 + 毛玻璃提示卡 → 写一句话 → 表单被填好（不会自动生成）。独立栈里本地模型连不上，
所以这里读的是「只按规则」的结果（正好验证模型没开也能用）；后端的读法有单元测试。

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_ui2_home.py
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
SENTENCE = "10月20号去巴黎玩4天，两个人，喜欢艺术和咖啡馆，预算700欧，想去奥赛博物馆"


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


def open_home(page: Page) -> None:
    """首页 /：英雄图 + 一句话卡（Q2 起这里没有表单）。"""
    page.goto(WEB)
    page.get_by_test_id("home-hero").wait_for()


def open_new(page: Page) -> None:
    """新建行程页 /new：纯表单 + 一句话卡。"""
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()


def pref_field(page: Page, label: str):
    """「偏好」步骤里的文字框（label 没绑 input，按所在的 .field 找）。"""
    return page.locator(".field").filter(has=page.locator("label", has_text=re.compile(f"^{label}"))).locator("input")


def read(page: Page, text: str) -> None:
    page.get_by_test_id("nl-text").fill(text)
    page.get_by_test_id("nl-go").click()
    expect(page.get_by_test_id("nl-result")).to_be_visible(timeout=30_000)


# ---- S11：首屏 ----
def test_hero_is_a_full_screen_photo_with_a_glass_card_and_a_credit(page: Page):
    open_home(page)
    hero = page.get_by_test_id("home-hero")
    expect(hero.locator("h1")).to_have_text("你想去哪里？")
    page.get_by_test_id("hero-photo").wait_for()
    box = hero.bounding_box()
    assert box and box["height"] >= 800 * 0.8, box                                 # 首屏占满视口
    card = page.get_by_test_id("nl-card")
    style = card.evaluate(
        "e => { const s = getComputedStyle(e); return [s.backdropFilter, s.borderTopWidth, s.borderTopLeftRadius] }")
    assert "blur(20px)" in style[0] and style[1] == "0px" and style[2] == "28px", style     # 米白毛玻璃输入卡（S24 起）
    credit = page.get_by_test_id("hero-credit")
    expect(credit).to_contain_text("摄影：")
    assert credit.locator("a").count() == 2                                       # 授权链接 + 来源链接
    assert "commons.wikimedia.org" in credit.locator("a").nth(1).get_attribute("href")
    expect(page.get_by_test_id("nl-go")).to_be_disabled()                          # 空的时候灰着
    expect(page.get_by_test_id("nl-text")).to_have_attribute("placeholder", re.compile("奥赛博物馆"))


def test_hero_photo_is_a_real_large_image(page: Page):
    open_home(page)
    url = page.get_by_test_id("hero-photo").evaluate("e => getComputedStyle(e).backgroundImage")
    m = re.search(r'url\("([^"]+)"\)', url)
    assert m and "/media/hero/" in m.group(1)
    got = httpx.get(m.group(1), timeout=30)
    assert got.status_code == 200 and len(got.content) > 150_000


def test_the_hero_photo_slowly_pushes_in_unless_motion_is_reduced(browser):
    for reduce, expected in (("no-preference", True), ("reduce", False)):
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, reduced_motion=reduce)
        pg = ctx.new_page()
        try:
            open_home(pg)
            name = pg.get_by_test_id("hero-photo").evaluate("e => getComputedStyle(e).animationName")
            assert (name != "none") is expected, (reduce, name)
        finally:
            ctx.close()


def test_without_a_photo_the_hero_falls_back_to_the_city_gradient(page: Page):
    page.route("**/media/hero/*", lambda r: r.abort())
    open_home(page)
    hero = page.get_by_test_id("home-hero")
    expect(hero.locator("h1")).to_have_text("你想去哪里？")
    expect(page.get_by_test_id("hero-photo")).to_have_count(0)
    expect(page.get_by_test_id("hero-credit")).to_have_count(0)                    # 没有图就没有署名
    assert "linear-gradient" in hero.evaluate("e => getComputedStyle(e).backgroundImage")
    expect(page.get_by_test_id("nl-card")).to_be_visible()


# ---- S12：自然语言填表 ----
def test_a_sentence_fills_the_form_but_does_not_generate(page: Page):
    open_new(page)
    read(page, SENTENCE)
    said = page.get_by_test_id("nl-understood")
    parts = ("出发 10月20日，共 4 天（到 10月23日）", "主题：艺术、美食", "出行人：2 位成人", "预算上限：700 欧元",
             "必去：奥赛博物馆")
    for part in parts:
        expect(said).to_contain_text(part)
    expect(page.get_by_test_id("nl-no-model")).to_be_visible()                     # 独立栈里模型连不上：只按规则
    # 表单被填好了（今天固定 2026-10-21，所以 10 月 20 号是明年）
    expect(page.get_by_label("开始日期")).to_have_value("2027-10-20")
    expect(page.get_by_label("离开日期")).to_have_value("2027-10-23")
    expect(page.locator(".theme-btn.active")).to_have_count(2)
    expect(page.locator(".theme-btn.active").first).to_contain_text("艺术")
    expect(page.get_by_label(re.compile("^预算上限"))).to_have_value("700")
    expect(page.get_by_label(re.compile("^结算货币"))).to_have_value("EUR")
    expect(page.locator(".party-row, [data-testid=party-row]")).to_have_count(2)
    expect(pref_field(page, "必去")).to_have_value("奥赛博物馆")      # 「偏好」步骤里的文字框
    expect(pref_field(page, "想避开")).to_have_value("")
    assert page.url.split("?")[0].rstrip("/") == f"{WEB}/new"                                  # 没有跳走：不会自动生成
    expect(page.get_by_role("button", name="生成行程")).to_be_visible()


def test_must_go_and_avoid_land_in_the_preferences_fields(page: Page):
    # 用户反馈：读完后「偏好」里没有必去奥赛博物馆、不去卢浮宫——两个文字框都要填上，不用再去展开候选池
    open_new(page)
    read(page, "10月28日去巴黎玩4天，想去奥赛博物馆，不想去卢浮宫")
    expect(pref_field(page, "必去")).to_have_value("奥赛博物馆")
    expect(pref_field(page, "想避开")).to_have_value("卢浮宫")
    # 候选池里不再悄悄多出一份设置：文字框是唯一来源，用户改文字框就是改了
    expect(page.locator(".pool-count")).to_have_count(0)
    pref_field(page, "想避开").fill("")
    expect(page.locator(".pool-count")).to_have_count(0)


def test_unread_items_are_listed_and_the_form_keeps_its_defaults(page: Page):
    open_new(page)
    before = page.get_by_label("开始日期").input_value()
    read(page, "五一去巴黎，喜欢摄影")
    expect(page.get_by_test_id("nl-missing")).to_contain_text("出发日期")
    expect(page.get_by_test_id("nl-missing")).to_contain_text("五一")
    assert page.get_by_label("开始日期").input_value() == before                  # 读不出来的不动
    expect(page.locator(".theme-btn.active")).to_have_count(1)
    expect(page.locator(".theme-btn.active")).to_contain_text("摄影")


def test_a_city_in_the_sentence_fills_the_destination_and_opens_the_lookup(page: Page):
    open_new(page)
    read(page, "10月28日出发去东京玩8天")
    expect(page.get_by_label("目的地", exact=True)).to_have_value("东京")
    expect(page.get_by_test_id("destination-flow")).to_be_visible()       # 不在库里的：马上去认 / 建


def test_nothing_readable_says_so_and_changes_nothing(page: Page):
    open_new(page)
    read(page, "你好")
    expect(page.get_by_test_id("nl-result")).to_contain_text("没读出能填的内容")
    page.get_by_test_id("nl-close").click()
    expect(page.get_by_test_id("nl-result")).to_have_count(0)


def test_children_and_students_show_up_in_the_party(page: Page):
    open_new(page)
    read(page, "我们三个人，带孩子5岁，10月28日出发玩3天")
    expect(page.get_by_test_id("nl-understood")).to_contain_text("2 位成人 + 1 位孩子（5 岁）")


def test_the_text_limit_and_the_error_popup(page: Page):
    open_new(page)
    page.get_by_test_id("nl-text").fill("字" * 501)
    expect(page.get_by_test_id("nl-go")).to_be_disabled()
    expect(page.get_by_test_id("nl-count")).to_contain_text("501 / 500")
    detail = {"detail": {"code": "too_long", "message": "最多 500 字，请精简一下"}}
    page.route("**/intake/nl", lambda r: r.fulfill(
        status=422, content_type="application/json", body=json.dumps(detail)))
    page.get_by_test_id("nl-text").fill("卢浮宫")
    page.get_by_test_id("nl-go").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("最多 500 字")
    page.get_by_test_id("notice-ok").click()


def test_ctrl_enter_submits(page: Page):
    open_new(page)
    page.get_by_test_id("nl-text").fill("10月28日出发玩3天")
    page.get_by_test_id("nl-text").press("Control+Enter")
    expect(page.get_by_test_id("nl-result")).to_be_visible(timeout=30_000)


# ---- S13：导入笔记入口 ----
def test_the_round_button_opens_the_note_import_dialog(page: Page):
    open_new(page)
    page.get_by_test_id("nl-import").click()
    expect(page.get_by_test_id("note-import")).to_be_visible()
    page.wait_for_timeout(200)                                                      # 等弹窗的 Esc 监听挂上
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("note-import")).to_be_hidden()


# ---- S14：其余保持 + 手机 ----
def test_the_rest_of_the_home_page_is_unchanged(page: Page):
    open_new(page)
    expect(page.locator(".step-no")).to_have_count(5)
    expect(page.get_by_role("button", name="生成行程")).to_have_count(1)
    expect(page.get_by_role("link", name=re.compile("AI 旅行规划"))).to_be_visible()
    expect(page.get_by_role("navigation", name="主导航")).to_be_visible()


def test_hero_fits_a_phone_screen(browser):
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, locale="zh-CN")
    pg = ctx.new_page()
    try:
        open_home(pg)
        card = pg.get_by_test_id("nl-card").bounding_box()
        assert card and card["x"] >= 0 and card["x"] + card["width"] <= 375, card
        assert pg.evaluate("document.documentElement.scrollWidth") <= 376
        expect(pg.get_by_test_id("nl-go")).to_be_visible()
        expect(pg.get_by_test_id("hero-credit")).to_be_visible()
    finally:
        ctx.close()
