"""排程结果 → 行程（规格 M01 S1/S2/S5，M06 S1/S2，M07 S2）。

事实（链接、预约、闭馆）全部来自地点库；模型只允许补文案，且文案里的网址 / 价格 / 开放时间一律丢弃。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from app.generator import STEPS, generate_from_theme
from app.library import load_library
from app.llm import LLMError
from app.schemas import ItemType, TravelRequest
from app.validator import validate_itinerary

LIB = load_library("paris")
THEMES = sorted(LIB.themes)
SAT = date(2026, 10, 3)


def make_request(start=SAT, days=4, style="balanced", **kw) -> TravelRequest:
    return TravelRequest(
        destination="Paris", start_date=start, end_date=start + timedelta(days=days - 1),
        budget_level="medium", travel_style=style, interests=kw.pop("interests", ["art"]), **kw,
    )


class FakeLLM:
    def __init__(self, reply: str | Exception):
        self.reply, self.calls = reply, 0

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def narration_for(trip, *, theme="L'art d'un jour", blurb="先看最有名的那一幅，再慢慢逛。") -> str:
    days = []
    for d in trip.itinerary.days:
        notes = {i.place_id: blurb for i in d.items if i.place_id}
        days.append({"date": d.date.isoformat(), "theme": theme, "notes": notes})
    return json.dumps({"days": days}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 结构：每个主题都能生成合法、0 error 的行程（M01 S1）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", THEMES)
def test_every_theme_generates_a_valid_itinerary_with_no_errors(theme):
    req = make_request(days=5, interests=[theme])
    trip = generate_from_theme(req, theme, client=None, library=LIB)
    report = validate_itinerary(trip.itinerary, req)
    assert report.errors == [], [f.message for f in report.errors]
    assert len(trip.itinerary.days) == 5
    assert trip.itinerary.generation.theme_id == theme


def test_items_carry_place_ids_that_exist_in_the_library():
    trip = generate_from_theme(make_request(), "art", client=None, library=LIB)
    for d in trip.itinerary.days:
        for i in d.items:
            if i.type in {ItemType.meal, ItemType.logistics}:
                assert i.place_id is None
            else:
                assert i.place_id in LIB.places
                assert i.title == LIB.get_place(i.place_id).name_zh


def test_items_are_sorted_and_have_transport_text():
    trip = generate_from_theme(make_request(days=5), "fashion", client=None, library=LIB)
    for d in trip.itinerary.days:
        times = [i.time for i in d.items]
        assert times == sorted(times)
        assert all(i.transport for i in d.items[1:])


# --------------------------------------------------------------------------- #
# 事实只来自地点库：预约清单与官网链接
# --------------------------------------------------------------------------- #
def test_booking_checklist_lists_every_place_with_a_ticket_channel_and_links_come_from_library():
    from app.library import Ticketing
    trip = generate_from_theme(make_request(days=4, interests=["museum"]), "museums", client=None, library=LIB)
    it = trip.itinerary
    in_trip = {i.place_id for d in it.days for i in d.items if i.place_id}
    with_channel = {pid for pid in in_trip if LIB.get_place(pid).ticketing != Ticketing.none}
    must = {i.place_id for d in it.days for i in d.items if i.booking_required}
    listed = {b.place_id for b in it.booking_checklist}
    assert listed == with_channel and with_channel
    assert must <= listed
    for b in it.booking_checklist:
        p = LIB.get_place(b.place_id)
        assert b.must_online == (p.ticketing == Ticketing.online_required)
        assert b.url == p.official_url
        assert b.url_status == (p.url_status.value if p.url_status else None)
        assert b.verified_at == p.verified_at


def test_place_without_a_verified_link_gets_no_link():
    """订票地点的官网核对不了时，宁可不给链接（页面显示「请自行搜索官网」）。"""
    lib = LIB.model_copy(deep=True)
    lib.places["lido"].official_url = None
    lib.places["lido"].url_status = None
    req = make_request(days=3, interests=["entertainment"], must_go=["Lido"])
    trip = generate_from_theme(req, "entertainment", client=None, library=lib)
    lido = [b for b in trip.itinerary.booking_checklist if b.place_id == "lido"]
    assert lido and lido[0].url is None and lido[0].url_status is None


# --------------------------------------------------------------------------- #
# 降级：模型不可用时仍有完整行程（M01 S5）
# --------------------------------------------------------------------------- #
def test_no_llm_gives_full_itinerary_with_template_text():
    trip = generate_from_theme(make_request(), "art", client=None, library=LIB)
    g = trip.itinerary.generation
    assert g.narration == "template"
    assert g.narration_note
    assert all(d.theme for d in trip.itinerary.days)


@pytest.mark.parametrize("reply", [LLMError("连不上"), "not json", "{}", '{"days": [{"date": "2030-01-01"}]}'])
def test_broken_llm_degrades_to_template(reply):
    llm = FakeLLM(reply)
    trip = generate_from_theme(make_request(), "art", client=llm, library=LIB)
    assert llm.calls == 1
    assert trip.itinerary.generation.narration == "template"
    assert trip.itinerary.generation.narration_note


def test_llm_text_is_used_but_facts_are_untouched():
    base = generate_from_theme(make_request(), "art", client=None, library=LIB)
    llm = FakeLLM(narration_for(base))
    trip = generate_from_theme(make_request(), "art", client=llm, library=LIB)
    assert trip.itinerary.generation.narration == "llm"
    assert all(d.theme == "L'art d'un jour" for d in trip.itinerary.days)
    # 地点、时间、顺序、预约、链接与模板版完全一致
    def facts(it):
        return [[(i.time, i.title, i.place_id, i.duration_minutes, i.booking_required) for i in d.items]
                for d in it.days], [(b.title, b.url) for b in it.booking_checklist]
    assert facts(trip.itinerary) == facts(base.itinerary)
    assert any("先看最有名的那一幅" in (i.notes or "") for d in trip.itinerary.days for i in d.items)


@pytest.mark.parametrize("bad", [
    "官网 https://fake-louvre.example/tickets 可订票",
    "门票只要 5 欧元",
    "每天 9:00 开门",
    "周二闭馆请注意",
    "www.louvre-tickets.com",
])
def test_llm_text_with_urls_prices_or_hours_is_dropped(bad):
    base = generate_from_theme(make_request(), "art", client=None, library=LIB)
    trip = generate_from_theme(make_request(), "art", client=FakeLLM(narration_for(base, blurb=bad)), library=LIB)
    for d in trip.itinerary.days:
        for i in d.items:
            assert bad not in (i.notes or "")
    assert not any("fake-louvre" in (i.notes or "") for d in trip.itinerary.days for i in d.items)


def test_llm_cannot_inject_unknown_places_or_change_dates():
    base = generate_from_theme(make_request(), "art", client=None, library=LIB)
    reply = json.dumps({"days": [{"date": "2026-10-03", "theme": "x" * 80,
                                  "notes": {"ghost-place": "不存在的地点"}}]}, ensure_ascii=False)
    trip = generate_from_theme(make_request(), "art", client=FakeLLM(reply), library=LIB)
    assert [d.date for d in trip.itinerary.days] == [d.date for d in base.itinerary.days]
    assert all(len(d.theme) <= 40 for d in trip.itinerary.days)
    assert not any("不存在的地点" in (i.notes or "") for d in trip.itinerary.days for i in d.items)


# --------------------------------------------------------------------------- #
# 进度步骤固定（M01 S1：前端按步骤名显示文案）
# --------------------------------------------------------------------------- #
def test_steps_are_reported_in_fixed_order():
    seen: list[str] = []
    generate_from_theme(make_request(), "art", client=None, library=LIB, on_step=seen.append)
    assert seen == list(STEPS) == ["library", "schedule", "validate", "narrate"]


# --------------------------------------------------------------------------- #
# 说明、风险提示
# --------------------------------------------------------------------------- #
def test_generation_info_has_reasons_notes_and_estimate_flag():
    trip = generate_from_theme(make_request(must_go=["蓬皮杜中心"]), "art", client=None, library=LIB)
    g = trip.itinerary.generation
    assert g.travel_estimated is True
    assert set(g.day_reasons) <= {d.date.isoformat() for d in trip.itinerary.days}
    assert any("蓬皮杜" in n for n in g.notes)
    assert any("估算" in r for r in trip.itinerary.risk_notes)


def test_arrival_and_departure_days_only_have_logistics_when_too_tight():
    req = make_request(arrival_time=datetime(2026, 10, 3, 21, 40), departure_time=datetime(2026, 10, 6, 7, 30))
    trip = generate_from_theme(req, "art", client=None, library=LIB)
    first, last = trip.itinerary.days[0], trip.itinerary.days[-1]
    assert [i.type for i in first.items] == [ItemType.logistics]
    assert [i.type for i in last.items] == [ItemType.logistics]
    assert validate_itinerary(trip.itinerary, req).errors == []


# --------------------------------------------------------------------------- #
# 从真实页面走查里发现的问题
# --------------------------------------------------------------------------- #
def test_day_trip_day_does_not_trigger_overpacked_or_thin_day_alarms():
    """迪士尼 10 小时、一天只有一个点——这是设计，不该被「超强度 / 偏薄」误报。"""
    req = make_request(days=5, interests=["family"])
    trip = generate_from_theme(req, "family", client=None, library=LIB)
    assert any(i.place_id == "disneyland-paris" for d in trip.itinerary.days for i in d.items)
    codes = {f.code for f in validate_itinerary(trip.itinerary, req).findings}
    assert not codes & {"day_overpacked", "thin_day", "overlong_item"}


def test_place_notes_do_not_repeat_library_text_shown_elsewhere():
    """库里的说明 / 看点页面和导出里会单独展示，条目备注里不再重复。"""
    trip = generate_from_theme(make_request(), "art", client=None, library=LIB)
    for d in trip.itinerary.days:
        for i in d.items:
            if i.place_id:
                p = LIB.get_place(i.place_id)
                assert not (p.note and p.note in (i.notes or "")), i.title
                assert not any(h in (i.notes or "") for h in p.highlights), i.title


def test_exports_show_highlights_and_address_for_library_places_not_in_the_old_whitelist():
    from app.renderer import render_html, render_markdown
    req = make_request(days=4, interests=["family"])
    trip = generate_from_theme(req, "family", client=None, library=LIB)
    html, md = render_html(trip.itinerary, req, trip.report), render_markdown(trip.itinerary, req, trip.report)
    assert "巴黎植物园" in md and "参观重点" in md
    place = LIB.get_place("jardin-des-plantes")
    assert not place.booking.required
    assert place.address in md and place.address in html            # 不订票：给地址
    assert place.official_url not in md and place.official_url not in html   # 不给官网


def test_exports_link_only_places_with_a_ticket_channel():
    """M06 S1b：卢浮宫有官网链接 + 地址，杜乐丽花园（无需购票）只有地址。"""
    from app.renderer import render_html, render_markdown
    req = make_request(days=4, interests=["art"], must_go=["卢浮宫", "杜乐丽花园"])
    trip = generate_from_theme(req, "art", client=None, library=LIB)
    html, md = render_html(trip.itinerary, req, trip.report), render_markdown(trip.itinerary, req, trip.report)
    louvre, tuileries = LIB.get_place("louvre"), LIB.get_place("tuileries-garden")
    for doc in (md, html):
        assert louvre.official_url in doc and louvre.address in doc
        assert tuileries.address in doc
        for p in LIB.places.values():
            if p.public_url is None and p.official_url:
                assert p.official_url not in doc, p.id


# --------------------------------------------------------------------------- #
# 购票三档与纸质票根偏好（规格 M06 S1 / S1b / S1c）
# --------------------------------------------------------------------------- #
def test_checklist_has_a_must_online_tier_and_a_recommended_tier():
    req = make_request(days=5, interests=["art"], must_go=["卢浮宫", "罗丹博物馆", "杜乐丽花园"])
    it = generate_from_theme(req, "art", client=None, library=LIB).itinerary
    by = {b.place_id: b for b in it.booking_checklist}
    assert by["louvre"].must_online is True and by["louvre"].url
    assert by["rodin"].must_online is False and by["rodin"].url
    assert "tuileries-garden" not in by                      # 无需购票：不进清单、没有链接
    flags = [b.must_online for b in it.booking_checklist]
    assert flags == sorted(flags, reverse=True)              # 「必须线上」排在前面
    assert "必须" in by["louvre"].reason and "排队" in by["rodin"].reason


def test_checklist_carries_the_researched_ticket_note():
    req = make_request(days=5, interests=["art"], must_go=["埃菲尔铁塔"])
    it = generate_from_theme(req, "art", client=None, library=LIB).itinerary
    eiffel = next(b for b in it.booking_checklist if b.place_id == "eiffel-tower")
    assert eiffel.note == LIB.get_place("eiffel-tower").ticket_note


def test_paper_ticket_preference_changes_the_advice_not_the_facts():
    kw = dict(days=5, interests=["art"], must_go=["卢浮宫", "罗丹博物馆"])
    online = generate_from_theme(make_request(**kw), "art", client=None, library=LIB).itinerary
    paper = generate_from_theme(make_request(paper_tickets=True, **kw), "art", client=None, library=LIB).itinerary
    on, pa = ({b.place_id: b for b in it.booking_checklist} for it in (online, paper))
    assert "现场窗口" not in on["rodin"].reason and "现场窗口" in pa["rodin"].reason
    assert "纸质票根不一定" in pa["louvre"].reason and "纸质票根" not in on["louvre"].reason
    assert pa["louvre"].url == on["louvre"].url and pa["rodin"].must_online == on["rodin"].must_online
