"""自驾的大致路程：出发地（或回到哪里）写了文字，就估一个到目的地的公里数和时间。

只是估算：把出发地定位成坐标，取到目的地城市中心的直线距离，再按 `scheduler.geo.drive_estimate` 换算；
不查路线。定位不到就什么都不写，不报错。出发地是用户输入的私人信息，只用来定位，估出来的公里数和时间才存下来。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.cities import City
from app.geocode import GeoHit
from app.scheduler.geo import drive_estimate, haversine_km

OriginGeocoder = Callable[[str, City], GeoHit | None]


def _fill(leg: Any, place_key: str, city: City, geocode: OriginGeocoder) -> None:
    if not isinstance(leg, dict) or leg.get("mode") != "drive":
        return
    text = str(leg.get(place_key) or "").strip()
    if not text or leg.get("drive_km") is not None:
        return
    hit = geocode(text, city)
    if hit is None:
        return
    km, minutes = drive_estimate(haversine_km((hit.lat, hit.lng), city.center))
    if km > 0:
        leg["drive_km"], leg["drive_min"] = km, minutes


def annotate(transport: Any, first: City | None, last: City | None, geocode: OriginGeocoder) -> None:
    """就地给 transport（dict：outbound / inbound）补 drive_km / drive_min。去程估到第一座城市，
    返程估从最后一座城市出发。"""
    if not isinstance(transport, dict):
        return
    if first is not None:
        _fill(transport.get("outbound"), "from_place", first, geocode)
    if last is not None:
        _fill(transport.get("inbound"), "to_place", last, geocode)
