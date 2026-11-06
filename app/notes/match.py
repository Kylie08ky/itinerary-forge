"""把笔记里的一个写法对到地点库里的地点。全部是确定性规则，不让模型决定「这是哪个地点」。

    tier 3  写法 == 地点的中文名 / 英文名 / 别名            → 高
    tier 2  写法里包含地点名（「奥赛博物馆」里有「奥赛」）    → 中，取最长的名字
    tier 1  地点名里包含写法（「圣母院」在「巴黎圣母院（外观）」里）→ 中
    同一档里同样好地命中了多个不同地点（「老佛爷」）            → 低，让用户从候选里选
    名字后面跟着「脚下的 / 附近的 / 旁边的…」再带别的东西       → 跳过（那是地点旁边的另一样东西，不是这个地点）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.library import PlaceLibrary, _alias_groups
from app.notes.models import Confidence
from app.notes.text import Folded, fold

# 「购物 / outlet / 跟团」这几组别名是给意图识别用的泛称，不是某个地点
_GENERIC_ALIASES = {"购物", "shopping", "outlet", "血拼", "名品折扣", "团体游", "旅行团", "跟团", "guided tour"}
# 光秃秃的类别词（「公园」「博物馆」）不是某个地点，哪怕地点库里恰好只有一个名字带它
BARE_WORDS = {fold(w) for w in (
    "公园", "广场", "博物馆", "美术馆", "教堂", "大教堂", "花园", "餐厅", "餐馆", "咖啡馆", "咖啡店", "商店", "超市",
    "百货", "桥", "塔", "宫", "街", "大街", "市场", "书店", "剧院", "歌剧院", "酒店", "游船", "地铁", "车站", "景点",
    "museum", "church", "park", "cafe",
    "restaurant", "market", "bridge", "garden", "palace",
)}
_NEAR = re.compile(r"^\s*(脚下|附近|旁边|周边|周围|门口|对面|外面|里面|边上|底下|楼下)的[^\s，。,.！!？?；;]+")
_PAREN = re.compile(r"[（(][^）)]*[）)]")


@dataclass
class _Entry:
    folded: str
    raw: str
    place_id: str


@dataclass
class _Index:
    lib: PlaceLibrary
    entries: list[_Entry]
    groups: list[list[str]]                         # 非泛称的别名组（原写法）
    group_of: dict[str, list[str]] = field(default_factory=dict)      # 折叠后的写法 -> 它所在的别名组


_INDEXES: dict[int, _Index] = {}


def _index(lib: PlaceLibrary) -> _Index:
    cached = _INDEXES.get(id(lib))
    if cached and cached.lib is lib:
        return cached
    generic = {fold(x) for x in _GENERIC_ALIASES}
    groups = [sorted(g) for g in _alias_groups() if not ({fold(x) for x in g} & generic)]
    group_of: dict[str, list[str]] = {}
    for g in groups:
        for member in g:
            group_of[fold(member)] = g
    entries: list[_Entry] = []
    for p in lib.places.values():
        raws = {p.name_zh, p.name_en, _PAREN.sub("", p.name_zh).strip(), _PAREN.sub("", p.name_en).strip()}
        if p.name_local:
            raws.add(p.name_local)
        for r in list(raws):
            if (g := group_of.get(fold(r))) is not None:
                raws.update(g)
        seen: set[str] = set()
        for r in sorted(raws):
            f = fold(r)
            if len(f) >= 2 and f not in seen:
                seen.add(f)
                entries.append(_Entry(f, r, p.id))
    idx = _Index(lib, entries, groups, group_of)
    _INDEXES[id(lib)] = idx
    return idx


def _has_cjk(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in s)


def _long_enough(folded: str) -> bool:
    """太短的名字（中文 3 字以下、外文 5 字以下）容易撞上别的词，只在模型明确给出时才认。"""
    return len(folded) >= (3 if _has_cjk(folded) else 5)


def scan_names(lib: PlaceLibrary) -> list[str]:
    """能在原文里直接扫的名字：地点库里的名字、变体、别名。"""
    idx = _index(lib)
    out: dict[str, str] = {}
    for e in idx.entries:
        out.setdefault(e.folded, e.raw)
    for g in idx.groups:
        for m in g:
            out.setdefault(fold(m), m)
    return [raw for f, raw in out.items() if _long_enough(f)]


@dataclass
class Match:
    kind: Literal["matched", "unmatched", "skip"]
    confidence: Confidence | None = None
    place_ids: list[str] = field(default_factory=list)


def _near_other(raw: str, after: str, entry_raw: str) -> bool:
    """名字出现在写法里的位置之后，紧跟「脚下的小贩」这种说法吗？"""
    return any(_NEAR.match(raw[end:] + after) for _s, end in Folded(raw).find_all(entry_raw))


def match_mention(raw: str, lib: PlaceLibrary, *, after: str = "") -> Match:
    """`after` 是写法在原文里紧接着的后文，用来判断「铁塔脚下的小贩」。"""
    idx = _index(lib)
    m = fold(raw)
    if len(m) < 2 or m in BARE_WORDS:
        return Match("unmatched")
    forms = {m}
    if (g := idx.group_of.get(m)) is not None:                # 写法是某个别名组里的一个：整组都当作它的写法
        forms |= {fold(x) for x in g}

    exact: list[str] = []
    inside: dict[str, tuple[int, _Entry]] = {}                 # 地点 id -> (它被包含的最长名字的长度, 那个名字)
    within: list[str] = []
    for e in idx.entries:
        if e.folded in forms:
            if e.place_id not in exact:
                exact.append(e.place_id)
        elif e.folded in m and _long_enough(e.folded):
            best = inside.get(e.place_id)
            if best is None or len(e.folded) > best[0]:
                inside[e.place_id] = (len(e.folded), e)
        elif any(f in e.folded and len(f) >= 2 for f in forms) and e.place_id not in within:
            within.append(e.place_id)

    if (exact or within) and _NEAR.match(after):                # 写法本身就是地点名，但后文说的是它旁边的东西
        return Match("skip")
    if exact:
        return Match("matched", Confidence.high if len(exact) == 1 else Confidence.low, exact)
    if inside:
        longest = max(n for n, _e in inside.values())
        top = [(pid, e) for pid, (n, e) in inside.items() if n == longest]
        if _near_other(raw, after, top[0][1].raw):
            return Match("skip")
        ids = [pid for pid, _e in top]
        return Match("matched", Confidence.medium if len(ids) == 1 else Confidence.low, ids)
    # 两个字的泛词（「公园」）撞上好几个地点：不认
    if within and (len(within) == 1 or any(_long_enough(f) for f in forms)):
        return Match("matched", Confidence.medium if len(within) == 1 else Confidence.low, within)
    return Match("unmatched")
