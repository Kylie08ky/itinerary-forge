"""P17 深度游学路线（规格 09）：数据闸门、路线排程、导览检查、接口。"""

# ruff: noqa: E501  （导览样例句是长中文数据，不折行）
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import create_app
from app.generator import generate_from_theme
from app.library import load_library
from app.llm import LLMError
from app.route_generator import deviation, guide_ok, template_guide
from app.routes import Chapter, Route, RouteError, Stop, get_route, load_routes, summarize
from app.scheduler.engine import schedule
from app.scheduler.route_mode import schedule_route
from app.schemas import TravelRequest
from app.validator import validate_itinerary
from app.versions import PlanStore

ART = "art-royal-to-contemporary"
HERITAGE = "heritage-royal-to-iron"
GASTRONOMY = "gastronomy-market-to-cafe"
MARKETS = "christmas-markets-notre-dame-to-alsace"
ROUTE_IDS = [ART, "fashion-couture-to-street", "history-thought-medieval-to-existentialism", HERITAGE, GASTRONOMY, MARKETS]
SRC = {"title": "x", "url": "https://example.org/x", "checked_at": "2026-10-25"}


def route() -> Route:
    return get_route("paris", ART)


def is_curated(guide: str | None) -> bool:
    """导览是策展原文拼的（章节故事）而不是模型写的：第一段就是某一章的故事，或「接着…往下走」。"""
    first = (guide or "").split("\n")[0]
    return first in {c.story for c in route().chapters} or first.startswith("接着「")


def request(n: int, *, start: date = date(2026, 12, 15), style: str = "balanced", **extra) -> TravelRequest:
    end = start + timedelta(days=n - 1)
    return TravelRequest(destination="Paris", start_date=start, end_date=end, budget_level="medium",
                         travel_style=style, interests=["art"], route=ART,
                         arrival_time=datetime.combine(start, datetime.min.time()).replace(hour=9),
                         departure_time=datetime.combine(end, datetime.min.time()).replace(hour=20), **extra)


class FakeLLM:
    def __init__(self, reply: dict | str | None = None, error: bool = False) -> None:
        self.reply, self.error, self.calls = reply, error, 0

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        self.calls += 1
        if self.error:
            raise LLMError("模型没启动")
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 数据闸门
# --------------------------------------------------------------------------- #
def test_the_art_route_loads_and_every_fact_has_a_source():
    r = route()
    lib = load_library("paris")
    assert [c.id for c in r.chapters] == ["collections", "impressionism", "modern-contemporary"]
    assert r.days.suggested == 3 and len(r.place_ids()) == 12 and len(set(r.place_ids())) == 12
    assert all(pid in lib.places for pid in r.place_ids())
    for c in r.chapters:
        assert c.sources and all(s.url.startswith("https://") for s in c.sources), c.id
        for s in c.stops:
            if s.priority == "core":
                assert s.look_for and s.why_here and s.sources, s.place_id
    assert 3 <= len(r.reading) <= 5 and all(x.source.url.startswith("https://") for x in r.reading)


def test_a_route_never_contains_a_place_that_is_closed_for_good():
    lib = load_library("paris")
    assert lib.places["pompidou"].long_term_closed and "pompidou" not in route().place_ids()
    assert not any(lib.places[p].long_term_closed for p in route().place_ids())
    assert any("蓬皮杜" in n for n in route().notes) and any("卡蒙多" in n for n in route().notes)


def test_the_new_places_carry_their_verification_records_and_unverified_facts_are_marked():
    lib = load_library("paris")
    moreau, jam = lib.places["musee-gustave-moreau"], lib.places["musee-jacquemart-andre"]
    assert moreau.price_eur == 8.0 and moreau.price_source_url.startswith("https://musee-moreau.fr")
    assert moreau.closed_weekdays == [1] and "12-25" in moreau.closed_dates and moreau.coords_verified
    assert moreau.name_local and moreau.name_local_check
    assert jam.price_eur is None and "官网" in (jam.price_note or "") and not jam.hours_verified   # 没核对的不写成事实
    assert jam.url_status is not None and jam.name_local_check is not None


