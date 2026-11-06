"""行程的「路线」视图：每天按顺序经过哪些点、颜色、直线距离、Google 地图导航链接。

只用地点库里核对过的坐标（行程条目靠 place_id 对上地点库）；
没有 place_id 的条目（午餐占位、自由安排）没有坐标，不进路线。
前端的全程地图、PDF 的路线图、KML 导出和一键导航都从这里取数，保证各处看到的是同一条路线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt
from urllib.parse import quote

from app.library import Place, PlaceLibrary, library_for_itinerary
from app.maps.coords import in_china, wgs84_to_gcj02
from app.names import local_label
from app.schemas import Itinerary

# 每天一种颜色（够用 8 天，之后循环）；前端、PDF、KML 都用这一份
DAY_COLORS = ["#c2410c", "#1d4ed8", "#15803d", "#a21caf", "#b45309", "#0e7490", "#be123c", "#4d7c0f"]

_EARTH_KM = 6371.0088
# Google 地图网页链接最多支持 起点 + 9 个途经点 + 终点
_MAX_STOPS_PER_LINK = 11


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点（纬度, 经度）的直线距离，公里。"""
    (la1, lo1), (la2, lo2) = a, b
    dla, dlo = radians(la2 - la1), radians(lo2 - lo1)
    h = sin(dla / 2) ** 2 + cos(radians(la1)) * cos(radians(la2)) * sin(dlo / 2) ** 2
    return 2 * _EARTH_KM * asin(sqrt(h))


@dataclass
class Stop:
    place_id: str
    name: str
    lat: float
    lng: float
    time: str
    address: str | None = None
    name_local: str | None = None                  # 当地语言名（和中文名没区别时为 None）
    city_id: str | None = None                     # 多城市行程里这个点在哪座城市；单城市行程没有


@dataclass
class DayRoute:
    date: str
    color: str
    stops: list[Stop] = field(default_factory=list)
    distance_km: float = 0.0                       # 按顺序经过各点的直线距离累加（不是实际步行 / 通勤距离）
    google_urls: list[str] = field(default_factory=list)   # 一天点太多时拆成几段链接（境外的点）
    amap_urls: list[str] = field(default_factory=list)     # 境内的点：高德「多点标注」链接，一条最多 10 个点


@dataclass
class RouteSummary:
    days: list[DayRoute]
    total_km: float
    place_count: int

    def as_dict(self) -> dict:
        return {
            "total_km": round(self.total_km, 1),
            "place_count": self.place_count,
            "days": [
                {
                    "date": d.date, "color": d.color, "distance_km": round(d.distance_km, 1),
                    "google_urls": d.google_urls,
                    **({"amap_urls": d.amap_urls} if d.amap_urls else {}),
                    "stops": [{"place_id": s.place_id, "name": s.name, "lat": s.lat, "lng": s.lng,
                               "time": s.time, "address": s.address, "name_local": s.name_local,
                               **({"city_id": s.city_id} if s.city_id else {})} for s in d.stops],
                }
                for d in self.days
            ],
        }


def _pt(p: Stop | Place) -> str:
    return f"{p.lat:.6f},{p.lng:.6f}"


def google_dir_url(stops: list[Stop], *, mode: str = "walking") -> str:
    """Google 地图「路线」链接：第一个点是起点、最后一个是终点，中间按顺序当途经点。

    只用坐标（来自核对过的地点库），不依赖名字能否被 Google 正确识别。不需要 API Key。
    """
    if len(stops) < 2:
        raise ValueError("至少要有两个点才能画路线")
    url = (f"https://www.google.com/maps/dir/?api=1&origin={_pt(stops[0])}&destination={_pt(stops[-1])}"
           f"&travelmode={quote(mode)}")
    mid = stops[1:-1]
    if mid:
        url += "&waypoints=" + "%7C".join(_pt(s) for s in mid)
    return url


def google_urls_for_day(stops: list[Stop], *, mode: str = "walking") -> list[str]:
    """一天的路线链接。点数超过上限就拆成几段，相邻两段共用一个衔接点，保证走完整条路线。"""
    if len(stops) < 2:
        return []
    urls: list[str] = []
    i = 0
    while i < len(stops) - 1:
        chunk = stops[i:i + _MAX_STOPS_PER_LINK]
        urls.append(google_dir_url(chunk, mode=mode))
        i += _MAX_STOPS_PER_LINK - 1
    return urls


_AMAP_MAX_MARKERS = 10          # 高德 URI「多点标注」一条最多 10 个点


def amap_urls_for_day(stops: list[Stop]) -> list[str]:
    """国内一天的地点链接：高德网页「多点标注」（`uri.amap.com`，不要 Key、不耗配额）。

    高德的网页链接只支持两点之间的路线（途经点只在驾车有效），所以这里是「在高德里看当天所有地点」，
    不是连成一条步行路线。坐标换成高德的 GCJ-02；名字里的分隔符去掉，免得把参数切乱。
    """
    if len(stops) < 2:
        return []
    urls: list[str] = []
    for i in range(0, len(stops), _AMAP_MAX_MARKERS):
        marks = []
        for s in stops[i:i + _AMAP_MAX_MARKERS]:
            lat, lng = wgs84_to_gcj02(s.lat, s.lng)
            name = s.name.replace("|", " ").replace(",", " ")
            marks.append(f"{lng:.6f},{lat:.6f},{name}")
        markers = quote("|".join(marks), safe=",")
        urls.append(f"https://uri.amap.com/marker?markers={markers}&src=ai-travel-planner")
    return urls


def by_city(stops: list[Stop]) -> dict[str | None, list[Stop]]:
    """按城市把一天的点分段（顺序不变）；单城市行程只有一段（键是 None）。"""
    out: dict[str | None, list[Stop]] = {}
    for s in stops:
        out.setdefault(s.city_id, []).append(s)
    return out


def build_route(itinerary: Itinerary, library: PlaceLibrary | None = None) -> RouteSummary:
    lib = library or library_for_itinerary(itinerary)
    days: list[DayRoute] = []
    seen: set[str] = set()
    for n, day in enumerate(itinerary.days):
        route = DayRoute(date=day.date.isoformat(), color=DAY_COLORS[n % len(DAY_COLORS)])
        for item in day.items:
            pid = item.place_id
            if not pid or pid not in lib.places:
                continue
            p = lib.places[pid]
            route.stops.append(Stop(place_id=pid, name=p.name_zh, lat=p.lat, lng=p.lng, time=item.time,
                                    address=p.address, name_local=local_label(p), city_id=item.city_id))
            seen.add(pid)
        # 多城市换城市那天一天里有两座城市：距离和路线链接都按城市分段，不把两座城市之间的直线距离算进去
        segments = list(by_city(route.stops).values())
        route.distance_km = sum(
            haversine_km((a.lat, a.lng), (b.lat, b.lng)) for seg in segments for a, b in zip(seg, seg[1:], strict=False)
        )
        for seg in segments:          # 境内的点走高德，其余走 Google（规格 M05 S7e）
            if all(in_china(s.lat, s.lng) for s in seg):
                route.amap_urls += amap_urls_for_day(seg)
            else:
                route.google_urls += google_urls_for_day(seg)
        days.append(route)
    return RouteSummary(days=days, total_km=sum(d.distance_km for d in days), place_count=len(seen))
