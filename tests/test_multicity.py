"""多城市「一份行程」（规格 11 第八节）：合并 / 拆分不丢东西，按一份行程生成。"""

from __future__ import annotations

from datetime import date

import pytest
from city_fixtures import write_small_city

from app import multicity
from app.cities import reload_cities
from app.trips import TripSpec

TODAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def second_city(tmp_path, monkeypatch):
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path / "generated"))
    reload_cities()
    write_small_city("lyon", "里昂", "Lyon", (45.7640, 4.8357), "FR", "Europe/Paris", "EUR", TODAY)
    write_small_city("brussels", "布鲁塞尔", "Brussels", (50.8503, 4.3517), "BE", "Europe/Brussels", "EUR", TODAY)
    yield
    reload_cities()


def spec(**over) -> TripSpec:
    base = {"legs": [{"destination": "Paris", "start_date": "2026-12-12", "end_date": "2026-12-14"},
                     {"destination": "里昂", "start_date": "2026-12-14", "end_date": "2026-12-16"}],
            "outbound": {"mode": "flight", "depart_at": "2026-12-12T06:00", "arrive_at": "2026-12-12T09:30"},
            "inbound": {"mode": "flight", "depart_at": "2026-12-16T18:00"},
            "common": {"budget_level": "medium", "travel_style": "balanced", "interests": ["均衡"]}}
    return TripSpec.model_validate({**base, **over})


def built(**over):
    parent, _ = multicity.build_request(spec(**over))
    it = multicity.generate(parent, client=None)
    return parent, it


def test_the_parent_request_holds_the_whole_trip():
    parent, transfers = multicity.build_request(spec())
    assert parent.is_multi and [leg.request.city_id for leg in parent.legs] == ["paris", "lyon"]
    assert (parent.start_date, parent.end_date) == (date(2026, 12, 12), date(2026, 12, 16))
    assert parent.transport.outbound.arrive_at.isoformat() == "2026-12-12T09:30:00"
    assert parent.transport.inbound.depart_at.isoformat() == "2026-12-16T18:00:00"
    assert transfers[0][1] is True                                       # 没填交通：估算
    assert multicity.title_of(parent) == "巴黎 → 里昂"


def test_one_itinerary_with_every_day_and_item_tagged_with_its_city():
    parent, it = built()
    assert [d.date.day for d in it.days] == [12, 13, 14, 15, 16]
    assert it.trip.destination == "巴黎 → 里昂" and it.legs is not None and len(it.legs) == 2
    by_date = {d.date.isoformat(): d for d in it.days}
    assert by_date["2026-12-12"].city_id == "paris" and by_date["2026-12-16"].city_id == "lyon"
    shared = by_date["2026-12-14"]                                       # 换城市那天：一天，两座城市各一半
    assert set(shared.theme_by_city) == {"paris", "lyon"}
    cities = [i.city_id for i in shared.items]
    assert cities == sorted(cities, key=lambda c: c != "paris")           # 巴黎的在前、里昂的在后
    times = [i.time for i in shared.items]
    assert times == sorted(times)
    assert all(i.city_id for d in it.days for i in d.items)


def test_split_gives_back_exactly_what_each_city_generated():
    from app.generator import generate_from_theme

    parent, it = built()
    for (req, leg_it), leg in zip(multicity.split(parent, it), parent.legs, strict=True):
        original = generate_from_theme(leg.request, leg.theme, client=None).itinerary
        assert req is leg.request
        assert [d.model_dump(mode="json") for d in leg_it.days] == [d.model_dump(mode="json") for d in original.days]
        assert leg_it.generation == original.generation
        assert leg_it.legs is None


def test_merge_after_split_is_stable():
    parent, it = built()
    again = multicity.merge(parent, multicity.split(parent, it))
    assert again.model_dump(mode="json") == it.model_dump(mode="json")


def test_existing_single_city_plans_combine_into_the_same_one_plan():
    """旧的「一座城市一份」行程并成一份：和一次生成出来的一模一样（迁移不丢东西）。"""
    parent, it = built()
    again_parent, again = multicity.combine(multicity.split(parent, it))
    assert again.model_dump(mode="json") == it.model_dump(mode="json")
    assert again_parent.model_dump(mode="json") == parent.model_dump(mode="json")


