"""M05 S7e：国内行程的导航链接用高德（网页 URI，不要 Key、不耗配额），境外仍用 Google。"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest

from app.generator import generate_from_theme
from app.geo import Stop, amap_urls_for_day, build_route
from app.library import load_library
from app.maps.coords import wgs84_to_gcj02
from app.schemas import TravelRequest

SHANGHAI = [("人民广场", 31.2304, 121.4737), ("外滩", 31.2397, 121.4900), ("豫园", 31.2272, 121.4920)]


def _stops(points=SHANGHAI):
    return [Stop(place_id=f"p{i}", name=n, lat=la, lng=ln, time="10:00") for i, (n, la, ln) in enumerate(points)]


def test_a_mainland_day_links_to_amap_with_gcj02_coordinates_and_names():
    [url] = amap_urls_for_day(_stops())
    assert url.startswith("https://uri.amap.com/marker?")
    q = parse_qs(urlparse(url).query)
    marks = q["markers"][0].split("|")
    assert [m.split(",")[2] for m in marks] == ["人民广场", "外滩", "豫园"]
    lat, lng = wgs84_to_gcj02(31.2304, 121.4737)
    assert marks[0].split(",")[:2] == [f"{lng:.6f}", f"{lat:.6f}"]          # 高德用 GCJ-02，不是库里的 WGS-84
    assert "key=" not in unquote(url)


def test_amap_links_hold_at_most_ten_points_and_skip_a_single_point():
    assert amap_urls_for_day(_stops(SHANGHAI[:1])) == []
    many = _stops([(f"点{i}", 31.2 + i * 0.001, 121.4) for i in range(23)])
    urls = amap_urls_for_day(many)
    assert [len(parse_qs(urlparse(u).query)["markers"][0].split("|")) for u in urls] == [10, 10, 3]


def test_names_cannot_break_the_marker_separators():
    [url] = amap_urls_for_day(_stops([("A|B", 31.23, 121.47), ("C,D", 31.24, 121.48)]))
    marks = parse_qs(urlparse(url).query)["markers"][0].split("|")
    assert len(marks) == 2 and all(len(m.split(",")) == 3 for m in marks)


@pytest.fixture(scope="module")
def paris_trip():
    lib = load_library("paris")
    req = TravelRequest(destination="Paris", start_date="2026-10-03", end_date="2026-10-05",
                        budget_level="medium", travel_style="balanced", interests=["art"])
    return lib, generate_from_theme(req, "art", client=None, library=lib).itinerary


def test_paris_keeps_google_links_and_has_no_amap_links(paris_trip):
    lib, it = paris_trip
    route = build_route(it, lib)
    assert all(d.amap_urls == [] for d in route.days)
    assert any(d.google_urls for d in route.days)
    assert all("amap_urls" not in d for d in route.as_dict()["days"])          # 巴黎的接口输出一个字都不变


def test_the_same_places_moved_into_china_get_amap_links_instead_of_google(paris_trip):
    lib, it = paris_trip
    moved = lib.model_copy(deep=True)
    for i, p in enumerate(moved.places.values()):
        p.lat, p.lng = 31.2304 + i * 0.0004, 121.4737 + i * 0.0004
    route = build_route(it, moved)
    days = [d for d in route.days if len(d.stops) >= 2]
    assert days and all(d.google_urls == [] and d.amap_urls for d in days)
    assert all(u.startswith("https://uri.amap.com/marker") for d in days for u in d.amap_urls)
    assert all(d["amap_urls"] for d in route.as_dict()["days"] if len(d["stops"]) >= 2)
