"""M17 S1：城市是一份配置；巴黎的配置和之前写死的值一致。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.cities import City, city_id_of, city_of, list_cities, load_city, supported_cities


def test_paris_config_matches_what_used_to_be_hard_coded():
    c = load_city("paris")
    assert c.center == (48.8566, 2.3522)                                        # 原来的 PARIS_CENTER / CITY_CENTERS
    assert c.bbox.lat == (48.80, 48.92) and c.bbox.lng == (2.22, 2.47)           # 原来的 _PARIS_LAT / _PARIS_LNG
    assert c.timezone == "Europe/Paris" and c.utc_offset == 1 and c.dst == "eu"
    assert c.currency == "EUR" and c.country == "FR" and c.coord_system == "wgs84"
    assert c.geocode_viewbox == "1.9,49.2,2.9,48.5"
    assert c.residency_options == ["fr", "eu"]


@pytest.mark.parametrize("text", ["Paris", "paris", " PARIS ", "巴黎"])
def test_a_destination_is_recognised_by_id_name_or_alias(text):
    assert city_of(text).id == "paris" and city_id_of(text) == "paris"


def test_an_unknown_destination_is_not_a_city():
    assert city_of("Tokyo") is None
    assert city_id_of("Tokyo") == "tokyo"          # 原样小写，让后面「没有这座城市的数据」的报错说清楚


def test_every_configured_city_has_its_data_files_and_is_listed():
    ids = [c.id for c in list_cities()]
    assert "paris" in ids and ids == sorted(ids)
    assert [c.id for c in supported_cities()] == [c.id for c in list_cities() if c.supported]
    from app.library import load_library
    for c in supported_cities():
        lib = load_library(c.id)
        assert lib.places and lib.themes, c.id


def test_the_config_rejects_unknown_fields_and_bad_values():
    base = load_city("paris").model_dump()
    with pytest.raises(ValidationError):
        City.model_validate({**base, "oops": 1})
    with pytest.raises(ValidationError):
        City.model_validate({**base, "currency": "usd"})
    with pytest.raises(ValidationError):
        City.model_validate({**base, "id": "Paris Center"})


# --------------------------------------------------------------------------- #
# 全站不再写死巴黎（M17 S1 / S2）
# --------------------------------------------------------------------------- #
def test_app_code_has_no_hard_coded_city_literals():
    """城市只能从 app/cities.py 和城市配置来：代码里不再出现 "paris" 字面量、写死的时区或中心点。"""
    import re
    from pathlib import Path

    allowed = {                                   # 文件 -> 允许出现的原因
        "cities.py": "默认城市常量",
        "exports.py": "VTIMEZONE 表按 IANA 时区名索引（Europe/Paris 是表的键，不是写死某座城市）",
        "library.py": "老数据文件里 zone: paris 的兼容读取",
    }
    pattern = re.compile(r"""["']paris["']|Europe/Paris|48\.8566|\bPARIS_""")
    bad = []
    for path in sorted((Path(__file__).resolve().parents[1] / "app").rglob("*.py")):
        if path.name in allowed:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                bad.append(f"{path.name}:{n}: {line.strip()[:80]}")
    assert not bad, bad


# --------------------------------------------------------------------------- #
# 接口：城市列表、按城市取数据
# --------------------------------------------------------------------------- #
def _client(tmp_path):
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.versions import PlanStore

    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans")))


def test_cities_endpoint_lists_the_supported_cities(tmp_path):
    got = _client(tmp_path).get("/cities").json()
    paris = next(c for c in got if c["id"] == "paris")
    assert paris["name_zh"] == "巴黎" and paris["currency"] == "EUR" and paris["timezone"] == "Europe/Paris"
    assert paris["residency_options"] == ["fr", "eu"] and paris["map_provider"] == "osm"


def test_library_routes_default_to_paris_and_reject_unknown_cities(tmp_path):
    client = _client(tmp_path)
    assert client.get("/places").json() == client.get("/places", params={"city": "paris"}).json()
    r = client.get("/themes", params={"city": "atlantis"})
    assert r.status_code == 404 and "paris" in r.json()["detail"]                    # 说清楚现在支持哪些


# --------------------------------------------------------------------------- #
# 时区、日落：按城市配置算（给国内城市用）
# --------------------------------------------------------------------------- #
def test_a_city_without_daylight_saving_keeps_its_standard_offset():
    from datetime import date

    from app.scheduler.sun import sunset_minutes, utc_offset_hours

    assert utc_offset_hours(date(2026, 7, 1), 8, "none") == 8 and utc_offset_hours(date(2026, 1, 1), 8, "none") == 8
    assert utc_offset_hours(date(2026, 7, 1)) == 2 and utc_offset_hours(date(2026, 1, 1)) == 1      # 默认巴黎不变
    # 北京夏至日落约 19:46（UTC+8，没有夏令时）
    got = sunset_minutes(date(2026, 6, 21), 39.9, 116.4, utc_std=8, dst="none")
    assert abs(got - (19 * 60 + 46)) <= 6, got


def test_the_ics_timezone_table_covers_the_configured_timezones():
    from app.exports import _VTIMEZONES

    for city in list_cities():
        assert city.timezone in _VTIMEZONES, city.timezone
    cst = _VTIMEZONES["Asia/Shanghai"]
    assert cst[0] == "BEGIN:VTIMEZONE" and cst[-1] == "END:VTIMEZONE" and "TZOFFSETTO:+0800" in cst
    assert sum(x.startswith("BEGIN:") for x in cst) == sum(x.startswith("END:") for x in cst)


def test_the_arrival_line_names_the_trip_city():
    from app.generator import generate_from_theme
    from app.library import load_library
    from app.schemas import TravelRequest

    req = TravelRequest.model_validate({
        "destination": "巴黎", "start_date": "2026-10-03", "end_date": "2026-10-05", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "arrival_time": "2026-10-03T10:30:00"})
    assert req.city_id == "paris" and req.city.name_zh == "巴黎"
    it = generate_from_theme(req, "art", client=None, library=load_library("paris")).itinerary
    assert any(i.title == "抵达巴黎并前往住宿" for i in it.days[0].items)