def test_combine_refuses_plans_that_do_not_join_up():
    parent, it = built()
    paris, lyon = multicity.split(parent, it)
    gap = lyon[0].model_copy(update={"start_date": date(2026, 12, 15)})
    with pytest.raises(multicity.TripError, match="对不上"):
        multicity.combine([paris, (gap, lyon[1])])
    with pytest.raises(multicity.TripError, match="至少"):
        multicity.combine([paris])


def test_positions_map_a_city_view_back_to_the_merged_day():
    parent, it = built()
    shared = next(d for d in it.days if d.theme_by_city)
    paris, lyon = (multicity.positions(shared, c) for c in ("paris", "lyon"))
    assert sorted(paris + lyon) == list(range(len(shared.items)))
    assert all(shared.items[k].city_id == "lyon" for k in lyon)


def test_a_one_day_middle_city_shares_both_of_its_ends():
    legs = [{"destination": "Paris", "start_date": "2026-12-12", "end_date": "2026-12-14"},
            {"destination": "里昂", "start_date": "2026-12-14", "end_date": "2026-12-14"},
            {"destination": "布鲁塞尔", "start_date": "2026-12-14", "end_date": "2026-12-16"}]
    parent, it = built(legs=legs)
    shared = next(d for d in it.days if d.date.isoformat() == "2026-12-14")
    assert set(shared.theme_by_city) == {"paris", "lyon", "bruxelles"}
    again = multicity.merge(parent, multicity.split(parent, it))
    assert again.model_dump(mode="json") == it.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# 接口：一份行程
# --------------------------------------------------------------------------- #
import time  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.llm import LLMError  # noqa: E402
from app.versions import PlanStore  # noqa: E402


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(tmp_path):
    app = create_app(store=PlanStore(tmp_path / "plans.db"), client_factory=lambda: DeadLLM())
    return TestClient(app)


def payload(**over) -> dict:
    return {**spec().model_dump(mode="json"), **over}


def make_plan(client, **over) -> str:
    r = client.post("/tasks", json=payload(**over))
    assert r.status_code == 202, r.text
    for _ in range(300):
        t = client.get(f"/tasks/{r.json()['task_id']}").json()
        if t["status"] in ("succeeded", "failed"):
            assert t["status"] == "succeeded", t["error"]
            return t["plan_id"]
        time.sleep(0.1)
    raise AssertionError("生成超时")


def test_one_task_makes_one_plan_with_every_city(client):
    pid = make_plan(client)
    d = client.get(f"/plans/{pid}").json()
    assert d["itinerary"]["trip"]["destination"] == "巴黎 → 里昂"
    assert [c["name_zh"] for c in d["cities"]] == ["巴黎", "里昂"]
    assert d["itinerary"]["days"][0]["date"] == "2026-12-12"
    assert d["version"] == 1 and "trip" not in d
    cards = client.get("/plan-cards").json()
    assert [c["title"].split(" · ")[0] for c in cards] == ["巴黎 → 里昂"]            # 一张卡片
    assert [c["name_zh"] for c in cards[0]["cities"]] == ["巴黎", "里昂"]
    assert cards[0]["days"] == 5


def test_a_bad_trip_is_refused_before_anything_starts(client):
    legs = payload()["legs"]
    assert client.post("/tasks", json=payload(legs=legs[:1])).status_code == 422
    r = client.post("/tasks", json=payload(legs=[legs[0], {**legs[1], "destination": "Nowhere"}]))
    assert r.status_code == 422


def test_a_bad_theme_names_the_city_and_nothing_is_generated(client):
    legs = payload()["legs"]
    r = client.post("/tasks", json=payload(legs=[{**legs[0], "theme": "no-such-theme"}, legs[1]]))
    assert r.status_code == 422 and "第 1 段" in r.json()["detail"]["message"]
    assert client.get("/plan-cards").json() == []