def test_bad_route_data_is_rejected():
    with pytest.raises(ValidationError, match="core"):
        Stop(place_id="louvre")                                                    # core 站没有看点 / 理由 / 来源
    ok = Stop(place_id="louvre", look_for=["a"], why_here="b", sources=[SRC])
    later = Stop(place_id="orsay", look_for=["a"], why_here="b", sources=[SRC], after="nope")
    with pytest.raises(ValidationError, match="after"):
        Chapter(id="c", title="t", story="x" * 30, sources=[SRC], stops=[ok, later])
    ch = Chapter(id="c", title="t", story="x" * 30, sources=[SRC], stops=[ok])
    days = {"min": 1, "suggested": 2, "max": 3}
    base = dict(id="r", name_zh="r", tagline="t", thesis="t", base_theme="art", days=days)
    with pytest.raises(ValidationError, match="两次"):
        Route(**base, chapters=[ch, ch.model_copy(update={"id": "d"})])
    with pytest.raises(ValidationError, match="min"):
        Route(**{**base, "days": {"min": 3, "suggested": 2, "max": 3}}, chapters=[ch])
    with pytest.raises(RouteError, match="没有这条路线"):
        get_route("paris", "nope")
    assert load_routes("nowhere") == {}                                           # 没有路线文件 = 没有路线


def test_a_request_can_only_name_a_route_that_exists():
    with pytest.raises(ValidationError, match="没有这条路线"):
        TravelRequest.model_validate({**request(3).model_dump(mode="json"), "route": "nope"})


# --------------------------------------------------------------------------- #
# 路线排程：硬约束照旧，章的先后不乱
# --------------------------------------------------------------------------- #
STARTS = [date(2026, 12, 14) + timedelta(days=i) for i in range(7)]                # 一周里每个开始日都试


@pytest.mark.parametrize("style", ["relaxed", "balanced", "intense"])
@pytest.mark.parametrize("n", [1, 2, 3, 4, 6])
def test_every_route_plan_has_no_hard_errors_lunch_every_day_and_no_closed_place(style, n):
    lib = load_library("paris")
    for start in STARTS:
        req = request(n, start=start, style=style)
        trip = generate_from_theme(req, "art", client=None)
        assert not trip.report.errors, (style, n, start, trip.report.errors)
        for d in trip.itinerary.days:
            places = [i for i in d.items if i.place_id in lib.places and i.type.value != "meal"]
            assert places, (style, n, start, d.date)                                 # 没有空天
            assert all(lib.places[i.place_id].closed_reason(d.date) is None for i in places)
            assert any(i.title.startswith("午餐") for i in d.items), (style, n, start, d.date)


def test_chapters_stay_in_order_when_nothing_is_closed():
    # 2026-12-17（周四）起三天：周四 / 周五 / 周六，路线里没有任何站在这几天闭馆
    req = request(3, start=date(2026, 12, 17))
    draft = schedule_route(req, route())
    order = [cid for d in draft.days for cid in d.chapters]
    assert order == sorted(order, key=[c.id for c in route().chapters].index)
    assert not any("闭馆" in n for n in draft.notes)


def test_a_stop_closed_that_day_moves_to_another_day_and_says_so():
    draft = schedule_route(request(3, start=date(2026, 12, 15)), route())             # 周二：卢浮宫、橘园、莫罗闭馆
    day1 = [s.place_id for s in draft.days[0].stops if s.kind == "place"]
    assert "louvre" not in day1 and "orangerie" not in day1
    assert any("闭馆" in n and "卢浮宫" in n for n in draft.notes)


def test_too_few_days_drops_optional_stops_first_and_says_which():
    draft = schedule_route(request(1, start=date(2026, 12, 17), style="relaxed"), route())
    placed = {s.place_id for d in draft.days for s in d.stops if s.place_id}
    assert 2 <= len(placed) <= 3                                                    # 一天放不下全部站
    skipped = set(draft.route_skipped)
    lib = load_library("paris")
    assert "tuileries-garden" in skipped and "fondation-louis-vuitton" in skipped        # optional 先砍
    assert all(any(lib.places[p].name_zh in n for n in draft.notes) for p in skipped)


def test_any_number_of_days_works_the_advice_is_only_a_note():
    few = schedule_route(request(1, start=date(2026, 12, 17)), route())
    assert any("推荐至少 2 天" in n for n in few.notes)
    many = schedule_route(request(7, start=date(2026, 12, 17)), route())
    assert any("大约够 5 天" in n and "2 天" in n for n in many.notes)
    labels = [d.label for d in many.days]
    assert labels[:5] == [None] * 5 and all(x and "自由日" in x for x in labels[5:])
    route_days = [d for d in many.days if d.chapters]
    assert len(route_days) == 5 and all(not d.chapters for d in many.days[5:])
    free_places = {s.place_id for d in many.days[5:] for s in d.stops if s.place_id}
    assert free_places and not free_places & set(route().place_ids())                 # 补充的天不重复路线上的站
    assert not any(n for n in schedule_route(request(5, start=date(2026, 12, 17)), route()).notes
                   if "大约够" in n or "推荐至少" in n)


