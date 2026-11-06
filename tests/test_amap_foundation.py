"""M05 S7 / S7c：坐标换算、配额保护、地图配置。高德的真实请求不进单元测试。"""

from __future__ import annotations

import math

import pytest

from app.cities import load_city
from app.db import Database
from app.maps import quota
from app.maps.coords import gcj02_to_wgs84, wgs84_to_gcj02


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((a[0] - b[0]) * 111000, (a[1] - b[1]) * 111000 * math.cos(math.radians(a[0])))


# 已知点：WGS-84 原坐标 → 高德自己的换算接口（coordsys=gps）返回的 GCJ-02（2026-11-02 取到）
KNOWN = [
    ((39.9087, 116.3975), (39.910103, 116.403743)),     # 天安门
    ((34.2610, 108.9423), (34.259427, 108.946973)),     # 西安钟楼
    ((29.5580, 106.5770), (29.555119, 106.580696)),     # 重庆解放碑
    ((31.2397, 121.4900), (31.237715, 121.494468)),     # 上海外滩
    ((30.6700, 104.0540), (30.667569, 104.056488)),     # 成都宽窄巷子
    ((36.0720, 103.8280), (36.071703, 103.830437)),     # 兰州中山桥
]


@pytest.mark.parametrize("wgs,gcj", KNOWN)
def test_wgs84_to_gcj02_is_within_five_metres_of_amaps_own_conversion(wgs, gcj):
    assert _metres(wgs84_to_gcj02(*wgs), gcj) <= 5


@pytest.mark.parametrize("wgs,gcj", KNOWN)
def test_gcj02_back_to_wgs84_returns_the_original_point(wgs, gcj):
    assert _metres(gcj02_to_wgs84(*wgs84_to_gcj02(*wgs)), wgs) <= 0.5


def test_points_outside_china_are_not_shifted():
    assert wgs84_to_gcj02(48.8566, 2.3522) == (48.8566, 2.3522)
    assert gcj02_to_wgs84(35.68, 139.76) == (35.68, 139.76)


def test_chinese_cities_use_amap_and_the_rest_do_not():
    import json
    from pathlib import Path

    from app.cities import City
    raw = json.loads((Path(__file__).resolve().parents[1] / "data" / "cities" / "paris.json").read_text("utf-8"))
    raw.pop("map_provider", None)                          # 现建的城市文件不写这一项
    assert City.model_validate({**raw, "country": "CN"}).map_provider == "amap"
    assert City.model_validate({**raw, "country": "JP"}).map_provider == "osm"
    assert City.model_validate({**raw, "map_provider": "osm", "country": "CN"}).map_provider == "osm"   # 写了就听写的
    assert load_city("paris").map_provider == "osm"


# ---- 配额保护 ------------------------------------------------------------ #

@pytest.fixture
def usage(tmp_path):
    return quota.Usage(Database(tmp_path / "t.db"), clock=lambda: "2026-11")


def test_our_own_caps_stay_far_below_the_free_allowance():
    for service, cap in quota.CAPS.items():
        assert cap <= quota.FREE_ALLOWANCE[service] * 0.4, service
    assert quota.FREE_ALLOWANCE["search"] == 5000 and quota.FREE_ALLOWANCE["lbs"] == 150_000


def test_calls_are_counted_per_service_and_month(usage):
    assert usage.used("search") == 0
    usage.record("search")
    usage.record("search")
    usage.record("lbs")
    assert (usage.used("search"), usage.used("lbs")) == (2, 1)
    later = quota.Usage(usage.db, clock=lambda: "2026-12")
    assert later.used("search") == 0                      # 新的一个月重新数


def test_a_service_stops_at_its_own_cap_and_the_rest_keep_working(usage):
    cap = quota.CAPS["search"]
    for _ in range(cap):
        assert usage.allow("search")
        usage.record("search")
    assert not usage.allow("search")                      # 到自留上限：当月不再调高德
    assert usage.allow("lbs")
    assert usage.used("search") == cap                    # 被拒绝的不计数


def test_the_warning_shows_from_eighty_percent_of_the_cap(usage):
    snap = {s["service"]: s for s in usage.snapshot()}
    assert snap["search"]["warn"] is False and snap["search"]["blocked"] is False
    for _ in range(int(quota.CAPS["search"] * 0.8)):
        usage.record("search")
    snap = {s["service"]: s for s in usage.snapshot()}
    assert snap["search"]["warn"] is True and snap["search"]["blocked"] is False
    for _ in range(quota.CAPS["search"]):
        usage.record("search")
    assert {s["service"]: s for s in usage.snapshot()}["search"]["blocked"] is True


def test_the_snapshot_never_contains_a_key(usage):
    assert "key" not in str(usage.snapshot()).lower()


# ---- 接口 ---------------------------------------------------------------- #

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.versions import PlanStore
    for name in ("AMAP_WEB_KEY", "AMAP_JS_KEY", "AMAP_JS_SECRET"):
        monkeypatch.setenv(name, "")                       # 不让本机 .env 里的真 Key 混进测试
    return TestClient(create_app(store=PlanStore(tmp_path)))


def test_without_keys_the_map_falls_back_to_osm(client):
    r = client.get("/map/config").json()
    assert r == {"provider": "osm", "reason": "no_key"}


def test_with_keys_the_front_end_gets_what_the_js_api_needs_and_the_load_is_counted(client, monkeypatch):
    monkeypatch.setenv("AMAP_JS_KEY", "jskey")
    monkeypatch.setenv("AMAP_JS_SECRET", "jssecret")
    r = client.get("/map/config").json()
    assert r == {"provider": "amap", "js_key": "jskey", "security_code": "jssecret"}
    usage = {u["service"]: u for u in client.get("/map/usage").json()["services"]}
    assert usage["map_init"]["used"] == 1


def test_the_web_service_key_never_leaves_the_backend(client, monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "webkey")
    monkeypatch.setenv("AMAP_JS_KEY", "jskey")
    monkeypatch.setenv("AMAP_JS_SECRET", "jssecret")
    assert "webkey" not in client.get("/map/config").text + client.get("/map/usage").text


def test_when_the_map_load_cap_is_reached_the_map_falls_back_to_osm(client, monkeypatch):
    monkeypatch.setenv("AMAP_JS_KEY", "jskey")
    monkeypatch.setenv("AMAP_JS_SECRET", "jssecret")
    monkeypatch.setitem(quota.CAPS, "map_init", 2)
    assert client.get("/map/config").json()["provider"] == "amap"
    assert client.get("/map/config").json()["provider"] == "amap"
    assert client.get("/map/config").json() == {"provider": "osm", "reason": "quota"}
    assert {u["service"]: u for u in client.get("/map/usage").json()["services"]}["map_init"]["blocked"] is True


def test_a_chinese_city_is_found_with_or_without_the_shi_suffix():
    paris = load_city("paris")
    city = type(paris).model_validate({**paris.model_dump(), "id": "chongqing", "name_zh": "重庆市",
                                       "name_en": "Chongqing", "aliases": [], "country": "CN"})
    assert city.matches("重庆") and city.matches("重庆市") and city.matches(" Chongqing ")
    assert not city.matches("市") and not city.matches("重")
