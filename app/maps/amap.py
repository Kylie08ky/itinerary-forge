"""高德 Web 服务客户端（规格 M05 S7b / S7d）：搜库外的地点、给酒店地址定位、查相邻两点的步行 / 公交时间。

- 只在用户主动点了才调用；没 Key、本月到自留上限、网络出错都返回 None，调用方退回原来的做法，不报错。
- **不缓存、不保存**高德返回的内容（服务协议 3.5、4.12.3、7.3 条，2026-11-03 核对）；再问就再调、再计数。
- 高德返回 GCJ-02 坐标，出口前换成 WGS-84（库里都是 WGS-84）。
- 只取名字、地址、类型和坐标；经营信息（营业时间、价格、评分）一律不要。
- Key 只在请求参数里，不写日志、不进异常信息。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.geocode import GeoHit
from app.maps.coords import gcj02_to_wgs84, wgs84_to_gcj02
from app.maps.quota import Usage
from app.scheduler.geo import travel

_SEARCH_URL = "https://restapi.amap.com/v5/place/text"
_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
_WALK_URL = "https://restapi.amap.com/v3/direction/walking"
_DRIVE_URL = "https://restapi.amap.com/v3/direction/driving"
_TRANSIT_URL = "https://restapi.amap.com/v3/direction/transit/integrated"
MAX_HITS = 5


class AmapError(Exception):
    """高德没给出可用的结果（网络、Key 无效、高德自己的限流……）。"""


@dataclass(frozen=True)
class AmapPlace:
    id: str                 # 高德的地点编号
    name: str
    address: str
    area: str               # 所在区
    type: str               # 小类，给人看
    category: str           # 换算成地点库的类别
    lat: float
    lng: float


@dataclass(frozen=True)
class Commute:
    """相邻两点之间高德算出的通勤：分钟、方式、距离（米）、分段（给人看的几句话）。"""

    minutes: int
    mode: str               # 步行 / 公交 / 地铁
    distance_m: int
    steps: list[str]


def category_of(amap_type: str) -> str:
    """高德的分类（「大类;中类;小类」）→ 地点库的类别；认不出的按景点。"""
    big = amap_type.split(";")[0]
    if "公园广场" in amap_type:
        return "park"
    if "博物馆" in amap_type or "美术馆" in amap_type or "展览馆" in amap_type:
        return "museum"
    return {"餐饮服务": "food", "购物服务": "shopping", "体育休闲服务": "entertainment"}.get(big, "sight")


def _http_get(url: str, params: dict) -> dict:
    try:
        r = httpx.get(url, params=params, timeout=6.0)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AmapError("连不上高德") from exc
    if not isinstance(body, dict) or str(body.get("status")) != "1":
        raise AmapError(f"高德没有给出结果（{body.get('info', '') if isinstance(body, dict) else ''}）")
    return body


def _location(text: object) -> tuple[float, float] | None:
    """「经度,纬度」（GCJ-02）→ (纬度, 经度)（WGS-84）；格式不对返回 None。"""
    try:
        lng, lat = (float(x) for x in str(text).split(","))
    except ValueError:
        return None
    return gcj02_to_wgs84(lat, lng)


class AmapClient:
    def __init__(self, key: Callable[[], str], usage: Usage) -> None:
        self._key = key
        self.usage = usage

    def reason_unavailable(self, service: str) -> str | None:
        """现在不能调的原因：没 Key / 本月到上限；能调返回 None。"""
        if not self._key():
            return "no_key"
        if not self.usage.allow(service):
            return "quota"
        return None

    def _fetch(self, service: str, url: str, params: dict) -> dict | None:
        """查 Key 和配额 → 调高德并记账；不能调 / 出错返回 None。"""
        if self.reason_unavailable(service):
            return None
        try:
            body = _http_get(url, {"key": self._key(), **params})
        except AmapError:
            return None
        self.usage.record(service)
        return body

    def search(self, q: str, city: str) -> list[AmapPlace] | None:
        """关键字搜索，限定在 city 里；None = 没搜成（没 Key / 到上限 / 出错），[] = 搜了但没有。"""
        q = " ".join(q.split())
        got = self._fetch("search", _SEARCH_URL,
                          {"keywords": q, "region": city, "city_limit": "true", "page_size": MAX_HITS}) if q else None
        if got is None:
            return None
        out: list[AmapPlace] = []
        for poi in got.get("pois") or []:
            pos = _location(poi.get("location"))
            name = str(poi.get("name") or "").strip()
            if pos is None or not name or not poi.get("id"):
                continue
            address, full = poi.get("address"), str(poi.get("type") or "")
            area = poi.get("adname")
            out.append(AmapPlace(str(poi.get("id") or ""), name, address if isinstance(address, str) else "",
                                 area if isinstance(area, str) else "", full.split(";")[-1], category_of(full),
                                 pos[0], pos[1]))
        return out[:MAX_HITS]

    def geocode(self, address: str, city: str) -> GeoHit | None:
        """地址 → 坐标（WGS-84）；定位不到 / 不能调 / 出错都是 None，调用方退回别的办法。"""
        address = " ".join(address.split())
        got = self._fetch("lbs", _GEOCODE_URL, {"address": address, "city": city}) if address else None
        if got is None:
            return None
        for g in got.get("geocodes") or []:
            pos = _location(g.get("location"))
            if pos is not None:
                return GeoHit(pos[0], pos[1], str(g.get("formatted_address") or address))
        return None

    def route(self, a: tuple[float, float], b: tuple[float, float], city: str, *,
              drive: bool = False) -> Commute | None:
        """两点（纬度, 经度，WGS-84）之间的通勤：排程估算是步行的查步行，其余查公交 / 地铁
        （drive = 自驾：查驾车）；查不到是 None。"""
        def lnglat(p: tuple[float, float]) -> str:
            lat, lng = wgs84_to_gcj02(*p)
            return f"{lng:.6f},{lat:.6f}"

        origin, destination = lnglat(a), lnglat(b)
        if travel(a, b)[1] == "步行":
            got = self._fetch("lbs", _WALK_URL, {"origin": origin, "destination": destination})
            return _walk(got) if got else None
        if drive:
            got = self._fetch("lbs", _DRIVE_URL, {"origin": origin, "destination": destination})
            return _drive(got) if got else None
        got = self._fetch("lbs", _TRANSIT_URL, {"origin": origin, "destination": destination, "city": city})
        return _transit(got) if got else None


def _int(value: object) -> int:
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


def _minutes(seconds: object) -> int:
    return max(1, math.ceil(_int(seconds) / 60))


def _walk(body: dict) -> Commute | None:
    paths = (body.get("route") or {}).get("paths") or []
    if not paths:
        return None
    metres = _int(paths[0].get("distance"))
    return Commute(_minutes(paths[0].get("duration")), "步行", metres, [f"步行 {metres} 米"])


def _drive(body: dict) -> Commute | None:
    paths = (body.get("route") or {}).get("paths") or []
    if not paths:
        return None
    metres = _int(paths[0].get("distance"))
    return Commute(_minutes(paths[0].get("duration")), "自驾", metres, [f"自驾 {metres / 1000:.1f} 公里"])


def _transit(body: dict) -> Commute | None:
    route = body.get("route") or {}
    transits = route.get("transits") or []
    if not transits:
        return None
    best = transits[0]
    steps: list[str] = []
    for seg in best.get("segments") or []:
        walking = seg.get("walking")
        if isinstance(walking, dict) and _int(walking.get("distance")) > 0:
            steps.append(f"步行 {_int(walking.get('distance'))} 米")
        bus = seg.get("bus")
        lines = bus.get("buslines") if isinstance(bus, dict) else None
        if lines:
            line = lines[0]
            name = str(line.get("name") or "").split("(")[0]
            dep = (line.get("departure_stop") or {}).get("name", "")
            arr = (line.get("arrival_stop") or {}).get("name", "")
            steps.append(f"乘{name}：{dep} → {arr}（{_int(line.get('via_num')) + 1} 站）")
    return Commute(_minutes(best.get("duration")), "公交 / 地铁", _int(route.get("distance")), steps)
