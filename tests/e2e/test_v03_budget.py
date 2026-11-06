"""v0.3 批次 D 预算的前端验收（specs/modules/M09 S1a–S1h、S2a–S2d，M01 S4b）。

连的是真实后端（含 Frankfurter 汇率）；汇率拿不到时依赖人民币的用例会跳过，「汇率失败」「汇率旧数据」用拦截模拟。
默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_budget.py
"""

from __future__ import annotations

import json
import os
import re

import httpx
import pytest

pytest.importorskip("playwright")
from helpers_form import fill_trip  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e

WEB = os.getenv("E2E_WEB", "http://127.0.0.1:5173").rstrip("/")
API = os.getenv("E2E_API", "http://127.0.0.1:8137").rstrip("/")
BUDGET_URL = re.compile(r".*/plans/[^/]+/budget(\?.*)?$")
ADULT = {"age": 30, "residence": "none", "student": False}


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


def new_plan(theme="museums", **extra) -> str:
    body = {"destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
            "travel_style": "balanced", "interests": [theme], "theme": theme,
            "arrival_time": "2026-10-20 10:30", "departure_time": "2026-10-23 18:00"}
    body.update(extra)
    r = httpx.post(f"{API}/plans", json=body, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def budget(pid: str) -> dict:
    r = httpx.get(f"{API}/plans/{pid}/budget", timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def need_fx(pid: str) -> dict:
    b = budget(pid)
    if b["fx"] is None:
        pytest.skip("拿不到汇率（Frankfurter 连不上），人民币相关的用例跳过")
    return b


def cny(n: float) -> str:
    return f"¥{round(n):,}"


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()


def card(page: Page):
    return page.get_by_test_id("budget-card")


def save(page: Page) -> None:
    card(page).get_by_role("button", name="保存").click()


# --------------------------------------------------------------------------- #
# M01 S4b 首页：出行人与预算
# --------------------------------------------------------------------------- #
def test_home_has_a_party_editor_a_budget_cap_and_a_currency_and_sends_them_with_the_request(page):
    captured: dict = {}

    def handler(route):
        captured.update(route.request.post_data_json)
        route.fulfill(status=201, json={"task_id": "t-fake", "status": "queued"})

    page.route(re.compile(r".*/tasks$"), handler)
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()                                     # 主题加载完才会走「后台任务」路径
    block = page.get_by_test_id("party-block")
    block.wait_for()
    rows = block.get_by_test_id("party-row")
    expect(rows).to_have_count(1)                                                   # 默认一位成人
    expect(page.get_by_label("第 1 位的年龄")).to_have_value("30")
    expect(page.get_by_label("第 1 位长居哪里")).to_have_value("none")
    expect(page.get_by_label("第 1 位是学生")).not_to_be_checked()

    block.get_by_role("button", name="＋ 添加出行人").click()
    page.get_by_label("第 2 位的年龄").fill("8")
    block.get_by_role("button", name="＋ 添加出行人").click()
    page.get_by_label("第 3 位的年龄").fill("21")
    page.get_by_label("第 3 位长居哪里").select_option("fr")
    page.get_by_label("第 3 位是学生").check()
    expect(rows).to_have_count(3)
    expect(block).to_contain_text("共 3 人")
    block.get_by_role("button", name="删除第 2 位").click()
    expect(rows).to_have_count(2)
    expect(page.get_by_label("第 2 位的年龄")).to_have_value("21")                       # 删掉的是 8 岁那位

    page.get_by_label("预算上限").fill("8000")
    page.get_by_label("结算货币").select_option("EUR")
    fill_trip(page)
    page.get_by_role("button", name="生成行程").click()
    page.wait_for_url(re.compile(r"/tasks/t-fake"))
    assert captured["travelers"] == [ADULT, {"age": 21, "residence": "fr", "student": True}]
    assert captured["budget_cap"] == 8000 and captured["currency"] == "EUR"


def test_without_a_cap_the_request_carries_no_budget_cap(page):
    captured: dict = {}

    def handler(route):
        captured.update(route.request.post_data_json)
        route.fulfill(status=201, json={"task_id": "t-fake", "status": "queued"})

    page.route(re.compile(r".*/tasks$"), handler)
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    page.get_by_test_id("party-block").wait_for()
    fill_trip(page)
    page.get_by_role("button", name="生成行程").click()
    page.wait_for_url(re.compile(r"/tasks/t-fake"))
    assert "budget_cap" not in captured and captured["travelers"] == [ADULT] and captured["currency"] == "CNY"


def test_an_invalid_cap_on_the_home_form_is_refused_with_a_message(page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.get_by_test_id("party-block").wait_for()
    page.get_by_label("预算上限").fill("-5")
    page.get_by_role("button", name="生成行程").click()
    expect(page.get_by_test_id("party-block").get_by_text("预算上限要填不小于 0 的数字")).to_be_visible()
    expect(page).to_have_url(re.compile(rf"{re.escape(WEB)}/new"))                  # 没有跳走：还在新建页


def test_a_trip_generated_with_a_party_and_a_cap_shows_them_in_the_budget_card_and_leaves_a_trail_in_the_notes(page):
    party = [ADULT, {"age": 8, "residence": "none", "student": False},
             {"age": 21, "residence": "fr", "student": True}]
    pid = new_plan(travelers=party, budget_cap=4000, currency="CNY")
    b = need_fx(pid)
    open_plan(page, pid)
    expect(page.get_by_test_id("party-row")).to_have_count(3)
    expect(page.get_by_label("第 2 位的年龄")).to_have_value("8")
    expect(page.get_by_label("第 3 位长居哪里")).to_have_value("fr")
    expect(page.get_by_label("第 3 位是学生")).to_be_checked()
    expect(page.get_by_label("总预算上限")).to_have_value("4000")
    expect(page.get_by_test_id("budget-total")).to_contain_text(cny(b["total"]["cny_total"]))
    trail = page.get_by_text(re.compile("为了预算|门票以外的费用|拿不到汇率|已经换不动了"))
    expect(trail.first).to_be_visible()                                                    # 生成时的预算说明留了痕


# --------------------------------------------------------------------------- #
# S1a / S1b 预算卡与来源、优惠
# --------------------------------------------------------------------------- #
def test_the_budget_card_has_four_categories_each_with_its_source_and_never_shows_a_fake_zero(page):
    pid = new_plan()
    b = budget(pid)
    open_plan(page, pid)
    expect(card(page)).to_be_visible()
    for key, label, tag in (("tickets", "门票", "地点库价格"), ("meals", "餐饮", "估算"),
                            ("transport", "交通", "官方票价"), ("lodging", "住宿", "你填的")):
        row = page.get_by_test_id(f"budget-{key}")
        expect(row).to_contain_text(label)
        expect(row.locator(".tag")).to_have_text(tag)
    expect(page.get_by_test_id("budget-meals")).to_contain_text("估算，非实时价格")
    expect(page.get_by_test_id("budget-transport")).to_contain_text("官方 2026 年票价")
    expect(page.get_by_test_id("budget-lodging").locator(".b-amount")).to_have_text("待补")   # 没填住宿：待补，不是 ¥0
    expect(page.get_by_test_id("budget-lodging-pending")).to_contain_text("3 晚还没填住宿")
    for name in b["categories"][0]["pending"]:
        expect(page.get_by_test_id("budget-tickets-pending")).to_contain_text(name)       # 没价格的景点点名待补
    expect(card(page)).to_contain_text("合计")
    expect(card(page)).to_contain_text("人均")


def test_children_and_resident_students_get_discounts_that_are_itemised(page):
    pid = new_plan()
    solo = budget(pid)
    open_plan(page, pid)
    expect(page.get_by_test_id("budget-discounts")).to_have_count(0)                      # 一位成人：没有优惠
    card(page).get_by_role("button", name="＋ 添加出行人").click()
    page.get_by_label("第 2 位的年龄").fill("8")
    card(page).get_by_role("button", name="＋ 添加出行人").click()
    page.get_by_label("第 3 位的年龄").fill("21")
    page.get_by_label("第 3 位长居哪里").select_option("fr")
    page.get_by_label("第 3 位是学生").check()
    save(page)
    expect(page.locator(".budget-head")).to_contain_text("3 人合计")
    discounts = page.get_by_test_id("budget-discount")
    expect(discounts).to_have_count(2)
    expect(discounts.nth(0)).to_contain_text("第 2 位（8 岁）")
    expect(discounts.nth(1)).to_contain_text("第 3 位（21 岁 · 长居法国 · 学生）")
    after = budget(pid)
    assert len(after["travelers"]) == 3
    tickets_solo, tickets_3 = solo["categories"][0]["eur_total"], after["categories"][0]["eur_total"]
    assert tickets_3 < 3 * tickets_solo                                                    # 比三个成人便宜
    expect(page.get_by_test_id("budget-tickets")).to_contain_text("已按出行人身份计入优惠")
    assert httpx.get(f"{API}/plans/{pid}", timeout=10).json()["latest_version"] == 1       # 保存设置不产生行程版本


def test_places_whose_discount_rules_are_unchecked_are_named_and_priced_at_the_full_adult_fare(page):
    pid = new_plan(travelers=[ADULT, {"age": 8, "residence": "none", "student": False}])
    open_plan(page, pid)
    note = page.get_by_test_id("budget-note").filter(has_text="优惠规则未核对")
    expect(note).to_be_visible()
    expect(note).to_contain_text("按成人全价")


def test_amounts_are_shown_in_the_settlement_currency_first_with_the_other_alongside_and_a_dated_rate(page):
    pid = new_plan()
    b = need_fx(pid)
    open_plan(page, pid)
    total = page.get_by_test_id("budget-total")
    expect(total).to_contain_text(cny(b["total"]["cny_total"]))
    expect(page.get_by_test_id("budget-total")).to_have_text(re.compile(r"^\s*¥"))          # 默认人民币在前
    expect(page.get_by_test_id("budget-tickets")).to_contain_text(cny(b["categories"][0]["cny_total"]))
    fx = page.get_by_test_id("budget-fx")
    expect(fx).to_contain_text("汇率 1 欧元 =")
    expect(fx).to_contain_text(b["fx"]["date"])
    expect(fx).to_contain_text("欧洲央行参考汇率")


def test_switching_the_settlement_currency_to_euro_puts_euros_first_and_yuan_alongside_and_is_remembered(page):
    pid = new_plan()
    b = need_fx(pid)
    open_plan(page, pid)
    page.get_by_label("结算货币").select_option("EUR")
    save(page)
    total = page.get_by_test_id("budget-total")
    expect(total).to_have_text(re.compile(r"^\s*€"))                                        # 欧元在前
    expect(total).to_contain_text(cny(b["total"]["cny_total"]))                            # 人民币并列
    expect(page.get_by_test_id("budget-tickets").locator(".b-amount")).to_have_text(re.compile(r"^\s*€"))
    assert budget(pid)["currency"] == "EUR"
    page.reload()
    page.get_by_test_id("budget-total").wait_for()
    expect(page.get_by_test_id("budget-total")).to_have_text(re.compile(r"^\s*€"))
    expect(page.get_by_label("结算货币")).to_have_value("EUR")
    page.get_by_label("结算货币").select_option("CNY")
    save(page)
    expect(page.get_by_test_id("budget-total")).to_have_text(re.compile(r"^\s*¥"))


def test_a_stay_with_a_nightly_price_fills_the_lodging_line(page):
    pid = new_plan()
    httpx.put(f"{API}/plans/{pid}/stays", timeout=60, json={"reschedule": False, "stays": [
        {"name": "测试酒店", "check_in": "2026-10-20", "check_out": "2026-10-23", "area": "1区", "nightly_eur": 180}]})
    open_plan(page, pid)
    expect(page.get_by_test_id("budget-lodging")).to_contain_text("€540")                  # 3 晚 × 180
    expect(page.get_by_test_id("budget-lodging-pending")).to_have_count(0)
    assert budget(pid)["categories"][3]["eur_total"] == 540


# --------------------------------------------------------------------------- #
# S1h 事后修改；餐饮自填；非法输入
# --------------------------------------------------------------------------- #
def test_meal_override_and_cap_are_saved_remembered_and_change_the_numbers(page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_label("餐饮每人每天（欧元）").fill("40")
    save(page)
    expect(page.get_by_test_id("budget-meals").locator(".tag")).to_have_text("你填的")
    after = budget(pid)
    assert after["meal"]["is_override"] and after["meal"]["value"] == 40
    assert after["categories"][1]["eur_total"] == 160                                       # 40 × 4 天 × 1 人
    page.reload()
    page.get_by_test_id("budget-card").wait_for()
    expect(page.get_by_label("餐饮每人每天（欧元）")).to_have_value("40")
    page.get_by_label("餐饮每人每天（欧元）").fill("")                                         # 清掉覆盖：回到档位估算
    save(page)
    expect(page.get_by_test_id("budget-meals").locator(".tag")).to_have_text("估算")
    assert budget(pid)["meal"]["is_override"] is False


def test_invalid_settings_are_refused_with_a_popup_and_nothing_is_changed(page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_label("总预算上限").fill("-5")
    save(page)
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_contain_text("不小于 0")
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert budget(pid)["cap"] is None and len(budget(pid)["travelers"]) == 1


def test_the_last_traveler_cannot_be_deleted_and_ages_are_kept_within_range(page):
    pid = new_plan()
    open_plan(page, pid)
    expect(card(page).get_by_role("button", name="删除第 1 位")).to_have_count(0)             # 只剩一位：不给删除
    page.get_by_label("第 1 位的年龄").fill("999")
    expect(page.get_by_label("第 1 位的年龄")).to_have_value("120")


# --------------------------------------------------------------------------- #
# S2 预算上限与超预算提示
# --------------------------------------------------------------------------- #
def test_a_cap_above_the_estimate_says_within_budget_and_shows_what_is_left(page):
    pid = new_plan()
    b = need_fx(pid)
    open_plan(page, pid)
    cap = round(b["total"]["cny_total"]) + 1000
    page.get_by_label("总预算上限").fill(str(cap))
    save(page)
    within = page.get_by_test_id("budget-within")
    expect(within).to_contain_text("在预算内")
    expect(within).to_contain_text(cny(cap - b["total"]["cny_total"]))
    expect(page.get_by_test_id("budget-over")).to_have_count(0)
    expect(page.get_by_test_id("budget-suggestions")).to_have_count(0)
    expect(within).to_contain_text("待补")                                                 # 有待补项：实际可能更高


def test_a_cap_in_euros_works_without_any_rate_and_is_reported_in_euros(page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_label("结算货币").select_option("EUR")
    page.get_by_label("总预算上限").fill("50")
    page.get_by_label("上限的货币").select_option("EUR")
    save(page)
    over = page.get_by_test_id("budget-over")
    expect(over).to_contain_text("超出预算 €")
    expect(over).to_contain_text("上限 €50")


def test_exceeding_the_cap_shows_how_much_and_up_to_three_cheaper_swaps(page):
    pid = new_plan()
    need_fx(pid)
    open_plan(page, pid)
    page.get_by_label("总预算上限").fill("300")
    save(page)
    over = page.get_by_test_id("budget-over")
    expect(over).to_contain_text("超出预算")
    b = budget(pid)
    expect(over).to_contain_text(cny(b["status_amount"]))
    rows = page.get_by_test_id("budget-suggestion")
    n = rows.count()
    assert 1 <= n <= 3 and n == len(b["suggestions"])
    s0 = b["suggestions"][0]
    first = rows.first
    expect(first).to_contain_text(s0["title"])
    expect(first).to_contain_text(s0["alt_title"])
    expect(first).to_contain_text(cny(s0["saving"]))
    expect(first.get_by_role("button", name="编辑行程，换成它")).to_be_visible()
    assert b["suggestions"] == sorted(b["suggestions"], key=lambda s: -s["saving_eur"])


def test_clicking_a_suggestion_enters_edit_mode_and_applies_the_swap_unsaved(page):
    pid = new_plan()
    need_fx(pid)
    open_plan(page, pid)
    page.get_by_label("总预算上限").fill("300")
    save(page)
    page.get_by_test_id("budget-suggestion").first.wait_for()
    s0 = budget(pid)["suggestions"][0]
    page.get_by_test_id("budget-suggestion").first.get_by_role("button", name="编辑行程，换成它").click()
    expect(page.get_by_role("button", name=re.compile("保存为新版本"))).to_be_visible()      # 进入了编辑
    titles = page.locator(".day .item h4")
    expect(titles.filter(has_text=s0["alt_title"]).first).to_be_visible()                  # 换进来的点出现了
    expect(titles.filter(has_text=s0["title"])).to_have_count(0)                           # 被换掉的点消失了
    assert httpx.get(f"{API}/plans/{pid}", timeout=10).json()["latest_version"] == 1       # 还没保存，没有新版本
    expect(card(page)).to_have_count(0)                                                    # 编辑时预算卡收起


# --------------------------------------------------------------------------- #
# S1e 汇率拿不到 / 旧数据；预算本身失败
# --------------------------------------------------------------------------- #
def test_without_a_rate_the_card_shows_euros_only_and_says_so_without_any_error(page):
    pid = new_plan()
    real = budget(pid)

    def handler(route):
        body = json.loads(json.dumps(real))
        body["fx"], body["status"], body["status_amount"], body["status_currency"] = None, "none", None, None
        body["total"]["cny_total"] = body["total"]["cny_person"] = None
        for c in body["categories"]:
            c["cny_total"] = c["cny_person"] = None
        route.fulfill(json=body)

    page.route(BUDGET_URL, handler)
    open_plan(page, pid)
    expect(card(page)).to_be_visible()
    expect(page.get_by_test_id("budget-fx")).to_have_text("暂时拿不到汇率，只显示欧元")
    expect(page.get_by_test_id("budget-total")).not_to_contain_text("¥")
    expect(page.get_by_test_id("budget-tickets")).not_to_contain_text("¥")
    expect(page.get_by_test_id("budget-total")).to_contain_text("€")
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    expect(page.locator(".banner.err")).to_have_count(0)


def test_an_old_rate_is_labelled_as_old_data_with_its_date(page):
    pid = new_plan()
    real = need_fx(pid)

    def handler(route):
        body = json.loads(json.dumps(real))
        body["fx"] = {"rate": 7.4, "date": "2026-09-30", "stale": True}
        route.fulfill(json=body)

    page.route(BUDGET_URL, handler)
    open_plan(page, pid)
    fx = page.get_by_test_id("budget-fx")
    expect(fx).to_contain_text("2026-09-30")
    expect(fx).to_contain_text("旧数据")


@pytest.mark.parametrize("mode", ["server-error", "aborted"])
def test_when_the_budget_cannot_be_loaded_the_card_is_just_absent_and_the_page_stays_editable(page, mode):
    pid = new_plan("art")
    if mode == "server-error":
        page.route(BUDGET_URL, lambda r: r.fulfill(status=500, body='{"detail":"boom"}',
                                                   content_type="application/json"))
    else:
        page.route(BUDGET_URL, lambda r: r.abort())
    open_plan(page, pid)
    page.wait_for_timeout(600)
    expect(card(page)).to_have_count(0)
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    expect(page.get_by_text("离线")).to_have_count(0)                                       # 不会把页面切成只读
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_be_enabled()
    expect(page.locator(".day")).to_have_count(4)


# --------------------------------------------------------------------------- #
# 可收起（记住）；分享页不带预算；导出带费用
# --------------------------------------------------------------------------- #
def test_the_budget_card_can_be_collapsed_and_remembers_it(page):
    pid = new_plan()
    open_plan(page, pid)
    expect(card(page).get_by_test_id("budget-tickets")).to_be_visible()
    card(page).get_by_role("button", name="收起预算").click()
    expect(card(page).get_by_test_id("budget-tickets")).to_have_count(0)
    expect(card(page).get_by_test_id("budget-total")).to_be_visible()                       # 收起后总额仍在标题行
    page.reload()
    card(page).wait_for()
    expect(page.get_by_test_id("budget-tickets")).to_have_count(0)
    page.get_by_role("button", name="展开预算").click()
    expect(page.get_by_test_id("budget-tickets")).to_be_visible()


def test_the_public_share_page_shows_no_budget_party_or_cap(page):
    pid = new_plan(travelers=[{"age": 31, "residence": "fr", "student": False}, ADULT], budget_cap=12345)
    token = httpx.post(f"{API}/plans/{pid}/share", timeout=10).json()["token"]
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    expect(card(page)).to_have_count(0)
    expect(page.get_by_test_id("party-row")).to_have_count(0)
    expect(page.get_by_text("12,345")).to_have_count(0)
    shared = httpx.get(f"{API}/share/{token}", timeout=10)
    assert "budget" not in shared.json() and "cap_amount" not in shared.text and "12345" not in shared.text
    assert "travelers" not in shared.text and "residence" not in shared.text


def test_the_exports_carry_the_same_cost_section_in_the_settlement_currency(page):
    pid = new_plan()
    httpx.put(f"{API}/plans/{pid}/budget-settings", timeout=10, json={
        "travelers": [ADULT, {"age": 8, "residence": "none", "student": False}], "cap_amount": 300,
        "cap_currency": "CNY"})
    md = httpx.get(f"{API}/plans/{pid}/itinerary.md", timeout=60)
    html = httpx.get(f"{API}/plans/{pid}/itinerary.html", timeout=60)
    assert md.status_code == 200 and html.status_code == 200
    assert "## 费用估算" in md.text and "总计（2 人）" in md.text and "估算，非实时价格" in md.text
    assert "出行人：30 岁、8 岁" in md.text and "费用估算" in html.text and "合计" in html.text
    if budget(pid)["fx"] is not None:
        assert "预算上限 ¥300：估算超出" in md.text and "欧洲央行参考汇率" in md.text
    pdf = httpx.get(f"{API}/plans/{pid}/itinerary.pdf", timeout=120)
    assert pdf.status_code in (200, 503)
    if pdf.status_code == 200:
        assert pdf.content.startswith(b"%PDF")


# --------------------------------------------------------------------------- #
# 你的反馈：年龄可清空重填、新加的出行人默认空着、保存后直接带到提示、差距过大时提示
# --------------------------------------------------------------------------- #
def test_an_age_can_be_cleared_completely_and_retyped_and_a_new_traveler_starts_empty(page):
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    age = page.get_by_label("第 1 位的年龄")
    age.fill("")
    expect(age).to_have_value("")                                                         # 不会被强行变成 0
    age.fill("8")
    expect(age).to_have_value("8")
    page.get_by_role("button", name="＋ 添加出行人").click()
    new_age = page.get_by_label("第 2 位的年龄")
    expect(new_age).to_have_value("")                                                     # 新添加的默认空着
    expect(new_age).to_have_attribute("placeholder", "必填")
    new_age.press_sequentially("08")
    new_age.press("Backspace")
    new_age.press("Backspace")
    expect(new_age).to_have_value("")                                                     # 退格能删到空


def test_submitting_with_an_empty_age_names_the_traveler_and_sends_nothing(page):
    sent: list = []
    page.route(re.compile(r".*/(tasks|plans)$"), lambda r: (sent.append(r.request.url), r.abort()))
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()
    page.get_by_role("button", name="＋ 添加出行人").click()
    fill_trip(page)
    page.get_by_role("button", name="生成行程").click()
    expect(page.get_by_test_id("party-block").get_by_text("第 2 位出行人的年龄还没填")).to_be_visible()
    page.wait_for_timeout(300)
    assert sent == []


def test_saving_the_budget_card_with_an_empty_age_is_refused_with_a_popup(page):
    pid = new_plan()
    open_plan(page, pid)
    card(page).get_by_role("button", name="＋ 添加出行人").click()
    save(page)
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_contain_text("第 2 位出行人的年龄还没填")
    page.keyboard.press("Escape")
    assert len(budget(pid)["travelers"]) == 1                                             # 没保存


def test_after_saving_the_over_budget_notice_is_brought_into_view_without_scrolling(page):
    pid = new_plan()
    total = budget(pid)["total"]["eur_total"]
    open_plan(page, pid)
    page.get_by_label("结算货币").select_option("EUR")
    page.get_by_label("总预算上限").fill(str(round(total - 30)))                            # 比估算少一点：会超出
    page.get_by_label("上限的货币").select_option("EUR")
    page.get_by_label("总预算上限").scroll_into_view_if_needed()                              # 人在卡片最下面的表单处
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    save(page)
    over = page.get_by_test_id("budget-over")
    expect(over).to_contain_text("超出预算")
    expect(over).to_be_in_viewport()                                                      # 自动滚到提示处
    page.get_by_label("总预算上限").fill("999999")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    save(page)
    expect(page.get_by_test_id("budget-within")).to_be_in_viewport()                      # 「在预算内」也一样


def test_a_hopeless_cap_says_the_gap_is_too_big_for_swapping_while_a_near_miss_gets_no_such_warning(page):
    pid = new_plan()
    b = budget(pid)
    open_plan(page, pid)
    page.get_by_label("结算货币").select_option("EUR")
    page.get_by_label("上限的货币").select_option("EUR")
    page.get_by_label("总预算上限").fill("200")                                             # 门票以外的费用就已经超了
    save(page)
    gap = page.get_by_test_id("budget-gap")
    expect(gap).to_be_visible()
    expect(gap).to_contain_text("差距很大，换景点解决不了")
    expect(gap).to_contain_text("门票以外的费用")
    assert budget(pid)["gap"]["level"] == "unreachable"

    page.get_by_label("总预算上限").fill(str(round(b["total"]["eur_total"] - 10)))             # 只超一点
    save(page)
    expect(page.get_by_test_id("budget-over")).to_be_visible()
    expect(page.get_by_test_id("budget-gap")).to_have_count(0)
