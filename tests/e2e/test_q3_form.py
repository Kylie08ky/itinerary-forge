"""Q3 新建表单的前端验收（specs/07-polish-backlog.md P06–P09、P15）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_q3_form.py

均衡默认且主题不设上限、天数建议、来回交通必填（含值机 / 取票提醒）、多处住宿、出行人姓名 / 身份 / 常用出行人。
提交一律拦截 POST /tasks 看请求内容，不真的生成（只有最后一个测试走完整流程）。
"""

from __future__ import annotations

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


def capture_tasks(page: Page) -> dict:
    """拦截 POST /tasks：把请求内容记下来，不真的生成。"""
    captured: dict = {}

    def handler(route):
        captured.update(route.request.post_data_json)
        route.fulfill(status=201, json={"task_id": "t-fake", "status": "queued"})

    page.route(re.compile(r".*/tasks$"), handler)
    return captured


def open_new(page: Page) -> None:
    page.goto(f"{WEB}/new?destination=Paris")
    page.locator(".theme-btn").first.wait_for()


def theme(page: Page, name: str):
    label = page.locator(".theme-label", has_text=re.compile(f"^{name}$"))
    return page.locator(".theme-btn:not(.balanced)").filter(has=label)


def submit(page: Page) -> None:
    page.get_by_role("button", name="生成行程").click()


# ---- P15：均衡默认、主题不设上限 ----
def test_balanced_is_the_default_and_sends_every_theme_with_equal_weight(page: Page):
    sent = capture_tasks(page)
    open_new(page)
    balanced = page.get_by_test_id("theme-balanced")
    expect(balanced).to_have_class(re.compile("active"))
    expect(balanced).to_contain_text("均衡 · 让我来推荐")
    expect(page.locator(".theme-btn.active")).to_have_count(1)                     # 具体主题一个都没选中
    expect(page.locator(".pool")).to_have_count(1)                                 # 候选池默认收起，均衡下也能用
    fill_trip(page)
    submit(page)
    page.wait_for_url(re.compile(r"/tasks/t-fake"))
    assert len(sent["themes"]) == 8 and {t["weight"] for t in sent["themes"]} == {3}
    assert sent["theme"] == sent["themes"][0]["id"] and sent["interests"] == ["均衡"]


def test_picking_themes_has_no_cap_and_unpicking_everything_returns_to_balanced(page: Page):
    sent = capture_tasks(page)
    open_new(page)
    for name in ("艺术", "美食", "购物", "摄影", "时尚"):
        theme(page, name).click()
    expect(page.locator(".theme-btn.active")).to_have_count(5)                     # 第 4、5 个也点得上
    expect(page.get_by_test_id("theme-balanced")).not_to_have_class(re.compile("active"))
    expect(page.locator(".mix-weights select")).to_have_count(5)
    for name in ("艺术", "美食", "购物", "摄影", "时尚"):
        theme(page, name).click()
    expect(page.get_by_test_id("theme-balanced")).to_have_class(re.compile("active"))     # 全取消 → 回到均衡
    theme(page, "美食").click()
    page.get_by_test_id("theme-balanced").click()                                  # 随时能点回均衡
    expect(page.locator(".theme-btn.active")).to_have_count(1)
    theme(page, "艺术").click()
    theme(page, "美食").click()
    fill_trip(page)
    submit(page)
    page.wait_for_url(re.compile(r"/tasks/t-fake"))
    assert [t["id"] for t in sent["themes"]] == ["art", "food"]


# ---- P06：天数建议 ----
@pytest.mark.parametrize("end, tone, text", [
    ("2026-11-11", "soft", "比较赶"),
    ("2026-11-13", "ok", "节奏比较从容"),
    ("2026-11-16", "soft", "偏长"),
    ("2026-11-20", "soft", "偏长"),
    ("2026-11-30", "warn", "其他主题的代表性景点"),
    ("2026-12-15", "warn", "留成自由日"),
])
def test_the_duration_advice_follows_the_city_and_never_blocks(page: Page, end, tone, text):
    open_new(page)
    page.get_by_label("开始日期").fill("2026-11-10")
    page.get_by_label("离开日期").fill(end)
    verdict = page.get_by_test_id("duration-verdict")
    expect(verdict).to_contain_text("巴黎建议 4–6 天" if tone != "ok" else "4–6 天")
    expect(verdict).to_contain_text(text)
    expect(verdict).to_have_class(re.compile(tone))
    expect(page.get_by_test_id("duration-advice")).to_contain_text("国内玩一座城市一般不超过一周")