def test_you_can_pick_which_stops_to_visit_and_unticked_ones_never_come_back_as_filler():
    pick = ["louvre", "orsay", "rodin"]
    draft = schedule_route(request(4, start=date(2026, 12, 17), route_stops=pick), route())
    placed = {s.place_id for d in draft.days for s in d.stops if s.place_id}
    assert set(pick) <= placed
    route_placed = placed & set(route().place_ids())
    assert route_placed == set(pick)                                                   # 没勾的站（含补充的天）一个都没出现
    assert not draft.route_skipped
    with pytest.raises(ValidationError, match="route_stops"):
        request(3, route_stops=["not-on-the-route"])
    with pytest.raises(ValidationError, match="route_stops"):
        request(3, route_stops=[])
    with pytest.raises(ValidationError, match="route_stops"):
        TravelRequest.model_validate({**request(3).model_dump(mode="json"), "route": None, "route_stops": ["louvre"]})


def test_the_same_request_always_gives_the_same_plan():
    a = schedule_route(request(3), route()).model_dump()
    b = schedule_route(request(3), route()).model_dump()
    assert a == b


# --------------------------------------------------------------------------- #
# 行程里的路线信息、导览
# --------------------------------------------------------------------------- #
def test_the_plan_carries_chapters_look_for_guides_and_reading_without_a_model():
    trip = generate_from_theme(request(3, start=date(2026, 12, 17)), "art", client=None)
    it = trip.itinerary
    gen = it.generation
    assert gen.theme_id == "art" and gen.theme_name == route().name_zh and gen.narration == "template"
    assert gen.route.id == ART and len(gen.route.reading) == len(route().reading)
    assert gen.route.baseline and set(gen.route.baseline) == {d.date.isoformat() for d in it.days}
    for d in it.days:
        assert d.chapter and d.chapter.startswith("第 ") and d.guide and is_curated(d.guide)
        assert "今天的顺序" not in d.guide and "→" not in d.guide          # 顺序就是下面的时间轴，不在导览里重复
        assert all(i.look_for for i in d.items if i.place_id in route().place_ids()
                   and next(s for _c, s in route().stops() if s.place_id == i.place_id).look_for)
    assert not validate_itinerary(it, request(3, start=date(2026, 12, 17))).errors


def test_guides_from_the_model_are_checked_and_bad_ones_fall_back_to_the_curated_text():
    req = request(3, start=date(2026, 12, 17))
    first = generate_from_theme(req, "art", client=None).itinerary
    dates = [d.date.isoformat() for d in first.days]
    good = ("今天从卢浮宫开始，先看「《蒙娜丽莎》（达芬奇，德农馆）」，理解王室收藏怎样变成公共博物馆；再去雅克马尔-安德烈，"
            "对照一对藏家夫妇靠自己的眼光攒出来的收藏，国家和私人，两种眼光就都看清了。")
    bad_link = "今天先去卢浮宫，再去别处，详情见 https://example.com，路上顺便看看，把王室收藏和私人藏家两条线索接起来，慢慢走。" * 2
    bad_name = ("今天从卢浮宫开始，先看「蒙德里安的红黄蓝」，再去雅克马尔-安德烈，对照一对藏家夫妇靠自己的眼光攒出来的收藏，"
                "国家和私人，两种眼光就都看清了，路上不用赶。")
    llm = FakeLLM({"days": [{"date": dates[0], "guide": good}, {"date": dates[1], "guide": bad_link},
                            {"date": dates[2], "guide": bad_name}]})
    trip = generate_from_theme(req, "art", client=llm)
    days = trip.itinerary.days
    assert llm.calls == 1
    assert days[0].guide == good                                        # 合格的用模型的
    assert "https://" not in (days[1].guide or "") and is_curated(days[1].guide)    # 不合格的回退策展原文
    assert "蒙德里安" not in (days[2].guide or "")
    assert trip.itinerary.generation.narration == "llm" and "没通过检查" in trip.itinerary.generation.narration_note


@pytest.mark.parametrize("llm", [FakeLLM(error=True), FakeLLM("not json"), FakeLLM({"days": []})])
def test_without_a_usable_model_the_curated_guides_are_used_and_the_reason_is_written(llm):
    trip = generate_from_theme(request(2, start=date(2026, 12, 17)), "art", client=llm)
    gen = trip.itinerary.generation
    assert gen.narration == "template" and gen.narration_note
    assert all(is_curated(d.guide) for d in trip.itinerary.days)


