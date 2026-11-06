"""一次性迁移：旧的「一座城市一份行程」的旅行并成一份多城市行程（scripts/merge_trip_plans.py）。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from city_fixtures import write_small_city

from app import multicity
from app.cities import reload_cities
from app.trips import TripError, TripSpec
from app.versions import PlanStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import merge_trip_plans as script  # noqa: E402

TODAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def second_city(tmp_path, monkeypatch):
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path / "generated"))
    reload_cities()
    write_small_city("lyon", "里昂", "Lyon", (45.7640, 4.8357), "FR", "Europe/Paris", "EUR", TODAY)
    yield
    reload_cities()


OLD_TABLES = """
CREATE TABLE trips (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, spec_json TEXT);
CREATE TABLE trip_legs (trip_id TEXT, position INTEGER, destination TEXT, city_id TEXT, start_date TEXT,
                        end_date TEXT, task_id TEXT, plan_id TEXT);
CREATE TABLE trip_transfers (trip_id TEXT, position INTEGER, transfer_json TEXT, estimated INTEGER);
"""


def old_style_trip(tmp_path):
    """造一趟旧的旅行：两份各自生成的单城市行程 + 旅行表里记着换城市的交通是估算的。"""
    spec = TripSpec.model_validate({
        "legs": [{"destination": "Paris", "start_date": "2026-12-12", "end_date": "2026-12-14"},
                 {"destination": "里昂", "start_date": "2026-12-14", "end_date": "2026-12-16"}],
        "outbound": {"mode": "flight", "depart_at": "2026-12-12T06:00", "arrive_at": "2026-12-12T09:30"},
        "inbound": {"mode": "flight", "depart_at": "2026-12-16T18:00"},
        "common": {"budget_level": "medium", "travel_style": "balanced", "interests": ["均衡"]}})
    parent, _ = multicity.build_request(spec)
    parts = multicity.split(parent, multicity.generate(parent, client=None))
    store = PlanStore(tmp_path / "plans")
    ids = []
    for req, it in parts:                       # 旧做法存下来的需求里没有「估算」标记
        wipe = lambda leg: leg and leg.model_copy(update={"estimated": False})  # noqa: E731
        req = req.model_copy(update={"transport": req.transport.model_copy(update={
            "outbound": wipe(req.transport.outbound), "inbound": wipe(req.transport.inbound)})})
        ids.append(store.create(req, it))
    with store.database.connect(write=True) as con:
        for ddl in filter(str.strip, OLD_TABLES.split(";")):
            con.execute(ddl)
        con.execute("INSERT INTO trips VALUES ('trip-x', '巴黎 → 里昂', 'now', '{}')")
        for i, (name, pid) in enumerate(zip(("巴黎", "里昂"), ids, strict=True)):
            con.execute("INSERT INTO trip_legs VALUES ('trip-x', ?, ?, '', '', '', 't', ?)", (i, name, pid))
        con.execute("INSERT INTO trip_transfers VALUES ('trip-x', 0, '{}', 1)")
    return store, parent


def test_two_old_plans_become_one_plan_and_the_originals_stay(tmp_path):
    store, parent = old_style_trip(tmp_path)
    [trip] = script.trips(store)
    before = set(store.list_plans())
    assert script.merge(store, trip, apply=False) is None and set(store.list_plans()) == before   # 试并不写
    new_id = script.merge(store, trip, apply=True)
    assert set(store.list_plans()) == before | {new_id}                                          # 只新增，原来的都在
    request, itinerary = store.request(new_id), store.load(new_id)
    assert multicity.title_of(request) == "巴黎 → 里昂" and len(itinerary.days) == 5
    assert request.legs[0].request.transport.inbound.estimated is True        # 旅行表里的「估算」标记带过来了
    assert request.legs[1].request.transport.outbound.estimated is True
    assert [i.city_id for d in itinerary.days if d.theme_by_city for i in d.items] != []


def test_a_trip_with_a_leg_that_never_finished_is_not_merged(tmp_path):
    store, _ = old_style_trip(tmp_path)
    with store.database.connect(write=True) as con:
        con.execute("UPDATE trip_legs SET plan_id = NULL WHERE position = 1")
    [trip] = script.trips(store)
    with pytest.raises(TripError, match="没生成好|还没有生成好"):
        script.merge(store, trip, apply=True)