def test_each_city_keeps_its_own_theme_must_go_and_stays(client):
    from app.library import load_library

    paris_theme = next(iter(load_library("paris").themes))
    lyon_place = sorted(load_library("lyon").places)[2]

    def stay(name, start, end):
        return {"name": name, "check_in": start, "check_out": end, "area": "市中心"}

    legs = payload()["legs"]
    legs[0] = {**legs[0], "theme": paris_theme, "options": {"stays": [stay("巴黎的旅馆", "2026-12-12", "2026-12-14")]}}
    legs[1] = {**legs[1], "options": {"must_go_ids": [lyon_place], "bogus": 1,
                                      "stays": [stay("里昂的旅馆", "2026-12-14", "2026-12-16")]}}
    d = client.get(f"/plans/{make_plan(client, legs=legs)}").json()
    assert [s["name"] for s in d["stays"]] == ["巴黎的旅馆", "里昂的旅馆"]
    assert lyon_place in {i.get("place_id") for day in d["itinerary"]["days"] for i in day["items"]}
    assert paris_theme in d["itinerary"]["generation"]["themes"]
    bad = [{**legs[0], "options": {"stays": [stay("太早", "2026-12-01", "2026-12-02")]}}, legs[1]]
    r = client.post("/tasks", json=payload(legs=bad))
    assert r.status_code == 422 and "第 1 段" in r.json()["detail"]["message"]


def test_a_one_day_stop_in_the_middle_still_makes_one_plan_with_three_cities(client):
    legs = [{"destination": "Paris", "start_date": "2026-12-12", "end_date": "2026-12-14"},
            {"destination": "里昂", "start_date": "2026-12-14", "end_date": "2026-12-14"},
            {"destination": "布鲁塞尔", "start_date": "2026-12-14", "end_date": "2026-12-16"}]
    d = client.get(f"/plans/{make_plan(client, legs=legs)}").json()
    assert [c["name_zh"] for c in d["cities"]] == ["巴黎", "里昂", "布鲁塞尔"]
    assert [x["date"] for x in d["itinerary"]["days"]] == [f"2026-12-{n}" for n in (12, 13, 14, 15, 16)]
    assert len(d["transfers"]) == 2


# --------------------------------------------------------------------------- #
# 读取：路线、预订、预算、天气、随行清单、导出、分享都按一份行程出
# --------------------------------------------------------------------------- #
def two_cities(client):
    return make_plan(client)


def test_route_covers_places_from_both_cities(client):
    pid = two_cities(client)
    route = client.get(f"/plans/{pid}/route").json()
    assert len(route["days"]) == 5
    from app.library import load_library

    stops = {s["place_id"] for d in route["days"] for s in d["stops"]}
    assert stops & set(load_library("paris").places) and stops & set(load_library("lyon").places)


def test_reservations_todos_and_stay_advice_read_across_cities(client):
    pid = two_cities(client)
    assert client.get(f"/plans/{pid}/reservations").status_code == 200
    todos = client.get(f"/plans/{pid}/todos")
    assert todos.status_code == 200 and todos.json()["suggestions"]
    assert client.get(f"/plans/{pid}/stay-recommendation").status_code == 200


def test_budget_adds_up_the_cities_that_have_prices_and_names_the_rest(client):
    pid = two_cities(client)
    b = client.get(f"/plans/{pid}/budget").json()
    assert b["days"] == 5
    assert any("里昂" in n and "没有计入" in n and "餐饮按巴黎" in n for n in b["notes"])
    assert any("飞机" in n for n in b["notes"])                           # 城市之间的大交通不在估算里
    meals = next(c for c in b["categories"] if c["key"] == "meals")
    per_day = b["meal"]["value"] * len(b["travelers"])
    assert meals["eur_total"] == pytest.approx(per_day * 5, abs=0.01)         # 5 天，换城市那天只算一次


def test_weather_is_asked_per_city():
    from app.weather import WeatherDay

    asked: list[tuple[str, list[str]]] = []

    class Fake:
        def get(self, destination, dates, today):
            asked.append((destination, [d.isoformat() for d in dates]))
            return [WeatherDay(date=d, source="history", code=1, label="晴", icon="sun") for d in dates]

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(store=PlanStore(Path(tmp) / "p.db"), client_factory=lambda: DeadLLM(), weather=Fake())
        c = TestClient(app)
        pid = make_plan(c)
        w = c.get(f"/plans/{pid}/weather").json()
    assert w["available"] and [d["date"] for d in w["days"]] == [f"2026-12-{n}" for n in (12, 13, 14, 15, 16)]
    assert len({a[0] for a in asked}) == 2 and [len(a[1]) for a in asked] == [3, 3]


def test_packing_is_one_list_for_the_whole_trip(client):
    pid = two_cities(client)
    got = client.post(f"/plans/{pid}/packing/generate").json()
    assert got["total"] > 0 and got["added"] > 0
    assert client.get(f"/plans/{pid}/packing").json()["total"] == got["total"]


