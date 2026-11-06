"""文化入门（规格 09 十三）：选路线之前先了解一座城市——电影、纪录片、书、播客，每一部连到它带你看的地方。

和路线一样是**策展数据**（`data/culture/<城市>.json`）：作品的作者 / 导演 / 年份、「带你看的地方」都由来源页面证明
（见 scripts/build_culture.py），地点必须在地点库里。模型不推荐作品。

纯数据 + 校验，不联网、不调模型。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.cities import DEFAULT_CITY
from app.food import signature_stories
from app.library import Place, PlaceLibrary, load_library
from app.names import bilingual, fold
from app.routes import Source, load_routes

_DIR = Path(__file__).resolve().parents[1] / "data" / "culture"
# data/culture/<城市>.auto.json：scripts/build_culture_auto.py 从维基数据批量生成，可以随时重跑
AUTO_SUFFIX = ".auto"

# 名气门槛：维基百科至少有这么多个语言版本才收（太冷门的作品不放进行程里打扰人）。手工和自动收录都用它。
MIN_POPULARITY = 20

KINDS: dict[str, str] = {"film": "电影", "documentary": "纪录片", "book": "书", "podcast": "播客"}
_KIND_ORDER = list(KINDS)


class CultureError(ValueError):
    """文化入门数据不成立（地点不在库里、缺来源……）；数据闸门测试和加载时都会抛。"""


class Sees(BaseModel):
    """作品带你看的一个地方：note 说它在作品里是什么（场景 / 章节 / 人物）。"""

    model_config = ConfigDict(extra="forbid")

    place_id: str
    note: str = Field(min_length=4)


class Work(BaseModel):
    """一部作品。title 是原名（片名 / 书名 / 节目名），title_zh 是常用中文译名（可空）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    kind: Literal["film", "documentary", "book", "podcast"]
    title: str = Field(min_length=1)
    title_zh: str = ""
    creator: str = Field(min_length=1)                  # 导演 / 作者 / 主播
    year: int = Field(ge=1000, le=2100)                 # 首映 / 首版 / 开播年份
    summary: str = ""                                   # 一两句话：讲什么（手工策展才有；自动收录的不写，页面上不显示）
    sees: list[Sees] = Field(min_length=1)
    source: Source
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")   # 维基数据编号：去重和算名气用
    popularity: int = Field(default=0, ge=0)                         # 名气 = 维基百科有多少个语言版本（可核对的数字）

    @model_validator(mode="after")
    def _summary_is_a_real_sentence(self) -> Work:
        if self.summary and len(self.summary) < 10:
            raise ValueError(f"{self.id}: summary 要么不写，要么写成完整的一两句话")
        return self

    @model_validator(mode="after")
    def _no_duplicate_places(self) -> Work:
        ids = [s.place_id for s in self.sees]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.id}: sees 里有重复的地点")
        return self


class CultureFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str
    works: list[Work]


def validate_against_library(work: Work, lib: PlaceLibrary) -> None:
    for s in work.sees:
        p = lib.places.get(s.place_id)
        if p is None:
            raise CultureError(f"作品 {work.id!r} 的地点 {s.place_id!r} 不在地点库里")
        if p.long_term_closed:
            raise CultureError(f"作品 {work.id!r} 的地点 {s.place_id!r} 长期闭馆，不能放进「带你看的地方」")


@lru_cache(maxsize=8)
def load_culture(city: str = DEFAULT_CITY) -> dict[str, Work]:
    """某座城市的全部作品（id → Work，按文件里的顺序）。没有文件 = 没有作品（不是错误）。"""
    curated = _DIR / f"{city}.json"
    auto = _DIR / f"{city}{AUTO_SUFFIX}.json"
    if not curated.exists() and not auto.exists():
        return {}
    lib = load_library(city)
    out: dict[str, Work] = {}
    known_qids: set[str] = set()
    for path, is_auto in ((curated, False), (auto, True)):
        if not path.exists():
            continue
        for w in CultureFile.model_validate(json.loads(path.read_text(encoding="utf-8"))).works:
            if is_auto and (w.id in out or (w.wikidata and w.wikidata in known_qids)):
                continue                                    # 人工策展的优先，自动收录的不重复
            if w.id in out:
                raise CultureError(f"作品 id 重复：{w.id!r}")
            if w.popularity < MIN_POPULARITY:
                raise CultureError(f"作品 {w.id!r} 名气不够（{w.popularity} < {MIN_POPULARITY} 个语言版本），不收")
            validate_against_library(w, lib)
            out[w.id] = w
            if w.wikidata:
                known_qids.add(w.wikidata)
    return out


def get_work(city: str, work_id: str) -> Work:
    works = load_culture(city)
    if work_id not in works:
        raise CultureError(f"没有这部作品：{work_id!r}")
    return works[work_id]


# --------------------------------------------------------------------------- #
# 地点名 → 在来源文字里怎么认（批量核对脚本和测试共用）
# --------------------------------------------------------------------------- #
_PREFIX = re.compile(
    r"^(mus[ée]e|jardin|cath[ée]drale|[ée]glise|ch[âa]teau|palais|place|pont|avenue|rue|centre|tour|"
    r"cimeti[èe]re|caveau|bourse|passage|galerie|march[ée])\s+(de la |de l'|du |des |d'|de |national )?",
    re.IGNORECASE)
_TAIL = re.compile(r"\b(walk|exterior|shopping|rooftop|cruise|boutiques?|concept stores)\b", re.IGNORECASE)
_MIN_KEY = 5                                                # 折叠后少于 5 个字符的名字太容易误中，不用


