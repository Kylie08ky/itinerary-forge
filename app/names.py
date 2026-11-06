"""景点名的显示规则（规格 07 P18）：中文名 · 当地语言名。

当地名是事实：来自 OpenStreetMap / 维基数据并带核对记录（见 scripts/fetch_local_names.py），不由模型生成。
当地名和中文名相同（忽略大小写、重音、标点）或没有当地名时，只显示一个；没有当地名的老数据回退到英文名。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.notes.text import fold

if TYPE_CHECKING:
    from app.library import Place, PlaceLibrary
    from app.schemas import Itinerary

__all__ = ["bilingual", "fold", "item_location", "local_label", "local_language_of", "localize_itinerary"]


def local_label(place: Place) -> str | None:
    """要在中文名旁边显示的当地名；没有或和中文名没区别 → None。"""
    local = (place.name_local or "").strip()
    if not local or fold(local) == fold(place.name_zh):
        return None
    return local


def bilingual(place: Place) -> str:
    """「卢浮宫 · Musée du Louvre」；没有当地名就只有中文名。"""
    local = local_label(place)
    return f"{place.name_zh} · {local}" if local else place.name_zh


def item_location(place: Place, language: str | None = None) -> str:
    """行程项第二行的「地点名 · 区域」：地点名用当地名，老数据没有就用英文名。
    目的地的官方语言就是中文（国内，language == "zh"）：只用中文名，不带第二种语言。"""
    if language == "zh":
        return f"{place.name_zh} · {place.area}"
    return f"{place.name_local or place.name_en} · {place.area}"


def local_language_of(city_id: str) -> str | None:
    """这座城市的当地语言（ISO 639-1）；不认识这座城市 → None。"""
    from app.cities import load_city

    try:
        return load_city(city_id).local_language
    except (KeyError, FileNotFoundError, ValueError):
        return None


def localize_itinerary(itinerary: Itinerary, lib: PlaceLibrary) -> Itinerary:
    """老行程里的 location 是生成时写进去的「英文名 · 区域」，换成当地名；人手改过的、没有地点的（午餐 / 交通）不动。
    返回新对象，原件和已保存的版本不改；没有要换的就原样返回。"""
    swaps: list[tuple[int, int, str]] = []
    language = local_language_of(lib.city)
    for di, day in enumerate(itinerary.days):
        for ii, item in enumerate(day.items):
            p = lib.places.get(item.place_id) if item.place_id else None
            if p is None or not (p.name_local or language == "zh"):
                continue
            if item.location == f"{p.name_en} · {p.area}" and (new := item_location(p, language)) != item.location:
                swaps.append((di, ii, new))
    if not swaps:
        return itinerary
    out = itinerary.model_copy(deep=True)
    for di, ii, new in swaps:
        out.days[di].items[ii].location = new
    return out
