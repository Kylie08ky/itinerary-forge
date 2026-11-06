"""候选池与混选（规格 M01 S9、M02 S5–S7、M05 S4 的库内搜索部分）。

- 候选池：必去 / 不去（按地点 id）、权重覆盖。
- 混选：几个主题按权重同一天混搭，权重决定占比。
- 地点详情、库内搜索。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import create_app
from app.generator import generate_from_theme
from app.library import load_library
from app.llm import LLMError
from app.scheduler import schedule
from app.schemas import TravelRequest
from app.validator import validate_itinerary
from app.versions import PlanStore

LIB = load_library("paris")
SAT = date(2026, 10, 3)


def make_request(days=5, **kw) -> TravelRequest:
    kw.setdefault("interests", ["art"])
    return TravelRequest(destination="Paris", start_date=SAT, end_date=SAT + timedelta(days=days - 1),
                         budget_level="medium", travel_style=kw.pop("style", "balanced"), **kw)


def scheduled(draft) -> list[str]:
    return [s.place_id for d in draft.days for s in d.stops if s.kind == "place"]


def mains(draft) -> list[str]:
    return [s.place_id for d in draft.days for s in d.stops if s.kind == "place" and s.role != "filler"]


# --------------------------------------------------------------------------- #
# 请求里的新字段
# --------------------------------------------------------------------------- #
def test_new_request_fields_default_to_empty_so_old_requests_still_load():
    req = make_request()
    assert req.themes == [] and req.must_go_ids == [] and req.avoid_ids == [] and req.place_weights == {}
    old = TravelRequest.model_validate_json(req.model_dump_json(exclude={"themes", "must_go_ids", "avoid_ids",
                                                                         "place_weights"}))
    assert old == req


def test_request_validation():
    with pytest.raises(ValidationError):
        make_request(themes=[{"id": "art", "weight": 3}, {"id": "art", "weight": 1}])              # 重复
    four = make_request(themes=[{"id": t, "weight": 3} for t in ("art", "food", "fashion", "family")])
    # 不再有「最多 3 个」的限制（规格 07 P15）
    assert len(four.themes) == 4
    with pytest.raises(ValidationError, match="最多混选"):
        make_request(themes=[{"id": f"t{i}", "weight": 3} for i in range(13)])      # 只防乱传
    with pytest.raises(ValidationError):
        make_request(themes=[{"id": "art", "weight": 9}])
    with pytest.raises(ValidationError):
        make_request(place_weights={"orsay": 0})


# --------------------------------------------------------------------------- #
# 候选池（M02 S5 / S6）
# --------------------------------------------------------------------------- #
def test_must_go_ids_are_always_in_the_trip():
    draft = schedule(make_request(days=4, must_go_ids=["sainte-chapelle", "pantheon"]), "art", LIB)
    assert {"sainte-chapelle", "pantheon"} <= set(scheduled(draft))


def test_avoid_ids_never_appear_on_any_weekday():
    for off in range(7):
        req = make_request(days=5, avoid_ids=["louvre", "orsay"])
        req = req.model_copy(update={"start_date": SAT + timedelta(days=off),
                                     "end_date": SAT + timedelta(days=off + 4)})
        assert not {"louvre", "orsay"} & set(scheduled(schedule(req, "art", LIB)))


def test_a_high_weight_place_outside_the_theme_gets_in_but_not_without_the_weight():
    base = scheduled(schedule(make_request(days=3), "art", LIB))
    assert "eiffel-tower" not in base
    boosted = scheduled(schedule(make_request(days=3, place_weights={"eiffel-tower": 5}), "art", LIB))
    assert "eiffel-tower" in boosted


def test_a_low_weight_demotes_a_place_to_a_last_resort():
    """权重压到 1：只有别的点都排不下时才会用到它。"""
    req = make_request(days=3, style="relaxed", place_weights={"louvre": 1, "orsay": 1})
    assert not {"louvre", "orsay"} & set(mains(schedule(req, "art", LIB)))


def test_empty_pool_preferences_equal_the_plain_theme():
    assert scheduled(schedule(make_request(), "art", LIB)) == scheduled(
        schedule(make_request(must_go_ids=[], avoid_ids=[], place_weights={}), "art", LIB))


def test_unknown_ids_are_reported_not_fatal():
    draft = schedule(make_request(must_go_ids=["nope-1"], avoid_ids=["nope-2"]), "art", LIB)
    assert any("nope-1" in n for n in draft.notes) and scheduled(draft)


def test_pool_preferences_survive_generation_and_quality_check():
    req = make_request(days=5, must_go_ids=["sainte-chapelle"], avoid_ids=["montmartre-sacre-coeur"])
    trip = generate_from_theme(req, "art", client=None, library=LIB)
    ids = {i.place_id for d in trip.itinerary.days for i in d.items if i.place_id}
    assert "sainte-chapelle" in ids and "montmartre-sacre-coeur" not in ids
    assert validate_itinerary(trip.itinerary, req).errors == []


# --------------------------------------------------------------------------- #
# 混选（M01 S9）
# --------------------------------------------------------------------------- #
def members(theme: str) -> set[str]:
    return {m.place_id for m in LIB.get_theme(theme).members}


def test_a_mix_draws_from_both_themes_in_proportion_to_the_weights():
    req = make_request(days=7, themes=[{"id": "art", "weight": 3}, {"id": "food", "weight": 1}])
    ids = mains(schedule(req, "art", LIB))
    art, food = [i for i in ids if i in members("art") and i not in members("food")], \
                [i for i in ids if i in members("food") and i not in members("art")]
    assert len(art) > len(food) >= 1


def test_equal_weights_split_the_anchors_about_evenly():
    req = make_request(days=7, themes=[{"id": "art", "weight": 3}, {"id": "food", "weight": 3}])
    ids = mains(schedule(req, "art", LIB))
    art = [i for i in ids if i in members("art") and i not in members("food")]
    food = [i for i in ids if i in members("food") and i not in members("art")]
    assert abs(len(art) - len(food)) <= 3 and min(len(art), len(food)) >= 3


def test_mixing_happens_within_days_not_one_theme_per_day():
    req = make_request(days=7, themes=[{"id": "art", "weight": 3}, {"id": "food", "weight": 3}])
    draft = schedule(req, "art", LIB)
    both = 0
    for d in draft.days:
        ids = {s.place_id for s in d.stops if s.kind == "place" and s.role != "filler"}
        both += bool(ids & (members("art") - members("food"))) and bool(ids & (members("food") - members("art")))
    assert both >= 2


def test_mixed_generation_names_and_lists_the_themes():
    req = make_request(themes=[{"id": "art", "weight": 3}, {"id": "food", "weight": 2}])
    trip = generate_from_theme(req, "art", client=None, library=LIB)
    gen = trip.itinerary.generation
    assert gen.theme_name == "艺术 + 美食" and gen.themes == ["art", "food"]
    assert validate_itinerary(trip.itinerary, req).errors == []


def test_single_theme_generation_is_unchanged():
    gen = generate_from_theme(make_request(), "art", client=None, library=LIB).itinerary.generation
    assert gen.theme_name == "艺术" and gen.themes == ["art"]


def test_mixed_plans_are_deterministic():
    req = make_request(themes=[{"id": "fashion", "weight": 2}, {"id": "photography", "weight": 3}])
    assert schedule(req, "photography", LIB).model_dump() == schedule(req, "photography", LIB).model_dump()


# --------------------------------------------------------------------------- #
# 库内搜索（M05 S4 的库内部分）
# --------------------------------------------------------------------------- #
def test_search_finds_by_chinese_name_english_name_and_alias():
    assert LIB.search("卢浮")[0].id == "louvre"
    assert LIB.search("orsay")[0].id == "orsay"
    assert LIB.search("Musée Rodin")[0].id == "rodin"


def test_search_ranks_exact_before_partial_and_respects_the_limit():
    res = LIB.search("百货")
    assert 1 <= len(res) <= 8
    assert len(LIB.search("区", limit=3)) <= 3


def test_search_by_area_and_empty_query():
    assert {p.area for p in LIB.search("7区", limit=20)} == {"7区"}
    assert LIB.search("") == [] and LIB.search("   ") == [] and LIB.search("zzzzzz-no-such") == []


# --------------------------------------------------------------------------- #
# 接口：地点详情、搜索、混选生成
# --------------------------------------------------------------------------- #
class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("没启动")


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


def test_place_detail_endpoint(client):
    r = client.get("/places/louvre")
    assert r.status_code == 200
    d = r.json()
    assert d["name_zh"] == "卢浮宫" and d["address"] and d["ticketing"] == "online_required"
    assert d["official_url"].startswith("https://") and d["closed_weekdays"] == [1]
    assert {"coords_verified", "hours_verified", "verified_at"} <= set(d["verification"])
    art = next(t for t in d["themes"] if t["id"] == "art")
    assert art["name_zh"] == "艺术" and art["role"] == "anchor" and 1 <= art["weight"] <= 5


def test_place_detail_hides_the_link_for_places_without_a_ticket_channel(client):
    d = client.get("/places/tuileries-garden").json()
    assert d["official_url"] is None and d["address"]


def test_place_detail_404(client):
    assert client.get("/places/nope").status_code == 404


def test_search_endpoint(client):
    r = client.get("/places/search", params={"q": "卢浮"})
    assert r.status_code == 200 and r.json()[0]["id"] == "louvre"
    assert client.get("/places/search", params={"q": ""}).json() == []
    one = client.get("/places/search", params={"q": "7区", "limit": 2}).json()
    assert len(one) == 2 and all(p["address"] is not None for p in one)


def payload(**over) -> dict:
    body = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-07", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["art"]}
    body.update(over)
    return body


def test_post_tasks_with_a_theme_mix_and_pool_preferences(client):
    r = client.post("/tasks", json=payload(themes=[{"id": "art", "weight": 3}, {"id": "food", "weight": 1}],
                                           must_go_ids=["sainte-chapelle"], avoid_ids=["orsay"]))
    assert r.status_code == 202, r.text
    import time
    for _ in range(100):
        t = client.get(f"/tasks/{r.json()['task_id']}").json()
        if t["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)
    assert t["status"] == "succeeded"
    plan = client.get(f"/plans/{t['plan_id']}").json()
    assert plan["itinerary"]["generation"]["theme_name"] == "艺术 + 美食"
    ids = {i["place_id"] for d in plan["itinerary"]["days"] for i in d["items"] if i.get("place_id")}
    assert "sainte-chapelle" in ids and "orsay" not in ids
    card = client.get("/plan-cards").json()[0]
    assert card["title"] == "巴黎 · 艺术 + 美食"


def test_theme_mix_validation_over_http(client):
    bad = client.post("/tasks", json=payload(themes=[{"id": "art", "weight": 3}, {"id": "no-such", "weight": 1}]))
    assert bad.status_code == 422 and "themes" in bad.json()["detail"]
    absurd = [{"id": f"t{i}", "weight": 3} for i in range(13)]
    many = client.post("/tasks", json=payload(themes=absurd))
    assert many.status_code == 422


def test_sync_endpoint_accepts_a_mix_too(client):
    mix = [{"id": "art", "weight": 2}, {"id": "shopping", "weight": 2}]
    r = client.post("/plans", json=payload(theme="art", themes=mix))
    assert r.status_code == 201 and r.json()["itinerary"]["generation"]["themes"] == ["art", "shopping"]


def test_theme_detail_lists_members_with_role_and_default_weight(client):
    r = client.get("/themes/art")
    assert r.status_code == 200
    d = r.json()
    assert d["name_zh"] == "艺术" and d["tips"] and d["anchors_per_day"]["balanced"] == 2
    by_id = {m["place_id"]: m for m in d["members"]}
    assert by_id["louvre"] == {"place_id": "louvre", "role": "anchor", "weight": 5}
    assert all(m["place_id"] in LIB.places for m in d["members"])
    weights = [m["weight"] for m in d["members"] if m["role"] == "anchor"]
    assert weights == sorted(weights, reverse=True)                              # 主要点按权重从高到低
    assert client.get("/themes/no-such").status_code == 404
