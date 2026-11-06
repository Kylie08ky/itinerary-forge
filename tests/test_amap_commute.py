"""M05 S7b：国内一天里相邻两点的真实通勤时间（高德路径规划）。点了才查，不缓存，不保存；假的 HTTP 函数代替真实请求。"""

from __future__ import annotations

from datetime import date

import pytest
from city_fixtures import write_small_city
from fastapi.testclient import TestClient

from app.api import create_app
from app.cities import reload_cities
from app.db import Database
from app.maps import amap, quota
from app.maps.coords import wgs84_to_gcj02
from app.versions import PlanStore

WALK = {"status": "1", "info": "OK", "route": {"paths": [{"distance": "612", "duration": "441", "steps": [
    {"instruction": "沿文明街向北步行300米", "distance": "300"}]}]}}
TRANSIT = {"status": "1", "info": "OK", "route": {"distance": "6200", "transits": [{
    "duration": "1620", "cost": "2", "walking_distance": "450", "segments": [
        {"walking": {"distance": "300", "duration": "216"}, "bus": []},
        {"walking": [], "bus": {"buslines": [{"name": "地铁1号线(北京路--昆明火车站)", "via_num": "3",
                                            "departure_stop": {"name": "北京路"},
                                            "arrival_stop": {"name": "昆明火车站"}}]}},
        {"walking": {"distance": "150", "duration": "108"}, "bus": []}]}]}}
NO_TRANSIT = {"status": "1", "info": "OK", "route": {"distance": "6200", "transits": []}}

A = (25.0389, 102.7183)
NEAR = (25.0420, 102.7183)        # 约 350 米
FAR = (25.0950, 102.7183)         # 约 6 公里


@pytest.fixture
def usage(tmp_path):
    return quota.Usage(Database(tmp_path / "t.db"))


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[str, dict]] = []

    def fake(url, params):
        seen.append((url, params))
        return WALK if "walking" in url else TRANSIT

    monkeypatch.setattr(amap, "_http_get", fake)
    return seen


def test_a_near_pair_is_walked_and_counts_one_lbs_call(usage, calls):
    got = amap.AmapClient(lambda: "k", usage).route(A, NEAR, "昆明")
    assert got is not None and got.mode == "步行" and got.minutes == 8 and got.distance_m == 612
    assert got.steps == ["步行 612 米"]
    [(url, params)] = calls
    assert url.endswith("/v3/direction/walking") and usage.used("lbs") == 1
    lat, lng = wgs84_to_gcj02(*A)
    assert params["origin"] == f"{lng:.6f},{lat:.6f}"                  # 高德要「经度,纬度」，GCJ-02


def test_a_far_pair_takes_transit_and_lists_the_steps(usage, calls):
    got = amap.AmapClient(lambda: "k", usage).route(A, FAR, "昆明")
    assert got is not None and got.mode == "公交 / 地铁" and got.minutes == 27 and got.distance_m == 6200
    assert got.steps == ["步行 300 米", "乘地铁1号线：北京路 → 昆明火车站（4 站）", "步行 150 米"]
    [(url, params)] = calls
    assert url.endswith("/v3/direction/transit/integrated") and params["city"] == "昆明"


def test_transit_with_no_route_gives_none(usage, monkeypatch):
    monkeypatch.setattr(amap, "_http_get", lambda u, p: NO_TRANSIT)
    assert amap.AmapClient(lambda: "k", usage).route(A, FAR, "昆明") is None


def test_no_key_or_cap_means_no_call(usage, calls, monkeypatch):
    assert amap.AmapClient(lambda: "", usage).route(A, NEAR, "昆明") is None
    monkeypatch.setitem(quota.CAPS, "lbs", 1)
    usage.record("lbs")
    assert amap.AmapClient(lambda: "k", usage).route(A, NEAR, "昆明") is None
    assert calls == []


def test_the_same_route_is_asked_again_every_time(usage, calls):
    c = amap.AmapClient(lambda: "k", usage)
    c.route(A, NEAR, "昆明")
    c.route(A, NEAR, "昆明")
    assert len(calls) == 2 and usage.used("lbs") == 2                  # 不缓存（服务协议不许）


