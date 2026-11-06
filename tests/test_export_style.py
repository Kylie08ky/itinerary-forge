"""导出（PDF / 长图 / HTML）跟进新的界面风格（规格 M08 S9 后半）。

只看页面的样子和内容没丢：城市配色、标题带、统计卡；内容、顺序、分页不变。
"""

from __future__ import annotations

import re

import pytest

from app.generator import generate_from_theme
from app.library import load_library
from app.renderer import render_html
from app.schemas import TravelRequest


@pytest.fixture(scope="module")
def parts():
    lib = load_library("paris")
    req = TravelRequest(destination="Paris", start_date="2026-10-03", end_date="2026-10-06",
                        budget_level="medium", travel_style="balanced", interests=["art"])
    it = generate_from_theme(req, "art", client=None, library=lib).itinerary
    return req, it


def html(parts, **kw):
    req, it = parts
    return render_html(it, req, **kw)


def test_the_export_uses_the_city_palette_and_no_longer_the_old_orange(parts):
    page = html(parts)
    assert "--accent: #27405f" in page and "--brass: #b88a38" in page and "--bg: #f4efe6" in page
    assert "#b3541e" not in page                                   # 旧版的橙色


def test_a_city_without_its_own_palette_gets_the_neutral_one(parts):
    req, it = parts
    other = it.model_copy(update={"trip": it.trip.model_copy(update={"destination": "Atlantis"})})
    page = render_html(other, req)
    assert "--accent: #2c3e55" in page and "--brass: #b98d3e" in page
    assert "#27405f" not in page


def test_the_header_is_a_dark_band_with_a_serif_title_stats_and_a_brass_line(parts):
    page = html(parts)
    assert re.search(r'<header class="band"', page)
    assert "linear-gradient" in page and "font-family: var(--serif)" in page
    assert page.count('class="stat"') == 4
    assert "<img" not in page.split("</header>")[0]               # 封面不放图片（06 原则 6）


def test_content_order_and_paging_are_unchanged(parts):
    page = html(parts)
    assert page.count('class="day page-break"') == 4
    days = re.findall(r"第 (\d) 天 · ", page)
    assert days == ["1", "2", "3", "4"]
    assert "购票清单" in page and "风险提醒" in page and "出发前请以各景点官网信息为准" in page
    flat = html(parts, page_per_day=False)
    assert "page-break" not in flat.split("<body>")[1]


def test_the_long_image_keeps_its_phone_width_and_gets_the_same_style(parts):
    page = html(parts, image=True)
    assert "max-width: 760px" in page and "--accent: #27405f" in page


def test_printing_to_pdf_uses_plain_white_paper_with_no_fills(parts):
    page = html(parts)
    printed = page.split("@media print")[1].split("</style>")[0]
    assert "body { background: #fff; }" in printed
    assert ".band { background: none" in printed and ".stat, table { background: none; }" in printed
    assert "@media print" not in html(parts, image=True).split("@media print")[0]   # 长图是截屏，不受打印样式影响