def test_every_export_works_and_the_calendar_uses_each_citys_time_zone(client):
    legs = [{"destination": "Paris", "start_date": "2026-12-12", "end_date": "2026-12-14"},
            {"destination": "布鲁塞尔", "start_date": "2026-12-14", "end_date": "2026-12-16"}]
    pid = make_plan(client, legs=legs)
    md = client.get(f"/plans/{pid}/itinerary.md")
    assert md.status_code == 200 and "巴黎 → 布鲁塞尔" in md.text
    html = client.get(f"/plans/{pid}/itinerary.html")
    assert html.status_code == 200 and "巴黎 → 布鲁塞尔" in html.text
    kml = client.get(f"/plans/{pid}/itinerary.kml")
    assert kml.status_code == 200 and "<Placemark>" in kml.text
    ics = client.get(f"/plans/{pid}/itinerary.ics").text
    assert "DTSTART;TZID=Europe/Paris:20261212" in ics
    assert "DTSTART;TZID=Europe/Brussels:20261216" in ics
    assert "TZID:Europe/Brussels" in ics and "TZID:Europe/Paris" in ics


def test_a_shared_link_shows_every_city(client):
    pid = two_cities(client)
    token = client.post(f"/plans/{pid}/share").json()["token"]
    got = client.get(f"/share/{token}").json()
    assert got["itinerary"]["trip"]["destination"] == "巴黎 → 里昂"
    assert len(got["route"]["days"]) == 5


def test_a_one_sentence_revision_is_refused_clearly_for_now(client):
    pid = two_cities(client)
    r = client.post(f"/plans/{pid}/revisions", json={"feedback": "第二天轻松一点"})
    assert r.status_code == 409 and "一句话修改" in r.json()["detail"]


def test_duplicate_and_delete_a_multi_city_plan(client):
    pid = two_cities(client)
    dup = client.post(f"/plans/{pid}/duplicate").json()
    assert dup["itinerary"]["trip"]["destination"] == "巴黎 → 里昂"
    assert client.delete(f"/plans/{pid}").status_code == 204



# --------------------------------------------------------------------------- #
# 编辑（批次 ②）：每个操作只动被改的那座城市，换城市那天各取各的
# --------------------------------------------------------------------------- #
def plan(client, pid):
    return client.get(f"/plans/{pid}").json()


def edit(client, pid, itinerary, op):
    return client.post(f"/plans/{pid}/edit", json={"itinerary": itinerary, "edit": op})


def items_of(it, city):
    return [(d["date"], i) for d in it["days"] for i in d["items"] if i.get("city_id") == city and i.get("place_id")]


def snapshot(it, city):
    return [(d, i["place_id"], i["time"]) for d, i in items_of(it, city)]


def test_locking_a_place_changes_only_that_place(client):
    pid = two_cities(client)
    it = plan(client, pid)["itinerary"]
    day, item = items_of(it, "lyon")[0]
    res = edit(client, pid, it, {"op": "lock", "place_id": item["place_id"], "locked": True})
    assert res.status_code == 200, res.text
    out = res.json()["itinerary"]
    locked = [i["place_id"] for _d, i in items_of(out, "lyon") if i["locked"]]
    assert locked == [item["place_id"]]
    assert snapshot(out, "paris") == snapshot(it, "paris")
    assert out["trip"]["destination"] == "巴黎 → 里昂" and len(out["legs"]) == 2


def test_removing_a_place_on_the_transfer_day_leaves_the_other_citys_half_alone(client):
    pid = two_cities(client)
    it = plan(client, pid)["itinerary"]
    shared = next(d for d in it["days"] if d.get("theme_by_city"))
    victim = next(i for i in shared["items"] if i["city_id"] == "lyon" and i.get("place_id"))
    res = edit(client, pid, it, {"op": "remove", "place_id": victim["place_id"]})
    assert res.status_code == 200, res.text
    out = res.json()["itinerary"]
    day = next(d for d in out["days"] if d["date"] == shared["date"])
    assert victim["place_id"] not in {i.get("place_id") for i in day["items"]}
    assert snapshot(out, "paris") == snapshot(it, "paris")
    assert [i["title"] for i in day["items"] if i["city_id"] == "paris"] == [
        i["title"] for i in shared["items"] if i["city_id"] == "paris"]       # 巴黎那半天原样
    assert set(day["theme_by_city"]) == {"paris", "lyon"}