# ---- P07：交通必填 ----
def test_transport_times_are_prefilled_from_the_dates_at_noon_and_follow_them(page: Page):
    # 用户反馈：到达 / 离开时间每次都要手填年月日太麻烦——按开始 / 离开日期预填（到达 10:00、返程 18:00），只改钟点
    open_new(page)
    start, end = page.get_by_label("开始日期").input_value(), page.get_by_label("离开日期").input_value()
    expect(page.get_by_label("去程到达时间")).to_have_value(f"{start}T10:00")
    expect(page.get_by_label("去程出发时间")).to_have_value(f"{start}T07:00")               # 出发早于到达，所以是 7 点
    expect(page.get_by_label("返程出发时间")).to_have_value(f"{end}T18:00")
    page.get_by_label("开始日期").fill("2026-12-20")
    page.get_by_label("离开日期").fill("2026-12-24")
    expect(page.get_by_label("去程到达时间")).to_have_value("2026-12-20T10:00")
    expect(page.get_by_label("返程出发时间")).to_have_value("2026-12-24T18:00")
    expect(page.get_by_test_id("transport-step")).to_contain_text("已按上面的日期预填")


def test_a_trip_can_be_submitted_with_only_the_prefilled_transport(page: Page):
    sent = capture_tasks(page)
    open_new(page)
    submit(page)
    page.wait_for_url(re.compile(r"/tasks/t-fake"))
    assert sent["transport"]["outbound"]["arrive_at"].endswith("T10:00:00")


def test_transport_is_required_and_the_first_error_scrolls_into_view(page: Page):
    sent = capture_tasks(page)
    open_new(page)
    for label in ("去程出发时间", "去程到达时间", "返程出发时间"):                          # 默认是预填的；清空才算没填
        page.get_by_label(label).fill("")
    submit(page)
    expect(page.get_by_test_id("transport-step").get_by_text("去程出发时间必填")).to_be_visible()
    expect(page.get_by_test_id("transport-step").get_by_text("去程到达时间必填")).to_be_visible()
    expect(page.get_by_test_id("transport-step").get_by_text("返程出发时间必填")).to_be_visible()
    expect(page.get_by_test_id("submit-errors")).to_contain_text("还有 3 项没填好")
    page.wait_for_timeout(600)
    top = page.locator(".home-form .ferr").first.bounding_box()["y"]
    assert 0 <= top <= 800, "第一处错误应该被滚到屏幕里"
    assert sent == {}                                                              # 没有发出生成请求


def test_filling_the_arrival_and_return_times_sets_the_trip_dates_and_the_order_is_checked(page: Page):
    # 用户反馈：时间里已经有日期，不该再因为「开始 / 离开日期」没改而报错——日期只填一次，两边互相跟着变
    open_new(page)
    page.get_by_label("开始日期").fill("2026-11-10")
    page.get_by_label("离开日期").fill("2026-11-13")
    page.get_by_label("去程出发时间").fill("2026-11-14T12:36")
    page.get_by_label("去程到达时间").fill("2026-11-14T14:20")
    page.get_by_label("返程出发时间").fill("2026-11-18T16:40")
    expect(page.get_by_label("开始日期")).to_have_value("2026-11-14")                 # 到达那天 = 开始日期
    expect(page.get_by_label("离开日期")).to_have_value("2026-11-18")                 # 返程出发那天 = 离开日期
    expect(page.get_by_test_id("trip-span")).to_contain_text("共 5 天（2026-11-14 → 2026-11-18）")
    page.get_by_label("去程出发时间").fill("2026-11-14T15:00")                         # 出发晚于到达：照样检查先后
    submit(page)
    step = page.get_by_test_id("transport-step")
    expect(step.get_by_text("到达时间要晚于出发时间")).to_be_visible()
    expect(step.get_by_text("要在开始日期")).to_have_count(0)


