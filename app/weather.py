"""天气（规格 M10 S1）：Open-Meteo，免费、不需要 Key。

- 今天起 15 天内用预报；更远的日期用「去年同一天」的实际天气（只作参考：没有降雨概率，也不触发雨天联动）。
- 后端缓存 3 小时；接口失败 / 超时 / 数据异常一律不抛给页面：有缓存用缓存，没有就返回 None（页面不显示天气卡）。
- 只发坐标和日期，不带任何个人信息。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel

from app.cities import city_of

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_HORIZON_DAYS = 15          # 预报最多看这么远（Open-Meteo 的上限约 16 天，留一天余量）
FORECAST_LOOKBACK_DAYS = 80         # 行程已经开始 / 刚结束时，预报接口还能回看这么久
RAIN_PROB_THRESHOLD = 60            # 降雨概率 ≥ 这个值才算「雨天」
CACHE_TTL_SECONDS = 3 * 3600
TIMEOUT_SECONDS = 5.0

# 目的地城市中心（取天气用）来自城市配置；认不出的城市拿不到天气，页面就不显示天气卡。

# WMO 天气代码 -> (中文, 图标键)
_WMO: dict[int, tuple[str, str]] = {
    0: ("晴", "sun"), 1: ("大部晴朗", "sun"), 2: ("局部多云", "partly"), 3: ("阴", "cloud"),
    45: ("雾", "fog"), 48: ("雾凇", "fog"),
    51: ("小毛毛雨", "rain"), 53: ("毛毛雨", "rain"), 55: ("大毛毛雨", "rain"),
    56: ("冻毛毛雨", "rain"), 57: ("冻毛毛雨", "rain"),
    61: ("小雨", "rain"), 63: ("中雨", "rain"), 65: ("大雨", "rain"), 66: ("冻雨", "rain"), 67: ("冻雨", "rain"),
    71: ("小雪", "snow"), 73: ("中雪", "snow"), 75: ("大雪", "snow"), 77: ("雪粒", "snow"),
    80: ("小阵雨", "rain"), 81: ("阵雨", "rain"), 82: ("强阵雨", "rain"),
    85: ("小阵雪", "snow"), 86: ("阵雪", "snow"),
    95: ("雷雨", "storm"), 96: ("雷雨伴冰雹", "storm"), 99: ("强雷雨伴冰雹", "storm"),
}


class WeatherError(Exception):
    """天气接口失败或返回了看不懂的数据；调用方吞掉它，页面不显示天气卡。"""


class WeatherDay(BaseModel):
    date: date
    source: Literal["forecast", "history"]      # history = 去年同一天的实际天气，只作参考
    code: int
    label: str
    icon: str
    t_max: float | None = None
    t_min: float | None = None
    rain_prob: int | None = None                # 历史数据没有
    rain_mm: float | None = None
    rainy: bool = False                         # 只有预报且概率 ≥ 阈值才为真


def wmo(code: int) -> tuple[str, str]:
    return _WMO.get(code, ("天气不明", "cloud"))


def source_for(d: date, today: date) -> Literal["forecast", "history"]:
    if today - timedelta(days=FORECAST_LOOKBACK_DAYS) <= d <= today + timedelta(days=FORECAST_HORIZON_DAYS):
        return "forecast"
    return "history"


def same_day_last_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                           # 2 月 29 日
        return d.replace(year=d.year - 1, day=28)


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _query(client: httpx.Client, url: str, lat: float, lng: float, start: date, end: date, *, with_prob: bool) -> dict:
    daily = ["weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum"]
    if with_prob:
        daily.append("precipitation_probability_max")
    try:
        r = client.get(url, params={"latitude": lat, "longitude": lng, "daily": ",".join(daily), "timezone": "auto",
                                    "start_date": start.isoformat(), "end_date": end.isoformat()},
                       timeout=TIMEOUT_SECONDS)
        r.raise_for_status()
        body = r.json()
        rows = body["daily"]
        times = rows["time"]
        codes = rows["weather_code"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise WeatherError(f"天气接口不可用：{type(exc).__name__}") from exc
    if not isinstance(times, list) or not isinstance(codes, list) or len(times) != len(codes):
        raise WeatherError("天气接口返回的数据对不上")
    return rows


def _rows_by_date(rows: dict) -> dict[date, dict[str, object]]:
    out: dict[date, dict[str, object]] = {}
    for i, t in enumerate(rows["time"]):
        try:
            d = date.fromisoformat(t)
        except (TypeError, ValueError):
            continue
        out[d] = {k: (v[i] if isinstance(v, list) and i < len(v) else None) for k, v in rows.items()}
    return out


def fetch_days(client: httpx.Client, lat: float, lng: float, dates: Sequence[date], today: date) -> list[WeatherDay]:
    """每个行程日期一条；接口没给的日期直接略过（页面里那天不显示天气）。任何接口问题抛 WeatherError。"""
    want_fc = [d for d in dates if source_for(d, today) == "forecast"]
    want_hist = [d for d in dates if source_for(d, today) == "history"]
    found: dict[date, WeatherDay] = {}

    if want_fc:
        rows = _rows_by_date(_query(client, FORECAST_URL, lat, lng, min(want_fc), max(want_fc), with_prob=True))
        for d in want_fc:
            r = rows.get(d)
            code = _num(r["weather_code"]) if r else None
            if r is None or code is None:
                continue
            label, icon = wmo(int(code))
            prob = _num(r.get("precipitation_probability_max"))
            found[d] = WeatherDay(
                date=d, source="forecast", code=int(code), label=label, icon=icon,
                t_max=_num(r.get("temperature_2m_max")), t_min=_num(r.get("temperature_2m_min")),
                rain_prob=None if prob is None else round(prob), rain_mm=_num(r.get("precipitation_sum")),
                rainy=prob is not None and prob >= RAIN_PROB_THRESHOLD)

    if want_hist:
        past = {d: same_day_last_year(d) for d in want_hist}
        rows = _rows_by_date(_query(client, ARCHIVE_URL, lat, lng, min(past.values()), max(past.values()),
                                    with_prob=False))
        for d, past_d in past.items():
            r = rows.get(past_d)
            code = _num(r["weather_code"]) if r else None
            if r is None or code is None:
                continue
            label, icon = wmo(int(code))
            found[d] = WeatherDay(
                date=d, source="history", code=int(code), label=label, icon=icon,
                t_max=_num(r.get("temperature_2m_max")), t_min=_num(r.get("temperature_2m_min")),
                rain_prob=None, rain_mm=_num(r.get("precipitation_sum")), rainy=False)

    return [found[d] for d in dates if d in found]


class WeatherService:
    """取天气 + 3 小时缓存 + 失败降级。`client` 可注入（测试用 httpx.MockTransport）。"""

    def __init__(self, client: httpx.Client | None = None, *, ttl: float = CACHE_TTL_SECONDS,
                 clock: Callable[[], float] = time.time) -> None:
        self._client = client or httpx.Client(headers={"User-Agent": "ai-travel-planner-agent/0.3"})
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, list[WeatherDay]]] = {}

    def get(self, destination: str, dates: Sequence[date], today: date) -> list[WeatherDay] | None:
        city = city_of(destination)
        center = city.center if city else None
        if center is None or not dates:
            return None
        key = (city.id, tuple(dates), today)
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        try:
            days = fetch_days(self._client, center[0], center[1], dates, today)
        except WeatherError:
            return hit[1] if hit else None         # 过期的缓存也比没有强
        if not days:
            return hit[1] if hit else None
        with self._lock:
            self._cache[key] = (now, days)
        return days
