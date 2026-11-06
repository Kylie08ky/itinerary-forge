"""圣诞市场当季活动（规格 08 3.4 / R0-e）：数据文件和地点库一致，日期带来源，只在期内排得进行程。"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from fetch_local_names import haversine_m  # noqa: E402

from app.cities import load_city  # noqa: E402
from app.library import load_library  # noqa: E402

MARKETS = json.loads((Path(__file__).resolve().parents[1] / "data" / "seasonal" / "christmas-markets.json")
                     .read_text(encoding="utf-8"))["markets"]
# 只有我亲自打开官方网站核对过的才写 official
OFFICIAL_HOSTS = ("heidelberg-marketing.de", "noel.strasbourg.eu")


def test_every_market_has_dates_a_source_and_says_how_it_was_confirmed():
    assert len(MARKETS) >= 7
    for m in MARKETS:
        assert m["start"] < m["end"] and m["start"].startswith("2026-") and m["source_url"].startswith("https://")
        assert m["confirmed_by"] in ("official", "listing") and m["checked_at"] == "2026-10-25"
        assert (m["confirmed_by"] == "official") == any(h in m["source_url"] for h in OFFICIAL_HOSTS), m["city"]


@pytest.mark.parametrize("m", MARKETS, ids=[m["city"] for m in MARKETS])
def test_the_market_is_a_place_in_its_city_with_the_same_season_and_is_a_top_pick(m):
    lib = load_library(m["city"])
    p = lib.places[f"{m['city']}-weihnachtsmarkt"]
    assert p.season.start == date.fromisoformat(m["start"]) and p.season.end == date.fromisoformat(m["end"])
    assert str(p.season.source_url) == m["source_url"] and p.season.confirmed_by == m["confirmed_by"]
    assert p.name_local == m["name_local"] and p.data_status == "draft"
    assert [x.place_id for x in lib.themes["daytrip"].members][0] == p.id
    c = load_city(m["city"])
    assert haversine_m(c.center[0], c.center[1], p.lat, p.lng) <= 1500
    assert p.closed_reason(date.fromisoformat(m["start"])) is None
    assert p.closed_reason(date.fromisoformat(m["end"])) is None
    assert p.closed_reason(date(2026, 7, 4)) is not None
