"""城市配置（规格 M17 S1）：一座城市 = 一份 data/cities/<id>.json + 这座城市的地点 / 主题 / 预算数据。

全站不再写死巴黎：时区、中心点、范围、币种、出行人身份选项、地图供应商都从这里取。
行程认城市：`city_of(destination)` 按 id / 中文名 / 别名认出来（「Paris」「巴黎」都是 paris）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.citybuild.store import generated_dir, generated_path

_CITIES_DIR = Path(__file__).resolve().parents[1] / "data" / "cities"

DEFAULT_CITY = "paris"


class Bbox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: tuple[float, float]
    lng: tuple[float, float]

    def contains(self, lat: float, lng: float) -> bool:
        return self.lat[0] <= lat <= self.lat[1] and self.lng[0] <= lng <= self.lng[1]


class SuggestedDays(BaseModel):
    """这座城市的建议游玩天数（规格 07 P06）：min–max 是从容的区间，quality_max 是主题里的点够用的最长天数，
    fill_max 是点最多能排满的天数。"""

    model_config = ConfigDict(extra="forbid")

    min: int = Field(ge=1)
    max: int = Field(ge=1)
    quality_max: int = Field(ge=1)
    fill_max: int | None = Field(default=None, ge=1)    # 点最多能把几天排满（每天 ≥ 2 个）；没量过 = None
    note: str


class City(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    name_zh: str
    name_en: str
    local_language: str = Field(min_length=2, max_length=3)   # 当地语言（ISO 639-1）：景点的当地名用它取（规格 07 P18）
    aliases: list[str]
    country: str = Field(min_length=2, max_length=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")      # ISO 4217 币种代码（现建的城市用所在国的货币）
    timezone: str
    utc_offset: float                              # 标准时间（冬令时）相对 UTC 的小时数
    dst: Literal["eu", "none"]                     # 夏令时规则：欧盟 / 没有
    center: tuple[float, float]                    # 城市中心（纬度, 经度）：天气、日落、没住宿时的路线起点
    bbox: Bbox                                     # 市区范围；范围外的点必须标 day_trip
    geocode_viewbox: str | None = None             # 地址定位时限制的范围（左, 上, 右, 下），避免同名街道定位到别的国家
    coord_system: Literal["wgs84"] = "wgs84"       # 库里的坐标一律 WGS-84（调高德时才转成 GCJ-02）
    map_provider: Literal["osm", "amap"] = "osm"    # 不写 = 按国家：中国用高德，其余 OSM（规格 M05 S7）
    residency_options: list[Literal["fr", "eu"]] = Field(default_factory=list)   # 出行人身份：空 = 不问「长居」
    suggested_days: SuggestedDays
    landmarks: list[str] = Field(default_factory=list)   # 标志性地点（地点 id），按优先顺序：首页 / 封面选大图先看它们
    supported: bool = True
    # 城市分层（规格 08 3.4、10）：full = 巴黎这种完整策展库；light = 一日游目的地（每座 8–12 个点，开放数据草稿）；
    # standard = `build_city` 自动建的完整城市（价格 / 预约 / 部分闭馆日待核对，精校后升 full）
    tier: Literal["full", "standard", "light"] = "full"
    region: str | None = None                      # 轻量城市属于哪个区域（如 suedwest-de）
    builder_version: int | None = None               # 现建的城市是哪一版流程建的（旧的会自动重建）
    monthly_low_c: dict[int, float] | None = None    # 每月平均最低气温（早晚温差）
    monthly_rain_mm: dict[int, float] | None = None  # 每月降水量（毫米）
    elevation_m: float | None = None                 # 海拔（米）
    monthly_high_c: dict[int, float] | None = None   # 现建的城市：每月平均最高气温（行李清单用，Open-Meteo 去年的实测）
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")   # 现建的城市记着维基数据编号（同一座城市不重复建）
    metro: bool | None = None                        # 有没有地铁：通勤文字写「地铁」/「公交」；不确定写「公共交通」

    @model_validator(mode="before")
    @classmethod
    def _default_map_provider(cls, data):
        if isinstance(data, dict) and "map_provider" not in data:
            return {**data, "map_provider": "amap" if data.get("country") == "CN" else "osm"}
        return data

    @model_validator(mode="after")
    def _light_has_region(self) -> City:
        if self.tier == "light" and not self.region:
            raise ValueError(f"{self.id}: 轻量城市（tier=light）必须写 region")
        return self

    @field_validator("aliases")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [a.strip().lower() for a in v]

    def transit_word(self, mode: str) -> str:
        """排程估算的通勤方式（geo.travel 的返回）在这座城市里怎么叫：策展的完整城市保持原来的写法，
        现建的城市按有没有地铁写，不确定就写「公共交通」，不借用别的城市的线路名（RER）。"""
        if mode in ("步行", "自驾") or (self.tier == "full" and self.metro is None):
            return mode
        if mode == "RER/火车":
            return "区间火车 / 公交"
        return {True: "地铁", False: "公交"}.get(self.metro, "公共交通")

    def matches(self, text: str) -> bool:
        t = text.strip().lower()
        zh = self.name_zh.lower()
        short = zh[:-1] if zh.endswith("市") and len(zh) >= 3 else zh       # 「重庆」也认得出是「重庆市」
        return t in {self.id, zh, short, self.name_en.lower(), *self.aliases}


# data/cities/ 里不是城市配置的文件（城际时间表，见 app/daytrips.py）
_NOT_CITY_FILES = {"city-pairs"}


@lru_cache(maxsize=64)
def load_city(city_id: str) -> City:
    """精校的 `data/cities/` 里先找，再找现建的 `data/generated/cities/`（规格 10）。"""
    path = _CITIES_DIR / f"{city_id}.json"
    if not path.exists():
        path = generated_path("cities", city_id)
    if not path.exists():
        raise KeyError(f"没有这座城市的配置：{city_id!r}")
    return City.model_validate(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def list_cities() -> tuple[City, ...]:
    """所有已配置的城市（精校的在前，按 id 排序；再加现建的）。"""
    curated = [p.stem for p in sorted(_CITIES_DIR.glob("*.json")) if p.stem not in _NOT_CITY_FILES]
    made = [p.stem for p in sorted((generated_dir() / "cities").glob("*.json")) if p.stem not in curated]
    return tuple(load_city(i) for i in [*curated, *made])


def city_by_wikidata(qid: str) -> City | None:
    """已经建好（或精校）的城市里，维基数据编号是 qid 的那座。"""
    return next((c for c in list_cities() if c.wikidata == qid), None)


def needs_rebuild(city: City) -> bool:
    """现建的城市是旧版流程建的（景点少、没有图片）：下次有人输入就重建。精校的不动。"""
    from app.citybuild.core import BUILDER_VERSION

    return city.tier == "standard" and (city.builder_version or 0) < BUILDER_VERSION


def reload_cities() -> None:
    """现建了新城市之后调用：清掉城市和地点库的缓存，下次读就能看到。"""
    from app.library import load_library
    from app.media import load_hero, load_media

    load_city.cache_clear()
    list_cities.cache_clear()
    load_library.cache_clear()
    load_media.cache_clear()
    load_hero.cache_clear()


def supported_cities() -> list[City]:
    return [c for c in list_cities() if c.supported]


_NAME_INDEX: tuple[tuple[City, ...], dict[str, City]] | None = None


def _name_index() -> dict[str, City]:
    """「名字 → 城市」：和逐个城市 `matches` 的结果一样（同名时靠前的城市优先），但一次查表。
    绑在 `list_cities()` 的结果上：城市列表重建了（`reload_cities`），这张表下次用时自动重建。"""
    global _NAME_INDEX
    cities = list_cities()
    if _NAME_INDEX is None or _NAME_INDEX[0] is not cities:
        index: dict[str, City] = {}
        for c in cities:
            if not c.supported:
                continue
            zh = c.name_zh.lower()
            short = zh[:-1] if zh.endswith("市") and len(zh) >= 3 else zh
            for key in (c.id, zh, short, c.name_en.lower(), *c.aliases):
                index.setdefault(key, c)
        _NAME_INDEX = (cities, index)
    return _NAME_INDEX[1]


def city_of(destination: str) -> City | None:
    """「Paris」「paris」「巴黎」→ 巴黎；不认识 → None。只在已支持的城市里找。"""
    return _name_index().get(destination.strip().lower())


def city_id_of(destination: str) -> str:
    """认得出就返回城市 id；认不出按原文小写（让后面的「没有数据」错误说清楚）。"""
    c = city_of(destination)
    return c.id if c else destination.strip().lower()


def itinerary_city_id(destination: str) -> str:
    """从一份已有行程的 destination 找城市 id；认不出（模型写错 / 老数据）按默认城市，不让导出和校验因此崩掉。"""
    c = city_of(destination)
    return c.id if c else DEFAULT_CITY


__all__ = [
    "Bbox", "City", "DEFAULT_CITY", "SuggestedDays", "city_id_of", "city_of", "itinerary_city_id", "list_cities",
    "load_city", "supported_cities",
]