def test_a_place_cannot_leave_its_own_city(client):
    pid = two_cities(client)
    it = plan(client, pid)["itinerary"]
    _d, lyon_item = items_of(it, "lyon")[-1]
    paris_only = next(d for d in it["days"] if d["city_id"] == "paris" and not d.get("theme_by_city"))
    r = edit(client, pid, it, {"op": "move", "place_id": lyon_item["place_id"], "to_date": paris_only["date"]})
    assert r.status_code == 422 and "里昂" in r.json()["detail"]
    _d, paris_item = items_of(it, "paris")[0]
    r = edit(client, pid, it, {"op": "swap", "place_id": lyon_item["place_id"], "new_place_id": paris_item["place_id"]})
    assert r.status_code == 422 and "里昂" in r.json()["detail"]


def test_swap_alternatives_come_from_that_places_own_city(client):
    from app.library import load_library

    pid = two_cities(client)
    it = plan(client, pid)["itinerary"]
    _d, lyon_item = items_of(it, "lyon")[0]
    alts = client.post(f"/plans/{pid}/alternatives", json={"itinerary": it, "place_id": lyon_item["place_id"]}).json()
    assert alts and {a["place_id"] for a in alts} <= set(load_library("lyon").places)
    res = edit(client, pid, it, {"op": "swap", "place_id": lyon_item["place_id"], "new_place_id": alts[0]["place_id"]})
    assert res.status_code == 200, res.text
    placed = {i["place_id"] for _d, i in items_of(res.json()["itinerary"], "lyon")}
    assert alts[0]["place_id"] in placed and lyon_item["place_id"] not in placed


def test_reordering_the_transfer_day_reorders_each_citys_half(client):
    pid = two_cities(client)
    it = plan(client, pid)["itinerary"]
    shared = next(d for d in it["days"] if d.get("theme_by_city"))
    ids = [i["place_id"] for i in shared["items"] if i.get("place_id")]
    paris = [p for p in ids if p in {i["place_id"] for _d, i in items_of(it, "paris")}]
    lyon = [p for p in ids if p in {i["place_id"] for _d, i in items_of(it, "lyon")}]
    if len(paris) < 2 and len(lyon) < 2:
        pytest.skip("换城市那天每座城市都不到两个点，没法重排")
    order = [*reversed(paris), *lyon]
    res = edit(client, pid, it, {"op": "reorder", "date": shared["date"], "place_ids": order})
    assert res.status_code == 200, res.text
    day = next(d for d in res.json()["itinerary"]["days"] if d["date"] == shared["date"])
    assert [i["place_id"] for i in day["items"] if i.get("place_id") in paris] == list(reversed(paris))
    assert [i["place_id"] for i in day["items"] if i.get("place_id") in lyon] == lyon


def test_reschedule_keeps_locked_places_in_every_city(client):
    pid = two_cities(client)
    it = plan(client, pid)["itinerary"]
    pins = [items_of(it, c)[0][1]["place_id"] for c in ("paris", "lyon")]
    for pin in pins:
        it = edit(client, pid, it, {"op": "lock", "place_id": pin, "locked": True}).json()["itinerary"]
    res = edit(client, pid, it, {"op": "reschedule"})
    assert res.status_code == 200, res.text
    out = res.json()["itinerary"]
    for pin in pins:
        assert pin in {i["place_id"] for c in ("paris", "lyon") for _d, i in items_of(out, c)}


def test_an_edited_plan_saves_as_a_new_version_and_rolls_back(client):
    pid = two_cities(client)
    it = plan(client, pid)["itinerary"]
    _d, item = items_of(it, "lyon")[0]
    edited = edit(client, pid, it, {"op": "remove", "place_id": item["place_id"]}).json()["itinerary"]
    saved = client.post(f"/plans/{pid}/versions", json={"itinerary": edited, "name": "删一个"})
    assert saved.status_code == 201, saved.text
    assert saved.json()["version"] == 2
    got = plan(client, pid)
    assert item["place_id"] not in {i["place_id"] for _d, i in items_of(got["itinerary"], "lyon")}
    assert [c["name_zh"] for c in got["cities"]] == ["巴黎", "里昂"]
    back = client.post(f"/plans/{pid}/versions/1/restore")
    assert back.status_code == 201 and back.json()["version"] == 3
    assert item["place_id"] in {i["place_id"] for _d, i in items_of(plan(client, pid)["itinerary"], "lyon")}