def test_changing_the_dates_moves_the_filled_transport_times_along(page: Page):
    open_new(page)
    fill_trip(page, "2026-11-10", "2026-11-13", arrive="15:30", leave="18:00")
    page.get_by_label("开始日期").fill("2026-11-20")
    page.get_by_label("离开日期").fill("2026-11-24")
    expect(page.get_by_label("去程到达时间")).to_have_value("2026-11-20T15:30")        # 时间不变、日期跟着换
    expect(page.get_by_label("返程出发时间")).to_have_value("2026-11-24T18:00")


def test_the_request_carries_the_transport_with_optional_details_only_when_filled(page: Page):
    sent = capture_tasks(page)
    open_new(page)
    fill_trip(page, "2026-11-10", "2026-11-13", arrive="09:40", leave="17:05")
    page.get_by_label("出发地").fill("上海浦东")
    page.get_by_label("航班号").first.fill("MU553")
    page.get_by_label("返程交通方式").select_option("high_speed_rail")
    expect(page.get_by_label("车次")).to_be_visible()                                  # 标签随方式变成「车次」
    submit(page)
    page.wait_for_url(re.compile(r"/tasks/t-fake"))
    out, back = sent["transport"]["outbound"], sent["transport"]["inbound"]
    assert out["mode"] == "flight" and out["from_place"] == "上海浦东" and out["number"] == "MU553"
    assert out["arrive_at"] == "2026-11-10T09:40:00" and back["depart_at"] == "2026-11-13T17:05:00"
    assert back["mode"] == "high_speed_rail" and "number" not in back and "to_place" not in back
    assert "arrival_time" not in sent and "hotel_area" not in sent


# ---- P08：多处住宿 ----
def test_several_stays_default_to_consecutive_dates_and_are_sent_with_automatic_names(page: Page):
    sent = capture_tasks(page)
    open_new(page)
    fill_trip(page, "2026-11-10", "2026-11-14")
    block = page.get_by_test_id("stay-block")
    expect(block.get_by_test_id("stay-row")).to_have_count(0)
    block.get_by_role("button", name="＋ 添加住宿").click()
    page.locator("#stay_area_0").fill("9区")
    page.locator("#stay_out_0").fill("2026-11-12")
    block.get_by_role("button", name="＋ 添加住宿").click()
    expect(page.locator("#stay_in_1")).to_have_value("2026-11-12")                     # 接着上一处的退房日
    expect(page.locator("#stay_out_1")).to_have_value("2026-11-14")
    page.locator("#stay_area_1").fill("蒙马特")
    page.locator("#stay_name_1").fill("蒙马特民宿")
    page.locator("#stay_price_1").fill("120")
    submit(page)
    page.wait_for_url(re.compile(r"/tasks/t-fake"))
    first, second = sent["stays"]
    assert first == {"name": "住宿 1", "check_in": "2026-11-10", "check_out": "2026-11-12", "area": "9区"}
    assert second["name"] == "蒙马特民宿" and second["nightly_eur"] == 120 and second["check_in"] == "2026-11-12"


def test_a_stay_without_area_or_address_or_overlapping_another_is_refused_with_a_message(page: Page):
    sent = capture_tasks(page)
    open_new(page)
    fill_trip(page, "2026-11-10", "2026-11-14")
    block = page.get_by_test_id("stay-block")
    block.get_by_role("button", name="＋ 添加住宿").click()
    submit(page)
    expect(block).to_contain_text("请填所在区或地址")
    page.locator("#stay_area_0").fill("9区")
    block.get_by_role("button", name="＋ 添加住宿").click()
    page.locator("#stay_area_1").fill("蒙马特")
    page.locator("#stay_in_1").fill("2026-11-11")                                      # 和第一处重叠
    page.locator("#stay_out_0").fill("2026-11-12")
    submit(page)
    expect(block).to_contain_text("日期重叠")
    assert sent == {}
    block.get_by_role("button", name="删除第 2 处住宿").click()
    expect(block.get_by_test_id("stay-row")).to_have_count(1)


