"""住宿：模型、每天锚点、平移顺延、住哪个区、保存接口（规格 M06 S5 / S5b）。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import create_app
from app.generator import generate_from_theme
from app.geocode import GeoHit
from app.library import load_library
from app.llm import LLMError
from app.scheduler import plan_day, schedule
from app.scheduler.engine import _hotel_point
from app.schemas import Itinerary, Stay, TravelRequest
from app.shifting import shift_plan
from app.stay import recommend_areas
from app.versions import PlanStore

LIB = load_library("paris")


def stay(name="A", ci="2026-10-03", co="2026-10-06", **kw) -> dict:
    return {"name": name, "check_in": ci, "check_out": co, "area": kw.pop("area", "1区"), **kw}


def make_request(stays=None, **over) -> TravelRequest:
    body = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["art"], "arrival_time": "2026-10-03T10:30:00",
            "departure_time": "2026-10-06T18:00:00", "stays": stays or []}
    body.update(over)
    return TravelRequest.model_validate(body)


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
def test_a_stay_needs_a_name_ordered_dates_and_somewhere_to_locate_it():
    Stay.model_validate(stay())
    with pytest.raises(ValidationError, match="退房"):
        Stay.model_validate(stay(ci="2026-10-05", co="2026-10-05"))
    with pytest.raises(ValidationError):
        Stay.model_validate(stay(name="  "))
    with pytest.raises(ValidationError, match="所在区或地址"):
        Stay.model_validate({"name": "X", "check_in": "2026-10-03", "check_out": "2026-10-04"})
    with pytest.raises(ValidationError, match="经纬度"):
        Stay.model_validate(stay(lat=48.85))
    with pytest.raises(ValidationError):
        Stay.model_validate(stay(nightly_eur=-1))


def test_area_text_is_normalised_like_the_library_writes_it():
    assert Stay.model_validate(stay(area="9th arrondissement")).area == "9区"
    assert Stay.model_validate(stay(area="第16区")).area == "16区"


def test_stays_must_not_overlap_and_must_fit_the_trip():
    make_request([stay("A", "2026-10-03", "2026-10-05"), stay("B", "2026-10-05", "2026-10-06")])   # 首尾相接可以
    with pytest.raises(ValidationError, match="重叠"):
        make_request([stay("A", "2026-10-03", "2026-10-05"), stay("B", "2026-10-04", "2026-10-06")])
    with pytest.raises(ValidationError, match="范围"):
        make_request([stay("A", "2026-10-02", "2026-10-05")])
    with pytest.raises(ValidationError, match="范围"):
        make_request([stay("A", "2026-10-03", "2026-10-08")])


def test_stays_are_kept_sorted_by_check_in():
    req = make_request([stay("B", "2026-10-05", "2026-10-06"), stay("A", "2026-10-03", "2026-10-05")])
    assert [s.name for s in req.stays] == ["A", "B"]


def test_which_hotel_each_day_is_anchored_to():
    req = make_request([stay("A", "2026-10-03", "2026-10-05"), stay("B", "2026-10-05", "2026-10-06")])
    name = lambda d: (s.name if (s := req.hotel_for(date.fromisoformat(d))) else None)  # noqa: E731
    assert [name("2026-10-03"), name("2026-10-04"), name("2026-10-05"), name("2026-10-06")] == ["A", "A", "B", "B"]
    assert make_request().hotel_for(date(2026, 10, 3)) is None
    gap = make_request([stay("A", "2026-10-04", "2026-10-05")])
    assert gap.hotel_for(date(2026, 10, 3)) is None and gap.hotel_for(date(2026, 10, 4)).name == "A"


# --------------------------------------------------------------------------- #
# 调度器：每天用当天的住宿当起终点
# --------------------------------------------------------------------------- #
def test_hotel_point_follows_the_stay_of_that_day():
    req = make_request([stay("A", "2026-10-03", "2026-10-05", area="1区"),
                        stay("B", "2026-10-05", "2026-10-06", area="16区")])
    a = _hotel_point(req, LIB, None, date(2026, 10, 3))
    b = _hotel_point(req, LIB, None, date(2026, 10, 5))
    assert a != b
    assert _hotel_point(req, LIB, None, date(2026, 10, 4)) == a


def test_exact_coordinates_beat_the_area_centre():
    req = make_request([stay("A", area="1区", lat=48.9, lng=2.4)])
    assert _hotel_point(req, LIB, None, date(2026, 10, 4)) == (48.9, 2.4)


def test_without_a_stay_the_old_hotel_area_still_works():
    req = make_request(hotel_area="16区")
    assert _hotel_point(req, LIB, None, date(2026, 10, 4)) == _hotel_point(make_request(hotel_area="16区"), LIB, None)
    stay_req = make_request([stay("A", ci="2026-10-04", co="2026-10-05", area="1区")], hotel_area="16区")
    # 没住宿的那天退回 hotel_area
    assert _hotel_point(stay_req, LIB, None, date(2026, 10, 3)) == _hotel_point(req, LIB, None)


def test_first_leg_gets_longer_when_the_hotel_is_far_from_the_first_stop():
    day = date(2026, 10, 4)
    near = make_request([stay("A", area="1区")])
    far = make_request([stay("A", area="19区")])
    first = lambda r: next(s for s in plan_day(r, day, ["louvre"], LIB, optimize=False).plan.stops  # noqa: E731
                           if s.kind == "place").travel_min
    t_near, t_far = first(near), first(far)
    assert t_far > t_near


def test_schedule_runs_with_stays_and_reports_the_first_days_hotel():
    req = make_request([stay("A", "2026-10-03", "2026-10-05", area="1区"),
                        stay("B", "2026-10-05", "2026-10-06", area="16区")])
    draft = schedule(req, "art", LIB)
    assert draft.hotel == _hotel_point(req, LIB, None, date(2026, 10, 3))
    assert all(d.stops for d in draft.days)


# --------------------------------------------------------------------------- #
# 平移：住宿跟着顺延
# --------------------------------------------------------------------------- #
def test_shifting_the_trip_shifts_the_stays_too():
    req = make_request([stay("A", "2026-10-03", "2026-10-05"), stay("B", "2026-10-05", "2026-10-06")])
    it = generate_from_theme(req, "art", client=None, library=LIB).itinerary
    _, req2 = shift_plan(it, req, date(2026, 10, 8), LIB)
    assert [(s.check_in, s.check_out) for s in req2.stays] == [
        (date(2026, 10, 8), date(2026, 10, 10)), (date(2026, 10, 10), date(2026, 10, 11))]


# --------------------------------------------------------------------------- #
# 住哪个区
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def art_itinerary() -> Itinerary:
    return generate_from_theme(make_request(), "art", client=None, library=LIB).itinerary


def test_recommendations_are_ranked_by_average_distance_to_the_planned_places(art_itinerary):
    recs = recommend_areas(art_itinerary, LIB)
    assert 2 <= len(recs) <= 3
    assert [r.avg_km for r in recs] == sorted(r.avg_km for r in recs)
    for r in recs:
        assert r.area.endswith("区") and r.area[:-1].isdigit()
        assert r.n_places > 0 and 0 <= r.within_2km <= r.n_places
        assert "平均" in r.reason and "公里" in r.reason and str(r.n_places) in r.reason


def test_the_best_area_is_really_closer_than_a_far_away_one(art_itinerary):
    from app.geo import haversine_km

    best = recommend_areas(art_itinerary, LIB)[0]
    pts = [(LIB.places[i.place_id].lat, LIB.places[i.place_id].lng)
           for d in art_itinerary.days for i in d.items if i.place_id in LIB.places]
    far_area = [(p.lat, p.lng) for p in LIB.places.values() if p.area == "19区"]
    far = (sum(a for a, _ in far_area) / len(far_area), sum(b for _, b in far_area) / len(far_area))
    far_avg = sum(haversine_km(far, p) for p in pts) / len(pts)
    assert best.avg_km < far_avg


def test_day_trip_places_do_not_pull_the_recommendation():
    req = make_request()
    it = generate_from_theme(req, "art", client=None, library=LIB).itinerary
    only_versailles = it.model_copy(deep=True)
    for d in only_versailles.days:
        d.items = [i for i in d.items if i.place_id is None]
    only_versailles.days[0].items.insert(0, only_versailles.days[0].items[0].model_copy(
        update={"place_id": "versailles", "title": "凡尔赛宫"}))
    assert recommend_areas(only_versailles, LIB) == []                      # 郊区景点不参与计算


def test_no_places_means_no_recommendation(art_itinerary):
    empty = art_itinerary.model_copy(deep=True)
    for d in empty.days:
        d.items = [i for i in d.items if i.place_id is None]
    assert recommend_areas(empty, LIB) == []


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


class FakeGeo:
    def __init__(self) -> None:
        self.hit: GeoHit | None = GeoHit(48.8606, 2.3376, "Rue de Rivoli, Paris")
        self.calls: list[str] = []

    def __call__(self, address: str) -> GeoHit | None:
        self.calls.append(address)
        return self.hit


@pytest.fixture
def geo() -> FakeGeo:
    return FakeGeo()


@pytest.fixture
def client(tmp_path, geo) -> TestClient:
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM, geocoder=geo))


@pytest.fixture
def plan(client) -> dict:
    r = client.post("/plans", json={
        "destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art", "arrival_time": "2026-10-03 10:30",
        "departure_time": "2026-10-06 18:00"})
    assert r.status_code == 201
    return r.json()


def put_stays(client, plan, stays, **extra):
    return client.put(f"/plans/{plan['plan_id']}/stays", json={"stays": stays, **extra})


def test_saving_stays_creates_a_labelled_version_and_keeps_the_itinerary(client, plan):
    r = put_stays(client, plan, [stay("Hôtel Test", area="9区", nightly_eur=180)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 2 and body["itinerary"]["days"] == plan["itinerary"]["days"]
    detail = client.get(f"/plans/{plan['plan_id']}").json()
    assert [s["name"] for s in detail["stays"]] == ["Hôtel Test"] and detail["stays"][0]["nightly_eur"] == 180
    hist = detail["history"][-1]
    assert hist["trigger"] == "stay-update" and "Hôtel Test" in hist["change_summary"]
    assert client.get(f"/plans/{plan['plan_id']}?version=1").json()["stays"] == []      # 旧版本没有住宿


def test_invalid_stays_are_422_with_a_readable_reason(client, plan):
    r = put_stays(client, plan, [stay("A", "2026-10-03", "2026-10-05"), stay("B", "2026-10-04", "2026-10-06")])
    assert r.status_code == 422 and "重叠" in r.text
    r = put_stays(client, plan, [stay("A", "2026-10-01", "2026-10-05")])
    assert r.status_code == 422 and "范围" in r.text
    assert client.get(f"/plans/{plan['plan_id']}").json()["latest_version"] == 1      # 失败不留版本


def test_an_address_is_geocoded_and_a_failure_falls_back_to_the_area(client, plan, geo):
    r = put_stays(client, plan, [stay("A", area="9区", address="1 Rue de Rivoli, Paris")])
    s = r.json()["stays"][0]
    assert (s["lat"], s["lng"]) == (48.8606, 2.3376) and geo.calls == ["1 Rue de Rivoli, Paris"]
    assert not any("估算" in n for n in r.json()["notes"])
    geo.hit = None
    r2 = put_stays(client, plan, [stay("A", area="9区", address="乱写的地址")])
    s2 = r2.json()["stays"][0]
    assert s2["lat"] is None
    assert any("9区" in n and "估算" in n for n in r2.json()["notes"])


def test_known_coordinates_are_not_geocoded_again(client, plan, geo):
    put_stays(client, plan, [stay("A", address="X", lat=48.9, lng=2.4)])
    assert geo.calls == []


def test_saving_stays_can_reschedule_around_the_new_hotel(client, plan):
    r = put_stays(client, plan, [stay("Far", area="19区")], reschedule=True)
    assert r.status_code == 201
    assert r.json()["itinerary"]["days"] and r.json()["version"] == 2
    assert client.get(f"/plans/{plan['plan_id']}").json()["history"][-1]["change_summary"]


def test_stays_survive_shift_rollback_and_duplicate(client, plan):
    pid = plan["plan_id"]
    put_stays(client, plan, [stay("A", "2026-10-03", "2026-10-06")])
    client.post(f"/plans/{pid}/shift", json={"start_date": "2026-10-08"})
    assert client.get(f"/plans/{pid}").json()["stays"][0]["check_in"] == "2026-10-08"
    copy = client.post(f"/plans/{pid}/duplicate").json()["plan_id"]
    assert client.get(f"/plans/{copy}").json()["stays"][0]["name"] == "A"
    client.post(f"/plans/{pid}/versions/1/restore")
    assert client.get(f"/plans/{pid}").json()["stays"] == []


def test_the_public_share_page_never_shows_where_you_stay(client, plan):
    pid = plan["plan_id"]
    put_stays(client, plan, [stay("Secret Hotel", area="9区", address="12 Rue Secrète")])
    token = client.post(f"/plans/{pid}/share").json()["token"]
    text = client.get(f"/share/{token}").text
    assert "Secret Hotel" not in text and "Secrète" not in text and '"stays"' not in text


def test_stay_recommendation_endpoint(client, plan):
    r = client.get(f"/plans/{plan['plan_id']}/stay-recommendation")
    assert r.status_code == 200
    body = r.json()
    assert body["recommendations"] and body["recommendations"][0]["area"].endswith("区")
    assert "不含酒店价格" in body["caveat"]
    assert client.get("/plans/nope/stay-recommendation").status_code == 404


def test_put_stays_404_for_unknown_plan(client):
    assert client.put("/plans/nope/stays", json={"stays": []}).status_code == 404