def test_shifting_moves_every_city_the_stays_and_the_transfer_together(client):
    stays = [{"name": "巴黎旅馆", "area": "市中心", "check_in": "2026-12-12", "check_out": "2026-12-14"},
             {"name": "里昂旅馆", "area": "市中心", "check_in": "2026-12-14", "check_out": "2026-12-16"}]
    pid = two_cities(client)
    assert client.put(f"/plans/{pid}/stays", json={"stays": stays}).status_code == 201
    before = plan(client, pid)
    r = client.post(f"/plans/{pid}/shift", json={"start_date": "2026-12-19"})
    assert r.status_code == 201, r.text
    after = plan(client, pid)
    assert [d["date"] for d in after["itinerary"]["days"]] == [f"2026-12-{n}" for n in (19, 20, 21, 22, 23)]
    assert [(c["start_date"], c["end_date"]) for c in after["cities"]] == [
        ("2026-12-19", "2026-12-21"), ("2026-12-21", "2026-12-23")]
    assert [s["check_in"] for s in after["stays"]] == ["2026-12-19", "2026-12-21"]
    assert after["transfers"][0]["depart_at"][:10] == "2026-12-21"
    assert after["version"] == before["version"] + 1
    assert snapshot(after["itinerary"], "lyon") == [(f"2026-12-{int(d[-2:]) + 7}", p, t)
                                                      for d, p, t in snapshot(before["itinerary"], "lyon")]


def test_stays_go_to_the_city_their_check_in_falls_in(client):
    pid = two_cities(client)
    stays = [{"name": "巴黎旅馆", "area": "市中心", "check_in": "2026-12-12", "check_out": "2026-12-14"},
             {"name": "里昂旅馆", "area": "市中心", "check_in": "2026-12-14", "check_out": "2026-12-16"}]
    r = client.put(f"/plans/{pid}/stays", json={"stays": stays, "reschedule": True})
    assert r.status_code == 201, r.text
    assert [s["name"] for s in plan(client, pid)["stays"]] == ["巴黎旅馆", "里昂旅馆"]
    bad = [{"name": "太晚", "area": "市中心", "check_in": "2026-12-20", "check_out": "2026-12-21"}]
    r = client.put(f"/plans/{pid}/stays", json={"stays": bad})
    assert r.status_code == 422


def test_the_transfer_between_cities_can_be_changed(client):
    pid = two_cities(client)
    before = plan(client, pid)
    assert before["transfers"][0]["estimated"] is True
    body = {"mode": "high_speed_rail", "depart_at": "2026-12-14T11:30", "arrive_at": "2026-12-14T15:00",
            "number": "G123"}
    r = client.put(f"/plans/{pid}/transfers/0", json=body)
    assert r.status_code == 201, r.text
    after = plan(client, pid)
    t = after["transfers"][0]
    assert (t["number"], t["estimated"], t["depart_at"], t["arrive_at"]) == ("G123", False, "2026-12-14T11:30",
                                                                           "2026-12-14T15:00")
    assert after["version"] == before["version"] + 1
    wrong_day = {**body, "depart_at": "2026-12-13T11:30"}
    assert client.put(f"/plans/{pid}/transfers/0", json=wrong_day).status_code == 422
    backwards = {**body, "depart_at": "2026-12-14T16:00"}
    assert client.put(f"/plans/{pid}/transfers/0", json=backwards).status_code == 422
    assert client.put(f"/plans/{pid}/transfers/5", json=body).status_code == 422


# --------------------------------------------------------------------------- #
# 导出封面（规格 11 M22）：每座城市一张路线图；距离不跨城市
# --------------------------------------------------------------------------- #
def test_route_distance_and_links_never_cross_from_one_city_to_the_other():
    from app.geo import build_route, google_urls_for_day

    parent, it = built()
    morning = it.days[0].items[1].model_copy(update={"time": "09:30"})        # 换城市那天上午在巴黎看一个点
    shared_day = it.days[2]
    shared_day = shared_day.model_copy(update={"items": [morning, *shared_day.items]})
    it = it.model_copy(update={"days": [*it.days[:2], shared_day, *it.days[3:]]})
    route = build_route(it)
    shared = next(d for d in route.days if d.date == "2026-12-14")            # 换城市那天：巴黎和里昂各一半
    assert {s.city_id for s in shared.stops} == {"paris", "lyon"}
    assert shared.distance_km < 60                                           # 巴黎到里昂 390 公里，不能算进当天
    assert route.total_km < 200
    for d in route.days:                                                    # 链接按城市分段，每段只含一座城市的点
        by_city: dict[str | None, list] = {}
        for st in d.stops:
            by_city.setdefault(st.city_id, []).append(st)
        assert d.google_urls == [u for seg in by_city.values() for u in google_urls_for_day(seg)]


