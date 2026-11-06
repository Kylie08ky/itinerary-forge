"""M02 S9 / M01 S15：名气分，以及「均衡」模式按全城名气选点（规格 M34、M35、M36）。"""

from __future__ import annotations

from datetime import date

import pytest
from city_fixtures import write_small_city

from app.cities import reload_cities
from app.citybuild.core import build_data
from app.generator import generate_from_theme
from app.library import load_library
from app.schemas import TravelRequest


def _raws():
    from city_fixtures import _raw

    return [_raw(f"Q{900 + i}", f"景点{i}", f"Sight {i}", 25.0 + 0.004 * i, 102.0 + 0.004 * (i % 3), 60 - 5 * i,
                 cls="Q33506" if i % 3 == 0 else "Q570116", cat="museum" if i % 3 == 0 else "sight")
            for i in range(8)]


def test_a_built_place_keeps_its_wikipedia_language_count_as_the_fame_score():
    places, _themes = build_data("x", "某城", _raws(), date(2026, 11, 4))
    by_name = {p["name_en"]: p for p in places}
    assert by_name["Sight 0"]["fame"] == {"sitelinks": 60, "source": "wikidata", "checked_at": "2026-11-04"}
    assert by_name["Sight 3"]["fame"]["sitelinks"] == 45


@pytest.fixture
def city(tmp_path, monkeypatch):
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path))
    reload_cities()
    write_small_city("kunming", "昆明", "Kunming", (25.0389, 102.7183), "CN", "Asia/Shanghai", "CNY",
                     date(2026, 10, 31))
    yield load_library("kunming")
    reload_cities()


def test_the_library_ranks_the_places_by_fame_and_says_when_it_can_be_used(city):
    rank = city.fame_ranking()
    assert city.fame_ready() and sorted(rank.values()) == list(range(1, len(rank) + 1))
    first = min(rank, key=rank.get)
    assert city.places[first].fame.sitelinks == max(p.fame.sitelinks for p in city.places.values())
    for p in city.places.values():
        p.fame = None
    assert not city.fame_ready()                       # 没有名气分（老城市）：照旧按主题


def test_balanced_picks_the_most_famous_places_and_says_why(city):
    req = TravelRequest(destination="昆明", start_date="2026-11-07", end_date="2026-11-08", budget_level="medium",
                        travel_style="balanced", interests=["均衡"],
                        themes=[{"id": t, "weight": 3} for t in city.themes])          # 「均衡」= 全部主题、权重一样
    trip = generate_from_theme(req, next(iter(city.themes)), client=None, library=city)
    gen = trip.itinerary.generation
    placed = [i.place_id for d in trip.itinerary.days for i in d.items if i.place_id]
    rank = city.fame_ranking()
    assert placed and max(rank[p] for p in placed) <= len(placed) + 3          # 排进去的都是名气靠前的
    reasons = " ".join(r for rs in gen.day_reasons.values() for r in rs)
    assert "名气——维基百科" in reasons and "全城第" in reasons
    assert any("选点标准" in n for n in gen.notes)


def test_a_specific_theme_still_works_by_theme_not_by_fame(city):
    theme = next(iter(city.themes))
    req = TravelRequest(destination="昆明", start_date="2026-11-07", end_date="2026-11-08", budget_level="medium",
                        travel_style="balanced", interests=["均衡"])
    gen = generate_from_theme(req, theme, client=None, library=city).itinerary.generation
    reasons = " ".join(r for rs in gen.day_reasons.values() for r in rs)
    assert "名气——" not in reasons and not any("选点标准" in n for n in gen.notes)
