"""深度游学路线（规格 09）：有主线、有顺序、有背景故事的文化路线。

路线是**策展数据**（`data/routes/<城市>.json`）：每章的故事、每站「看什么 / 为什么在这里」都带来源和核对日期，
地点必须在地点库里。模型不参与选点和事实，只在生成行程时把一天的主线讲顺（见 generator 的导览部分）。

纯数据 + 校验，不联网、不调模型。
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.cities import DEFAULT_CITY
from app.library import PlaceLibrary, load_library
from app.names import bilingual

_DIR = Path(__file__).resolve().parents[1] / "data" / "routes"


# 兴趣线（规格 09 十三）：新建页按它给路线分组。历史 = 博物馆 / 古建筑 / 名胜古迹；民俗 = 有日期的节庆和体验；
# 美食 = 带故事的美食地图；其他 = 艺术、时尚、音乐、文学……
TRACKS: dict[str, str] = {"history": "历史", "folk": "民俗体验", "food": "美食", "other": "其他"}


class RouteError(ValueError):
    """路线数据不成立（地点不在库里、缺来源……）；数据闸门测试和加载时都会抛。"""


class Source(BaseModel):
    """一条依据：维基百科条目、官方网站、书目。事实必须带来源和核对日期。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    url: str
    checked_at: date

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("来源网址必须以 https:// 开头")
        return v


class Stop(BaseModel):
    """路线上的一站。"""

    model_config = ConfigDict(extra="forbid")

    place_id: str
    priority: Literal["core", "optional"] = "core"      # 天数不够 / 节奏紧时先砍 optional
    weight: int = Field(default=3, ge=1, le=5)          # 同一优先级里，先砍权重低的
    look_for: list[str] = Field(default_factory=list)   # 这一站看什么（2–3 条）
    why_here: str = ""                                  # 为什么这条线上要有它
    after: str | None = None                            # 必须排在同一章的哪一站之后
    sources: list[Source] = Field(default_factory=list)  # look_for / why_here 的依据

    @model_validator(mode="after")
    def _core_is_explained(self) -> Stop:
        if self.priority == "core" and not (self.look_for and self.why_here and self.sources):
            raise ValueError(f"{self.place_id}: core 站必须有 look_for、why_here 和来源")
        return self


class Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    title: str
    story: str = Field(min_length=20)                   # 这一章在讲什么（给导览用，150–250 字）
    sources: list[Source] = Field(min_length=1)
    stops: list[Stop] = Field(min_length=1)

    @model_validator(mode="after")
    def _after_refers_back(self) -> Chapter:
        seen: set[str] = set()
        for s in self.stops:
            if s.after is not None and s.after not in seen:
                raise ValueError(f"{self.id}: {s.place_id} 的 after={s.after!r} 必须是同一章里排在它前面的站")
            seen.add(s.place_id)
        return self


