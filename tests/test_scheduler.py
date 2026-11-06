"""排程器验收（规格 M03 S1–S6、S9；M01 S1、S2）。

排程器是纯函数：输入相同输出相同，不碰网络和 LLM，所以这里不需要任何假模型。
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta

import pytest

from app.library import BestTime, Zone, load_library
from app.scheduler import SchedulerConfig, Stop, schedule
from app.scheduler.engine import is_lunch
from app.scheduler.geo import haversine_km, travel
from app.schemas import TravelRequest
from app.validator import ValidatorConfig

LIB = load_library("paris")
CFG = ValidatorConfig()
SAT = date(2026, 10, 3)       # 2026-10-03 是周六；10-05 周一，10-06 周二


def make_request(start=SAT, days=4, style="balanced", arrival=None, departure=None, **kw) -> TravelRequest:
    end = start + timedelta(days=days - 1)
    return TravelRequest(
        destination="Paris", start_date=start, end_date=end, budget_level="medium",
        travel_style=style, interests=kw.pop("interests", ["art"]),
        arrival_time=arrival, departure_time=departure, **kw,
    )


def places_of(day) -> list[Stop]:
    return [s for s in day.stops if s.kind == "place"]


def is_trip_day(day) -> bool:
    return any(LIB.places[s.place_id].zone == Zone.day_trip for s in places_of(day))


def mains(day) -> list[Stop]:
    """「主要点」：不含顺手补的小点（role=filler）。节奏上限只约束主要点。"""
    return [s for s in places_of(day) if s.role != "filler"]


def all_place_stops(draft):
    return [(d, s) for d in draft.days for s in places_of(d)]


THEMES = sorted(LIB.themes)
# 覆盖每个星期几作为起点，保证每个闭馆日规则都被踩到
STARTS = [SAT + timedelta(days=i) for i in range(7)]


# --------------------------------------------------------------------------- #
# S1 / M01-S2：不排闭馆日（对所有主题、所有起始星期做属性检查）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("start", STARTS, ids=lambda d: d.strftime("%a"))
def test_never_schedules_a_place_on_its_closed_day(theme, start):
    draft = schedule(make_request(start=start, days=5), theme, LIB)
    for day, stop in all_place_stops(draft):
        place = LIB.get_place(stop.place_id)
        assert not LIB.is_closed_on(place, day.date), f"{place.name_zh} 在 {day.date} 闭馆"


def test_louvre_not_on_tuesday_and_orsay_not_on_monday():
    draft = schedule(make_request(days=4, interests=["museum"]), "museums", LIB)   # 周六–周二
    by_date = {d.date: {s.place_id for s in places_of(d)} for d in draft.days}
    assert "louvre" not in by_date[date(2026, 10, 6)]
    assert "orangerie" not in by_date[date(2026, 10, 6)]
    assert "orsay" not in by_date[date(2026, 10, 5)]
    assert "rodin" not in by_date[date(2026, 10, 5)]


# --------------------------------------------------------------------------- #
# S2：长期闭馆永远不出现；必去时给出醒目提示
# --------------------------------------------------------------------------- #
def test_long_term_closed_place_never_appears_by_default():
    closed = [p.id for p in LIB.places.values() if p.long_term_closed]
    assert closed, "地点库里应至少有一个长期闭馆的点（蓬皮杜）"
    for theme in THEMES:
        draft = schedule(make_request(days=5), theme, LIB)
        assert not {s.place_id for _, s in all_place_stops(draft)} & set(closed)


def test_must_go_long_term_closed_is_scheduled_with_a_loud_warning():
    draft = schedule(make_request(days=4, must_go=["蓬皮杜中心"]), "art", LIB)
    scheduled = {s.place_id for _, s in all_place_stops(draft)}
    assert "pompidou" in scheduled
    assert any("闭馆" in n and "蓬皮杜" in n for n in draft.notes)


def test_must_go_is_included_even_if_not_in_theme():
    draft = schedule(make_request(days=4, must_go=["埃菲尔铁塔"]), "art", LIB)
    assert "eiffel-tower" in {s.place_id for _, s in all_place_stops(draft)}


def test_avoid_is_respected():
    draft = schedule(make_request(days=4, avoid=["卢浮宫"]), "museums", LIB)
    assert "louvre" not in {s.place_id for _, s in all_place_stops(draft)}


# --------------------------------------------------------------------------- #
# S3：抵达 / 离开缓冲
# --------------------------------------------------------------------------- #
def test_late_arrival_and_early_departure_leave_only_logistics():
    arrival = datetime(2026, 10, 3, 21, 40)
    departure = datetime(2026, 10, 6, 7, 30)
    draft = schedule(make_request(arrival=arrival, departure=departure), "art", LIB)
    first, last = draft.days[0], draft.days[-1]
    assert places_of(first) == [] and [s.kind for s in first.stops] == ["logistics"]
    assert places_of(last) == [] and [s.kind for s in last.stops] == ["logistics"]
    assert "抵达" in first.stops[0].title and "机场" in last.stops[0].title


def test_activities_respect_arrival_and_departure_buffers():
    arrival = datetime(2026, 10, 3, 10, 30)
    departure = datetime(2026, 10, 6, 18, 0)
    draft = schedule(make_request(arrival=arrival, departure=departure), "art", LIB)
    arr, dep = 10 * 60 + 30, 18 * 60
    for s in places_of(draft.days[0]):
        assert s.start >= arr + CFG.arrival_buffer_minutes
    for s in draft.days[-1].stops:
        if s.kind != "logistics":
            assert s.start + s.duration <= dep - CFG.departure_buffer_minutes
    assert draft.days[-1].stops[-1].kind == "logistics"


# --------------------------------------------------------------------------- #
# S4：郊区一日游独占一天
# --------------------------------------------------------------------------- #
def test_day_trip_gets_its_own_day():
    draft = schedule(make_request(days=5, interests=["family"]), "family", LIB)
    trip_days = [d for d in draft.days
                 if any(LIB.get_place(s.place_id).zone.value == "day_trip" for s in places_of(d))]
    assert trip_days, "亲子主题 5 天应安排迪士尼"
    for d in trip_days:
        assert len(places_of(d)) == 1
    assert "disneyland-paris" in {s.place_id for d in trip_days for s in places_of(d)}


def test_at_most_one_full_day_place_per_day_in_every_theme():
    """≥4 小时的馆（城市科学馆之类）同一天最多一个。"""
    for theme in THEMES:
        draft = schedule(make_request(days=5), theme, LIB)
        for d in draft.days:
            assert sum(LIB.get_place(s.place_id).full_day for s in places_of(d)) <= 1


# --------------------------------------------------------------------------- #
# S5：路线不折返；固定点不动
# --------------------------------------------------------------------------- #
def _engine_cost(draft, req, day_index, order_ids, theme):
    """用引擎自己的代价函数（通勤 + 时段偏好 + 空档）给「这天按这个顺序排」打分；排不下返回 None。"""
    from app.scheduler.engine import (
        Cand,
        _build_windows,
        _mixed_recipe,
        _timeline,
        selected_themes,
        theme_windows,
    )

    windows = _build_windows(req, SchedulerConfig(), CFG)
    recipe = _mixed_recipe(LIB, selected_themes(req, theme))
    w = theme_windows(windows, recipe)[day_index]                 # 和 schedule() 同样的窗口
    cands = tuple(Cand(LIB.get_place(pid), 3, 0, "anchor", "t") for pid in order_ids)
    cap = int(CFG.active_cap(req.travel_style.value) * w.cap_ratio)
    tl = _timeline(w, cands, draft.hotel, SchedulerConfig(), cap, None, req.travel_style.value)
    return None if tl is None else tl.cost


def test_chosen_order_is_the_cheapest_of_all_orders():
    """每天的顺序在所有排列里代价最小（代价 = 通勤 + 时段偏好 + 空档），固定点除外。"""
    req = make_request(days=5, style="intense")
    checked = 0
    for theme in THEMES:
        draft = schedule(req, theme, LIB)
        for i, d in enumerate(draft.days):
            ids = [s.place_id for s in places_of(d)]
            if not 3 <= len(ids) <= 5:
                continue
            chosen = _engine_cost(draft, req, i, ids, theme)
            best = min(c for p in itertools.permutations(ids)
                       if (c := _engine_cost(draft, req, i, p, theme)) is not None)
            assert chosen == pytest.approx(best), f"{theme} {d.date}"
            checked += 1
    assert checked >= 8


def test_route_is_not_far_worse_than_alphabetical_order_in_travel_time():
    """通勤时间只是代价的一部分（还要避免空档），所以只要求不比按名称排差太多。"""
    for theme in THEMES:
        draft = schedule(make_request(days=5, style="intense"), theme, LIB)
        for d in draft.days:
            ps = [LIB.get_place(s.place_id) for s in places_of(d)]
            if len(ps) < 3:
                continue
            ours = sum(s.travel_min for s in places_of(d))
            names = sorted(ps, key=lambda p: p.name_zh)
            naive = sum(travel(a, b)[0] for a, b in itertools.pairwise([draft.hotel, *((p.lat, p.lng) for p in names)]))
            assert ours <= naive * 1.5 + 30, f"{theme} {d.date}: {ours} 分钟 vs 按名称排 {naive} 分钟"


def test_two_opt_untangles_a_crossing_route():
    """点数太多不能枚举时的退路：最近邻 + 2-opt 不会比最近邻更差。"""
    from app.scheduler.engine import Cand, _pt, _two_opt
    pts = [LIB.get_place(i) for i in ("louvre", "orsay", "eiffel-tower", "rodin", "orangerie", "picasso",
                                       "marmottan", "guimet", "mam-paris")]
    cands = [Cand(p, 3, 0, "anchor", "t") for p in pts]
    route = _two_opt(cands, (48.8566, 2.3522))
    assert {c.place.id for c in route} == {p.id for p in pts}
    nn = sorted(cands, key=lambda c: c.place.id)
    hotel = (48.8566, 2.3522)

    def length(r):
        return sum(haversine_km(a, b) for a, b in itertools.pairwise([hotel, *(_pt(c.place) for c in r)]))

    assert length(route) <= length(nn)


def test_pinned_place_keeps_its_position():
    base = schedule(make_request(days=4), "art", LIB)
    target_day = next(d for d in base.days if len(places_of(d)) >= 2)
    pid = places_of(target_day)[-1].place_id
    pinned = schedule(make_request(days=4), "art", LIB, pinned={pid: (target_day.date, 0)})
    day = next(d for d in pinned.days if d.date == target_day.date)
    assert places_of(day)[0].place_id == pid


# --------------------------------------------------------------------------- #
# S6：节奏控制
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("style", ["relaxed", "balanced", "intense"])
@pytest.mark.parametrize("theme", THEMES)
def test_rhythm_caps_are_respected(style, theme):
    n = LIB.get_theme(theme).recipe.anchors_per_day[style]
    draft = schedule(make_request(days=5, style=style), theme, LIB)
    for d in draft.days:
        ps = places_of(d)
        assert len([s for s in mains(d) if not _night(s) and not is_lunch(s)]) <= n
        assert len(ps) <= n + 2 + SchedulerConfig().filler_max
        assert sum(s.duration for s in ps) <= CFG.active_cap(style) or len(ps) == 1


def test_art_balanced_full_days_have_exactly_the_recipe_count():
    n = LIB.get_theme("art").recipe.anchors_per_day["balanced"]
    draft = schedule(make_request(days=4, arrival=datetime(2026, 10, 3, 10, 30),
                                  departure=datetime(2026, 10, 6, 18, 0)), "art", LIB)
    for d in draft.days[1:-1]:
        if is_trip_day(d):               # 凡尔赛这类一日游独占一天，不按主题的每天点数算
            continue
        assert len([s for s in mains(d) if not _night(s)]) == n                   # 晚上的点另算（P20）
    for d in draft.days:                # 抵达日 / 离开日窗口短，可以少，但不能是空的
        assert len(places_of(d)) >= 1


def test_every_anchor_comes_from_the_theme_unless_supplemented():
    draft = schedule(make_request(days=4), "art", LIB)
    members = {m.place_id for m in LIB.get_theme("art").members}
    outside = {s.place_id for _, s in all_place_stops(draft)
               if s.role != "filler" and not _night(s)} - members                # 晚上的夜景不要求是主题的点（P20）
    assert outside == set() or any("补充" in n for n in draft.notes)


# --------------------------------------------------------------------------- #
# 时间线：严格递增、不重叠、开门时间内、通勤已计入、通勤标注为估算
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", THEMES)
def test_timeline_is_sane(theme):
    draft = schedule(make_request(days=5), theme, LIB)
    for d in draft.days:
        t = 0
        for s in d.stops:
            assert s.start >= t, f"{d.date} {s.title} 与前一项重叠"
            assert s.duration > 0
            t = s.start + s.duration
        for s in places_of(d):
            p = LIB.get_place(s.place_id)
            if p.typical_hours:
                o = int(p.typical_hours.open[:2]) * 60 + int(p.typical_hours.open[3:])
                c = int(p.typical_hours.close[:2]) * 60 + int(p.typical_hours.close[3:])
                assert o <= s.start and s.start + s.duration <= c, f"{p.name_zh} 超出开放时间"
        assert t <= 22 * 60


def test_travel_time_is_included_and_flagged_as_estimate():
    draft = schedule(make_request(days=4), "art", LIB)
    for d in draft.days:
        prev_end = None
        for s in d.stops:
            if prev_end is not None and s.kind == "place":
                assert s.start >= prev_end + s.travel_min
                assert s.travel_estimated is True      # v0.1 没有真实路线服务，一律是估算
                assert s.travel_mode
            prev_end = s.start + s.duration


def test_lunch_and_dinner_placeholders_exist_on_full_days():
    draft = schedule(make_request(days=4), "art", LIB)
    for d in draft.days[1:-1]:
        if is_trip_day(d):               # 一日游当天不排午 / 晚餐
            continue
        meals = [s for s in d.stops if s.kind == "meal"]
        assert len(meals) == 2
        assert 11 * 60 + 30 <= meals[0].start <= 14 * 60
        assert meals[1].start >= 18 * 60


# --------------------------------------------------------------------------- #
# 确定性、解释
# --------------------------------------------------------------------------- #
def test_schedule_is_deterministic():
    a = schedule(make_request(days=5), "fashion", LIB)
    b = schedule(make_request(days=5), "fashion", LIB)
    assert a.model_dump() == b.model_dump()


def test_each_day_explains_why():
    draft = schedule(make_request(days=4), "art", LIB)
    for d in draft.days:
        if places_of(d):
            assert d.reasons
            assert any(LIB.get_place(places_of(d)[0].place_id).name_zh in r for r in d.reasons)


def test_unknown_theme_is_a_clear_error():
    with pytest.raises(KeyError, match="没有这个主题"):
        schedule(make_request(), "no-such-theme", LIB)


def test_thin_days_get_short_filler_stops_but_never_exceed_the_cap():
    cfg = SchedulerConfig(filler_max=3, filler_gap=60)
    draft = schedule(make_request(days=5, style="relaxed"), "art", LIB, config=cfg)
    fillers = [s for _, s in all_place_stops(draft) if s.role == "filler"]
    assert fillers, "打开「顺手的小点」后，轻松节奏下每天主要点少，应该会补几个"
    assert not [s for _, s in all_place_stops(schedule(make_request(days=5, style="relaxed"), "art", LIB))
                if s.role == "filler"], "默认不补小点（P20：一天只有你要的那几个行程）"
    for s in fillers:
        assert s.duration <= SchedulerConfig().filler_max_minutes
    for d in draft.days:
        assert sum(s.duration for s in places_of(d)) <= CFG.active_cap("relaxed") or len(places_of(d)) == 1


@pytest.mark.parametrize("text", ["9区", "9th arrondissement", "9e arrondissement", "第9区", "9 arr."])
def test_hotel_area_in_several_spellings_resolves_to_the_same_anchor(text):
    ref = schedule(make_request(hotel_area="9区"), "art", LIB).hotel
    assert ref != (48.8566, 2.3522)               # 9 区有地点，不该退回市中心
    assert schedule(make_request(hotel_area=text), "art", LIB).hotel == ref


# --------------------------------------------------------------------------- #
# 路线质量（规格 M03 S10）：一天里不留长时间没安排的空档
# --------------------------------------------------------------------------- #
STYLES = ("relaxed", "balanced", "intense")


def idle_gaps(day) -> list[int]:
    """相邻两项之间、扣掉通勤后还空着的分钟数（午 / 晚餐算已安排的内容；接驳不参与）。"""
    out, prev = [], None
    for s in day.stops:
        if s.kind == "logistics":
            prev = None
            continue
        if prev is not None:
            out.append(max(0, s.start - (prev.start + prev.duration) - (s.travel_min if s.kind == "place" else 0)))
        prev = s
    return out


@pytest.fixture(scope="module")
def worst_gaps() -> list[int]:
    """所有主题 × 7 个起始星期 × 3 种节奏，每个有景点的天的「最长空档」。"""
    gaps = []
    for theme in THEMES:
        for style in STYLES:
            for off in range(7):
                draft = schedule(make_request(start=SAT + timedelta(days=off), days=5, style=style), theme, LIB)
                gaps += [max(idle_gaps(d), default=0) for d in draft.days if places_of(d)]
    return gaps


def test_no_day_has_an_idle_gap_over_two_hours(worst_gaps):
    assert len(worst_gaps) >= 800
    assert max(worst_gaps) <= 120, f"最长空档 {max(worst_gaps)} 分钟"


def test_few_days_have_an_idle_gap_over_ninety_minutes(worst_gaps):
    # P20：白天只排「上午 1、下午 1」、不补小点，午饭前后自然有空档：超过 90 分钟的天从 ≤12% 放宽到 ≤20%（现在 14%）
    share = sum(g > 90 for g in worst_gaps) / len(worst_gaps)
    assert share <= 0.20, f"{share:.0%} 的天有超过 90 分钟的空档"


def test_most_days_have_no_idle_gap_over_an_hour(worst_gaps):
    # Q3：每天都有午饭、紧凑的午饭只有 30 分钟（餐算「已安排」，餐越短，剩下的空档越显眼），所以从 40% 放宽到 60%；
    # 真正要紧的「超过 2 小时」「超过 90 分钟」两条没有放宽（基线 38% → 现在约 56%）
    # P20：一天按「上午 1、下午 1、晚上 1」排、不补小点，超过 1 小时的天约 80%；放宽到 85%，最长仍 ≤ 120 分钟
    share = sum(g > 60 for g in worst_gaps) / len(worst_gaps)
    assert share <= 0.85, f"{share:.0%} 的天有超过 1 小时的空档"


def test_fillers_are_light_never_a_must_book_online_place():
    """「顺手的小点」不该给游客添订票负担，也不该是 2 小时的大景点。"""
    from app.library import Ticketing
    seen = 0
    for theme in THEMES:
        for style in STYLES:
            draft = schedule(make_request(days=5, style=style), theme, LIB, config=SchedulerConfig(filler_max=3))
            for _, s in all_place_stops(draft):
                if s.role != "filler":
                    continue
                p = LIB.get_place(s.place_id)
                seen += 1
                assert p.ticketing != Ticketing.online_required, f"{theme}: {p.name_zh}"
                members = {m.place_id for m in LIB.get_theme(theme).members}
                assert p.duration_min <= (120 if p.id in members else 90), f"{theme}: {p.name_zh} {p.duration_min}"
    assert seen >= 20


# --------------------------------------------------------------------------- #
# 编辑用：只重排某一天、锁定开始时间（规格 M04）
# --------------------------------------------------------------------------- #
def _stops(res):
    return [s for s in res.plan.stops if s.kind == "place"]


def test_plan_day_places_what_the_user_asked_for_even_on_a_closed_day():
    """手动放进来的就照放：卢浮宫周二闭馆也排进去，由质检去提示，而不是在这里拒绝。"""
    from app.scheduler import plan_day
    res = plan_day(make_request(days=4), date(2026, 10, 6), ["louvre", "orsay"], LIB)
    assert {s.place_id for s in _stops(res)} == {"louvre", "orsay"}
    assert res.broken_locks == []


def test_plan_day_without_optimizing_keeps_the_given_order():
    from app.scheduler import plan_day
    ids = ["rodin", "louvre", "orsay"]
    res = plan_day(make_request(days=4), date(2026, 10, 6), ids, LIB, optimize=False)
    assert [s.place_id for s in _stops(res)] == ids
    starts = [s.start for s in _stops(res)]
    assert starts == sorted(starts)


def test_plan_day_keeps_a_fixed_start_time():
    from app.scheduler import plan_day
    res = plan_day(make_request(days=4), date(2026, 10, 6), ["louvre", "orsay"], LIB, fixed={"orsay": 14 * 60 + 30})
    orsay = next(s for s in _stops(res) if s.place_id == "orsay")
    assert orsay.start == 14 * 60 + 30 and res.broken_locks == []


def test_a_fixed_start_that_cannot_be_met_is_reported_not_silently_changed():
    from app.scheduler import plan_day
    res = plan_day(make_request(days=4), date(2026, 10, 6), ["louvre", "orsay"], LIB, optimize=False,
                   fixed={"orsay": 9 * 60 + 10})
    assert res.broken_locks == ["orsay"]
    assert {s.place_id for s in _stops(res)} == {"louvre", "orsay"}


def test_plan_day_keeps_arrival_and_departure_logistics():
    from app.scheduler import plan_day
    req = make_request(days=4, arrival=datetime(2026, 10, 3, 10, 30), departure=datetime(2026, 10, 6, 18, 0))
    first = plan_day(req, date(2026, 10, 3), ["rodin"], LIB)
    last = plan_day(req, date(2026, 10, 6), ["rodin"], LIB)
    assert first.plan.stops[0].kind == "logistics" and last.plan.stops[-1].kind == "logistics"


def test_plan_day_rejects_a_date_outside_the_trip():
    from app.scheduler import plan_day
    with pytest.raises(ValueError, match="日期范围"):
        plan_day(make_request(days=4), date(2027, 1, 1), ["rodin"], LIB)


def test_schedule_honors_a_pinned_start_time():
    base = schedule(make_request(days=4), "art", LIB)
    d = next(x for x in base.days if len(places_of(x)) >= 2)
    target = places_of(d)[-1]
    again = schedule(make_request(days=4), "art", LIB, pinned={target.place_id: (d.date, None, target.start)})
    day = next(x for x in again.days if x.date == d.date)
    assert next(s for s in places_of(day) if s.place_id == target.place_id).start == target.start


# --------------------------------------------------------------------------- #
# S11：顺手的小点也要和主题搭（评测发现摄影之旅里有 3 个百货商店）
# --------------------------------------------------------------------------- #
def test_core_interests_are_the_tags_most_of_a_themes_members_share():
    from app.scheduler.engine import core_interests

    assert core_interests(LIB, ["photography"]) == {"photography", "landmark"}
    assert core_interests(LIB, ["food"]) == {"food", "street", "cafe"}
    assert core_interests(LIB, ["fashion"]) == {"fashion", "shopping"}
    assert {"architecture", "street", "relax"}.isdisjoint(core_interests(LIB, ["photography"]))   # 到处都有的标签不算
    assert core_interests(LIB, ["photography", "food"]) == {"photography", "landmark", "food", "street", "cafe"}
    assert core_interests(LIB, []) == set()


def test_the_photography_trip_no_longer_borrows_department_stores_or_boutiques():
    for style in STYLES:
        for off in range(7):
            draft = schedule(make_request(start=SAT + timedelta(days=off), days=5, style=style), "photography", LIB)
            ids = {st.place_id for d in draft.days for st in places_of(d)}
            assert not ids & {"printemps", "la-samaritaine", "avenue-montaigne", "galeries-lafayette-shopping",
                              "le-bon-marche", "st-germain-boutiques", "marais-boutiques"}, (style, off, ids)


def test_borrowed_places_almost_always_share_a_core_interest_with_the_theme():
    """借来的点（不是主题成员）里，不搭的只占极少数，而且只在空档太大时才出现。"""
    from app.scheduler.engine import core_interests

    borrowed = incoherent = 0
    for theme in THEMES:
        members = {p.id for p, _ in LIB.theme_members(theme)}
        daytime = {p.id for p, _ in LIB.theme_members(theme)
                   if p.zone == Zone.core and p.best_time not in (BestTime.evening, BestTime.golden_hour)}
        if len(daytime) < 12:
            continue            # 白天可排的成员太少的主题（娱乐、摄影多在夜里 / 黄昏，亲子、美食点少）白天只能借，另说
        core = core_interests(LIB, [theme])
        for style in STYLES:
            for off in range(0, 7, 2):
                draft = schedule(make_request(start=SAT + timedelta(days=off), days=7, style=style), theme, LIB)
                for d in draft.days:                                  # 7 天：白天每天 2 个，主题的点不够才会借
                    for pid in (st.place_id for st in places_of(d) if not _night(st)):    # 晚上的夜景不算
                        if pid not in members:
                            borrowed += 1
                            incoherent += not (set(LIB.places[pid].interests) & core)
    assert borrowed > 0
    assert incoherent / borrowed <= 0.2, f"借来的 {borrowed} 个点里有 {incoherent} 个和主题不搭"


# --------------------------------------------------------------------------- #
# S12：摄影之旅真的围着光线排
# --------------------------------------------------------------------------- #
def test_photography_golden_hour_places_start_close_to_the_days_sunset():
    from app.scheduler.sun import sunset_minutes

    checked = 0
    for off in range(0, 240, 17):                                    # 全年各个季节
        start = SAT + timedelta(days=off)
        draft = schedule(make_request(start=start, days=4, style="balanced"), "photography", LIB)
        for d in draft.days:
            sunset = sunset_minutes(d.date)
            for st in places_of(d):
                if LIB.get_place(st.place_id).best_time.value != "golden_hour":
                    continue
                checked += 1
                lo = max(15 * 60, min(sunset - 150, 19 * 60 + 30))
                assert lo <= st.start <= sunset + 60, (d.date, st.title, st.start, sunset)
    assert checked >= 20


def test_other_themes_keep_the_old_fixed_golden_hour_rule():
    for theme in ("entertainment", "art", "food"):
        draft = schedule(make_request(days=4, style="intense"), theme, LIB)
        for d in draft.days:
            for st in places_of(d):
                if LIB.get_place(st.place_id).best_time.value == "golden_hour":
                    assert st.start >= 16 * 60 + 30, (theme, d.date, st.title, st.start)


def test_the_photography_day_window_opens_at_eight_but_only_for_that_theme():
    from app.scheduler.engine import _build_windows, _mixed_recipe, selected_themes, theme_windows

    req = make_request(start=SAT, days=3, style="balanced")
    for theme, expected in (("photography", 8 * 60), ("art", 9 * 60)):
        recipe = _mixed_recipe(LIB, selected_themes(req, theme))
        windows = theme_windows(_build_windows(req, SchedulerConfig(), CFG), recipe)
        starts = [w.start for w in windows if w.arrival is None]
        assert starts and all(s == expected for s in starts), (theme, starts)
        assert (windows[0].sunset is not None) == (theme == "photography")


def test_the_golden_hour_penalty_is_zero_when_on_time_and_grows_when_early():
    from app.scheduler.engine import GOLDEN_SOFT, Window, _best_time_penalty, golden_start

    p = LIB.get_place("bir-hakeim")
    w = Window(day=SAT, start=480, end=1290, sunset=19 * 60 + 28)
    ideal = golden_start(w.sunset, GOLDEN_SOFT)
    assert _best_time_penalty(p, ideal, w) == 0 and _best_time_penalty(p, ideal + 40, w) == 0
    assert _best_time_penalty(p, ideal - 30, w) == 15 and _best_time_penalty(p, ideal - 60, w) == 30
    plain = Window(day=SAT, start=480, end=1290)                      # 没有日落信息：不扣分
    assert _best_time_penalty(p, 14 * 60, plain) == 0


def test_a_four_hour_day_trip_like_versailles_can_be_scheduled_on_a_day_of_its_own():
    """以前排程器要求一日游至少玩 5 小时，4 小时的凡尔赛在任何行程里都排不进去（必去也不行）。"""
    draft = schedule(make_request(days=4, must_go_ids=["versailles"]), "art")
    trip_days = [d for d in draft.days if any(s.place_id == "versailles" for s in places_of(d))]
    assert len(trip_days) == 1
    assert [s.place_id for s in places_of(trip_days[0])] == ["versailles"]            # 独占一天
    assert not any(s.title.startswith("午餐") for s in trip_days[0].stops)
    assert all(places_of(d) for d in draft.days)                                      # 其他天照常有安排


@pytest.mark.parametrize("theme", ["art", "museums"])
def test_versailles_is_part_of_the_art_and_museum_themes_and_gets_a_day_of_its_own(theme):
    members = {p.id: m.weight for p, m in LIB.theme_members(theme)}
    assert members["versailles"] == 4
    draft = schedule(make_request(days=4), theme, LIB)
    trip_days = [d for d in draft.days if "versailles" in [s.place_id for s in places_of(d)]]
    assert len(trip_days) == 1 and [s.place_id for s in places_of(trip_days[0])] == ["versailles"]


@pytest.mark.parametrize("days", [2, 3])
def test_a_theme_day_trip_does_not_show_up_by_itself_in_a_short_trip(days):
    draft = schedule(make_request(days=days), "art", LIB)
    assert not any(is_trip_day(d) for d in draft.days)
    forced = schedule(make_request(days=days, must_go_ids=["versailles"]), "art", LIB)     # 必去不受限制
    assert sum(is_trip_day(d) for d in forced.days) == 1


EVENING_FROM = 16 * 60 + 30


def _night(s) -> bool:
    return LIB.places[s.place_id].best_time in (BestTime.evening, BestTime.golden_hour)


def _before_lunch(day) -> list:
    lunch = next(s for s in day.stops if is_lunch(s))
    return [s for s in day.stops if s.kind == "place" and s.start < lunch.start and not _night(s)]


def _afternoon(day) -> list:
    lunch = next(s for s in day.stops if is_lunch(s))
    return [s for s in day.stops if s.kind == "place" and s.start > lunch.start and not _night(s)]


def _evening(day) -> list:
    return [s for s in day.stops if s.kind == "place" and _night(s)]


def _middle_days(theme, style, **kw):
    draft = schedule(make_request(days=4, style=style, arrival=datetime(2026, 10, 3, 10, 0),
                                  departure=datetime(2026, 10, 6, 18, 0), **kw), theme, LIB)
    return [d for d in draft.days[1:-1] if not is_trip_day(d)]


@pytest.mark.parametrize("theme", ["art", "museums", "shopping", "food", "photography", "family"])
def test_balanced_middle_day_is_one_morning_one_afternoon_and_at_most_one_evening(theme):
    """P20：均衡 = 上午 1、下午 1、晚上 1（晚上可自由）。"""
    days = _middle_days(theme, "balanced")
    assert days
    for d in days:
        titles = [s.title for s in d.stops]
        assert len(_before_lunch(d)) == 1, (theme, d.date, titles)
        assert len(_afternoon(d)) == 1, (theme, d.date, titles)
        assert len(_evening(d)) <= 1, (theme, d.date, titles)
        for s in _evening(d):
            # 晚上的点排在一天最后
            assert s.start >= max(x.start for x in _before_lunch(d) + _afternoon(d)), (theme, d.date, titles)
            assert LIB.places[s.place_id].best_time in (BestTime.evening, BestTime.golden_hour), (theme, s.place_id)


@pytest.mark.parametrize("theme", ["art", "museums", "shopping", "food"])
def test_relaxed_has_no_evening_and_intense_may_have_a_second_afternoon_place(theme):
    for d in _middle_days(theme, "relaxed"):
        assert len(_before_lunch(d)) == 1 and len(_afternoon(d)) == 1 and not _evening(d), (theme, d.date)
    for d in _middle_days(theme, "intense"):
        assert len(_before_lunch(d)) == 1 and 1 <= len(_afternoon(d)) <= 2 and len(_evening(d)) <= 1, (theme, d.date)


def test_an_evening_with_nothing_suitable_is_left_free_instead_of_padded():
    nights = [p.id for p in LIB.places.values() if p.best_time in (BestTime.evening, BestTime.golden_hour)]
    days = _middle_days("art", "balanced", avoid_ids=nights)
    assert days and all(not _evening(d) and len(_before_lunch(d)) == 1 and len(_afternoon(d)) == 1 for d in days)


def test_the_evening_place_is_not_repeated_across_days_and_is_in_the_evening():
    draft = schedule(make_request(days=6, arrival=datetime(2026, 10, 3, 10, 0),
                                  departure=datetime(2026, 10, 8, 18, 0)), "art", LIB)
    ids = [s.place_id for d in draft.days for s in _evening(d)]
    assert len(ids) == len(set(ids)) and len(ids) >= 2
    assert all(s.start >= EVENING_FROM for d in draft.days for s in _evening(d))


@pytest.mark.parametrize("theme", ["art", "museums", "shopping", "food", "photography", "family"])
def test_only_themes_that_own_a_show_get_a_show_in_the_evening_and_every_such_day_has_dinner(theme):
    for style in ("balanced", "intense"):
        draft = schedule(make_request(days=5, style=style), theme, LIB)
        for d in draft.days[1:-1]:
            for s in _evening(d):
                assert LIB.places[s.place_id].best_time == BestTime.golden_hour, (theme, s.place_id)
            if _evening(d):
                assert any(s.kind == "meal" and s.title.startswith("晚") for s in d.stops), (theme, d.date)


def test_the_entertainment_theme_keeps_its_shows_in_the_evening_with_dinner_first():
    draft = schedule(make_request(days=5), "entertainment", LIB)
    shows = [(d, s) for d in draft.days for s in _evening(d) if LIB.places[s.place_id].best_time == BestTime.evening]
    assert shows
    for d, s in shows:
        dinner = next(x for x in d.stops if x.kind == "meal" and x.title.startswith("晚"))
        assert dinner.start + dinner.duration <= s.start


# --------------------------------------------------------------------------- #
# 美食第 2 步（规格 09 十七之三）：本身就是一顿饭的点
# --------------------------------------------------------------------------- #
def _food_days(**kw):
    out = []
    for off in range(7):
        draft = schedule(make_request(start=SAT + timedelta(days=off), days=5, **kw), "food", LIB)
        out += draft.days
    return out


def test_only_places_whose_notes_say_so_are_tagged_as_a_meal():
    tagged = {p.id: p.meal for p in LIB.places.values() if p.meal}
    lunch = {"cafe-procope", "le-train-bleu", "la-coupole", "bouillon-chartier", "brasserie-bofinger", "brasserie-lipp",
             "fouquets", "cafe-de-l-homme"}
    tea = {"angelina", "laduree-champs", "cafe-de-flore", "les-deux-magots", "cafe-de-la-paix"}
    assert tagged == {**dict.fromkeys(lunch, "lunch"), **dict.fromkeys(tea, "tea")}


def test_a_lunch_place_is_the_days_lunch_and_not_one_of_its_two_daytime_places():
    seen = 0
    for d in _food_days():
        served = [s for s in d.stops if s.meal == "lunch"]
        if not served:
            continue
        seen += 1
        assert len(served) == 1
        assert 11 * 60 + 30 <= served[0].start <= 14 * 60
        assert not any(s.kind == "meal" and s.title.startswith("午") for s in d.stops), d.date    # 不再另排午餐占位
        assert len(_before_lunch(d)) <= 1 and len(_afternoon(d)) <= 1
    assert seen >= 3


def test_the_burnt_down_market_is_never_scheduled_and_those_days_still_have_lunch():
    """红孩子市场 2026-10-12 被大火烧毁（长期闭馆），不排；有它的主题日仍然有午饭。"""
    assert LIB.places["enfants-rouges"].long_term_closed
    for d in _food_days():
        assert not any(s.place_id == "enfants-rouges" for s in d.stops), d.date
        if places_of(d) and not is_trip_day(d) and not any(s.kind == "logistics" for s in d.stops):
            assert any(is_lunch(s) for s in d.stops), d.date


def test_a_tea_place_only_serves_as_tea_in_the_afternoon():
    seen = 0
    for style in STYLES:
        for d in _food_days(style=style):
            for s in d.stops:
                if s.place_id and LIB.places[s.place_id].meal == "tea":
                    seen += 1
                    assert s.start >= 14 * 60 + 30 and s.meal == "tea", (d.date, s.place_id, s.start)
    assert seen >= 3
