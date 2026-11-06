"""住哪个区（规格 M06 S5b，F17）：按行程里各景点的位置，给 2–3 个备选区。

只做一件事：算「住在哪个区，到行程里所有景点的平均直线距离最短」。没有酒店数据、价格、治安、噪音，所以不推荐具体酒店。
区的「中心」用该区已收录景点的位置中心近似；郊区景点（凡尔赛、迪士尼）不参与计算（没人会为它们选市区酒店）。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from app.geo import haversine_km
from app.library import PlaceLibrary, Zone
from app.schemas import Itinerary

CAVEAT = "只按景点位置算，不含酒店价格、治安、噪音、换乘是否方便；区的中心用该区已收录景点的位置近似。"
_ARRONDISSEMENT = re.compile(r"^\d{1,2}区$")
_NEAR_KM = 2.0
_TOP = 3


@dataclass(frozen=True)
class AreaRec:
    area: str
    avg_km: float
    within_2km: int
    n_places: int
    reason: str


def _area_centres(lib: PlaceLibrary) -> dict[str, tuple[float, float]]:
    by: dict[str, list[tuple[float, float]]] = {}
    for p in lib.places.values():
        if p.zone == Zone.core and _ARRONDISSEMENT.match(p.area):
            by.setdefault(p.area, []).append((p.lat, p.lng))
    return {a: (statistics.fmean(x for x, _ in pts), statistics.fmean(y for _, y in pts)) for a, pts in by.items()}


def recommend_areas(it: Itinerary, lib: PlaceLibrary) -> list[AreaRec]:
    stops = [lib.places[i.place_id] for d in it.days for i in d.items
             if i.place_id and i.place_id in lib.places and lib.places[i.place_id].zone == Zone.core]
    if not stops:
        return []
    pts = [(p.lat, p.lng) for p in stops]
    recs = []
    for area, centre in _area_centres(lib).items():
        dists = [haversine_km(centre, q) for q in pts]
        avg = statistics.fmean(dists)
        near = sum(1 for d in dists if d <= _NEAR_KM)
        recs.append(AreaRec(
            area=area, avg_km=round(avg, 1), within_2km=near, n_places=len(pts),
            reason=f"到行程里 {len(pts)} 个景点平均 {avg:.1f} 公里，其中 {near} 个在 2 公里内"))
    recs.sort(key=lambda r: (r.avg_km, -r.within_2km, r.area))
    return recs[:_TOP]
