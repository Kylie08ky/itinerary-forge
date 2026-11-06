"""M05 S7d：高德搜库外地点、住宿地址定位。高德的真实请求不进单元测试，用假的 HTTP 函数。"""

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

POI = {"status": "1", "info": "OK", "count": "2", "pois": [
    {"id": "B0FFG33I2E", "name": "解放碑步行街", "address": "民权路177号",
     "adname": "渝中区", "type": "购物服务;特色商业街;特色商业街",
     "location": "106.575329,29.557253", "adcode": "500103", "cityname": "重庆市"},
    {"id": "B00170L5D4", "name": "解放碑", "address": "民族路", "adname": "渝中区", "type": "风景名胜;风景名胜;纪念碑",
     "location": "106.577034,29.557204", "adcode": "500103", "cityname": "重庆市"},
]}
GEO = {"status": "1", "info": "OK", "count": "1", "geocodes": [{"formatted_address": "重庆市渝中区民权路",
                                                              "location": "106.575229,29.554666"}]}


@pytest.fixture
def usage(tmp_path):
    return quota.Usage(Database(tmp_path / "t.db"))


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[str, dict]] = []

    def fake(url, params):
        seen.append((url, params))
        return POI if "place/text" in url else GEO

    monkeypatch.setattr(amap, "_http_get", fake)
    return seen


def test_search_returns_places_in_wgs84_and_counts_one_search(usage, calls):
    hits = amap.AmapClient(lambda: "k", usage).search("解放碑", "重庆")
    assert [h.name for h in hits] == ["解放碑步行街", "解放碑"]
    assert hits[0].address == "民权路177号" and "商业街" in hits[0].type
    gcj = wgs84_to_gcj02(hits[1].lat, hits[1].lng)
    assert (round(gcj[1], 4), round(gcj[0], 4)) == (106.5770, 29.5572)       # 换回去是高德给的坐标
    assert usage.used("search") == 1 and usage.used("lbs") == 0
    assert calls[0][1]["region"] == "重庆" and calls[0][1]["city_limit"] == "true"


def test_amap_results_are_never_cached_so_the_same_search_is_called_and_counted_again(usage, calls):
    c = amap.AmapClient(lambda: "k", usage)
    c.search("解放碑", "重庆")
    c.search(" 解放碑 ", "重庆")
    assert len(calls) == 2 and usage.used("search") == 2          # 高德服务协议不许缓存服务数据（M05 S7b）
    assert not hasattr(amap, "_cache")


def test_no_key_or_cap_means_no_call(usage, calls, monkeypatch):
    assert amap.AmapClient(lambda: "", usage).search("解放碑", "重庆") is None
    assert amap.AmapClient(lambda: "", usage).geocode("民权路", "重庆") is None
    monkeypatch.setitem(quota.CAPS, "search", 1)
    usage.record("search")
    assert amap.AmapClient(lambda: "k", usage).search("磁器口", "重庆") is None
    assert calls == []


def test_network_failure_is_not_counted_and_not_cached(usage, monkeypatch):
    def boom(url, params):
        raise amap.AmapError("net")

    monkeypatch.setattr(amap, "_http_get", boom)
    assert amap.AmapClient(lambda: "k", usage).search("解放碑", "重庆") is None
    assert usage.used("search") == 0


def test_geocode_counts_lbs_and_gives_wgs84(usage, calls):
    hit = amap.AmapClient(lambda: "k", usage).geocode("重庆市渝中区民权路", "重庆")
    assert hit is not None and usage.used("lbs") == 1 and usage.used("search") == 0
    gcj = wgs84_to_gcj02(hit.lat, hit.lng)
    assert (round(gcj[1], 4), round(gcj[0], 4)) == (106.5752, 29.5547)


def test_a_geocode_that_finds_nothing_returns_none(usage, monkeypatch):
    monkeypatch.setattr(amap, "_http_get", lambda u, p: {"status": "1", "count": "0", "geocodes": []})
    assert amap.AmapClient(lambda: "k", usage).geocode("不存在的地址", "重庆") is None


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


def test_lookup_without_a_key_says_why_and_adds_nothing(client):
    r = client.get("/places/lookup", params={"q": "解放碑", "city": "kunming"}).json()
    assert r == {"hits": [], "reason": "no_key"}


