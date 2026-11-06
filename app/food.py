"""美食地图（规格 09 十七）：名菜名点 + 「招牌」（某个场所卖某样东西）。

和作品一样是**策展数据**（`data/food/<城市>.json`），由 scripts/build_food.py 批量核对：名菜在来源页面里出现，
「店名 + 菜名」在同一个来源页面里出现，数字有出处，名气够。页面里没有这样的出处就不写——不是每个场所都有招牌。

招牌在读取时折进「地点的故事」（app/culture.py 的 stories_for_place），行程条目、地点卡、今日页、分享页、导出自动带上。
纯数据 + 校验，不联网、不调模型。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.cities import DEFAULT_CITY, load_city
from app.citybuild.store import generated_path
from app.library import load_library
from app.names import bilingual
from app.routes import Source

_DIR = Path(__file__).resolve().parents[1] / "data" / "food"

# 名气门槛：维基百科至少有这么多个语言版本才收。比作品的 20 低——按 20 算，蒙布朗（17）、血鸭（12）和一半老字号都进不来
MIN_POPULARITY = 10

KINDS: dict[str, str] = {"bread": "面包", "pastry": "甜点", "dish": "菜", "drink": "饮品"}
SIGNATURE_LABEL = "招牌"


class FoodError(ValueError):
    """美食数据不成立（场所不在库里、缺来源……）；数据闸门测试和加载时都会抛。"""


class Signature(BaseModel):
    """一个场所的招牌：来源页面里同时提到这家店和这样东西（维基百科或店家官网）。"""

    model_config = ConfigDict(extra="forbid")

    place_id: str
    note: str = Field(min_length=4)
    source: Source


class Dish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    kind: Literal["bread", "pastry", "dish", "drink"]
    name_zh: str = Field(min_length=1)
    zh_wiki: str = Field(min_length=1)          # 中文维基百科上能证明这个译名的标题（简繁转换后存在）
    name_local: str = Field(min_length=1)       # 法语名
    summary: str = Field(min_length=10)         # 是什么、怎么来的：一句话，数字和年份来自来源页面
    source: Source
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")
    popularity: int = Field(default=0, ge=0)    # 名气 = 维基百科有多少个语言版本
    signatures: list[Signature] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_places(self) -> Dish:
        ids = [s.place_id for s in self.signatures]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.id}: signatures 里有重复的地点")
        return self


class FoodFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str
    dishes: list[Dish]


@lru_cache(maxsize=8)
def load_food(city: str = DEFAULT_CITY) -> dict[str, Dish]:
    """某座城市的全部名菜名点（id → Dish，按文件里的顺序）。没有文件 = 没有（不是错误）。"""
    path = _DIR / f"{city}.json"
    if not path.exists():
        return {}
    lib = load_library(city)
    out: dict[str, Dish] = {}
    for d in FoodFile.model_validate(json.loads(path.read_text(encoding="utf-8"))).dishes:
        if d.id in out:
            raise FoodError(f"名菜 id 重复：{d.id!r}")
        if d.popularity < MIN_POPULARITY:
            raise FoodError(f"名菜 {d.id!r} 名气不够（{d.popularity} < {MIN_POPULARITY} 个语言版本），不收")
        for s in d.signatures:
            p = lib.places.get(s.place_id)
            if p is None:
                raise FoodError(f"名菜 {d.id!r} 的招牌场所 {s.place_id!r} 不在地点库里")
            if p.long_term_closed:
                raise FoodError(f"名菜 {d.id!r} 的招牌场所 {s.place_id!r} 长期闭馆，不能放进招牌")
        out[d.id] = d
    return out


def signature_stories(city: str, place_id: str) -> list[dict[str, Any]]:
    """一个场所的招牌，写成和作品故事同一个形状（行程条目、地点卡、今日页、导出共用一套显示）。"""
    out: list[dict[str, Any]] = []
    for d in load_food(city).values():
        for s in d.signatures:
            if s.place_id == place_id:
                out.append({
                    "work_id": f"food-{d.id}", "kind": "food", "kind_zh": SIGNATURE_LABEL, "title": d.name_local,
                    "title_zh": d.name_zh, "creator": "", "year": None, "note": s.note,
                    "source": {"title": s.source.title, "url": s.source.url},
                })
    return out


class GeneratedDish(BaseModel):
    """现建城市的名菜（规格 10 H22）：中文维基百科饮食分类里的一个条目，简介是它的原文前两句。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^dish-[0-9a-f]{8}$")
    name_zh: str = Field(min_length=1)
    zh_wiki: str = Field(min_length=1)
    summary: str = Field(min_length=10)
    source: Source
    sitelinks: int = Field(ge=0)
    views: int | None = Field(default=None, ge=0)
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")
    name_local: str | None = None        # 外国城市：维基数据里当地语言的标签（「ragù napoletano」）


class GeneratedDishFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str
    city_zh: str
    city_local: str | None = None         # 城市的当地语言名（「Napoli」），外国城市才有
    category: str                        # 中文维基百科的分类（没有就是空）
    category_note: str | None = None     # 卡底那行来源说明，含当地语言维基的分类
    built_at: str
    dishes: list[GeneratedDish]


def load_generated_dishes(city: str) -> GeneratedDishFile | None:
    """现建城市的名菜；没有文件（没有饮食分类的城市）或文件坏了 = 没有，不报错。"""
    path = generated_path("dishes", city)
    if not path.exists():
        return None
    try:
        return GeneratedDishFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except ValueError:
        return None


def _frame(city: str) -> dict[str, Any]:
    """每一行名菜都带城市中文名和中心点：前端用它拼搜索链接（国内大众点评 / 小红书、国外 Google 地图）。"""
    c = load_city(city)
    return {"city_zh": c.name_zh, "near": [c.center[0], c.center[1]]}


def food_overview(city: str = DEFAULT_CITY) -> list[dict[str, Any]]:
    """给参考页 / 行程页的「美食」一节：每样名菜 + 它的招牌场所（没有招牌的不写去哪吃）。
    有策展数据（巴黎）用策展数据；现建城市用中文维基百科饮食分类里的名菜（没有就是空）。"""
    if not (_DIR / f"{city}.json").exists():
        auto = load_generated_dishes(city)
        if not auto:
            return []
        frame = {**_frame(city), "city_local": auto.city_local or ""}
        note = auto.category_note or f"取自中文维基百科「{auto.category}」分类"
        return [{"id": d.id, "kind": "dish", "kind_zh": "名菜", "name_zh": d.name_zh, "name_local": d.name_local or "",
                 "summary": d.summary, "source": {"title": d.source.title, "url": d.source.url,
                                                  "checked_at": d.source.checked_at.isoformat()},
                 "places": [], "generated": True, "category": auto.category,
                 "category_note": note, **frame} for d in auto.dishes]
    frame = _frame(city)
    lib = load_library(city)
    rows = []
    for d in load_food(city).values():
        rows.append({
            "id": d.id, "kind": d.kind, "kind_zh": KINDS[d.kind], "name_zh": d.name_zh, "name_local": d.name_local,
            "summary": d.summary,
            "source": {"title": d.source.title, "url": d.source.url, "checked_at": d.source.checked_at.isoformat()},
            "places": [{"place_id": s.place_id, "name": bilingual(lib.places[s.place_id]), "note": s.note,
                        "source": {"title": s.source.title, "url": s.source.url}} for s in d.signatures],
            **frame,
        })
    return rows
