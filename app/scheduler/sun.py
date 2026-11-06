"""日落时间（当地时间）：给「黄昏最佳」的点定几点去。

用 NOAA 的简化太阳位置公式算，不联网、不引依赖；时差和夏令时规则来自城市配置（默认巴黎：
欧盟规则，3 月最后一个周日 01:00 UTC 起，到 10 月最后一个周日 01:00 UTC 止）。
误差几分钟，够「黄昏前 75 分钟到场」用——这里要的是随季节变化的大致时刻，不是天文台级精度。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.cities import DEFAULT_CITY, load_city


def _last_sunday(year: int, month: int) -> date:
    d = date(year, month, 31) if month in (3, 10) else date(year, month, 30)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def utc_offset_hours(day: date, std: float | None = None, dst: str | None = None, tz: str | None = None) -> float:
    """当地当天的时差：给了时区名（任意城市）就按时区数据库算（当天 12 点）；否则欧盟规则下夏令时 = 标准 + 1，
    没有夏令时的城市就是标准时差。都不给就用默认城市（巴黎）的配置。"""
    if tz is not None:
        off = ZoneInfo(tz).utcoffset(datetime(day.year, day.month, day.day, 12))
        return off.total_seconds() / 3600 if off is not None else 0.0
    base = load_city(DEFAULT_CITY)
    std = base.utc_offset if std is None else std
    dst = base.dst if dst is None else dst
    if dst == "none":
        return std
    start, end = _last_sunday(day.year, 3), _last_sunday(day.year, 10)
    return std + 1 if start <= day < end else std


def sunset_minutes(day: date, lat: float | None = None, lng: float | None = None, *, utc_std: float | None = None,
                   dst: str | None = None, tz: str | None = None) -> int:
    """当地时间的日落，从当天 0:00 起的分钟数。坐标 / 时差 / 夏令时不给就用默认城市（巴黎）的配置。"""
    center = load_city(DEFAULT_CITY).center
    lat = center[0] if lat is None else lat
    lng = center[1] if lng is None else lng
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl)) - math.tan(la) * math.tan(decl)
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    utc_min = 720 - 4 * (lng - ha) - eqtime
    return round(utc_min + 60 * utc_offset_hours(day, utc_std, dst, tz))
