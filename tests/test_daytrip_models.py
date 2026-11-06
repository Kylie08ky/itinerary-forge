"""周边一日游的数据模型（规格 08 3.4）：城市分层、地点的草稿标记 / 具体日期闭馆 / 当季活动期。"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.cities import City, load_city
from app.library import Place


def _place(**over) -> Place:
    base = dict(id="x-place", name_zh="测试点", name_en="Test", category="sight", setting="outdoor", area="老城",
                lat=48.86, lng=2.34, duration_min=60)
    base.update(over)
    return Place.model_validate(base)


CITY = {"id": "tuebingen", "name_zh": "蒂宾根", "name_en": "Tübingen", "local_language": "de", "aliases": ["tübingen"],
        "country": "DE", "currency": "EUR", "timezone": "Europe/Berlin", "utc_offset": 1, "dst": "eu",
        "center": [48.5216, 9.0576], "bbox": {"lat": [48.48, 48.56], "lng": [8.98, 9.14]},
        "suggested_days": {"min": 1, "max": 1, "quality_max": 1, "note": "一日游"}}


# --------------------------------------------------------------------------- #
# 城市分层
# --------------------------------------------------------------------------- #
def test_paris_is_a_full_city_and_cities_default_to_full():
    assert load_city("paris").tier == "full"
    assert City.model_validate(CITY).tier == "full"


def test_a_light_city_is_a_day_trip_destination_and_must_say_its_region():
    c = City.model_validate({**CITY, "tier": "light", "region": "suedwest-de"})
    assert c.tier == "light" and c.region == "suedwest-de"
    with pytest.raises(ValidationError, match="region"):
        City.model_validate({**CITY, "tier": "light"})
    with pytest.raises(ValidationError):
        City.model_validate({**CITY, "tier": "huge"})


# --------------------------------------------------------------------------- #
# 地点：草稿标记
# --------------------------------------------------------------------------- #
def test_places_are_verified_by_default_and_drafts_say_where_they_came_from():
    assert _place().data_status == "verified"
    src = {"osm": "node/243874785", "wikidata": "Q61058", "built_at": "2026-10-25"}
    d = _place(data_status="draft", draft_source=src)
    assert d.data_status == "draft" and d.draft_source.wikidata == "Q61058"
    with pytest.raises(ValidationError, match="draft_source"):
        _place(data_status="draft")                                     # 草稿必须带来源和日期
    with pytest.raises(ValidationError):
        _place(data_status="guessed")


# --------------------------------------------------------------------------- #
# 地点：具体日期闭馆（R0-b）和当季活动期（R0-e）
# --------------------------------------------------------------------------- #
def test_fixed_closed_dates_close_the_place_on_that_day_every_year_and_carry_a_note():
    p = _place(closed_dates=["12-25", "01-01"], date_notes={"12-24": "12 月 24 日提前闭馆（出发前请到官网确认）"})
    assert p.closed_reason(date(2026, 12, 25)) == "12 月 25 日闭馆"
    assert p.closed_reason(date(2027, 1, 1)) == "1 月 1 日闭馆"
    assert p.closed_reason(date(2026, 12, 26)) is None
    assert p.note_for(date(2026, 12, 24)) == "12 月 24 日提前闭馆（出发前请到官网确认）"
    assert p.note_for(date(2026, 12, 26)) is None
    with pytest.raises(ValidationError):
        _place(closed_dates=["13-40"])
    with pytest.raises(ValidationError):
        _place(date_notes={"2-3": "x"})


def test_weekly_and_long_term_closures_are_reported_too():
    assert _place(closed_weekdays=[0]).closed_reason(date(2026, 10, 5)) == "每周一闭馆"       # 2026-10-05 是周一
    assert _place(long_term_closed=True).closed_reason(date(2026, 10, 6)) == "长期闭馆 / 整修中"
    assert _place(closed_weekdays=[0]).closed_reason(date(2026, 10, 6)) is None


def test_a_seasonal_place_is_only_open_inside_its_dated_window_and_cites_its_source():
    season = {"start": "2026-11-24", "end": "2026-12-23", "source_url": "https://example.org/markt",
              "checked_at": "2026-10-25"}
    p = _place(season=season)
    assert p.closed_reason(date(2026, 12, 1)) is None
    assert p.closed_reason(date(2026, 12, 24)) == "当季活动，2026-11-24 到 2026-12-23 举办，不在期内"
    assert p.closed_reason(date(2027, 12, 1)) == "当季活动，2026-11-24 到 2026-12-23 举办，不在期内"
    with pytest.raises(ValidationError):
        _place(season={**season, "start": "2026-12-30"})                # 结束早于开始
    with pytest.raises(ValidationError):
        _place(season={k: v for k, v in season.items() if k != "source_url"})   # 日期是事实：必须带来源
