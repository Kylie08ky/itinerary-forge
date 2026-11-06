"""规格 10 H21 / M02 S9：维基导游与维基百科浏览量（场景 M42–M44）。"""
# ruff: noqa: E501

from __future__ import annotations

from datetime import date

import pytest
from city_fixtures import _raw, write_small_city

from app.cities import reload_cities
from app.citybuild.core import build_data
from app.citybuild.voyage import chinese_name, parse_listings, views_total, window
from app.library import Fame, VoyageMark, Zone, load_library

WIKITEXT = """
Intro {{pagebanner|x.jpg}}
{{see
| name=Kaiyuan Temple | alt=开元寺 | url= | lat=24.9168 | long=118.581 | price=¥10 | wikidata=Q1721819
| content=The largest temple.
}}
{{see | name=Guan-Yue Temple | alt=Tonghuai Temple of Guan Yu and Yue Fei (通淮关岳庙) | lat= | long= | content=A [[Taoist|Daoist]] temple }}
{{eat | name=Bart's Kitchen | alt=椰林阁娘惹私房菜 | url=http://x.example/a {{dead link|December 2020}} | lat= | long= | lastedit=2015-03-04 }}
{{listing | type=eat | name=Noodle House | alt=面馆 | lat=24.9 | long=118.6 }}
{{sleep | name=Some Hotel | alt=酒店 }}
{{see | alt=只有别名没有名字 }}
{{Regionlist | region1name=Foo}}
"""


def test_listings_are_read_with_nested_templates_and_kinds():
    got = parse_listings(WIKITEXT)
    assert [(i.kind, i.name) for i in got] == [("see", "Kaiyuan Temple"), ("see", "Guan-Yue Temple"),
                                               ("eat", "Bart's Kitchen"), ("eat", "Noodle House")]
    kaiyuan = got[0]
    assert (kaiyuan.lat, kaiyuan.lng, kaiyuan.wikidata) == (24.9168, 118.581, "Q1721819")
    assert got[1].lat is None and got[1].wikidata == ""
    assert got[2].url == "http://x.example/a" and got[2].lastedit == "2015-03-04"      # {{dead link}} 不算网址


def test_the_chinese_name_comes_from_the_alias_first():
    got = {i.name: i for i in parse_listings(WIKITEXT)}
    assert chinese_name(got["Kaiyuan Temple"]) == "开元寺"
    assert chinese_name(got["Guan-Yue Temple"]) == "通淮关岳庙"
    assert chinese_name(parse_listings("{{see | name=Statue | alt=lit.: No Name }}")[0]) == ""


def test_the_views_window_is_the_last_twelve_full_months():
    assert window(date(2026, 11, 4)) == ("20251101", "20261001")
    assert window(date(2026, 1, 15)) == ("20250101", "20251201")
    assert views_total([{"views": 100}, {"views": 25}]) == 125


def _raws():
    return [_raw(f"Q{800 + i}", f"景点{i}", f"Sight {i}", 25.0 + 0.004 * i, 102.0, 10, cls="Q570116", cat="sight")
            for i in range(3)]


def test_views_and_the_voyage_mark_are_kept_on_the_built_place():
    raws = _raws()
    raws[0].update(views=14018, views_period="2025-11~2026-10",
                   voyage={"kind": "see", "url": "https://en.wikivoyage.org/wiki/Quanzhou", "revid": 5415382})
    places, _ = build_data("x", "某城", raws, date(2026, 11, 4))
    first = next(p for p in places if p["name_en"] == "Sight 0")
    assert first["fame"] == {"sitelinks": 10, "source": "wikidata", "checked_at": "2026-11-04",
                             "views": 14018, "views_period": "2025-11~2026-10"}
    assert first["voyage"]["kind"] == "see" and first["voyage"]["checked_at"] == "2026-11-04"
    other = next(p for p in places if p["name_en"] == "Sight 1")
    assert "views" not in other["fame"] and "voyage" not in other                 # 没有就不写，不编
    Fame.model_validate(first["fame"])                                              # 字段能被地点库装载
    VoyageMark.model_validate(first["voyage"])


@pytest.fixture
def city(tmp_path, monkeypatch):
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path))
    reload_cities()
    write_small_city("kunming", "昆明", "Kunming", (25.0389, 102.7183), "CN", "Asia/Shanghai", "CNY",
                     date(2026, 10, 31))
    yield load_library("kunming")
    reload_cities()


def test_equal_language_counts_are_ranked_by_views(city):
    core = [p for p in city.places.values() if p.fame and p.zone == Zone.core]
    a, b, c = core[:3]
    for p in (a, b, c):
        p.fame = Fame(sitelinks=99, checked_at=date(2026, 11, 4))
    b.fame = Fame(sitelinks=99, views=9000, checked_at=date(2026, 11, 4))
    c.fame = Fame(sitelinks=99, views=100, checked_at=date(2026, 11, 4))
    rank = city.fame_ranking()
    assert [rank[b.id], rank[c.id], rank[a.id]] == [1, 2, 3]            # 浏览量高的在前，没有浏览量的最后