# ---- P09：出行人 ----
def test_travelers_get_names_an_automatic_role_and_the_request_omits_blank_names(page: Page):
    sent = capture_tasks(page)
    open_new(page)
    page.get_by_label("第 1 位的姓名").fill("小红")
    expect(page.get_by_test_id("party-role").first).to_have_text("成人")
    page.get_by_label("第 1 位的年龄").fill("70")
    expect(page.get_by_test_id("party-role").first).to_have_text("老人")
    page.get_by_role("button", name="＋ 添加出行人").click()
    expect(page.get_by_test_id("party-role").nth(1)).to_have_text("待填年龄")
    page.get_by_label("第 2 位的年龄").fill("8")
    expect(page.get_by_test_id("party-role").nth(1)).to_have_text("儿童")
    page.get_by_label("第 2 位的年龄").fill("15")
    expect(page.get_by_test_id("party-role").nth(1)).to_have_text("青少年")
    fill_trip(page)
    submit(page)
    page.wait_for_url(re.compile(r"/tasks/t-fake"))
    assert sent["travelers"] == [{"name": "小红", "age": 70, "residence": "none", "student": False},
                                 {"age": 15, "residence": "none", "student": False}]


def test_copying_a_traveler_keeps_the_age_but_not_the_name(page: Page):
    open_new(page)
    page.get_by_label("第 1 位的姓名").fill("小明")
    page.get_by_label("第 1 位的年龄").fill("9")
    page.get_by_role("button", name="复制第 1 位").click()
    expect(page.get_by_test_id("party-row")).to_have_count(2)
    expect(page.get_by_label("第 2 位的年龄")).to_have_value("9")
    expect(page.get_by_label("第 2 位的姓名")).to_have_value("")


def test_frequent_travelers_are_saved_on_this_device_and_can_be_added_in_one_click(page: Page):
    open_new(page)
    expect(page.get_by_test_id("party-saved")).to_have_count(0)
    save = page.get_by_role("button", name="把第 1 位存为常用出行人")
    expect(save).to_be_disabled()                                                      # 没填姓名不能存
    page.get_by_label("第 1 位的姓名").fill("小明")
    page.get_by_label("第 1 位的年龄").fill("9")
    save.click()
    expect(page.get_by_test_id("party-saved")).to_contain_text("小明（9 岁）")
    page.reload()                                                                      # 换个会话还在
    saved = page.get_by_test_id("party-saved")
    saved.wait_for()
    saved.get_by_role("button", name=re.compile("^＋ 小明")).click()
    expect(page.get_by_label("第 1 位的姓名")).to_have_value("小明")                      # 第一位还是空白卡：直接替换
    expect(page.get_by_test_id("party-row")).to_have_count(1)
    expect(saved.get_by_role("button", name=re.compile("^＋ 小明"))).to_be_disabled()      # 已在名单里不能重复加
    page.get_by_role("button", name="忘掉常用出行人 小明").click()
    expect(page.get_by_test_id("party-saved")).to_have_count(0)


def test_the_residence_choice_only_appears_for_cities_that_have_residency_discounts(page: Page):
    cities = httpx.get(f"{API}/cities", timeout=10).json()
    for c in cities:
        c["residency_options"] = []
    page.route(re.compile(r".*/cities$"), lambda r: r.fulfill(json=cities))
    open_new(page)
    expect(page.get_by_label("第 1 位是学生")).to_be_visible()
    expect(page.get_by_label("第 1 位长居哪里")).to_have_count(0)


# ---- 完整流程：真的生成，交通进了行程、值机提醒出来了 ----
def test_a_real_plan_made_from_the_form_keeps_the_transport_and_gets_check_in_reminders(page: Page):
    open_new(page)
    fill_trip(page, "2026-11-10", "2026-11-13", arrive="15:30", leave="18:00")
    page.get_by_label("航班号").first.fill("MU553")
    page.get_by_label("第 1 位的姓名").fill("小红")
    submit(page)
    page.wait_for_url(re.compile(r"/plans/paris-"), timeout=240_000)
    page.locator(".day").first.wait_for()
    pid = page.url.rsplit("/", 1)[1].split("?")[0]
    first_day = httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]["days"][0]["items"]
    visits = [i for i in first_day if i["type"] not in ("logistics", "meal")]
    assert all(i["time"] >= "16:00" for i in visits), first_day                      # 下午 3 点半到：之后才开始排
    todos = {s["key"]: s for s in httpx.get(f"{API}/plans/{pid}/todos", timeout=30).json()["suggestions"]}
    assert todos["sug-checkin-out"]["text"] == "去程网上值机" and todos["sug-checkin-back"]["due"] == "2026-11-12"