def test_the_cover_has_one_route_map_per_city_and_a_single_city_cover_is_unchanged():
    from app.renderer import render_html

    parent, it = built()
    html = render_html(it, parent, None)
    assert html.count('class="routemap"') == 2
    assert "巴黎 · 第 1–3 天" in html and "里昂 · 第 3–5 天" in html
    # 单城市：一张图，没有城市标题
    from app.generator import generate_from_theme

    leg = parent.legs[0]
    single = generate_from_theme(leg.request, leg.theme, client=None).itinerary
    one = render_html(single, leg.request, None)
    assert one.count('class="routemap"') == 1 and "· 第 1–" not in one


# --------------------------------------------------------------------------- #
# 导出里的图片 / 简介和文件名（规格 11 M23、M24；M08 S2b）
# --------------------------------------------------------------------------- #
def _fake_media(pid: str, author: str):
    from app.media import PlaceMedia

    return PlaceMedia.model_validate({
        "image": {"file": f"{pid}.jpg", "title": f"File:{pid}.jpg",
                  "source_url": f"https://commons.wikimedia.org/wiki/File:{pid}.jpg", "author": author,
                  "license": "CC BY-SA 4.0", "width": 640, "height": 360, "checked_at": "2026-10-21"},
        "intro": {"text": f"{pid}的简介。", "source_url": "https://zh.wikipedia.org/wiki/X",
                  "checked_at": "2026-10-21"}})


def test_the_export_shows_each_cities_own_photo_intro_and_credit(monkeypatch):
    from app import renderer
    from app.renderer import render_html

    parent, it = built()
    media = {"paris": {"louvre": _fake_media("louvre", "作者甲")},
             "lyon": {"lyon-sight-0": _fake_media("lyon-sight-0", "作者乙")}}
    monkeypatch.setattr(renderer, "load_media", lambda city: media.get(city, {}))
    html = render_html(it, parent, None)
    assert 'src="/media/places/louvre.jpg"' in html and 'src="/media/places/lyon-sight-0.jpg"' in html
    assert "louvre的简介。" in html and "lyon-sight-0的简介。" in html
    assert "作者甲" in html and "作者乙" in html and "CC BY-SA 4.0" in html and "中文维基百科" in html
    assert html.count('class="thumb"') == 2                                  # 没有核对过图片的地点不配图


def test_pdf_and_long_image_embed_the_photos_into_the_file(monkeypatch, tmp_path):
    from app import media, renderer
    from app.renderer import render_html

    photo = tmp_path / "louvre.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    monkeypatch.setattr(media, "photo_path", lambda f: photo)
    monkeypatch.setattr(renderer, "photo_path", lambda f: photo)
    monkeypatch.setattr(renderer, "load_media",
                        lambda city: {"louvre": _fake_media("louvre", "甲")} if city == "paris" else {})
    parent, it = built()
    assert "data:image/jpeg;base64," in render_html(it, parent, None, inline_images=True)
    assert "data:image" not in render_html(it, parent, None)


def test_export_file_names_list_every_destination_in_order(client, monkeypatch):
    from urllib.parse import unquote

    from app import api as api_module

    def name_of(resp) -> str:
        return unquote(resp.headers["content-disposition"].split("''")[1].rstrip('"'))

    pid = make_plan(client)
    ics = name_of(client.get(f"/plans/{pid}/itinerary.ics"))
    assert ics.startswith("巴黎-里昂-2026") and ics.endswith("-v1.ics") and ics.count("巴黎") == 1
    assert name_of(client.get(f"/plans/{pid}/itinerary.kml")).startswith("巴黎-里昂-")

    def fake_pdf(src, dst, **kw):
        dst.write_bytes(b"%PDF-1.4 fake")
        return dst

    monkeypatch.setattr(api_module, "html_to_pdf", fake_pdf)
    pdf = client.get(f"/plans/{pid}/itinerary.pdf")
    assert pdf.status_code == 200
    assert name_of(pdf).startswith("巴黎-里昂-") and name_of(pdf).endswith("-v1.pdf")