def test_guide_check_rejects_prices_times_unknown_quotes_and_numbers():
    req = request(1, start=date(2026, 12, 17))
    trip = generate_from_theme(req, "art", client=None)
    from app.route_generator import _day_material

    chapters = {d.date.isoformat(): d.chapters for d in schedule_route(req, route()).days}
    m = _day_material(route(), load_library("paris"), trip.itinerary, chapters)[0]
    base = "今天从卢浮宫开始，先看《蒙娜丽莎》（达芬奇，德农馆），再看王室收藏怎样变成公共博物馆，一路走到橘园，把这条线索接起来。"
    assert guide_ok(base + "最后在橘园站进睡莲展厅，感受莫奈把睡莲画成环绕观众的空间，慢慢走完。", m)
    assert not guide_ok(base + "门票 17 欧。" * 3, m)
    assert not guide_ok(base + "下午 3:30 到。" * 3, m)
    assert not guide_ok(base + "路上可以顺便看看「某某名画」。" * 3, m)
    assert not guide_ok(base + "1999 年发生了一件事。" * 3, m)
    assert not guide_ok("太短了", m)
    assert template_guide({"chapters": [], "stops": []}) == ""


def test_deviation_is_detected_when_a_route_stop_is_moved_removed_or_reordered():
    it = generate_from_theme(request(3, start=date(2026, 12, 17)), "art", client=None).itinerary
    assert not deviation(it)
    moved = it.model_copy(deep=True)
    moved.days[0].items, moved.days[1].items = moved.days[1].items, moved.days[0].items
    assert not deviation(moved) or moved.days[0].items != it.days[0].items          # 整天对调：日期下的站变了
    cut = it.model_copy(deep=True)
    pid = next(i.place_id for i in cut.days[0].items if i.place_id in route().place_ids())
    cut.days[0].items = [i for i in cut.days[0].items if i.place_id != pid]
    assert deviation(cut)
    plain = generate_from_theme(TravelRequest.model_validate({**request(2).model_dump(mode="json"), "route": None}),
                                "art", client=None).itinerary
    assert not deviation(plain)                                                     # 普通行程没有路线，谈不上偏离


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


def payload(n: int = 3, **extra) -> dict:
    return {**request(n, start=date(2026, 12, 17)).model_dump(mode="json"), "theme": "art", **extra}


def test_routes_endpoints_list_and_describe_the_route(client):
    cards = client.get("/routes").json()
    assert {c["id"] for c in cards} == set(ROUTE_IDS)
    card = next(c for c in cards if c["id"] == ART)
    assert card["chapter_count"] == 3 and card["stop_count"] == 12 and card["days"]["suggested"] == 3
    assert card["examples"] and len(card["chapter_titles"]) == 3 and "chapters" not in card
    detail = client.get(f"/routes/{ART}").json()
    assert len(detail["chapters"]) == 3 and detail["reading"] and detail["notes"]
    first = detail["chapters"][0]["stops"][0]
    assert first["name"] == "卢浮宫 · Musée du Louvre" and first["why_here"]
    assert client.get("/routes/nope").status_code == 404
    assert client.get("/routes", params={"city": "heidelberg"}).json() == []         # 没有路线的城市：空列表
    assert summarize(route(), load_library("paris"), detail=True)["chapters"][0]["id"] == "collections"


def test_creating_a_plan_with_a_route_gives_a_route_plan_and_a_status_that_notices_edits(client):
    r = client.post("/plans", json=payload())
    assert r.status_code == 201, r.text
    pid = r.json()["plan_id"]
    detail = client.get(f"/plans/{pid}").json()
    assert detail["route_status"] == {"id": ART, "name_zh": route().name_zh, "deviated": False}
    assert detail["itinerary"]["days"][0]["chapter"] and detail["itinerary"]["generation"]["route"]["reading"]
    # 普通行程没有 route_status
    plain = client.post("/plans", json={**payload(), "route": None}).json()["plan_id"]
    assert "route_status" not in client.get(f"/plans/{plain}").json()


def test_a_route_plan_goes_through_tasks_and_any_day_count_or_stop_choice_works(client):
    assert client.post("/tasks", json=payload()).status_code == 202
    assert client.post("/tasks", json=payload(6)).status_code == 202                  # 天数不限制，只是推荐
    r = client.post("/plans", json=payload(5, route_stops=["louvre", "orsay"]))
    assert r.status_code == 201, r.text
    placed = {i["place_id"] for d in r.json()["itinerary"]["days"] for i in d["items"] if i.get("place_id")}
    assert {"louvre", "orsay"} <= placed and not placed & {"picasso", "rodin", "orangerie"}
    assert client.post("/plans", json={**payload(), "route": "nope"}).status_code == 422
    assert client.post("/plans", json=payload(3, route_stops=["nope"])).status_code == 422