def name_keys(p: Place) -> list[str]:
    """认一个地点的写法：英文名 / 当地名 / 中文名，去掉括号和「walk」之类的尾巴，按「&、/、–」拆开，
    再给「Musée d'Orsay」这类名字多一个去掉前缀的写法（「Orsay」）。返回折叠后的字符串，去重。"""
    raw: list[str] = []
    for name in (p.name_en, p.name_local or "", p.name_zh):
        if not name:
            continue
        name = re.sub(r"[（(].*?[）)]", " ", name)
        name = _TAIL.sub(" ", name)
        for part in re.split(r"\s+[&/–-]\s+|\s*/\s*|、", name):
            part = part.strip()
            if not part:
                continue
            raw.append(part)
            stripped = _PREFIX.sub("", part).strip()
            if stripped and stripped != part:
                raw.append(stripped)
    keys: list[str] = []
    for r in raw:
        k = fold(r)
        if len(k) >= _MIN_KEY and k not in keys:
            keys.append(k)
    return keys


def mentions(p: Place, text: str) -> bool:
    folded = fold(text)
    return any(k in folded for k in name_keys(p))


# --------------------------------------------------------------------------- #
# 给页面看的摘要
# --------------------------------------------------------------------------- #
def routes_through(city: str, place_ids: list[str]) -> list[dict[str, str]]:
    """哪些深度路线经过这些地点（派生，不手工维护）：按经过的个数从多到少。"""
    wanted = set(place_ids)
    hits: list[tuple[int, str, str]] = []
    for r in load_routes(city).values():
        n = len(wanted & set(r.place_ids()))
        if n:
            hits.append((n, r.id, r.name_zh))
    hits.sort(key=lambda h: -h[0])
    return [{"id": rid, "name_zh": name} for _n, rid, name in hits]


def summarize(work: Work, lib: PlaceLibrary, city: str = DEFAULT_CITY) -> dict[str, Any]:
    places = []
    for s in work.sees:
        p = lib.places[s.place_id]
        places.append({"place_id": p.id, "name": bilingual(p), "name_zh": p.name_zh, "note": s.note})
    return {
        "id": work.id, "kind": work.kind, "kind_zh": KINDS[work.kind], "title": work.title,
        "title_zh": work.title_zh or None, "creator": work.creator, "year": work.year, "summary": work.summary,
        "places": places, "routes": routes_through(city, [s.place_id for s in work.sees]),
        "source": {"title": work.source.title, "url": work.source.url,
                   "checked_at": work.source.checked_at.isoformat()},
    }


def culture_overview(city: str = DEFAULT_CITY) -> list[dict[str, Any]]:
    lib = load_library(city)
    works = sorted(load_culture(city).values(), key=lambda w: (_KIND_ORDER.index(w.kind), *_fame_order(w)))
    return [summarize(w, lib, city) for w in works]


# --------------------------------------------------------------------------- #
# 地点 → 故事（规格 09 十五）：读取时现算，不存进行程
# --------------------------------------------------------------------------- #
def _fame_order(w: Work) -> tuple[int, int, str]:
    """名气大的在前（语言版本数多），一样就年份早的在前，再按 id 保证稳定。"""
    return -w.popularity, w.year, w.id


def stories_for_place(city: str, place_id: str) -> list[dict[str, Any]]:
    """一个地点的故事：先是它的招牌（美食，app/food.py），再是它出现在哪些作品里（作品 + 它在作品里是什么 + 来源）。"""
    out: list[dict[str, Any]] = signature_stories(city, place_id)
    for w in sorted(load_culture(city).values(), key=_fame_order):
        for s in w.sees:
            if s.place_id == place_id:
                out.append({
                    "work_id": w.id, "kind": w.kind, "kind_zh": KINDS[w.kind], "title": w.title,
                    "title_zh": w.title_zh or None, "creator": w.creator, "year": w.year, "note": s.note,
                    "source": {"title": w.source.title, "url": w.source.url},
                })
    return out


def stories_by_place(city: str, place_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """一份行程里所有有故事的地点（没有故事的不出现）。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for pid in dict.fromkeys(place_ids):
        found = stories_for_place(city, pid)
        if found:
            out[pid] = found
    return out


def recap(city: str, place_ids: list[str]) -> list[dict[str, Any]]:
    """这趟旅行走过的作品：只列至少有一个地点被排进行程的作品，每部写「你会经过的地点」；经过的多的在前。"""
    works = load_culture(city)
    if not works:
        return []
    lib = load_library(city)
    wanted = set(place_ids)
    rows: list[tuple[int, Work, list[dict[str, str]]]] = []
    for w in works.values():
        visited = [{"place_id": s.place_id, "name": bilingual(lib.places[s.place_id]),
                    "name_zh": lib.places[s.place_id].name_zh, "note": s.note}
                   for s in w.sees if s.place_id in wanted]
        if visited:
            rows.append((len(visited), w, visited))
    rows.sort(key=lambda r: (-r[0], *_fame_order(r[1])))
    return [{
        "id": w.id, "kind": w.kind, "kind_zh": KINDS[w.kind], "title": w.title, "title_zh": w.title_zh or None,
        "creator": w.creator, "year": w.year, "summary": w.summary, "visited": visited,
        "source": {"title": w.source.title, "url": w.source.url, "checked_at": w.source.checked_at.isoformat()},
    } for _n, w, visited in rows]


def story_text(s: dict[str, Any]) -> str:
    """一条故事的一句话：「电影《午夜巴黎》（Midnight in Paris）：拍摄地点包括先贤祠附近的街道。」"""
    name = s["title_zh"] or s["title"]
    orig = f"（{s['title']}）" if s["title_zh"] else ""
    if s["kind"] == "food":
        return f"{s['kind_zh']}：{name}{orig}：{s['note']}"
    return f"{s['kind_zh']}《{name}》{orig}：{s['note']}"
