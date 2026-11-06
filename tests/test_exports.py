"""路线、ICS、KML 导出（规格 M08 S4、M05 S5、roadmap v0.2 第 5 / 8 条）。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from app.exports import render_ics, render_kml
from app.generator import generate_from_theme
from app.geo import DAY_COLORS, Stop, build_route, google_dir_url, google_urls_for_day, haversine_km
from app.library import load_library
from app.schemas import TravelRequest


@pytest.fixture(scope="module")
def lib():
    return load_library("paris")


@pytest.fixture(scope="module")
def itinerary(lib):
    req = TravelRequest(destination="Paris", start_date="2026-10-03", end_date="2026-10-06",
                        budget_level="medium", travel_style="balanced", interests=["art"])
    return generate_from_theme(req, "art", client=None, library=lib).itinerary


def stops(lib, *ids):
    return [Stop(place_id=i, name=lib.places[i].name_zh, lat=lib.places[i].lat, lng=lib.places[i].lng, time="10:00")
            for i in ids]


# --------------------------------------------------------------------------- #
# 路线
# --------------------------------------------------------------------------- #
def test_haversine_matches_a_known_distance():
    # 埃菲尔铁塔 → 巴黎圣母院，地图上直线约 4.1 公里
    d = haversine_km((48.8584, 2.2945), (48.8530, 2.3499))
    assert 3.9 < d < 4.3
    assert haversine_km((48.85, 2.35), (48.85, 2.35)) == 0


def test_route_has_one_entry_per_day_with_distinct_colors_and_library_coordinates(itinerary, lib):
    route = build_route(itinerary, lib)
    assert [d.date for d in route.days] == [d.date.isoformat() for d in itinerary.days]
    assert len({d.color for d in route.days}) == len(route.days)
    for d in route.days:
        assert d.color in DAY_COLORS
        for s in d.stops:
            assert (s.lat, s.lng) == (lib.places[s.place_id].lat, lib.places[s.place_id].lng)


def test_route_stops_follow_the_itinerary_order_and_skip_items_without_coordinates(itinerary, lib):
    route = build_route(itinerary, lib)
    for day, dr in zip(itinerary.days, route.days, strict=True):
        expect = [i.place_id for i in day.items if i.place_id in lib.places]
        assert [s.place_id for s in dr.stops] == expect


def test_route_distance_is_the_sum_of_leg_distances_and_totals_add_up(itinerary, lib):
    route = build_route(itinerary, lib)
    for d in route.days:
        legs = sum(haversine_km((a.lat, a.lng), (b.lat, b.lng)) for a, b in zip(d.stops, d.stops[1:], strict=False))
        assert d.distance_km == pytest.approx(legs)
    assert route.total_km == pytest.approx(sum(d.distance_km for d in route.days))
    assert route.place_count == len({s.place_id for d in route.days for s in d.stops})


def test_google_link_uses_coordinates_origin_destination_and_waypoints_in_order(lib):
    ids = ["louvre", "musee-d-orsay", "sainte-chapelle", "notre-dame"]
    ids = [i for i in ids if i in lib.places][:4]
    assert len(ids) >= 3
    s = stops(lib, *ids)
    q = parse_qs(urlparse(google_dir_url(s)).query)
    assert q["origin"] == [f"{s[0].lat:.6f},{s[0].lng:.6f}"]
    assert q["destination"] == [f"{s[-1].lat:.6f},{s[-1].lng:.6f}"]
    assert q["waypoints"] == ["|".join(f"{x.lat:.6f},{x.lng:.6f}" for x in s[1:-1])]
    assert "key=" not in google_dir_url(s)                  # 不需要 Key


def test_google_link_needs_at_least_two_stops(lib):
    with pytest.raises(ValueError):
        google_dir_url(stops(lib, next(iter(lib.places))))
    assert google_urls_for_day(stops(lib, next(iter(lib.places)))) == []


def test_google_links_are_split_when_a_day_has_too_many_stops(lib):
    ids = list(lib.places)[:25]
    s = stops(lib, *ids)
    urls = google_urls_for_day(s)
    assert len(urls) == 3
    for u in urls:
        q = parse_qs(urlparse(u).query)
        assert len(q.get("waypoints", [""])[0].split("|")) <= 9
    # 相邻两段衔接：上一段的终点就是下一段的起点，一个点都不漏
    for a, b in zip(urls, urls[1:], strict=False):
        assert parse_qs(urlparse(a).query)["destination"] == parse_qs(urlparse(b).query)["origin"]
    assert parse_qs(urlparse(urls[0]).query)["origin"] == [f"{s[0].lat:.6f},{s[0].lng:.6f}"]
    assert parse_qs(urlparse(urls[-1]).query)["destination"] == [f"{s[-1].lat:.6f},{s[-1].lng:.6f}"]


# --------------------------------------------------------------------------- #
# ICS
# --------------------------------------------------------------------------- #
def parse_ics(text: str) -> list[dict[str, list[str]]]:
    """最小的 ICS 反解：先把折行接回去，再把每个 VEVENT 拆成「属性 -> 值」。"""
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    unfolded = re.sub(r"\r\n[ \t]", "", text)
    events: list[dict[str, list[str]]] = []
    cur: dict[str, list[str]] | None = None
    depth = 0
    for line in unfolded.split("\r\n"):
        if line == "BEGIN:VEVENT":
            cur, depth = {}, 0
        elif line == "END:VEVENT":
            events.append(cur or {})
            cur = None
        elif cur is not None:
            if line == "BEGIN:VALARM":
                depth += 1
                cur.setdefault("VALARM", []).append("")
            elif line == "END:VALARM":
                depth -= 1
            elif depth == 0 and ":" in line:
                key, _, val = line.partition(":")
                cur.setdefault(key.split(";")[0], []).append(val)
            elif depth > 0:
                key, _, val = line.partition(":")
                cur["VALARM"][-1] += f"{key}={val}|"
    return events


@pytest.fixture(scope="module")
def ics(itinerary, lib):
    return render_ics(itinerary, uid_prefix="plan-x", library=lib, now=datetime(2026, 10, 1, tzinfo=UTC))


def test_ics_is_a_wellformed_calendar_with_a_paris_timezone(ics):
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "TZID:Europe/Paris" in ics and "BEGIN:VTIMEZONE" in ics
    assert "DTSTART;TZID=Europe/Paris:" in ics


def test_ics_has_one_event_per_itinerary_item_with_unique_ids(ics, itinerary):
    events = parse_ics(ics)
    assert len(events) == sum(len(d.items) for d in itinerary.days)
    uids = [e["UID"][0] for e in events]
    assert len(set(uids)) == len(uids)


def test_ics_lines_are_folded_to_75_bytes_and_chinese_is_not_cut_in_half(ics):
    for raw in ics.split("\r\n"):
        assert len(raw.encode("utf-8")) <= 75
    assert "�" not in ics
    ics.encode("utf-8").decode("utf-8")                       # 不会因为切断多字节字符而解码失败


def test_ics_event_times_and_durations_match_the_itinerary(ics, itinerary):
    events = parse_ics(ics)
    k = 0
    for day in itinerary.days:
        for item in day.items:
            e = events[k]
            k += 1
            hh, mm = item.time.split(":")
            assert e["DTSTART"][0] == f"{day.date:%Y%m%d}T{hh}{mm}00"
            s = datetime.strptime(e["DTSTART"][0], "%Y%m%dT%H%M%S")
            t = datetime.strptime(e["DTEND"][0], "%Y%m%dT%H%M%S")
            assert (t - s).total_seconds() / 60 == max(item.duration_minutes, 15)


def test_ics_places_carry_location_and_coordinates(ics, itinerary, lib):
    events = parse_ics(ics)
    k = 0
    seen_geo = 0
    for day in itinerary.days:
        for item in day.items:
            e = events[k]
            k += 1
            if item.place_id in lib.places:
                p = lib.places[item.place_id]
                assert e["GEO"] == [f"{p.lat:.6f};{p.lng:.6f}"]
                assert p.name_zh in e["LOCATION"][0]
                seen_geo += 1
    assert seen_geo > 0


def test_ics_marks_must_book_places_with_a_day_before_reminder(ics, itinerary):
    must_titles = {b.title for b in itinerary.booking_checklist if b.must_online}
    assert must_titles, "艺术主题行程里应该有必须线上购票的点"
    events = parse_ics(ics)
    flagged = [e for e in events if any("TRIGGER=-P1D" in a for a in e["VALARM"])]
    assert flagged, "必须线上购票的点没有带「前一天提醒」"
    for e in flagged:
        assert "需预订" in "".join(e["VALARM"])
        assert "必须提前线上购票" in e["DESCRIPTION"][0]
    # 不需要预约的点只有「开始前 30 分钟」提醒
    plain = [e for e in events if e not in flagged]
    assert plain and all(len(e["VALARM"]) == 1 and "TRIGGER=-PT30M" in e["VALARM"][0] for e in plain)


def test_ics_escapes_commas_semicolons_and_newlines(itinerary, lib):
    odd = itinerary.model_copy(deep=True)
    odd.days[0].items[0].title = "A, B; C"
    odd.days[0].items[0].notes = "第一行\n第二行, 有逗号"
    text = render_ics(odd, uid_prefix="x", library=lib)
    assert "SUMMARY:A\\, B\\; C" in text
    assert "第一行\\n第二行\\, 有逗号" in re.sub(r"\r\n[ \t]", "", text)


# --------------------------------------------------------------------------- #
# KML
# --------------------------------------------------------------------------- #
def test_kml_is_valid_xml_with_a_folder_per_day_and_a_placemark_per_stop(itinerary, lib):
    route = build_route(itinerary, lib)
    root = ET.fromstring(render_kml(itinerary, route).encode("utf-8"))
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    folders = root.findall(".//k:Folder", ns)
    assert len(folders) == len(itinerary.days)
    for f, d in zip(folders, route.days, strict=True):
        points = f.findall("k:Placemark/k:Point", ns)
        assert len(points) == len(d.stops)
        lines = f.findall("k:Placemark/k:LineString", ns)
        assert len(lines) == (1 if len(d.stops) >= 2 else 0)


def test_kml_coordinates_are_lng_lat_and_colors_are_abgr(itinerary, lib):
    route = build_route(itinerary, lib)
    text = render_kml(itinerary, route)
    first = route.days[0].stops[0]
    assert f"{first.lng:.6f},{first.lat:.6f},0" in text           # KML 是「经度,纬度」，和 Google 链接相反
    c = route.days[0].color                                        # #rrggbb -> ffbbggrr
    assert f"<color>ff{c[5:7]}{c[3:5]}{c[1:3]}</color>" in text
