"""M12 笔记导入的数据契约。只有 pydantic，不依赖别的模块（schemas.py 要用它们）。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

SNIPPET_MAX = 200           # 随行程保存的原文片段上限（规格 M12「笔记正文不入库」）
MAX_NOTE_TEXT = 8000        # 一次粘贴的正文上限
MAX_MENTIONS = 40           # 一次最多识别多少个地点
MAX_NOTE_ITEMS = 60         # 一份行程最多带多少条笔记来源 / 待核实


class Confidence(StrEnum):
    high = "high"           # 写法和地点库里的名字 / 别名完全相同
    medium = "medium"       # 一方包含另一方
    low = "low"             # 同样好地命中了多个地点，要用户自己选


class TipKind(StrEnum):
    booking = "booking"     # 预约 / 订票
    queue = "queue"         # 排队
    duration = "duration"   # 游玩时长
    closed = "closed"       # 闭馆日
    cash = "cash"           # 只收现金
    safety = "safety"       # 防扒 / 注意财物
    price = "price"         # 价格


class NoteTip(BaseModel):
    """从原文片段里用规则抽出的一条提示。`text` 是它对应的那一小段原文；`conflict` 非空 = 和地点库对不上。"""

    kind: TipKind
    label: str = Field(max_length=40)
    text: str = Field(default="", max_length=120)
    conflict: str | None = Field(default=None, max_length=160)


class Candidate(BaseModel):
    place_id: str
    name_zh: str
    area: str = ""


class ParsedPlace(BaseModel):
    """确认页里「在地点库里」的一行。置信度 low 时 `place_id` 为空，让用户从 `candidates` 里选。"""

    mention: str
    place_id: str | None = None
    name_zh: str | None = None
    area: str | None = None
    confidence: Confidence
    candidates: list[Candidate] = Field(default_factory=list)
    snippet: str = ""
    tips: list[NoteTip] = Field(default_factory=list)
    complaint: bool = False                # 原文像在吐槽（默认不替用户决定去不去）
    closed_long_term: bool = False         # 地点库里标了长期闭馆（整修中）


class PendingPlace(BaseModel):
    """笔记里提到、地点库里没有的地点：事实未核对，只显示，不进排程。"""

    name: str = Field(min_length=1, max_length=60)
    snippet: str = Field(default="", max_length=SNIPPET_MAX)


class NoteSource(BaseModel):
    """某个地点是从哪段笔记来的。随行程需求保存，所以复制 / 平移 / 回滚都带着。"""

    place_id: str
    snippet: str = Field(default="", max_length=SNIPPET_MAX)
    tips: list[NoteTip] = Field(default_factory=list, max_length=8)


class ParseResult(BaseModel):
    matched: list[ParsedPlace] = Field(default_factory=list)
    pending: list[PendingPlace] = Field(default_factory=list)
    model_used: bool = False               # False = 只用了地点库里的名字，没读出库外地点
    dropped_instructions: int = 0          # 被当作「指令」忽略掉的句子数
    source: str = "text"                   # text | url
    source_host: str | None = None
