"""PDF 封面统计、全程分色路线图、每天一页（规格 M08 S2 / S3）。只测 HTML；PDF 是 HTML 的打印结果。"""

from __future__ import annotations

import re

import pytest

from app.generator import generate_from_theme
from app.geo import build_route
from app.library import load_library
from app.renderer import cover_stats, render_html, route_svg
from app.schemas import TravelRequest
from app.validator import Finding, Severity, ValidationReport


@pytest.fixture(scope="module")
def lib():
    return load_library("paris")


@pytest.fixture(scope="module")
def request_():
    return TravelRequest(destination="Paris", start_date="2026-10-03", end_date="2026-10-06",
                         budget_level="medium", travel_style="balanced", interests=["art"])


@pytest.fixture(scope="module")
def itinerary(lib, request_):
    return generate_from_theme(request_, "art", client=None, library=lib).itinerary


def test_cover_stats_count_days_places_distance_and_tickets(itinerary, lib):
    s = cover_stats(itinerary, lib)
    route = build_route(itinerary, lib)
    assert s["days"] == len(itinerary.days)
    assert s["places"] == route.place_count
    assert s["distance_km"] == round(route.total_km, 1)
    prices = {i.place_id: lib.places[i.place_id].price_eur
              for d in itinerary.days for i in d.items if i.place_id in lib.places}
    assert s["ticket_eur"] == pytest.approx(sum(p for p in prices.values() if p))


def test_html_cover_shows_the_stats(itinerary, request_):
    html = render_html(itinerary, request_)
    cover = html.split('class="cover"', 1)[1].split("</section>", 1)[0]
    assert f"{len(itinerary.days)}" in cover
    for label in ("天", "地点", "公里", "门票"):
        assert label in cover
    assert "直线" in html                          # 距离是直线累计，必须说清楚，不能冒充实际步行距离


def test_html_has_one_colored_polyline_per_day_and_a_legend(itinerary, request_, lib):
    html = render_html(itinerary, request_)
    route = build_route(itinerary, lib)
    svg = html.split("<svg", 1)[1].split("</svg>", 1)[0]
    drawn_days = [d for d in route.days if len(d.stops) >= 2]
    assert svg.count("<polyline") == len(drawn_days)
    for d in route.days:
        assert d.color in html
    legend = html.split('class="legend"', 1)[1].split("</div>", 1)[0] if 'class="legend"' in html else ""
    assert legend.count("第") >= len(route.days)


def test_route_svg_places_every_stop_inside_the_canvas(itinerary, lib):
    svg = route_svg(build_route(itinerary, lib))
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    w, h = int(m.group(1)), int(m.group(2))
    circles = re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', svg)
    assert circles
    assert all(0 <= float(x) <= w and 0 <= float(y) <= h for x, y in circles)


def test_each_day_starts_on_a_new_page_unless_turned_off(itinerary, request_):
    on = render_html(itinerary, request_)
    off = render_html(itinerary, request_, page_per_day=False)
    assert on.count('class="day page-break"') == len(itinerary.days)      # 封面 / 路线图在第一页，每天各自一页
    assert 'class="day page-break"' not in off
    assert off.count('class="day"') == len(itinerary.days)


def test_cover_flags_unresolved_errors(itinerary, request_):
    report = ValidationReport(findings=[
        Finding(code="x", severity=Severity.error, message="m"),
        Finding(code="y", severity=Severity.error, message="m"),
    ])
    html = render_html(itinerary, request_, report)
    assert "含 2 个未处理问题" in html
    assert "未处理问题" not in render_html(itinerary, request_, ValidationReport(findings=[]))


# ---- M08 S2c：封面路线图带街道底图（OpenStreetMap），拿不到就退回示意图 ----
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002"
                    "0001e221bc330000000049454e44ae426082")


def test_the_cover_map_has_a_street_base_map_with_attribution(itinerary, request_, monkeypatch):
    from app.maps import basemap

    asked: list[tuple[int, int, int]] = []
    monkeypatch.setattr(basemap, "_download", lambda z, x, y: (asked.append((z, x, y)), PNG)[1])
    html = render_html(itinerary, request_)
    svg = html.split("<svg", 1)[1].split("</svg>", 1)[0]
    assert svg.count("<image") == len(asked) and 1 <= len(asked) <= 16
    assert "data:image/png;base64," in svg                       # 瓦片直接嵌进文件，离线打开也有图
    assert "© OpenStreetMap contributors" in svg
    assert svg.count("<circle") == sum(len(d.stops) for d in build_route(itinerary).days)
    again = len(asked)
    render_html(itinerary, request_)
    assert len(asked) == again                                   # 第二次导出用本机缓存，不再下载


def test_without_the_base_map_the_cover_falls_back_to_the_schematic_map(itinerary, request_):
    html = render_html(itinerary, request_)                      # conftest：测试里瓦片下不到
    svg = html.split("<svg", 1)[1].split("</svg>", 1)[0]
    assert "<image" not in svg and "OpenStreetMap" not in svg


def test_one_failed_tile_means_no_base_map_at_all(itinerary, request_, monkeypatch):
    from app.maps import basemap

    calls = iter([PNG])
    monkeypatch.setattr(basemap, "_download", lambda z, x, y: next(calls, None))
    assert "<image" not in render_html(itinerary, request_)