def test_lookup_for_a_non_amap_city_is_refused(client, monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "k")
    r = client.get("/places/lookup", params={"q": "卢浮宫", "city": "paris"}).json()
    assert r == {"hits": [], "reason": "not_amap_city"}


def test_lookup_returns_at_most_five_hits_and_never_the_key(client, calls, monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "secretkey")
    body = client.get("/places/lookup", params={"q": "解放碑", "city": "kunming"})
    assert body.status_code == 200 and "secretkey" not in body.text
    hits = body.json()["hits"]
    assert 1 <= len(hits) <= 5
    assert set(hits[0]) == {"id", "name", "address", "area", "type", "category", "lat", "lng"}
    assert hits[0]["category"] == "shopping" and hits[1]["category"] == "sight" and hits[1]["area"] == "渝中区"


def test_lookup_at_the_cap_says_quota(client, calls, monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "k")
    monkeypatch.setitem(quota.CAPS, "search", 0)
    assert client.get("/places/lookup", params={"q": "解放碑", "city": "kunming"}).json() == {
        "hits": [], "reason": "quota"}


def test_lookup_when_amap_is_unreachable_says_unreachable(client, monkeypatch):
    monkeypatch.setenv("AMAP_WEB_KEY", "k")

    def boom(url, params):
        raise amap.AmapError("net")

    monkeypatch.setattr(amap, "_http_get", boom)
    assert client.get("/places/lookup", params={"q": "解放碑", "city": "kunming"}).json() == {
        "hits": [], "reason": "unreachable"}


# ---- 住宿地址定位 -------------------------------------------------------- #

class _Dead:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        from app.llm import LLMError
        raise LLMError("模型没启动")


@pytest.fixture
def stay_client(tmp_path, monkeypatch):
    for name in ("AMAP_JS_KEY", "AMAP_JS_SECRET"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("AMAP_WEB_KEY", "k")
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path / "generated"))
    reload_cities()
    write_small_city("kunming", "昆明", "Kunming", (25.0389, 102.7183), "CN", "Asia/Shanghai", "CNY",
                     date(2026, 10, 31))
    osm_calls: list[str] = []

    def fake_osm(address, *, city=None, **kw):
        osm_calls.append(address)
        return None

    monkeypatch.setattr("app.api.nominatim", fake_osm)
    c = TestClient(create_app(store=PlanStore(tmp_path / "plans"), client_factory=_Dead))
    c.osm_calls = osm_calls
    yield c
    reload_cities()


def _kunming_plan(c) -> dict:
    theme = c.get("/themes", params={"city": "kunming"}).json()[0]["id"]
    r = c.post("/plans", json={"destination": "昆明", "start_date": "2026-12-12", "end_date": "2026-12-14",
                               "budget_level": "medium", "travel_style": "balanced", "interests": ["均衡"],
                               "theme": theme})
    assert r.status_code == 201, r.text
    return r.json()


def _put(c, plan, **stay):
    body = {"name": "酒店", "check_in": "2026-12-12", "check_out": "2026-12-14", "area": "五华区", **stay}
    return c.put(f"/plans/{plan['plan_id']}/stays", json={"stays": [body]})


def test_a_domestic_hotel_address_is_located_by_amap_first(stay_client, calls):
    r = _put(stay_client, _kunming_plan(stay_client), address="重庆市渝中区民权路")
    s = r.json()["stays"][0]
    assert s["lat"] is not None and stay_client.osm_calls == []
    assert [u for u, _ in calls] == ["https://restapi.amap.com/v3/geocode/geo"]
    assert not any("估算" in n for n in r.json()["notes"])


def test_when_amap_cannot_locate_it_falls_back_to_osm_then_to_the_area_centre(stay_client, monkeypatch):
    monkeypatch.setattr(amap, "_http_get", lambda u, p: {"status": "1", "count": "0", "geocodes": []})
    r = _put(stay_client, _kunming_plan(stay_client), address="乱写的地址")
    assert stay_client.osm_calls == ["乱写的地址"] and r.json()["stays"][0]["lat"] is None
    assert any("五华区" in n and "估算" in n for n in r.json()["notes"])


