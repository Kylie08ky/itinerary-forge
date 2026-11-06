"""美食（规格 09 十七）：数据闸门、招牌折进地点的故事、接口。不联网（来源核对在 scripts/build_food.py 里做）。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.food as food_mod
from app.api import create_app
from app.culture import stories_by_place, stories_for_place, story_text
from app.food import MIN_POPULARITY, Dish, FoodError, food_overview, load_food, signature_stories
from app.library import load_library
from app.llm import LLMError
from app.versions import PlanStore

SRC = {"title": "x", "url": "https://example.org/x", "checked_at": "2026-10-27"}


def dish(**over) -> dict:
    base = {"id": "d", "kind": "pastry", "name_zh": "马卡龙", "zh_wiki": "马卡龙", "name_local": "Macaron",
            "summary": "蛋白和杏仁粉做的圆饼夹心甜点，巴黎式的夹甘纳什。", "source": SRC, "popularity": 40,
            "signatures": [{"place_id": "laduree-champs", "note": "以马卡龙闻名于世。", "source": SRC}]}
    return {**base, **over}


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


@pytest.fixture
def food_file(tmp_path, monkeypatch):
    """把美食数据指到临时目录，造一份巴黎的文件。"""
    monkeypatch.setattr(food_mod, "_DIR", tmp_path)
    load_food.cache_clear()

    def write(*dishes: dict) -> None:
        (tmp_path / "paris.json").write_text(json.dumps({"city": "paris", "dishes": list(dishes)}), encoding="utf-8")
        load_food.cache_clear()

    yield write
    load_food.cache_clear()


# --------------------------------------------------------------------------- #
# 数据闸门
# --------------------------------------------------------------------------- #
def test_the_shipped_food_data_is_valid_and_every_dish_is_famous_enough():
    dishes = load_food("paris")
    assert dishes
    lib = load_library("paris")
    for d in dishes.values():
        assert d.popularity >= MIN_POPULARITY, d.id
        assert d.source.url.startswith("https://") and d.wikidata, d.id
        for s in d.signatures:
            assert s.place_id in lib.places and s.source.url.startswith("https://"), (d.id, s.place_id)


def test_a_dish_that_is_not_famous_enough_is_rejected(food_file):
    food_file(dish(popularity=MIN_POPULARITY - 1))
    with pytest.raises(FoodError, match="名气不够"):
        load_food("paris")


def test_a_signature_at_an_unknown_place_is_rejected(food_file):
    food_file(dish(signatures=[{"place_id": "nowhere", "note": "以马卡龙闻名。", "source": SRC}]))
    with pytest.raises(FoodError, match="不在地点库"):
        load_food("paris")


def test_a_signature_needs_a_source_and_a_real_note():
    with pytest.raises(ValidationError):
        Dish.model_validate(dish(signatures=[{"place_id": "laduree-champs", "note": "好吃", "source": SRC}]))
    with pytest.raises(ValidationError):
        Dish.model_validate(dish(signatures=[{"place_id": "laduree-champs", "note": "以马卡龙闻名于世。"}]))


def test_the_same_place_cannot_be_listed_twice_for_one_dish():
    sig = {"place_id": "laduree-champs", "note": "以马卡龙闻名于世。", "source": SRC}
    with pytest.raises(ValidationError, match="重复"):
        Dish.model_validate(dish(signatures=[sig, sig]))


def test_a_city_without_a_food_file_simply_has_nothing():
    assert load_food("ludwigsburg") == {}
    assert signature_stories("ludwigsburg", "anything") == []


# --------------------------------------------------------------------------- #
# 招牌折进地点的故事
# --------------------------------------------------------------------------- #
def test_a_signature_comes_out_as_a_story_of_that_place_and_comes_first():
    stories = stories_for_place("paris", "angelina")
    assert {s["title_zh"] for s in stories if s["kind"] == "food"} == {"蒙布朗", "热巧克力"}
    foods = [s for s in stories if s["kind"] == "food"]
    assert all(s["kind_zh"] == "招牌" and s["source"]["url"].startswith("https://") for s in foods)
    kinds = [s["kind"] for s in stories]
    assert kinds == sorted(kinds, key=lambda k: k != "food")                       # 招牌都排在作品前面


def test_a_place_without_signatures_gets_none_and_the_story_text_reads_as_a_signature():
    assert signature_stories("paris", "louvre") == []
    text = story_text(next(s for s in stories_for_place("paris", "laduree-champs") if s["kind"] == "food"))
    assert text.startswith("招牌：马卡龙（Macaron）：") and "1862" in text


def test_only_places_with_signatures_show_up_in_a_plans_stories():
    found = stories_by_place("paris", ["laduree-champs", "louvre", "rue-montorgueil"])
    assert any(s["kind"] == "food" for s in found["laduree-champs"])
    assert [s["title_zh"] for s in found["rue-montorgueil"] if s["kind"] == "food"] == ["朗姆巴巴"]
    assert not any(s["kind"] == "food" for s in found.get("louvre", []))


# --------------------------------------------------------------------------- #
# 参考页的「美食」一节
# --------------------------------------------------------------------------- #
def test_the_overview_lists_every_dish_with_a_source_and_only_real_signature_places():
    rows = {r["id"]: r for r in food_overview("paris")}
    assert {"croissant", "baguette", "macaron"} <= set(rows)
    assert rows["croissant"]["places"] == []                                         # 没有出处的不写「去哪吃」
    assert {p["place_id"] for p in rows["macaron"]["places"]} == {"laduree-champs", "pierre-herme-st-germain"}
    for r in rows.values():
        assert r["summary"] and r["source"]["url"].startswith("https://") and r["name_local"]


def test_the_food_endpoint_serves_the_overview(client):
    r = client.get("/food", params={"city": "paris"})
    assert r.status_code == 200
    assert [d["id"] for d in r.json()] == [d["id"] for d in food_overview("paris")]
    assert client.get("/food", params={"city": "ludwigsburg"}).json() == []


def test_a_plan_through_a_signature_place_carries_the_signature_everywhere(client):
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-17", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["shopping"], "theme": "shopping",
               "must_go_ids": ["angelina"], "arrival_time": "2026-12-17 09:00", "departure_time": "2026-12-17 20:00"}
    r = client.post("/plans", json=payload)
    assert r.status_code == 201, r.text
    pid = r.json()["plan_id"]
    detail = client.get(f"/plans/{pid}").json()
    assert any(s["kind"] == "food" for s in detail["stories"]["angelina"])
    assert "招牌" in client.get(f"/plans/{pid}/itinerary.md").text
    assert any(s["kind"] == "food" for s in client.get("/places/angelina").json()["stories"])


# --------------------------------------------------------------------------- #
# 第 4 步：午餐建议（规格 09 十七之五）
# --------------------------------------------------------------------------- #
def _lunch_day(*place_ids: str, lunch: str = "午餐（自行选餐厅）"):
    from datetime import date

    from app.schemas import Day, ItemType, ItineraryItem

    items = [ItineraryItem(time="10:00", title=place_ids[0], type=ItemType.museum, location="x", duration_minutes=90,
                           transport=None, notes="", place_id=place_ids[0]),
             ItineraryItem(time="12:00", title=lunch, type=ItemType.meal, location="就近", duration_minutes=75,
                           transport=None, notes="排程占位：具体餐厅请自行选择，热门餐厅记得提前订位")]
    items += [ItineraryItem(time="14:00", title=pid, type=ItemType.museum, location="x", duration_minutes=90,
                            transport=None, notes="", place_id=pid) for pid in place_ids[1:]]
    return Day(date=date(2026, 12, 17), theme="t", items=items)


def _meal_note(day) -> str:
    return next(i.notes for i in day.items if i.type.value == "meal")


def test_a_lunch_row_near_a_checked_lunch_place_says_where_with_distance_and_hours():
    from app.generator import add_lunch_hints

    lib = load_library("paris")
    day = add_lunch_hints([_lunch_day("orsay", "orangerie")], lib)[0]
    note = _meal_note(day)
    assert "附近可以去：" in note and "普罗可布咖啡馆 · Le Procope" in note
    assert "米" in note and "12:00–24:00" in note and "热门餐厅记得提前订位" in note


def test_a_lunch_row_far_from_every_lunch_place_stays_as_it_was():
    from app.generator import add_lunch_hints

    lib = load_library("paris")
    day = add_lunch_hints([_lunch_day("pere-lachaise", "canal-saint-martin")], lib)[0]
    assert _meal_note(day) == "排程占位：具体餐厅请自行选择，热门餐厅记得提前订位"


def test_a_quick_lunch_and_places_already_in_the_plan_get_no_suggestion():
    from app.generator import add_lunch_hints

    lib = load_library("paris")
    quick = add_lunch_hints([_lunch_day("orsay", "orangerie", lunch="午餐（简餐）")], lib)[0]
    assert "附近可以去" not in _meal_note(quick)
    taken = add_lunch_hints([_lunch_day("orsay", "cafe-procope")], lib)[0]
    assert "普罗可布" not in _meal_note(taken)


def test_a_lunch_place_closed_that_day_is_not_suggested_and_at_most_two_are_listed():
    from app.generator import add_lunch_hints

    lib = load_library("paris")
    closed = lib.model_copy(update={"places": {**lib.places, "cafe-procope": lib.places["cafe-procope"].model_copy(
        update={"closed_weekdays": [3]})}})                                    # 2026-12-17 是周四
    note = _meal_note(add_lunch_hints([_lunch_day("orsay", "orangerie")], closed)[0])
    assert "普罗可布" not in note
    assert note.count("；") <= 2 and note.count("约") <= 2


def test_generated_and_edited_plans_carry_the_suggestion_where_it_fits(client):
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-20", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["art"], "theme": "art", "must_go_ids": ["orsay"],
               "arrival_time": "2026-12-17 10:00", "departure_time": "2026-12-20 18:00"}
    r = client.post("/plans", json=payload)
    assert r.status_code == 201, r.text
    days = client.get(f"/plans/{r.json()['plan_id']}").json()["itinerary"]["days"]
    day = next(d for d in days if any(i.get("place_id") == "orsay" for i in d["items"]))
    meal = next(i for i in day["items"] if i["type"] == "meal" and i["title"].startswith("午餐"))
    assert "附近可以去：" in meal["notes"], meal


def test_after_moving_a_place_every_days_suggestion_is_the_one_its_new_neighbours_give(client):
    from app.generator import LUNCH_PLACEHOLDER, add_lunch_hints
    from app.schemas import Itinerary

    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-20", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["art"], "theme": "art", "must_go_ids": ["orsay"],
               "arrival_time": "2026-12-17 10:00", "departure_time": "2026-12-20 18:00"}
    pid = client.post("/plans", json=payload).json()["plan_id"]
    it = client.get(f"/plans/{pid}").json()["itinerary"]
    src = next(d["date"] for d in it["days"] if any(i.get("place_id") == "orsay" for i in d["items"]))
    dst = next(d["date"] for d in it["days"] if d["date"] != src and any(i.get("place_id") for i in d["items"]))
    edit = {"op": "move", "place_id": "orsay", "to_date": dst}
    r = client.post(f"/plans/{pid}/edit", json={"itinerary": it, "edit": edit})
    assert r.status_code == 200, r.text
    out = Itinerary.model_validate(r.json()["itinerary"])
    fresh = out.model_copy(deep=True)
    for d in fresh.days:
        for i in d.items:
            if i.notes.startswith("附近可以去："):
                i.notes = LUNCH_PLACEHOLDER
    add_lunch_hints(fresh.days, load_library("paris"))
    assert [i.notes for d in out.days for i in d.items] == [i.notes for d in fresh.days for i in d.items]


def test_the_eiffel_tower_day_suggests_the_cafe_next_door_and_says_its_hours_are_unchecked():
    from app.generator import add_lunch_hints

    lib = load_library("paris")
    note = _meal_note(add_lunch_hints([_lunch_day("eiffel-tower", "trocadero")], lib)[0])
    assert "人类博物馆咖啡馆 · Café de l'Homme" in note and "开放时间未核对" in note
