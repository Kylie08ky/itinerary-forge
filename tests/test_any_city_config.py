"""任意城市的城市配置（规格 10 阶段 2）：任意币种、按时区算时差和日落、日历时区。"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.cities import City, load_city
from app.exports import vtimezone_lines
from app.scheduler.sun import sunset_minutes, utc_offset_hours


def city(**over) -> City:
    base = load_city("paris").model_dump()
    return City.model_validate({**base, **over})


def test_any_iso_currency_code_is_accepted_and_junk_is_not():
    assert city(currency="JPY").currency == "JPY"
    for bad in ("yen", "jp", "usd", "EURO"):
        with pytest.raises(ValidationError):
            city(currency=bad)


def test_the_utc_offset_follows_the_time_zone_database():
    assert utc_offset_hours(date(2026, 12, 12), tz="Asia/Tokyo") == 9
    assert utc_offset_hours(date(2026, 7, 1), tz="America/New_York") == -4
    assert utc_offset_hours(date(2026, 12, 12), tz="America/New_York") == -5
    assert utc_offset_hours(date(2026, 12, 12), tz="Asia/Kolkata") == 5.5
    assert utc_offset_hours(date(2026, 7, 1), tz="Europe/Paris") == 2
    assert utc_offset_hours(date(2026, 12, 12), tz="Europe/Paris") == 1


def test_paris_sunset_is_unchanged_whether_it_comes_from_the_old_rule_or_the_time_zone():
    for d in (date(2026, 3, 28), date(2026, 3, 29), date(2026, 6, 21), date(2026, 10, 24), date(2026, 10, 25),
              date(2026, 12, 21)):
        assert sunset_minutes(d, 48.8566, 2.3522, tz="Europe/Paris") == sunset_minutes(d, 48.8566, 2.3522)


def test_tokyo_sunset_in_december_is_about_half_past_four():
    m = sunset_minutes(date(2026, 12, 12), 35.68, 139.76, tz="Asia/Tokyo")
    assert 16 * 60 + 15 <= m <= 16 * 60 + 50


def test_calendar_time_zone_block_exists_for_known_and_fixed_offset_zones_and_is_skipped_otherwise():
    assert any("TZID:Europe/Paris" in line for line in vtimezone_lines("Europe/Paris"))
    tokyo = vtimezone_lines("Asia/Tokyo")
    assert "TZID:Asia/Tokyo" in tokyo and "TZOFFSETTO:+0900" in tokyo
    # 有夏令时又没有现成规则：只写 TZID，由日历软件自带的时区库解释
    assert vtimezone_lines("America/New_York") == []
