"""地址 -> 坐标（OpenStreetMap Nominatim，免费、不需要 Key）。

只用在「住宿」上：你填了酒店地址，我们尝试定位到坐标，让每天的路线以它为起终点。
定位不到不报错（调用方退回到「所在区的中心」，并在页面上写明是估算）。

遵守 Nominatim 的使用政策：带可识别的 User-Agent、每秒最多一次请求、结果缓存。
酒店地址是用户输入的私人信息，只发给 Nominatim 这一个服务，不写日志。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.cities import DEFAULT_CITY, City, load_city

_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "ai-travel-planner-agent/0.3 (personal project; geocoding hotel addresses)"


@dataclass(frozen=True)
class GeoHit:
    lat: float
    lng: float
    label: str


Geocoder = Callable[[str], GeoHit | None]

_lock = threading.Lock()
_last_call = 0.0
_cache: dict[str, GeoHit | None] = {}


def nominatim(address: str, *, timeout: float = 6.0, city: City | None = None,
              anywhere: bool = False) -> GeoHit | None:
    """city 给出该城市的定位范围和国家（避免同名街道定位到别的国家）；不给就按巴黎。
    anywhere = 不限国家和范围（自驾的出发地可能在很远的另一座城市）。"""
    city = city or load_city(DEFAULT_CITY)
    text = " ".join(address.split()).lower()
    if not text:
        return None
    key = ("*|" if anywhere else f"{city.id}|") + text
    if key in _cache:
        return _cache[key]
    global _last_call
    with _lock:                                   # 一秒一次
        wait = 1.05 - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
    try:
        params: dict[str, object] = {"q": address, "format": "jsonv2", "limit": 1}
        if not anywhere:
            params.update({"countrycodes": city.country.lower(), "viewbox": city.geocode_viewbox, "bounded": 1})
        r = httpx.get(_URL, params=params,
                      headers={"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,en"}, timeout=timeout)
        r.raise_for_status()
        rows = r.json()
    except (httpx.HTTPError, ValueError):
        return None                               # 网络问题不缓存，下次还能试
    hit = GeoHit(float(rows[0]["lat"]), float(rows[0]["lon"]), str(rows[0].get("display_name", ""))) if rows else None
    _cache[key] = hit
    return hit