def test_exports_carry_the_chapter_the_guide_the_look_for_lines_and_the_reading_list(client):
    pid = client.post("/plans", json=payload()).json()["plan_id"]
    md = client.get(f"/plans/{pid}/itinerary.md").text
    html = client.get(f"/plans/{pid}/itinerary.html").text
    assert "第 " in md and "章 · " in md and "为什么在这里（" in md and "看什么（" in md and "延伸阅读 / 观影" in md
    assert "The Judgment of Paris" in md
    assert 'class="chapter"' in html and 'class="guide"' in html and "这一站看什么：" in html and "延伸阅读 / 观影" in html
    plain = client.post("/plans", json={**payload(), "route": None}).json()["plan_id"]
    assert "延伸阅读" not in client.get(f"/plans/{plain}/itinerary.md").text           # 普通行程不变


def test_saving_an_edited_route_plan_keeps_chapters_guides_and_look_for_and_flags_the_deviation(client):
    pid = client.post("/plans", json=payload()).json()["plan_id"]
    it = client.get(f"/plans/{pid}").json()["itinerary"]
    day0 = it["days"][0]
    victim = next(i for i in day0["items"] if i.get("place_id") in route().place_ids())
    day0["items"] = [i for i in day0["items"] if i is not victim]
    saved = client.post(f"/plans/{pid}/versions", json={"itinerary": it, "name": "删了一站", "note": ""})
    assert saved.status_code == 201, saved.text
    after = client.get(f"/plans/{pid}").json()
    assert after["route_status"]["deviated"] is True
    days = after["itinerary"]["days"]
    assert all(d.get("chapter") and d.get("guide") for d in days)                       # 章节和导览没被保存冲掉
    assert any(i.get("look_for") for d in days for i in d["items"])
    assert after["itinerary"]["generation"]["route"]["reading"]


# --------------------------------------------------------------------------- #
# 三条路线都过同一道闸门；批量核对脚本的规则
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rid", ROUTE_IDS)
def test_every_route_passes_the_same_data_gate(rid):
    r = get_route("paris", rid)
    lib = load_library("paris")
    assert r.days.min <= r.days.suggested <= r.days.max and 1 <= len(r.chapters) <= 4
    assert r.thesis and r.notes and (r.reading or r.track == "folk")      # 民俗路线没有延伸阅读
    for c in r.chapters:
        assert len(c.story) >= 80 and c.sources and all(s.url.startswith("https://") for s in c.sources), c.id
        assert sum(s.priority == "core" for s in c.stops) >= 2, c.id
        for s in c.stops:
            assert s.place_id in lib.places and not lib.places[s.place_id].long_term_closed
            if s.priority == "core":
                assert s.look_for and s.why_here and s.sources, s.place_id
    assert all(x.source.url.startswith("https://") and 1000 < x.year <= 2026 for x in r.reading)
    assert len(set(r.place_ids())) == len(r.place_ids())


@pytest.mark.parametrize("rid", ROUTE_IDS[1:])
@pytest.mark.parametrize("n", [1, 3, 6])
def test_the_other_routes_also_plan_cleanly_for_any_day_count(rid, n):
    for start in (date(2026, 12, 14), date(2026, 12, 17), date(2026, 12, 20)):
        req = TravelRequest.model_validate(
            {**request(n, start=start).model_dump(mode="json"), "route": rid})
        base = get_route("paris", rid).base_theme
        trip = generate_from_theme(req, base, client=None)
        assert not trip.report.errors, (rid, n, start, trip.report.errors)
        lib = load_library("paris")
        for d in trip.itinerary.days:
            places = [i for i in d.items if i.place_id in lib.places and i.type.value != "meal"]
            trip_day = any(lib.places[i.place_id].zone.value == "day_trip" for i in places)   # 凡尔赛这类一日游不排午饭
            lunch = any(i.title.startswith("午餐") or "这一站就是午餐" in i.notes for i in d.items)      # 或者午饭就在一个点里
            assert places and (trip_day or lunch), (rid, n, start, d.date)
            assert not trip_day or len(places) == 1                                       # 一日游独占一天
            assert all(lib.places[i.place_id].closed_reason(d.date) is None for i in places)


