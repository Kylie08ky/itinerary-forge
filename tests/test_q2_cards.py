"""Q2（规格 07 P01 / P03）：首页和「我的行程」卡片的封面大图来自后端选图，规则和行程页封面一致。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.cities import load_city
from app.llm import LLMError
from app.media import load_hero, pick_hero
from app.versions import PlanStore


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


def test_a_city_hero_is_its_first_landmark_that_has_a_big_photo():
    got = pick_hero("paris")
    heroes = load_hero("paris")
    first = next(p for p in load_city("paris").landmarks if p in heroes)
    assert got["place_id"] == first and got["url"].startswith("/media/hero/") and got["author"] and got["license"]


def test_a_trips_hero_prefers_landmarks_in_the_configured_order():
    # 行程里有卢浮宫和奥赛：卢浮宫排在奥赛前面（landmarks 的顺序），和行程里谁先出现无关
    assert pick_hero("paris", ["orsay", "louvre"])["place_id"] == "louvre"
    # 行程里一个标志性地点都没有：退回行程里第一个有大图的点；都没有就是 None
    non_landmarks = [p for p in load_hero("paris") if p not in load_city("paris").landmarks]
    if non_landmarks:
        assert pick_hero("paris", ["not-a-place", non_landmarks[0]])["place_id"] == non_landmarks[0]
    assert pick_hero("paris", ["not-a-place"]) is None and pick_hero("paris", []) is None


def test_the_frontend_trip_cover_uses_the_same_landmark_order():
    """前端行程页封面里写了一份 HOME_ORDER；它必须和城市配置的 landmarks 一致，否则卡片和封面会是两张图。"""
    src = (Path(__file__).resolve().parents[1] / "web" / "src" / "hero.tsx").read_text(encoding="utf-8")
    m = re.search(r"HOME_ORDER = \[([^\]]+)\]", src)
    assert m, "hero.tsx 里找不到 HOME_ORDER"
    assert re.findall(r'"([^"]+)"', m.group(1)) == load_city("paris").landmarks


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


def test_cities_carry_a_hero_for_the_landing_page(client):
    paris = next(c for c in client.get("/cities").json() if c["id"] == "paris")
    assert paris["hero"]["url"].startswith("/media/hero/") and paris["hero"]["author"]


def test_plan_cards_carry_the_city_and_a_hero_from_this_trip(client):
    body = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["art"], "theme": "art"}
    pid = client.post("/plans", json=body).json()["plan_id"]
    card = next(c for c in client.get("/plan-cards").json() if c["plan_id"] == pid)
    assert card["city_id"] == "paris"
    in_trip = {i["place_id"] for d in client.get(f"/plans/{pid}").json()["itinerary"]["days"] for i in d["items"]
               if i.get("place_id")}
    assert card["hero"] is not None and card["hero"]["place_id"] in in_trip      # 艺术主题里一定有带大图的地标
    order = list(dict.fromkeys(i for d in client.get(f"/plans/{pid}").json()["itinerary"]["days"] for i in
                               [x["place_id"] for x in d["items"] if x.get("place_id")]))
    assert card["hero"]["place_id"] == pick_hero("paris", order)["place_id"]    # 和行程页封面同一规则
