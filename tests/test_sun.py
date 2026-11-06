"""日落时间（app/scheduler/sun.py）：和已知的巴黎日落时间对一对，夏令时切换。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.scheduler.engine import GOLDEN_CEIL, GOLDEN_FLOOR, GOLDEN_HARD, GOLDEN_SOFT, golden_start
from app.scheduler.sun import sunset_minutes, utc_offset_hours


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


@pytest.mark.parametrize("day,expected", [
    (date(2026, 6, 21), "21:58"), (date(2026, 12, 21), "16:57"), (date(2026, 10, 20), "18:55"),
])
def test_the_sunset_matches_the_known_paris_times_within_five_minutes(day, expected):
    h, m = map(int, expected.split(":"))
    assert abs(sunset_minutes(day) - (h * 60 + m)) <= 5, hhmm(sunset_minutes(day))


def test_summer_sunsets_are_much_later_than_winter_ones():
    assert sunset_minutes(date(2026, 6, 21)) - sunset_minutes(date(2026, 12, 21)) > 4 * 60


def test_daylight_saving_time_follows_the_eu_rule():
    assert utc_offset_hours(date(2026, 3, 28)) == 1 and utc_offset_hours(date(2026, 3, 29)) == 2   # 3 月最后一个周日
    assert utc_offset_hours(date(2026, 10, 24)) == 2 and utc_offset_hours(date(2026, 10, 25)) == 1   # 10 月最后一个周日
    assert utc_offset_hours(date(2026, 7, 1)) == 2 and utc_offset_hours(date(2026, 1, 1)) == 1
    assert utc_offset_hours(date(2027, 3, 28)) == 2 and utc_offset_hours(date(2027, 3, 27)) == 1


def test_the_sunset_jumps_an_hour_across_the_autumn_clock_change():
    before, after = sunset_minutes(date(2026, 10, 24)), sunset_minutes(date(2026, 10, 25))
    assert before - after > 45                                    # 当地时间的日落一下子早了差不多一小时


def test_the_sunset_moves_smoothly_day_to_day_otherwise():
    d = date(2026, 5, 1)
    for _ in range(100):
        assert abs(sunset_minutes(d + timedelta(days=1)) - sunset_minutes(d)) < 6 or d.month in (3, 10)
        d += timedelta(days=1)


def test_golden_start_follows_the_season_and_is_clamped():
    winter = golden_start(sunset_minutes(date(2026, 12, 21)))
    autumn = golden_start(sunset_minutes(date(2026, 10, 3)))
    summer = golden_start(sunset_minutes(date(2026, 6, 21)))
    assert GOLDEN_FLOOR <= winter < autumn < summer <= GOLDEN_CEIL      # 夏天不晚于 19:30
    assert autumn == sunset_minutes(date(2026, 10, 3)) - GOLDEN_HARD
    assert golden_start(sunset_minutes(date(2026, 10, 3)), GOLDEN_SOFT) > autumn
    assert golden_start(None) == 16 * 60 + 30                      # 没有日落信息：原来的 16:30
    assert golden_start(0) == GOLDEN_FLOOR