class Reading(BaseModel):
    """延伸阅读 / 观影：只列核对过真实存在的，由来源页面证明（作者、年份）。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    kind: Literal["book", "film"]
    creator: str
    year: int = Field(ge=1000, le=2100)
    note: str
    source: Source


class DaysAdvice(BaseModel):
    """天数建议（只是标注，不限制）：min = 推荐最少几天；suggested = 建议几天；max = 路线自己的内容大约够几天
    （再多的天用路线对应的主题补充）。"""

    model_config = ConfigDict(extra="forbid")

    min: int = Field(ge=1)
    suggested: int = Field(ge=1)
    max: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> DaysAdvice:
        if not self.min <= self.suggested <= self.max:
            raise ValueError("天数建议要满足 min ≤ suggested ≤ max")
        return self


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name_zh: str
    tagline: str
    thesis: str                                         # 主线，一句话
    track: Literal["history", "folk", "food", "other"] = "other"      # 兴趣线：页面按它分组
    base_theme: str                                     # 对应的地点库主题：编辑重排、行李等沿用它
    # sequential = 章按顺序一天一天走（默认）；pick-one = 每章是一类，每天每类挑一个（美食）
    layout: Literal["sequential", "pick-one"] = "sequential"
    days: DaysAdvice
    chapters: list[Chapter] = Field(min_length=1)
    reading: list[Reading] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)      # 策展说明（如「蓬皮杜整修，未排入」）

    def stops(self) -> list[tuple[Chapter, Stop]]:
        return [(c, s) for c in self.chapters for s in c.stops]

    def place_ids(self) -> list[str]:
        return [s.place_id for _c, s in self.stops()]

    def chapter_of(self, place_id: str) -> Chapter | None:
        return next((c for c in self.chapters if any(s.place_id == place_id for s in c.stops)), None)

    @model_validator(mode="after")
    def _no_duplicate_stops(self) -> Route:
        ids = self.place_ids()
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.id}: 同一个地点不能在路线里出现两次")
        if len({c.id for c in self.chapters}) != len(self.chapters):
            raise ValueError(f"{self.id}: 章 id 重复")
        return self


class RoutesFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    city: str
    routes: list[Route]


def validate_against_library(route: Route, lib: PlaceLibrary) -> None:
    """路线和地点库对得上：地点都在库里、没有长期闭馆的、主题存在。"""
    if route.base_theme not in lib.themes:
        raise RouteError(f"{route.id}: base_theme {route.base_theme!r} 不在地点库的主题里")
    for _c, s in route.stops():
        place = lib.places.get(s.place_id)
        if place is None:
            raise RouteError(f"{route.id}: 地点 {s.place_id!r} 不在地点库里")
        if place.long_term_closed:
            raise RouteError(f"{route.id}: {s.place_id} 长期闭馆，不能排进路线")


@lru_cache(maxsize=8)
def load_routes(city: str = DEFAULT_CITY) -> dict[str, Route]:
    """某座城市的全部深度路线（id → Route）。没有路线文件 = 没有路线（不是错误）。"""
    path = _DIR / f"{city}.json"
    if not path.exists():
        return {}
    data = RoutesFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    lib = load_library(city)
    out: dict[str, Route] = {}
    for r in data.routes:
        validate_against_library(r, lib)
        out[r.id] = r
    return out


def get_route(city: str, route_id: str) -> Route:
    routes = load_routes(city)
    if route_id not in routes:
        raise RouteError(f"没有这条路线：{route_id!r}（可选：{', '.join(routes) or '无'}）")
    return routes[route_id]


# --------------------------------------------------------------------------- #
# 给页面看的摘要
# --------------------------------------------------------------------------- #
def summarize(route: Route, lib: PlaceLibrary, *, detail: bool = False) -> dict[str, Any]:
    """路线卡片（和展开的详情）用的数据：只给名字和一句话，不泄露整章故事以外的东西。"""
    def stop_view(s: Stop) -> dict[str, Any]:
        p = lib.places[s.place_id]
        return {"place_id": p.id, "name": bilingual(p), "name_zh": p.name_zh, "priority": s.priority,
                "why_here": s.why_here or None}

    chapters = [{"id": c.id, "title": c.title, "story": c.story, "stops": [stop_view(s) for s in c.stops]}
                for c in route.chapters]
    core = [lib.places[s.place_id].name_zh for _c, s in route.stops() if s.priority == "core"]
    seasons = [lib.places[pid].season for pid in route.place_ids()]
    # 限定：路线上每一站都是当季活动时，路线只在这些举办期里才有（最早开始到最晚结束）；只有一部分站是当季活动的不算
    limited = ({"start": min(s.start for s in seasons).isoformat(), "end": max(s.end for s in seasons).isoformat()}
               if seasons and all(seasons) else None)
    out: dict[str, Any] = {
        "id": route.id, "name_zh": route.name_zh, "tagline": route.tagline, "thesis": route.thesis,
        "days": route.days.model_dump(), "base_theme": route.base_theme,
        "track": route.track, "track_zh": TRACKS[route.track],
        "chapter_count": len(route.chapters), "stop_count": len(route.place_ids()),
        "core_count": len(core), "examples": core[:4],
        "chapter_titles": [c.title for c in route.chapters],
        "season": limited,
    }
    if detail:
        out.update(chapters=chapters, reading=[r.model_dump(mode="json") for r in route.reading], notes=route.notes)
    return out