# ---- 接口 ------------------------------------------------------------------ #

@pytest.fixture
def client(tmp_path, monkeypatch):
    for name in ("AMAP_WEB_KEY", "AMAP_JS_KEY", "AMAP_JS_SECRET"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path / "generated"))
    reload_cities()
    write_small_city("kunming", "昆明", "Kunming", (25.0389, 102.7183), "CN", "Asia/Shanghai", "CNY",
                     date(2026, 10, 31))
    yield TestClient(create_app(store=PlanStore(tmp_path)))
    reload_cities()


def _leg(a, b) -> dict:
    return {"from_lat": a[0], "from_lng": a[1], "to_lat": b[0], "to_lng": b[1]}


def test_commute_returns_each_leg_and_never_the_key(client, calls, monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "secretkey")
    r = client.post("/commute", json={"city": "kunming", "legs": [_leg(A, NEAR), _leg(NEAR, FAR)]})
    assert r.status_code == 200 and "secretkey" not in r.text
    body = r.json()
    assert "reason" not in body and len(body["legs"]) == 2
    assert body["legs"][0] == {"minutes": 8, "mode": "步行", "distance_m": 612, "steps": ["步行 612 米"]}
    assert body["legs"][1]["mode"] == "公交 / 地铁" and len(body["legs"][1]["steps"]) == 3
    rows = client.get("/map/usage").json()["services"]
    assert next(x for x in rows if x["service"] == "lbs")["used"] == 2


def test_commute_without_a_key_says_why_and_calls_nothing(client, calls):
    r = client.post("/commute", json={"city": "kunming", "legs": [_leg(A, NEAR)]}).json()
    assert r == {"legs": [None], "reason": "no_key"} and calls == []


def test_commute_stops_at_the_monthly_cap_and_says_quota(client, calls, monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "k")
    monkeypatch.setitem(quota.CAPS, "lbs", 1)
    r = client.post("/commute", json={"city": "kunming", "legs": [_leg(A, NEAR), _leg(NEAR, FAR)]}).json()
    assert r["reason"] == "quota" and r["legs"][0]["minutes"] == 8 and r["legs"][1] is None


def test_commute_when_amap_is_unreachable_says_unreachable(client, monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "k")

    def boom(url, params):
        raise amap.AmapError("net")

    monkeypatch.setattr(amap, "_http_get", boom)
    r = client.post("/commute", json={"city": "kunming", "legs": [_leg(A, NEAR)]}).json()
    assert r == {"legs": [None], "reason": "unreachable"}


def test_commute_is_refused_for_non_amap_cities_and_too_many_legs(client, calls, monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "k")
    paris = client.post("/commute", json={"city": "paris", "legs": [_leg(A, NEAR)]}).json()
    assert paris == {"legs": [None], "reason": "not_amap_city"} and calls == []
    assert client.post("/commute", json={"city": "nowhere", "legs": [_leg(A, NEAR)]}).status_code == 404
    assert client.post("/commute", json={"city": "kunming", "legs": [_leg(A, NEAR)] * 13}).status_code == 422
    assert client.post("/commute", json={"city": "kunming", "legs": []}).status_code == 422


DRIVE = {"status": "1", "info": "OK", "route": {"paths": [{"distance": "6800", "duration": "840"}]}}


def test_a_self_drive_day_asks_for_the_driving_route_not_transit(usage, monkeypatch):
    seen = []
    monkeypatch.setattr(amap, "_http_get", lambda u, p: (seen.append(u), DRIVE)[1])
    got = amap.AmapClient(lambda: "k", usage).route(A, FAR, "昆明", drive=True)
    assert got is not None and got.mode == "自驾" and got.minutes == 14 and got.distance_m == 6800
    assert seen[0].endswith("/v3/direction/driving")
    near = amap.AmapClient(lambda: "k", usage).route(A, NEAR, "昆明", drive=True)          # 很近的还是步行
    assert near is not None and near.mode == "步行"