def test_a_hotel_in_paris_never_goes_to_amap(tmp_path, monkeypatch, calls):
    monkeypatch.setenv("AMAP_WEB_KEY", "k")
    monkeypatch.setattr("app.api.nominatim", lambda address, *, city=None, **kw: None)
    c = TestClient(create_app(store=PlanStore(tmp_path), client_factory=_Dead))
    p = c.post("/plans", json={"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06",
                               "budget_level": "medium", "travel_style": "balanced", "interests": ["art"],
                               "theme": "art"}).json()
    c.put(f"/plans/{p['plan_id']}/stays", json={"stays": [{"name": "H", "check_in": "2026-10-03",
                                                          "check_out": "2026-10-06", "area": "9区",
                                                          "address": "1 Rue de Rivoli, Paris"}]})
    assert calls == []


# ---- 加入地点库 ---------------------------------------------------------- #

def test_amap_types_become_library_categories():
    for typ, cat in [("风景名胜;风景名胜;纪念碑", "sight"), ("风景名胜;公园广场;城市广场", "park"),
                     ("科教文化服务;博物馆;博物馆", "museum"), ("餐饮服务;中餐厅;火锅店", "food"),
                     ("购物服务;特色商业街;特色商业街", "shopping"), ("体育休闲服务;影剧院;电影院", "entertainment"),
                     ("交通设施服务;停车场;公共停车场", "sight")]:
        assert amap.category_of(typ) == cat, typ


KM = {"id": "B0AAAA1111", "name": "昆明老街", "address": "文明街", "area": "五华区", "category": "sight",
      "lat": 25.0405, "lng": 102.7190}


def test_a_chosen_amap_place_becomes_a_draft_library_place_with_unchecked_hours(stay_client, tmp_path):
    c = stay_client
    r = c.post("/places/custom", json={"city": "kunming", **KM})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["id"].startswith("amap-") and p["name_zh"] == "昆明老街" and p["area"] == "五华区"
    assert "b0aaaa1111" not in p["id"].lower()                           # 不存高德的地点编号
    assert p["data_status"] == "draft" and p["hours_verified"] is False and p["typical_hours"] is None
    assert p["price_eur"] is None and p["duration_min"] == 90 and p["draft_source"]["amap"] == "高德"
    found = c.get("/places/search", params={"q": "昆明老街", "city": "kunming"}).json()
    assert [x["id"] for x in found] == [p["id"]]
    saved = (tmp_path / "generated" / "user-places" / "kunming.json").read_text(encoding="utf-8")
    assert "B0AAAA1111" not in saved and "b0aaaa1111" not in saved.lower()


def test_adding_the_same_amap_place_twice_keeps_one_copy(stay_client):
    c = stay_client
    first = c.post("/places/custom", json={"city": "kunming", **KM}).json()["id"]
    again = c.post("/places/custom", json={"city": "kunming", **{**KM, "id": "OTHERID"}})
    assert again.status_code == 201 and again.json()["id"] == first
    assert len(c.get("/places/search", params={"q": "昆明老街", "city": "kunming"}).json()) == 1


def test_a_point_outside_the_city_or_in_a_non_amap_city_is_refused(stay_client):
    c = stay_client
    far = c.post("/places/custom", json={"city": "kunming", **{**KM, "lat": 29.55, "lng": 106.57}})
    assert far.status_code == 422 and "范围" in far.text
    paris = c.post("/places/custom", json={"city": "paris", **KM})
    assert paris.status_code == 422


def test_an_added_amap_place_is_scheduled_and_carries_the_check_your_own_hours_note(stay_client):
    c = stay_client
    pid = c.post("/places/custom", json={"city": "kunming", **KM}).json()["id"]
    theme = c.get("/themes", params={"city": "kunming"}).json()[0]["id"]
    r = c.post("/plans", json={"destination": "昆明", "start_date": "2026-12-12", "end_date": "2026-12-14",
                               "budget_level": "medium", "travel_style": "balanced", "interests": ["均衡"],
                               "theme": theme, "must_go_ids": [pid]})
    assert r.status_code == 201, r.text
    items = [i for d in r.json()["itinerary"]["days"] for i in d["items"] if i.get("place_id") == pid]
    assert len(items) == 1 and "营业时间没核对" in (items[0]["notes"] or "")