def test_the_batch_checker_catches_numbers_that_are_not_in_the_cited_sources():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from build_route import ground, numbers

    assert numbers("1656–1659 年建，今天有 5,000 多件，第 1 章") == {"1656", "1659", "5000"}
    problems: list[str] = []
    explain: list[str] = []
    ground("章", ["卢浮宫 1546 年成为王宫，1793 年开放"], ["... in 1546 the king ... opened 1793 ..."], problems,
           ["Louvre"], explain)
    assert not problems and explain == ["章：1546 ← Louvre", "章：1793 ← Louvre"]
    ground("章", ["花神咖啡馆 1887 年开业"], ["the café appeared in the 1880s"], problems)
    assert len(problems) == 1 and "1887" in problems[0]                              # 凭印象写的年份会被拦下


def test_routes_are_grouped_into_interest_tracks(client):
    from app.routes import TRACKS

    assert set(TRACKS) == {"history", "folk", "food", "other"}
    by_id = {c["id"]: c for c in client.get("/routes").json()}
    assert by_id[ART]["track"] == "other" and by_id[ART]["track_zh"] == "其他"
    assert by_id["history-thought-medieval-to-existentialism"]["track"] == "history"
    assert by_id["history-thought-medieval-to-existentialism"]["track_zh"] == "历史"
    assert all(get_route("paris", r).track in TRACKS for r in ROUTE_IDS)


def test_each_route_stop_carries_its_own_why_here_and_the_guide_no_longer_repeats_it():
    it = generate_from_theme(request(3, start=date(2026, 12, 17)), "art", client=None).itinerary
    stops = {s.place_id: s for _c, s in route().stops()}
    on_route = [i for d in it.days for i in d.items if i.place_id in stops]
    assert on_route and all(i.why_here == stops[i.place_id].why_here for i in on_route)
    others = [i for d in it.days for i in d.items if i.place_id not in stops]
    assert all(not i.why_here for i in others)                       # 午餐、自由日补的点等没有
    for d in it.days:
        assert not any(i.why_here in (d.guide or "") for i in on_route if i.why_here)     # 不在导览里再写一遍


def test_an_old_route_plan_is_refreshed_when_read_without_touching_model_written_guides():
    from app.route_generator import refresh_route_display

    it = generate_from_theme(request(3, start=date(2026, 12, 17)), "art", client=None).itinerary
    lib = load_library("paris")
    old_days = []
    for d in it.days:
        names = [lib.places[i.place_id].name_zh for i in d.items if i.place_id in route().place_ids()]
        stops = {s.place_id: s for _c, s in route().stops()}
        extra = [f"{lib.places[i.place_id].name_zh}：{stops[i.place_id].why_here}" for i in d.items
                 if i.place_id in stops]
        old_guide = "\n".join([d.guide, f"今天的顺序：{' → '.join(names)}。", *extra])
        old_days.append(d.model_copy(update={
            "guide": old_guide, "items": [i.model_copy(update={"why_here": ""}) for i in d.items]}))
    old = it.model_copy(update={"days": old_days})
    fresh = refresh_route_display(old, "paris")
    assert [d.guide for d in fresh.days] == [d.guide for d in it.days]
    assert [i.why_here for d in fresh.days for i in d.items] == [i.why_here for d in it.days for i in d.items]
    assert refresh_route_display(it, "paris") is it                                  # 新的不用改，原样返回
    llm_written = it.model_copy(update={"days": [d.model_copy(update={"guide": "模型写的一段导览，没有顺序句。"}) for d in it.days]})
    assert refresh_route_display(llm_written, "paris") is llm_written


def test_the_heritage_route_walks_from_royal_and_sacred_buildings_to_iron_and_glass():
    r = get_route("paris", HERITAGE)
    assert r.track == "history" and r.base_theme == "museums"
    assert [c.id for c in r.chapters] == ["faith-and-crown", "sun-king-and-empire", "iron-and-glass"]
    assert {"sainte-chapelle", "notre-dame", "versailles", "invalides", "arc-de-triomphe", "palais-garnier",
            "eiffel-tower", "passage-des-panoramas"} <= set(r.place_ids())
    assert [s.place_id for c in r.chapters for s in c.stops if s.priority == "optional"] == [
        "champs-elysees", "pont-alexandre-iii"]
    # 凡尔赛周一闭馆：周一开始的 3 天行程里它不在周一
    req = TravelRequest.model_validate({**request(3, start=date(2026, 12, 14)).model_dump(mode="json"), "route": HERITAGE})
    trip = generate_from_theme(req, "museums", client=None)
    monday = trip.itinerary.days[0]
    assert all(i.place_id != "versailles" for i in monday.items)
    days_with = [d for d in trip.itinerary.days if any(i.place_id == "versailles" for i in d.items)]
    assert len(days_with) == 1 and [i.place_id for i in days_with[0].items if i.place_id] == ["versailles"]


