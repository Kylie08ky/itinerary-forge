"""v0.3 批次 E 随行清单的前端验收（specs/modules/M10 S2–S4）：行李清单、待办、行程备注、分享时勾选一起分享。

默认不跑；`-m e2e` 会自动起一套独立的前后端和临时数据库（不碰你平时用的服务和数据）：

    .venv\\Scripts\\python -m pytest -m e2e tests/e2e/test_v03_companion.py
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
def ctx_page(browser):
    """一个房主页面 + 一个陌生人页面（各自独立的浏览器环境）。"""
    owner = browser.new_context(viewport={"width": 1280, "height": 2600}, locale="zh-CN")
    guest = browser.new_context(viewport={"width": 1280, "height": 2600}, locale="zh-CN")
    yield owner.new_page(), guest.new_page()
    owner.close()
    guest.close()


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
            "arrival_time": "2026-10-20 10:30", "departure_time": "2026-10-23 18:00",
            "avoid_ids": ["versailles"]}   # 凡尔赛那天只有一个点
    body.update(extra)
    r = httpx.post(f"{API}/plans", json=body, timeout=120)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def open_plan(page: Page, pid: str) -> None:
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    page.get_by_test_id("companion-card").wait_for()


def item_at(pid: str, day: int, nth: int = 0) -> tuple[str, str]:
    """第 day 天（从 0 数）第 nth 条安排的 (标题, 备注对象)；备注对象规则同 companion.note_ref。"""
    plan = httpx.get(f"{API}/plans/{pid}", timeout=30).json()["itinerary"]["days"][day]
    it = plan["items"][nth]
    return it["title"], it["place_id"] or f"{plan['date']}|{it['title']}"


def notes(pid: str) -> dict:
    return httpx.get(f"{API}/plans/{pid}/notes", timeout=10).json()["notes"]


def put_note(pid: str, ref: str, text: str) -> None:
    r = httpx.put(f"{API}/plans/{pid}/notes", json={"ref": ref, "text": text}, timeout=10)
    assert r.status_code == 200, r.text


def card(page: Page):
    return page.get_by_test_id("companion-card")


def packing(pid: str) -> dict:
    return httpx.get(f"{API}/plans/{pid}/packing", timeout=30).json()


def todos(pid: str) -> dict:
    return httpx.get(f"{API}/plans/{pid}/todos", timeout=30).json()


def generated(page: Page, pid: str) -> None:
    open_plan(page, pid)
    card(page).get_by_role("button", name="按行程生成清单").click()
    page.get_by_test_id("pack-item").first.wait_for()


# --------------------------------------------------------------------------- #
# S2 行李清单
# --------------------------------------------------------------------------- #
def test_the_card_starts_empty_and_generating_fills_categories_with_progress_and_a_basis(page):
    pid = new_plan()
    open_plan(page, pid)
    expect(card(page).get_by_test_id("companion-summary")).to_contain_text("已带 0/0")
    expect(card(page)).to_contain_text("还没有清单")
    card(page).get_by_role("button", name="按行程生成清单").click()
    items = page.get_by_test_id("pack-item")
    items.first.wait_for()
    n = packing(pid)["total"]
    assert n > 20 and items.count() == n
    expect(page.get_by_test_id("packing-progress")).to_have_text(f"已带 0/{n}")
    for cat in ("证件", "衣物", "电子", "药品", "洗护", "其他"):
        expect(page.get_by_test_id(f"pack-cat-{cat}")).to_be_visible()
    expect(page.get_by_test_id("pack-cat-儿童")).to_have_count(0)                          # 没有孩子：没有儿童用品
    basis = page.get_by_test_id("packing-basis")
    expect(basis).to_contain_text("按：")
    expect(basis).to_contain_text(re.compile("偏冷|偏凉|温和|偏热"))
    expect(card(page).get_by_role("button", name="按行程生成清单")).to_have_count(0)        # 生成后变成「补充推荐项」
    expect(card(page).get_by_role("button", name="补充推荐项")).to_be_visible()


def test_only_the_visa_item_has_a_source_link_passport_and_plug_do_not(page):
    pid = new_plan()
    generated(page, pid)
    expect(page.get_by_test_id("pack-item").filter(has_text="护照（有效期")).to_have_count(1)
    expect(page.get_by_test_id("pack-item").filter(has_text="转换插头")).to_have_count(1)
    for text in ("护照（有效期", "转换插头", "机票"):
        expect(page.get_by_test_id("pack-item").filter(has_text=text).locator("a")).to_have_count(0)
    visa = page.get_by_test_id("pack-item").filter(has_text="签证 / 入境材料")
    link = visa.get_by_role("link", name=re.compile("来源"))
    expect(link).to_have_attribute("href", "https://france-visas.gouv.fr/")
    expect(card(page).locator("a[href^='http']")).to_have_count(1)                           # 整张清单只有这一个外链


def test_checking_items_updates_the_progress_instantly_and_is_remembered(page):
    pid = new_plan()
    generated(page, pid)
    total = packing(pid)["total"]
    items = page.get_by_test_id("pack-item")
    for k in range(3):
        items.nth(k).get_by_role("checkbox").check()
    expect(page.get_by_test_id("packing-progress")).to_have_text(f"已带 3/{total}")
    expect(card(page).get_by_test_id("companion-summary")).to_contain_text(f"已带 3/{total}")
    expect(page.get_by_test_id("pack-cat-证件")).to_contain_text("3/")                       # 分类小计
    assert "packed" in items.first.get_attribute("class")                                   # 划线样式
    page.wait_for_function("() => true")
    assert packing(pid)["packed"] == 3
    page.reload()
    page.get_by_test_id("pack-item").first.wait_for()
    expect(page.get_by_test_id("packing-progress")).to_have_text(f"已带 3/{total}")
    page.get_by_test_id("pack-item").first.get_by_role("checkbox").uncheck()
    expect(page.get_by_test_id("packing-progress")).to_have_text(f"已带 2/{total}")


def test_items_can_be_renamed_added_and_deleted(page):
    pid = new_plan()
    generated(page, pid)
    first = page.get_by_test_id("pack-item").first
    old = first.locator(".pack-text").inner_text()
    first.get_by_role("button", name=f"修改「{old}」").click()
    box = page.get_by_label(f"修改「{old}」").nth(1) if False else first.locator(".pack-edit")
    box.fill("我改过的名字")
    box.press("Enter")
    expect(page.get_by_test_id("pack-item").first.locator(".pack-text")).to_have_text("我改过的名字")
    assert any(i["text"] == "我改过的名字" for i in packing(pid)["items"])

    page.get_by_label("添加到其他").fill("给朋友的礼物")
    page.get_by_label("添加到其他").press("Enter")
    gift = page.get_by_test_id("pack-item").filter(has_text="给朋友的礼物")
    expect(gift).to_have_count(1)
    gift.get_by_role("button", name=re.compile("删除「给朋友的礼物」")).click()
    expect(page.get_by_test_id("pack-item").filter(has_text="给朋友的礼物")).to_have_count(0)


def test_deleted_recommendations_stay_deleted_until_restored_and_regenerating_never_overwrites(page):
    pid = new_plan()
    generated(page, pid)
    total = packing(pid)["total"]
    items = page.get_by_test_id("pack-item")
    items.nth(1).get_by_role("checkbox").check()                                            # 勾一项
    gone = items.nth(3).locator(".pack-text").inner_text()
    items.nth(3).get_by_role("button", name=re.compile("^删除")).click()
    expect(items).to_have_count(total - 1)
    card(page).get_by_role("button", name="补充推荐项").click()
    page.wait_for_timeout(500)
    expect(items).to_have_count(total - 1)                                                   # 删掉的没有冒回来
    assert items.nth(1).get_by_role("checkbox").is_checked()                                 # 勾过的没被重置
    restore = card(page).get_by_role("button", name=re.compile("恢复推荐项（1）"))
    expect(restore).to_be_visible()
    restore.click()
    expect(items).to_have_count(total)
    expect(page.get_by_test_id("pack-item").filter(has_text=gone)).to_have_count(1)
    expect(card(page).get_by_role("button", name=re.compile("恢复推荐项"))).to_have_count(0)


def test_children_in_the_party_add_kids_items_when_the_list_is_supplemented(page):
    pid = new_plan()
    generated(page, pid)
    expect(page.get_by_test_id("pack-cat-儿童")).to_have_count(0)
    httpx.put(f"{API}/plans/{pid}/budget-settings", json={"travelers": [ADULT, {"age": 6, "residence": "none",
                                                                                "student": False}]}, timeout=10)
    page.reload()
    page.get_by_test_id("pack-item").first.wait_for()
    card(page).get_by_role("button", name="补充推荐项").click()
    expect(page.get_by_test_id("pack-cat-儿童")).to_be_visible()
    expect(page.get_by_test_id("pack-cat-儿童")).to_contain_text("儿童退烧药")


# --------------------------------------------------------------------------- #
# S3 待办
# --------------------------------------------------------------------------- #
def go_todos(page: Page) -> None:
    card(page).get_by_role("tab", name=re.compile("待办")).click()
    page.get_by_test_id("todos-tab").wait_for()


def test_adding_a_todo_with_a_due_date_shows_urgency_in_red_when_close_or_overdue(page):
    pid = new_plan()
    open_plan(page, pid)
    go_todos(page)
    expect(page.get_by_test_id("todos-tab")).to_contain_text("还没有待办")
    for text, due in (("办申根签证", "2020-01-02"), ("不急的事", "2099-01-01"), ("没截止日", "")):
        page.get_by_label("新待办", exact=True).fill(text)
        if due:
            page.get_by_label("新待办的截止日").fill(due)
        page.get_by_role("button", name="添加", exact=True).click()
        expect(page.get_by_test_id("todo-item").filter(has_text=text)).to_have_count(1)
    over = page.get_by_test_id("todo-item").filter(has_text="办申根签证")
    expect(over.get_by_test_id("todo-due")).to_have_text(re.compile(r"已过期 \d+ 天"))
    assert over.get_by_test_id("todo-due").get_attribute("data-level") == "overdue"
    assert "hot" in over.get_attribute("class")                                              # 红色
    later = page.get_by_test_id("todo-item").filter(has_text="不急的事")
    expect(later.get_by_test_id("todo-due")).to_have_text("2099-01-01")
    assert "hot" not in later.get_attribute("class")
    expect(page.get_by_test_id("todo-item").filter(has_text="没截止日").get_by_test_id("todo-due")).to_have_text("没设截止日")
    expect(page.get_by_test_id("todo-summary")).to_contain_text("3 项待办")
    expect(page.get_by_test_id("todo-summary")).to_contain_text("1 项快到期 / 已过期")
    expect(card(page).get_by_test_id("companion-summary")).to_contain_text("1 项快到期 / 已过期")   # 标题行也有


def test_completing_a_todo_strikes_it_through_and_stops_the_reminder_and_it_can_be_deleted(page):
    pid = new_plan()
    httpx.post(f"{API}/plans/{pid}/todos", json={"text": "过期的事", "due": "2020-01-02"}, timeout=10)
    open_plan(page, pid)
    go_todos(page)
    row = page.get_by_test_id("todo-item").filter(has_text="过期的事")
    expect(row.get_by_test_id("todo-due")).to_contain_text("已过期")
    row.get_by_role("checkbox").check()
    expect(page.get_by_test_id("todo-item").filter(has_text="过期的事")).to_have_class(re.compile("done"))
    expect(row.get_by_test_id("todo-due")).to_have_count(0)                                  # 完成后不再提醒
    expect(page.get_by_test_id("todo-summary")).to_contain_text("0 项待办")
    assert todos(pid)["items"][0]["done"] is True
    row.get_by_role("button", name=re.compile("删除待办")).click()
    expect(page.get_by_test_id("todo-item")).to_have_count(0)


def test_the_due_date_of_a_todo_can_be_changed_and_cleared(page):
    pid = new_plan()
    httpx.post(f"{API}/plans/{pid}/todos", json={"text": "改日期", "due": "2020-01-02"}, timeout=10)
    open_plan(page, pid)
    go_todos(page)
    row = page.get_by_test_id("todo-item").filter(has_text="改日期")
    row.get_by_label("「改日期」的截止日").fill("2099-05-05")
    expect(row.get_by_test_id("todo-due")).to_have_text("2099-05-05")
    assert todos(pid)["items"][0]["due"] == "2099-05-05"
    row.get_by_label("「改日期」的截止日").fill("")
    expect(row.get_by_test_id("todo-due")).to_have_text("没设截止日")
    assert todos(pid)["items"][0]["due"] is None


def test_suggested_todos_can_be_picked_added_once_and_include_real_ticket_deadlines(page):
    pid = new_plan()
    open_plan(page, pid)
    go_todos(page)
    card(page).get_by_role("button", name=re.compile("看推荐待办")).click()
    sug = page.get_by_test_id("todo-suggestions")
    expect(sug).to_contain_text("是建议不是官方规定")
    lines = sug.get_by_test_id("todo-suggestion")
    n = lines.count()
    assert n >= 9
    expect(lines.filter(has_text="订机票")).to_have_count(1)
    expect(lines.filter(has_text=re.compile("订「.+」门票"))).not_to_have_count(0)           # 购票清单里的截止日
    expect(lines.filter(has_text=re.compile("订「.+」门票")).first).to_contain_text("建议最晚订票日")
    for k in range(n):                                                                       # 取消全部，再只勾两条
        lines.nth(k).get_by_role("checkbox").uncheck()
    lines.filter(has_text="订机票").get_by_role("checkbox").check()
    lines.filter(has_text="买旅行保险").get_by_role("checkbox").check()
    card(page).get_by_role("button", name="加入选中的（2）").click()
    expect(page.get_by_test_id("todo-item")).to_have_count(2)
    expect(page.get_by_test_id("todo-item").filter(has_text="订机票")).to_have_count(1)
    card(page).get_by_role("button", name=re.compile("看推荐待办")).click()
    expect(page.get_by_test_id("todo-suggestions").get_by_test_id("todo-suggestion").filter(has_text="订机票")).to_have_count(0)
    assert len(todos(pid)["items"]) == 2


def test_todos_survive_a_reload_and_the_tab_choice_is_remembered(page):
    pid = new_plan()
    httpx.post(f"{API}/plans/{pid}/todos", json={"text": "办保险", "due": "2099-01-01"}, timeout=10)
    open_plan(page, pid)
    go_todos(page)
    page.reload()
    card(page).wait_for()
    expect(page.get_by_test_id("todos-tab")).to_be_visible()                                 # 记住上次在待办页签
    expect(page.get_by_test_id("todo-item").filter(has_text="办保险")).to_have_count(1)


# --------------------------------------------------------------------------- #
# S4 行程备注（每条安排各自的备注）
# --------------------------------------------------------------------------- #
def add_button(page: Page, title: str, day: int = 0):
    """「📝 备注」按钮；午餐占位每天都有同名的，所以要指明是第几天（从 0 数）。"""
    return page.locator(".day").nth(day).get_by_role("button", name=f"给「{title}」写备注")


def note_box(page: Page, title: str, day: int = 0):
    return page.locator(".day").nth(day).get_by_label(f"备注：{title}")


def test_a_note_belongs_to_one_item_autosaves_on_blur_and_shows_up_in_the_export(page):
    pid = new_plan()
    title, ref = item_at(pid, 1, 0)
    other_title, _ = item_at(pid, 1, 1)
    open_plan(page, pid)
    expect(page.get_by_label("第 2 天的笔记")).to_have_count(0)                              # 不再有整天一条的笔记框
    add_button(page, title, 1).scroll_into_view_if_needed()
    add_button(page, title, 1).click()
    box = note_box(page, title, 1)
    box.fill("下午去逛旧书摊\n记得带学生证")
    expect(page.get_by_test_id("note-state")).to_have_text("")                               # 输入中还没保存
    box.blur()
    expect(page.get_by_test_id("note-state")).to_have_text("已保存")
    assert notes(pid) == {ref: "下午去逛旧书摊\n记得带学生证"}
    expect(add_button(page, other_title, 1)).to_be_visible()  # 别的安排还是「📝 备注」按钮
    page.reload()
    page.locator(".day").first.wait_for()
    expect(page.get_by_label(f"备注：{title}")).to_have_value("下午去逛旧书摊\n记得带学生证")
    expect(page.get_by_test_id("item-note")).to_have_count(1)
    md = httpx.get(f"{API}/plans/{pid}/itinerary.md", timeout=60).text
    assert md.count("- 备注（") == 1 and "记得带学生证" in md
    html = httpx.get(f"{API}/plans/{pid}/itinerary.html", timeout=60).text
    assert html.count('class="item-note"') == 1


def test_clearing_a_note_deletes_it(page):
    pid = new_plan()
    title, ref = item_at(pid, 1, 0)
    put_note(pid, ref, "临时备注")
    open_plan(page, pid)
    box = note_box(page, title, 1)
    expect(box).to_have_value("临时备注")
    box.fill("   ")
    box.blur()
    expect(page.get_by_test_id("note-state")).to_have_text("已保存")
    assert notes(pid) == {}


def test_an_old_whole_day_note_is_kept_and_follows_its_day_when_the_dates_are_shifted(page):
    pid = new_plan()
    put_note(pid, "day|2026-10-20", "第一天的整天提醒")
    assert httpx.post(f"{API}/plans/{pid}/shift", json={"start_date": "2026-11-03"}, timeout=60).status_code == 201
    open_plan(page, pid)
    expect(page.get_by_label("备注：整天笔记")).to_have_value("第一天的整天提醒")
    assert list(notes(pid)) == ["day|2026-11-03"]
    box = page.get_by_label("备注：整天笔记")
    box.fill("")
    box.blur()
    expect(page.get_by_label("备注：整天笔记")).to_have_count(0)  # 清空后这个旧笔记框就消失了
    assert notes(pid) == {}


def test_a_note_that_cannot_be_saved_says_so(page):
    pid = new_plan()
    title, _ = item_at(pid, 0, 0)
    open_plan(page, pid)
    page.route(re.compile(r".*/plans/[^/]+/notes$"), lambda r: r.fulfill(status=500, body='{"detail":"boom"}',
                                                                           content_type="application/json"))
    add_button(page, title).click()
    box = note_box(page, title)
    box.fill("会失败的备注")
    box.blur()
    expect(page.get_by_test_id("note-state")).to_have_text("保存失败，再试一次")
    expect(box).to_have_value("会失败的备注")                                                  # 输入的内容不丢


def test_opening_the_note_box_and_leaving_it_empty_puts_the_button_back(page):
    pid = new_plan()
    title, _ = item_at(pid, 0, 0)
    open_plan(page, pid)
    add_button(page, title).click()
    note_box(page, title).blur()
    expect(add_button(page, title)).to_be_visible()
    assert notes(pid) == {}


# --------------------------------------------------------------------------- #
# 收起、分享页、编辑模式、失败降级
# --------------------------------------------------------------------------- #
def test_the_companion_card_can_be_collapsed_and_remembers_it(page):
    pid = new_plan()
    open_plan(page, pid)
    expect(page.get_by_test_id("companion-card").get_by_role("tablist")).to_be_visible()
    card(page).get_by_role("button", name="收起随行清单").click()
    expect(card(page).get_by_role("tablist")).to_have_count(0)
    expect(card(page).get_by_test_id("companion-summary")).to_be_visible()                   # 收起后摘要还在
    page.reload()
    card(page).wait_for()
    expect(card(page).get_by_role("tablist")).to_have_count(0)
    card(page).get_by_role("button", name="展开随行清单").click()
    expect(card(page).get_by_role("tablist")).to_be_visible()


def seed_private_stuff(pid: str) -> str:
    httpx.post(f"{API}/plans/{pid}/packing/generate", timeout=30)
    httpx.post(f"{API}/plans/{pid}/todos", json={"text": "私人待办XYZ", "due": "2099-01-01"}, timeout=10)
    _, ref = item_at(pid, 1, 0)
    put_note(pid, ref, "私人备注XYZ")
    return httpx.post(f"{API}/plans/{pid}/share", timeout=10).json()["token"]


def test_the_public_share_page_shows_nothing_private_until_the_owner_ticks_it(page):
    pid = new_plan()
    token = seed_private_stuff(pid)
    page.goto(f"{WEB}/s/{token}")
    page.locator(".day").first.wait_for()
    expect(card(page)).to_have_count(0)
    expect(page.get_by_test_id("shared-packing")).to_have_count(0)
    expect(page.get_by_test_id("shared-todos")).to_have_count(0)
    expect(page.get_by_test_id("item-note")).to_have_count(0)
    expect(page.get_by_text("XYZ")).to_have_count(0)
    assert "XYZ" not in httpx.get(f"{API}/share/{token}", timeout=10).text


def test_ticking_things_in_the_share_dialog_shows_them_on_the_public_page_and_unticking_hides_them(ctx_page):
    page, stranger = ctx_page
    pid = new_plan()
    seed_private_stuff(pid)
    open_plan(page, pid)
    page.get_by_role("button", name="分享").click()
    opts = page.get_by_test_id("share-options")
    expect(opts).to_be_visible()
    for name in ("行李清单", "待办", "行程备注"):
        expect(opts.get_by_label(re.compile(name))).not_to_be_checked()                      # 默认都不勾
    token = httpx.get(f"{API}/plans/{pid}/share", timeout=10).json()["token"]

    stranger.goto(f"{WEB}/s/{token}")
    stranger.locator(".day").first.wait_for()
    opts.get_by_label(re.compile("行李清单")).check()
    expect(opts.get_by_label(re.compile("行李清单"))).to_be_checked()
    stranger.reload()
    stranger.locator(".day").first.wait_for()
    expect(stranger.get_by_test_id("shared-packing")).to_be_visible()
    expect(stranger.get_by_test_id("shared-packing")).to_contain_text("证件")
    expect(stranger.get_by_test_id("shared-todos")).to_have_count(0)
    expect(stranger.get_by_text("XYZ")).to_have_count(0)

    opts.get_by_label(re.compile("待办")).check()
    opts.get_by_label(re.compile("行程备注")).check()
    expect(opts.get_by_label(re.compile("行程备注"))).to_be_checked()
    stranger.reload()
    stranger.locator(".day").first.wait_for()
    expect(stranger.get_by_test_id("shared-todos")).to_contain_text("私人待办XYZ")
    expect(stranger.get_by_test_id("item-note")).to_have_count(1)
    expect(stranger.get_by_test_id("item-note")).to_contain_text("私人备注XYZ")
    expect(stranger.get_by_role("textbox")).to_have_count(0)                                 # 只读：没有输入框
    expect(stranger.get_by_role("checkbox")).to_have_count(0)                                # 也没有可勾的框
    expect(stranger.get_by_role("button", name=re.compile("备注"))).to_have_count(0)

    opts.get_by_label(re.compile("行李清单")).uncheck()
    opts.get_by_label(re.compile("待办")).uncheck()
    opts.get_by_label(re.compile("行程备注")).uncheck()
    expect(opts.get_by_label(re.compile("行程备注"))).not_to_be_checked()
    stranger.reload()
    stranger.locator(".day").first.wait_for()
    expect(stranger.get_by_text("XYZ")).to_have_count(0)
    expect(stranger.get_by_test_id("shared-packing")).to_have_count(0)
    # 勾选在刷新页面后还在
    page.reload()
    page.locator(".day").first.wait_for()
    page.get_by_role("button", name="分享").click()
    expect(page.get_by_test_id("share-options").get_by_label(re.compile("行李清单"))).not_to_be_checked()


def test_a_share_option_that_fails_to_save_is_undone_and_explained(page):
    pid = new_plan()
    httpx.post(f"{API}/plans/{pid}/share", timeout=10)
    open_plan(page, pid)
    page.route(re.compile(r".*/share/options$"), lambda r: r.fulfill(status=500, body='{"detail":"boom"}',
                                                                      content_type="application/json"))
    page.get_by_role("button", name="分享").click()
    box = page.get_by_test_id("share-options").get_by_label(re.compile("行李清单"))
    box.check()
    expect(page.get_by_role("alertdialog")).to_contain_text("boom")
    expect(box).not_to_be_checked()


def test_the_card_and_note_boxes_step_aside_while_editing_the_itinerary(page):
    pid = new_plan()
    open_plan(page, pid)
    page.get_by_role("button", name="编辑行程", exact=True).click()
    page.get_by_role("button", name=re.compile("保存为新版本")).wait_for()
    expect(card(page)).to_have_count(0)


@pytest.mark.parametrize("mode", ["server-error", "aborted"])
def test_when_the_companion_data_cannot_be_loaded_the_card_is_absent_and_the_page_stays_editable(page, mode):
    pid = new_plan("art")
    pattern = re.compile(r".*/plans/[^/]+/(packing|todos)$")
    if mode == "server-error":
        page.route(pattern, lambda r: r.fulfill(status=500, body='{"detail":"boom"}', content_type="application/json"))
    else:
        page.route(pattern, lambda r: r.abort())
    page.goto(f"{WEB}/plans/{pid}")
    page.locator(".day").first.wait_for()
    page.wait_for_timeout(600)
    expect(card(page)).to_have_count(0)
    expect(page.get_by_role("alertdialog")).to_have_count(0)
    expect(page.get_by_text("离线")).to_have_count(0)                                         # 不会把页面切成只读
    expect(page.get_by_role("button", name="编辑行程", exact=True)).to_be_enabled()
    title, _ = item_at(pid, 0, 0)
    expect(add_button(page, title)).to_be_visible()                                          # 备注不受影响
