"""行程编辑（规格 M04 S3–S7）：移动、重排、锁定、删除、加入、换一个、重新排程。

编辑函数是纯函数：输入「当前行程 + 一个操作」，返回新行程；不改输入、不碰网络和模型。
事实（购票、官网、闭馆）仍然只来自地点库，所以每次编辑后购票清单和风险提醒都按地点库重算。
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from app.editing import (
    AddPlace,
    EditError,
    MoveToDay,
    RemovePlace,
    ReorderDay,
    Reschedule,
    SetLock,
    SwapPlace,
    alternatives,
    apply_edit,
)
from app.generator import generate_from_theme
from app.library import load_library
from app.schemas import Itinerary, TravelRequest
from app.validator import validate_itinerary

LIB = load_library("paris")
SAT = date(2026, 10, 3)          # 周六；10-04 周日，10-05 周一，10-06 周二


def make_request(days=5, **kw) -> TravelRequest:
    return TravelRequest(
        destination="Paris", start_date=SAT, end_date=SAT + timedelta(days=days - 1),
        budget_level="medium", travel_style="balanced", interests=["art"], **kw,
    )


@pytest.fixture(scope="module")
def req() -> TravelRequest:
    return make_request()


@pytest.fixture(scope="module")
def base(req) -> Itinerary:
    return generate_from_theme(req, "art", client=None, library=LIB).itinerary


def places(it: Itinerary, d: date) -> list[str]:
    day = next(x for x in it.days if x.date == d)
    return [i.place_id for i in day.items if i.place_id]


def all_places(it: Itinerary) -> list[str]:
    return [pid for day in it.days for pid in places(it, day.date)]


def mins(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def assert_day_is_sane(it: Itinerary, d: date):
    day = next(x for x in it.days if x.date == d)
    prev_end = -1
    for i in day.items:
        if i.type == "logistics" and i.time >= "12:00" and i is day.items[-1]:
            continue                                  # 去机场的接驳按航班时间放，不参与重叠检查
        assert mins(i.time) >= prev_end, f"{d} {i.title} 与前一项重叠"
        prev_end = mins(i.time) + i.duration_minutes


def day_with(it: Itinerary, n: int) -> date:
    """第一个至少有 n 个景点的日期。"""
    return next(d.date for d in it.days if len(places(it, d.date)) >= n)


# --------------------------------------------------------------------------- #
# 移动（M04 S3）
# --------------------------------------------------------------------------- #
def test_move_to_another_day_recomputes_both_days(base, req):
    src = day_with(base, 2)
    pid = places(base, src)[0]
    dst = next(d.date for d in base.days if d.date != src and len(places(base, d.date)) >= 1)
    before_dst = places(base, dst)
    res = apply_edit(base, req, MoveToDay(place_id=pid, to_date=dst), LIB)
    it = res.itinerary
    assert pid not in places(it, src) and pid in places(it, dst)
    assert set(places(it, dst)) == set(before_dst) | {pid}
    assert set(res.touched) == {src, dst}
    for d in (src, dst):
        assert_day_is_sane(it, d)


def test_move_leaves_other_days_untouched(base, req):
    src = day_with(base, 2)
    pid = places(base, src)[0]
    dst = next(d.date for d in base.days if d.date != src and len(places(base, d.date)) >= 1)
    res = apply_edit(base, req, MoveToDay(place_id=pid, to_date=dst), LIB)
    for day in base.days:
        if day.date not in (src, dst):
            assert next(x for x in res.itinerary.days if x.date == day.date) == day


def test_edits_do_not_mutate_the_input(base, req):
    snapshot = copy.deepcopy(base)
    src = day_with(base, 2)
    apply_edit(base, req, MoveToDay(place_id=places(base, src)[0], to_date=SAT + timedelta(days=4)), LIB)
    assert base == snapshot


def test_moving_onto_a_closed_day_is_allowed_but_the_quality_check_says_why(base, req):
    """规格 S3：落在闭馆日，徽标出现警告并说明原因，而不是拒绝。"""
    louvre_day = next((d.date for d in base.days if "louvre" in places(base, d.date)), None)
    it = base
    if louvre_day is None:                              # 艺术主题里卢浮宫一定在；防御一下
        it = apply_edit(base, req, AddPlace(place_id="louvre", date=SAT), LIB).itinerary
        louvre_day = SAT
    tuesday = date(2026, 10, 6)
    res = apply_edit(it, req, MoveToDay(place_id="louvre", to_date=tuesday), LIB)
    assert "louvre" in places(res.itinerary, tuesday)
    report = validate_itinerary(res.itinerary, req)
    assert any(f.code == "closed_day" and f.day == tuesday.isoformat() for f in report.findings)


def test_move_errors(base, req):
    pid = all_places(base)[0]
    with pytest.raises(EditError, match="不在行程"):
        apply_edit(base, req, MoveToDay(place_id="no-such-place", to_date=SAT), LIB)
    with pytest.raises(EditError, match="不在行程"):
        apply_edit(base, req, MoveToDay(place_id="orsay" if "orsay" not in all_places(base) else "pantheon",
                                        to_date=SAT), LIB)
    with pytest.raises(EditError, match="日期"):
        apply_edit(base, req, MoveToDay(place_id=pid, to_date=date(2027, 1, 1)), LIB)


# --------------------------------------------------------------------------- #
# 天内重排
# --------------------------------------------------------------------------- #
def test_reorder_inside_a_day_follows_the_given_order_and_retimes(base, req):
    d = day_with(base, 2)
    order = places(base, d)
    new_order = list(reversed(order))
    res = apply_edit(base, req, ReorderDay(date=d, place_ids=new_order), LIB)
    assert places(res.itinerary, d) == new_order
    assert_day_is_sane(res.itinerary, d)
    assert res.touched == [d]


def test_reorder_must_use_exactly_the_days_places(base, req):
    d = day_with(base, 2)
    with pytest.raises(EditError, match="必须"):
        apply_edit(base, req, ReorderDay(date=d, place_ids=places(base, d)[:-1]), LIB)


# --------------------------------------------------------------------------- #
# 锁定（M04 S5）
# --------------------------------------------------------------------------- #
def test_lock_flag_is_set_and_survives_other_edits(base, req):
    d = day_with(base, 2)
    pid = places(base, d)[0]
    locked = apply_edit(base, req, SetLock(place_id=pid, locked=True), LIB).itinerary
    item = next(i for day in locked.days for i in day.items if i.place_id == pid)
    assert item.locked is True
    other = places(base, d)[1]
    moved = apply_edit(locked, req, MoveToDay(place_id=other, to_date=SAT + timedelta(days=4)), LIB).itinerary
    assert next(i for day in moved.days for i in day.items if i.place_id == pid).locked is True
    unlocked = apply_edit(locked, req, SetLock(place_id=pid, locked=False), LIB).itinerary
    assert next(i for day in unlocked.days for i in day.items if i.place_id == pid).locked is False


def test_a_locked_place_cannot_be_moved_or_removed(base, req):
    d = day_with(base, 2)
    pid = places(base, d)[0]
    locked = apply_edit(base, req, SetLock(place_id=pid, locked=True), LIB).itinerary
    with pytest.raises(EditError, match="锁定"):
        apply_edit(locked, req, MoveToDay(place_id=pid, to_date=SAT + timedelta(days=4)), LIB)
    with pytest.raises(EditError, match="锁定"):
        apply_edit(locked, req, RemovePlace(place_id=pid), LIB)


def test_reschedule_keeps_locked_places_on_their_day_position_and_time(base, req):
    """规格 S5：锁定的点位置和时间不变，其余内容可以变。"""
    d = day_with(base, 2)
    pid = places(base, d)[-1]
    original = next(i for i in next(x for x in base.days if x.date == d).items if i.place_id == pid)
    locked = apply_edit(base, req, SetLock(place_id=pid, locked=True), LIB).itinerary
    res = apply_edit(locked, req, Reschedule(), LIB)
    it = res.itinerary
    assert pid in places(it, d)
    now = next(i for i in next(x for x in it.days if x.date == d).items if i.place_id == pid)
    assert now.time == original.time and now.locked is True
    assert places(it, d).index(pid) == places(base, d).index(pid)


def test_reschedule_without_locks_is_the_original_schedule(base, req):
    res = apply_edit(base, req, Reschedule(), LIB)
    assert [places(res.itinerary, d.date) for d in base.days] == [places(base, d.date) for d in base.days]


def test_reschedule_needs_a_themed_plan(base, req):
    legacy = base.model_copy(update={"generation": None})
    with pytest.raises(EditError, match="主题"):
        apply_edit(legacy, req, Reschedule(), LIB)


# --------------------------------------------------------------------------- #
# 删除 / 加入
# --------------------------------------------------------------------------- #
def test_remove_drops_the_place_and_refreshes_the_ticket_checklist(base, req):
    pid = next(p for p in all_places(base) if LIB.get_place(p).ticketing.value != "none")
    assert pid in {b.place_id for b in base.booking_checklist}
    d = next(d.date for d in base.days if pid in places(base, d.date))
    res = apply_edit(base, req, RemovePlace(place_id=pid), LIB)
    assert pid not in all_places(res.itinerary)
    assert pid not in {b.place_id for b in res.itinerary.booking_checklist}
    assert res.touched == [d]
    assert_day_is_sane(res.itinerary, d)


def test_add_puts_a_library_place_on_the_day_and_updates_checklist(base, req):
    newcomer = "eiffel-tower"
    assert newcomer not in all_places(base)
    d = day_with(base, 1)
    res = apply_edit(base, req, AddPlace(place_id=newcomer, date=d), LIB)
    assert newcomer in places(res.itinerary, d)
    assert newcomer in {b.place_id for b in res.itinerary.booking_checklist}
    assert_day_is_sane(res.itinerary, d)
    item = next(i for i in next(x for x in res.itinerary.days if x.date == d).items if i.place_id == newcomer)
    assert item.location.startswith("Tour Eiffel")                  # 条目内容来自地点库（第二行是当地语言名）


def test_add_errors(base, req):
    with pytest.raises(EditError, match="地点库"):
        apply_edit(base, req, AddPlace(place_id="not-in-library", date=SAT), LIB)
    with pytest.raises(EditError, match="已经在"):
        apply_edit(base, req, AddPlace(place_id=all_places(base)[0], date=SAT), LIB)


# --------------------------------------------------------------------------- #
# 换一个（M04 S6）
# --------------------------------------------------------------------------- #
def test_alternatives_are_three_open_unused_library_places_near_the_original(base, req):
    d = day_with(base, 2)
    pid = places(base, d)[0]
    alts = alternatives(base, req, pid, LIB)
    assert 1 <= len(alts) <= 3
    used = set(all_places(base))
    for a in alts:
        p = LIB.get_place(a.place_id)
        assert a.place_id not in used and not LIB.is_closed_on(p, d)
        assert a.reason and a.name_zh == p.name_zh and a.address == p.address
    assert len({a.place_id for a in alts}) == len(alts)


def test_alternatives_prefer_the_same_theme(base, req):
    d = day_with(base, 2)
    members = {m.place_id for m in LIB.get_theme("art").members}
    alts = alternatives(base, req, places(base, d)[0], LIB)
    assert alts[0].place_id in members


def test_swap_replaces_in_place_and_keeps_the_position(base, req):
    d = day_with(base, 2)
    order = places(base, d)
    victim = order[0]
    new = alternatives(base, req, victim, LIB)[0].place_id
    res = apply_edit(base, req, SwapPlace(place_id=victim, new_place_id=new), LIB)
    after = places(res.itinerary, d)
    assert victim not in all_places(res.itinerary) and after == [new, *order[1:]]
    assert_day_is_sane(res.itinerary, d)
    assert res.touched == [d]


def test_swap_errors(base, req):
    pid = all_places(base)[0]
    with pytest.raises(EditError, match="已经在"):
        apply_edit(base, req, SwapPlace(place_id=pid, new_place_id=all_places(base)[1]), LIB)


# --------------------------------------------------------------------------- #
# 其他
# --------------------------------------------------------------------------- #
def test_an_edited_itinerary_still_passes_the_quality_check_when_nothing_conflicts(base, req):
    d = day_with(base, 2)
    res = apply_edit(base, req, ReorderDay(date=d, place_ids=list(reversed(places(base, d)))), LIB)
    assert validate_itinerary(res.itinerary, req).errors == []


def test_edits_are_deterministic(base, req):
    d = day_with(base, 1)
    a = apply_edit(base, req, AddPlace(place_id="pantheon", date=d), LIB).itinerary
    b = apply_edit(base, req, AddPlace(place_id="pantheon", date=d), LIB).itinerary
    assert a == b


def test_moved_place_keeps_its_item_notes(base, req):
    src = day_with(base, 2)
    pid = places(base, src)[0]
    old = next(i for i in next(x for x in base.days if x.date == src).items if i.place_id == pid)
    dst = next(d.date for d in base.days if d.date != src and len(places(base, d.date)) >= 1)
    it = apply_edit(base, req, MoveToDay(place_id=pid, to_date=dst), LIB).itinerary
    new = next(i for i in next(x for x in it.days if x.date == dst).items if i.place_id == pid)
    assert new.notes == old.notes


def test_touched_days_get_a_manual_edit_reason(base, req):
    d = day_with(base, 2)
    res = apply_edit(base, req, ReorderDay(date=d, place_ids=list(reversed(places(base, d)))), LIB)
    reasons = res.itinerary.generation.day_reasons[d.isoformat()]
    assert any("手动" in r for r in reasons)


# --------------------------------------------------------------------------- #
# 把一天的景点删光：这一天变成「自由安排」，而不是出错（真实评测里抓到的）
# --------------------------------------------------------------------------- #
def test_removing_the_last_place_of_a_day_leaves_a_free_day_with_meals(base, req):
    d = day_with(base, 1)
    it = base
    for pid in places(base, d):
        it = apply_edit(it, req, RemovePlace(place_id=pid), LIB).itinerary
    day = next(x for x in it.days if x.date == d)
    assert places(it, d) == [] and day.items, "不能是空的一天"
    assert day.theme == "自由安排" or "自由" in day.theme
    assert validate_itinerary(it, req).errors == []


def test_a_day_with_nothing_at_all_still_has_one_item(req):
    """窗口太短、午晚餐都排不下的一天（比如早班机的离开日），删光景点后也不能是空列表。"""
    from datetime import datetime
    early = make_request(arrival=None, departure=datetime(2026, 10, 7, 7, 30))
    trip = generate_from_theme(early, "art", client=None, library=LIB).itinerary
    last = trip.days[-1].date
    newcomer = next(p for p in ("pantheon", "rue-cler", "cafe-de-flore") if p not in all_places(trip))
    it = apply_edit(trip, early, AddPlace(place_id=newcomer, date=last), LIB).itinerary
    it = apply_edit(it, early, RemovePlace(place_id=newcomer), LIB).itinerary
    assert next(x for x in it.days if x.date == last).items