@pytest.mark.parametrize(("n", "at_least"), [(4, 10), (5, 13)])
def test_a_route_with_a_day_trip_uses_the_free_days_before_dropping_stops(n, at_least):
    """凡尔赛占一整天，其余的站要在剩下的天里排满：排不下时先让前几天多排一站，再开始砍站。"""
    req = TravelRequest.model_validate({**request(n, start=date(2026, 12, 17)).model_dump(mode="json"),
                                        "route": HERITAGE})
    draft = schedule_route(req, get_route("paris", HERITAGE))
    placed = [s.place_id for d in draft.days for s in d.stops if s.place_id]
    assert len(placed) >= at_least and "versailles" in placed
    assert [s.place_id for d in draft.days for s in d.stops if s.place_id == "versailles"] == ["versailles"]


def test_the_gastronomy_route_follows_the_rhythm_of_a_day_of_eating():
    r = get_route("paris", GASTRONOMY)
    assert r.track == "food" and r.base_theme == "food"
    assert [c.id for c in r.chapters] == ["morning-markets", "old-house-lunch", "cafes-and-tea"]
    ids = set(r.place_ids())
    assert {"rue-mouffetard", "rue-montorgueil", "cafe-procope", "le-train-bleu", "la-coupole", "angelina",
            "laduree-champs", "cafe-de-la-paix", "cafe-de-flore", "les-deux-magots", "cafe-des-deux-moulins"} <= ids
    assert "enfants-rouges" not in ids                         # 2026-10-12 被大火烧毁，不排
    lib = load_library("paris")
    assert all(not lib.places[i].long_term_closed for i in ids)
    assert [s.place_id for c in r.chapters for s in c.stops if s.priority == "optional"] == [
        "le-train-bleu", "cafe-de-la-paix", "cafe-des-deux-moulins"]
    assert [s.place_id for s in r.chapters[2].stops][:2] == ["angelina", "cafe-de-flore"]


def test_a_gastronomy_plan_uses_its_lunch_places_instead_of_the_generic_lunch_row():
    req = TravelRequest.model_validate({**request(4, start=date(2026, 12, 17)).model_dump(mode="json"),
                                        "route": GASTRONOMY, "arrival_time": "2026-12-17T10:00:00",
                                        "departure_time": "2026-12-20T18:00:00"})
    draft = schedule_route(req, get_route("paris", GASTRONOMY))
    served = [(d.date, s.place_id) for d in draft.days for s in d.stops if s.meal == "lunch"]
    assert len(served) >= 2
    for d in draft.days:
        if any(s.meal == "lunch" for s in d.stops):
            assert not any(s.kind == "meal" and s.title.startswith("午") for s in d.stops), d.date
    notes = [i.notes for day in generate_from_theme(req, "food", client=None).itinerary.days for i in day.items]
    assert any("这一站就是午餐" in n for n in notes)


def test_the_six_new_food_places_carry_their_checks_and_say_so_when_hours_are_not_official():
    lib = load_library("paris")
    new = ["cafe-procope", "le-train-bleu", "la-coupole", "cafe-des-deux-moulins", "les-deux-magots", "cafe-de-la-paix"]
    for pid in new:
        p = lib.places[pid]
        assert p.category.value == "food" and p.official_url.startswith("https://") and p.url_status
        assert p.name_local and p.name_local_check and p.coords_verified and p.address
    assert not lib.places["cafe-de-la-paix"].hours_verified           # 官网没写时间
    assert "OpenStreetMap" in lib.places["les-deux-magots"].note       # 开放时间来自社区数据，备注里写明
    assert "不是" not in lib.places["cafe-procope"].note or "最老" not in lib.places["cafe-procope"].note
    assert lib.places["enfants-rouges"].long_term_closed and "大火" in lib.places["enfants-rouges"].note


