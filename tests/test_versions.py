import json
from pathlib import Path

import pytest

from app.schemas import Itinerary, TravelRequest
from app.versions import PlanNotFound, PlanStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "fixtures" / "stub-itinerary.json"
PERSONA = ROOT / "data" / "personas" / "persona-01-museum-foodie.json"


@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(root=tmp_path / "plans")


@pytest.fixture
def itinerary() -> Itinerary:
    return Itinerary.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def request_() -> TravelRequest:
    raw = json.loads(PERSONA.read_text(encoding="utf-8"))
    raw.pop("_meta", None)
    return TravelRequest.model_validate(raw)


def test_create_then_load(store, request_, itinerary):
    plan_id = store.create(request_, itinerary)
    assert plan_id.startswith("paris-20261003-")
    loaded = store.load(plan_id)
    assert loaded.version == "v1"
    assert len(loaded.days) == len(itinerary.days)
    assert store.request(plan_id).destination == "Paris"
    hist = store.history(plan_id)
    assert len(hist) == 1 and hist[0].trigger == "initial"


def test_add_version_bumps_and_keeps_old(store, request_, itinerary):
    plan_id = store.create(request_, itinerary)
    v = store.add_version(
        plan_id, itinerary, trigger="user-feedback", note="加个咖啡馆",
        change_summary="1 天有改动，新增 1 项", errors_before=3, errors_after=1,
    )
    assert v == 2
    assert store.latest_version(plan_id) == 2
    assert store.load(plan_id, 1).version == "v1"
    assert store.load(plan_id, 2).version == "v2"
    assert store.load(plan_id).version == "v2"          # 默认取最新
    hist = store.history(plan_id)
    assert [r.version for r in hist] == [1, 2]
    assert hist[1].trigger == "user-feedback"
    assert hist[1].note == "加个咖啡馆"
    assert (hist[1].errors_before, hist[1].errors_after) == (3, 1)


def test_missing_plan_raises(store):
    with pytest.raises(PlanNotFound):
        store.load("does-not-exist")


def test_list_plans(store, request_, itinerary):
    assert store.list_plans() == []
    a = store.create(request_, itinerary)
    b = store.create(request_, itinerary)
    assert set(store.list_plans()) == {a, b}
