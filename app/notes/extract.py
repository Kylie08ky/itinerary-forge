"""抽取：① 在原文里直接扫地点库里的名字（确定性）；② 让模型读出库外的地点名。

模型只被允许输出名字：返回里的网址、价格、立场、提示一律不看；名字必须在原文里找得到，找不到就是编造，丢掉。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.cities import supported_cities
from app.library import PlaceLibrary
from app.llm import LLMClient, LLMError
from app.llm_trace import trace_context
from app.notes.match import BARE_WORDS, scan_names
from app.notes.models import MAX_MENTIONS
from app.notes.text import Folded, fold

_SYSTEM = (Path(__file__).resolve().parent.parent / "prompts" / "note_extract_system.txt").read_text(encoding="utf-8")
_SCHEMA = {
    "type": "object",
    "properties": {"places": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}},
                                                          "required": ["name"]}}},
    "required": ["places"],
}
_STOP = {fold(w) for w in (
    "法国", "欧洲", "市区", "市中心", "塞纳河", "france", "europe", "地铁", "机场", "戴高乐机场",
    *(n for c in supported_cities() for n in (c.name_zh, c.name_en, *c.aliases)),      # 城市名本身不是地点
)}
# 外文名字后面常跟一个说明它是什么店的中文词：「Berthillon 冰淇淋」→「Berthillon」
_KIND_SUFFIX = re.compile(
    r"^(?P<name>[A-Za-zÀ-ÿŒœ0-9'’&.\- ]{3,}?)\s*(?:冰淇淋|冰激凌|书店|餐厅|咖啡馆|咖啡店|面包店|甜品店|酒吧|商店|店)$")


class ModelFailed(RuntimeError):
    """模型没开 / 超时 / 返回的不是约定的 JSON。"""


class _Reply(BaseModel):
    places: list[dict]


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    source: str            # scan | model

    @property
    def length(self) -> int:
        return self.end - self.start


def ask_model(text: str, client: LLMClient) -> list[str]:
    """让模型读出笔记里的地点名。正文放在 <note> 里，且去掉正文里自带的 </note>，防止它提前结束这个块。"""
    body = re.sub(r"</?\s*note\s*>", "", text, flags=re.IGNORECASE)
    try:
        with trace_context(purpose="note_extract"):
            raw = client.complete_json(system=_SYSTEM, user=f"<note>\n{body}\n</note>", schema=_SCHEMA)
    except LLMError as exc:
        raise ModelFailed(str(exc)) from exc
    try:
        reply = _Reply.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise ModelFailed(f"模型返回的不是约定的 JSON：{exc}") from exc
    names: list[str] = []
    for item in reply.places:
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and (name := name.strip()):
            names.append(name)
    return names


_PAGE_MARK = re.compile(r"\s*[pP]\s?\d+(?:\s?[-–~]\s?\d+)?\s*$")        # 「旋转木马p7」的页码标记
_GENERIC_THINGS = {fold(w) for w in ("纪念品店", "花车巡游", "巡游", "烟花秀", "吸管")}


def clean_name(name: str) -> str:
    name = _PAGE_MARK.sub("", name.strip()).strip()
    m = _KIND_SUFFIX.match(name)
    return m.group("name").strip() if m else name


def is_noise(name: str) -> bool:
    f = fold(name)
    if len(f) < 2 or f in _STOP or f in BARE_WORDS or f in _GENERIC_THINGS or f.isdigit():
        return True
    if re.fullmatch(r"[pP]\d+(?:[-–~]\d+)?", name.strip()):                          # 单独的「p6」「p3-4」
        return True
    # 单个全小写外文词（parc / adventure）：专名都有大写
    return bool(re.fullmatch(r"[a-z]+", name.strip()))


def find_spans(folded: Folded, lib: PlaceLibrary, model_names: list[str]) -> list[Span]:
    """原文里所有「可能是地点」的位置：扫到的库内名字 + 模型给的名字（必须在原文里找得到）。

    重叠时留最长的（「老佛爷百货」盖过「老佛爷」，「Shakespeare and Company 书店」盖过它里面的子串）。
    """
    cands: list[Span] = []
    for name in scan_names(lib):
        cands += [Span(s, e, "scan") for s, e in folded.find_all(name)]
    for name in model_names:
        if is_noise(name):
            continue
        cands += [Span(s, e, "model") for s, e in folded.find_all(name)]
    cands.sort(key=lambda c: (-c.length, c.start, c.source != "scan"))
    taken: list[Span] = []
    for c in cands:
        if not any(c.start < t.end and t.start < c.end for t in taken):
            taken.append(c)
    taken.sort(key=lambda s: s.start)
    return taken[: MAX_MENTIONS * 3]