def test_the_gastronomy_route_picks_one_place_per_chapter_per_day_never_three_cafes_in_a_day():
    r = get_route("paris", GASTRONOMY)
    assert r.layout == "pick-one" and r.days.max == 3
    chapter_of = {s.place_id: c.id for c, s in r.stops()}
    for style, days in (("balanced", 3), ("relaxed", 3), ("intense", 3), ("balanced", 2), ("balanced", 4)):
        req = TravelRequest.model_validate({**request(days, start=date(2026, 12, 17)).model_dump(mode="json"),
                                            "route": GASTRONOMY, "travel_style": style,
                                            "arrival_time": "2026-12-17T10:00:00",
                                            "departure_time": f"2026-12-{16 + days}T18:00:00"})
        draft = schedule_route(req, r)
        for d in draft.days:
            chapters = [chapter_of[s.place_id] for s in d.stops if s.place_id in chapter_of]
            assert len(chapters) == len(set(chapters)), (style, days, d.date, chapters)         # 每章最多一家
            assert chapters.count("cafes-and-tea") <= 1                                          # 咖啡馆一天最多一家
        placed = [s.place_id for d in draft.days for s in d.stops if s.place_id in chapter_of]
        assert len(placed) == len(set(placed))                                                   # 不重复
        if style == "balanced" and days == 3:
            assert len(placed) >= 7                                                              # 三天把各类逛一遍
    # 一天顶部不写章标题也不讲章的介绍：每一站自己带说明；标题就是这天去的几家
    trip = generate_from_theme(TravelRequest.model_validate(
        {**request(3, start=date(2026, 12, 17)).model_dump(mode="json"), "route": GASTRONOMY}), "food", client=None)
    assert all(not d.chapter and not d.guide for d in trip.itinerary.days)
    assert all("穆浮塔街" in d.theme or "普罗可布" in d.theme or d.items for d in trip.itinerary.days)
    assert trip.itinerary.generation.narration_note is None


def test_the_two_christmas_markets_are_dated_events_with_sources_and_an_honest_note():
    lib = load_library("paris")
    nd, als = lib.places["paris-notre-dame-christmas-market"], lib.places["paris-alsace-christmas-market"]
    assert (nd.season.start, nd.season.end) == (date(2026, 11, 26), date(2026, 12, 27))
    assert (als.season.start, als.season.end) == (date(2026, 12, 2), date(2026, 12, 18))
    for p in (nd, als):
        assert p.season.confirmed_by == "listing" and "官方网站" in p.season.note and p.season.source_url.startswith("https://")
        assert p.open_access and not p.hours_verified and p.name_local and p.name_local_check
    assert nd.closed_reason(date(2026, 12, 17)) is None and nd.closed_reason(date(2027, 3, 10)) is not None
    assert als.closed_reason(date(2026, 12, 17)) is None and als.closed_reason(date(2026, 12, 20)) is not None


def test_the_markets_route_only_schedules_a_market_inside_its_dates_and_never_leaves_a_day_empty():
    r = get_route("paris", MARKETS)
    assert r.track == "folk" and [s.place_id for s in r.chapters[0].stops] == [
        "paris-notre-dame-christmas-market", "paris-alsace-christmas-market"]
    for start, days, expect in ((date(2026, 12, 16), 3, {"paris-notre-dame-christmas-market", "paris-alsace-christmas-market"}),
                                (date(2026, 12, 21), 3, {"paris-notre-dame-christmas-market"}),
                                (date(2027, 3, 10), 2, set())):
        req = TravelRequest.model_validate({
            **request(days, start=start).model_dump(mode="json"), "route": MARKETS,
            "arrival_time": f"{start.isoformat()}T10:00:00",
            "departure_time": f"{(start + timedelta(days=days - 1)).isoformat()}T18:00:00"})
        draft = schedule_route(req, r)
        placed = {s.place_id for d in draft.days for s in d.stops}
        assert expect == placed & set(r.place_ids()), (start, placed)
        assert all(any(s.kind == "place" for s in d.stops) or d.date == start for d in draft.days)


def test_an_ordinary_shopping_trip_meets_the_market_in_the_evening_only_inside_its_dates():
    def evening_markets(start):
        found = set()
        for off in range(3):
            req = request(3, start=start + timedelta(days=off))
            draft = schedule(req, "shopping", load_library("paris"))
            found |= {s.place_id for d in draft.days for s in d.stops if s.place_id and "christmas-market" in s.place_id}
        return found
    assert "paris-notre-dame-christmas-market" in evening_markets(date(2026, 12, 8))
    assert evening_markets(date(2027, 3, 8)) == set()


def test_a_route_made_only_of_dated_events_is_marked_limited_with_its_window_and_the_others_are_not():
    from app.routes import summarize

    lib = load_library("paris")
    assert summarize(get_route("paris", MARKETS), lib)["season"] == {"start": "2026-11-26", "end": "2026-12-27"}
    for rid in ROUTE_IDS:
        if rid != MARKETS:
            assert summarize(get_route("paris", rid), lib)["season"] is None, rid
