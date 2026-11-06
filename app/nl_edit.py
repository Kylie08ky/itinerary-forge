"""M04 S2：自然语言修改。

    用户的一句话 ──► 模型翻译成「操作列表」──► 逐个校验 ──► 走编辑函数（app.editing）──► 新行程

模型只做「理解意图」：它看不到、也改不了行程本身，只能输出 add / remove / swap / move / lock 这几种操作，
而且新增的地点只能是地点库里的 id。每个操作都经过和手动拖拽完全相同的编辑函数，
所以闭馆日、锁定、购票清单这些规则一条不少；做不到或没看懂的操作被跳过并告诉用户。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from jinja2 import Template
from pydantic import TypeAdapter, ValidationError

from app.editing import AddPlace, Edit, EditError, MoveToDay, RemovePlace, SetLock, SwapPlace, apply_edit
from app.library import PlaceLibrary, load_library
from app.llm import LLMClient, LLMError
from app.llm_trace import trace_context
from app.schemas import Itinerary, TravelRequest

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_SYSTEM = (_PROMPT_DIR / "nl_edit_system.txt").read_text(encoding="utf-8")
_USER = Template((_PROMPT_DIR / "nl_edit_user.jinja").read_text(encoding="utf-8"))

MAX_EDITS = 5
_WEEKDAY = "一二三四五六日"
_EDIT = TypeAdapter(Edit)
# 自然语言只允许这几种；重排整份行程这类大动作要用户自己点按钮
_ALLOWED = (AddPlace, RemovePlace, SwapPlace, MoveToDay, SetLock)


class NlEditError(RuntimeError):
    """没法处理这句话：返回的内容解析不了、这份行程不是按主题生成的。"""


class ModelUnavailable(NlEditError):
    """模型连不上 / 调用失败。"""


@dataclass
class NlRevision:
    itinerary: Itinerary
    applied: list[str] = field(default_factory=list)      # 做成了的改动，人话
    skipped: list[str] = field(default_factory=list)      # 没做成的操作和原因
    touched: list[date] = field(default_factory=list)
    explanation: str = ""


def describe(edit: Edit, it: Itinerary, lib: PlaceLibrary) -> str:
    def name(pid: str) -> str:
        try:
            return lib.get_place(pid).name_zh
        except KeyError:
            return pid

    if isinstance(edit, AddPlace):
        return f"在 {edit.date} 加入「{name(edit.place_id)}」"
    if isinstance(edit, RemovePlace):
        return f"删除「{name(edit.place_id)}」"
    if isinstance(edit, SwapPlace):
        return f"把「{name(edit.place_id)}」换成「{name(edit.new_place_id)}」"
    if isinstance(edit, MoveToDay):
        return f"把「{name(edit.place_id)}」移到 {edit.to_date}"
    if isinstance(edit, SetLock):
        return f"{'锁定' if edit.locked else '解锁'}「{name(edit.place_id)}」"
    return "其他操作"                                                      # pragma: no cover


def _prompt(it: Itinerary, lib: PlaceLibrary, feedback: str) -> tuple[str, str]:
    in_trip = {i.place_id for d in it.days for i in d.items if i.place_id}
    days = [{
        "date": d.date.isoformat(), "weekday": _WEEKDAY[d.date.weekday()],
        "places": [{"id": i.place_id, "name": i.title, "locked": i.locked} for i in d.items if i.place_id],
    } for d in it.days]
    candidates = [{"id": p.id, "name": p.name_zh, "category": p.category.value, "area": p.area}
                  for p in lib.places.values() if p.id not in in_trip and not p.long_term_closed]
    return _SYSTEM, _USER.render(feedback=" ".join(feedback.split()), days=days, candidates=candidates)


@dataclass
class Translation:
    """模型把一句话翻译出来的操作（还没执行）。"""

    edits: list[Edit] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    explanation: str = ""


def parse_ops(raw_edits: list, translation: Translation | None = None) -> Translation:
    """把原始操作 dict 逐个校验成 Edit：认不出的、不允许的跳过；超过上限的丢掉。

    模型的输出和用户确认时回传的操作都走这里，所以确认后的操作也经过同样的服务端校验。
    """
    out = translation or Translation()
    for n, raw_edit in enumerate(raw_edits):
        if n >= MAX_EDITS:
            out.skipped.append(f"一句话最多改 {MAX_EDITS} 处，后面的操作没有执行")
            break
        try:
            edit = _EDIT.validate_python(raw_edit)
            if not isinstance(edit, _ALLOWED):
                raise ValueError("这类操作不能用一句话触发")
        except (ValidationError, ValueError, TypeError):
            out.skipped.append(f"有一个操作没看懂，已跳过：{json.dumps(raw_edit, ensure_ascii=False)[:80]}")
            continue
        out.edits.append(edit)
    return out


def translate(it: Itinerary, req: TravelRequest, feedback: str, client: LLMClient,
              library: PlaceLibrary | None = None) -> Translation:
    if it.generation is None:
        raise NlEditError("这份行程不是按主题生成的，暂时只能手动编辑（拖拽 / 换一个）")
    lib = library or load_library(req.city_id)
    system, user = _prompt(it, lib, feedback)
    try:
        with trace_context(purpose="nl_edit"):
            raw = client.complete_json(system=system, user=user, schema=_schema())
    except LLMError as exc:
        raise ModelUnavailable(f"模型暂时不可用，没法理解这句话：{exc}") from exc
    try:
        data = json.loads(raw)
        raw_edits = data["edits"]
        if not isinstance(raw_edits, list):
            raise TypeError
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise NlEditError("模型返回的内容无法解析，请换个说法再试") from exc
    return parse_ops(raw_edits, Translation(explanation=str(data.get("explanation", ""))[:200]))


def apply_ops(it: Itinerary, req: TravelRequest, t: Translation, library: PlaceLibrary | None = None) -> NlRevision:
    lib = library or load_library(req.city_id)
    out = NlRevision(itinerary=it, skipped=list(t.skipped), explanation=t.explanation)
    for edit in t.edits:
        text = describe(edit, out.itinerary, lib)
        try:
            res = apply_edit(out.itinerary, req, edit, lib)
        except EditError as exc:
            out.skipped.append(f"没能{text}：{exc}")
            continue
        out.itinerary = res.itinerary
        out.applied.append(text)
        out.touched = sorted({*out.touched, *res.touched})
    return out


def nl_revise(it: Itinerary, req: TravelRequest, feedback: str, client: LLMClient,
              library: PlaceLibrary | None = None) -> NlRevision:
    return apply_ops(it, req, translate(it, req, feedback, client, library), library)


def _schema() -> dict:
    return {"type": "object", "properties": {"edits": {"type": "array"}, "explanation": {"type": "string"}}}
