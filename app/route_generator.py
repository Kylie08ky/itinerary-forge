"""深度游学路线 → 行程（规格 09）。

    request + route ──► 路线排程（确定性，app/scheduler/route_mode）──► 行程骨架 ──► 质检 ──► 导览

事实（地点、顺序、时间、看点、理由、延伸阅读）只来自路线数据和地点库。模型只允许把每一天的导览写顺；
生成后逐条检查：出现网址、价格、钟点、开放 / 闭馆表述，或「」《》里引了不在路线数据里的名字、写了数据里没有的数字，
整段丢弃，回退到策展原文拼成的导览。模型不可用时同样回退，并在 generation.narration_note 里写明原因。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path

from jinja2 import Template
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.library import PlaceLibrary, load_library
from app.llm import LLMClient, LLMError
from app.llm_trace import trace_context
from app.names import bilingual
from app.routes import Route, RouteError, get_route
from app.scheduler.route_mode import chapter_label, schedule_route
from app.schemas import Day, Itinerary, RouteInfo, RouteReading, TravelRequest
from app.validator import validate_itinerary

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_SYSTEM = (_PROMPT_DIR / "route_narrate_system.txt").read_text(encoding="utf-8")
_USER = Template((_PROMPT_DIR / "route_narrate_user.jinja").read_text(encoding="utf-8"))

_FORBIDDEN = re.compile(r"(https?://|www\.|\.com|\.fr|€|欧元|\d{1,2}[:：]\d{2}|闭馆|休馆|营业|开放时间|门票)", re.I)
_QUOTED = re.compile(r"[「《]([^」》]+)[」》]")
_MIN_GUIDE, _MAX_GUIDE = 60, 360


class DayGuide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    guide: str = ""


class GuideReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: list[DayGuide] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 每天的材料：这一天讲哪些章、去哪几站（给模板导览和模型共用）
# --------------------------------------------------------------------------- #
def _day_material(route: Route, lib: PlaceLibrary, itinerary: Itinerary,
                  chapters_by_day: dict[str, list[str]]) -> list[dict]:
    """按日期给出：章节（第一次出现带故事，之后的天只写「接着」）、站（名称、看什么、为什么）。"""
    by_id = {c.id: c for c in route.chapters}
    stop_by_place = {s.place_id: s for _c, s in route.stops()}
    seen: set[str] = set()
    out = []
    for day in itinerary.days:
        ids = chapters_by_day.get(day.date.isoformat(), [])
        chapters = []
        for cid in ids:
            if route.layout == "pick-one":                    # 每一站自己带说明，一天顶部不再讲章的介绍
                continue
            chapters.append({"title": by_id[cid].title, "story": "" if cid in seen else by_id[cid].story})
            seen.add(cid)
        stops = []
        for it in day.items:
            st = stop_by_place.get(it.place_id or "")
            if st is None:
                continue
            stops.append({"place_id": st.place_id, "name": bilingual(lib.places[st.place_id]),
                          "name_zh": lib.places[st.place_id].name_zh, "look_for": st.look_for,
                          "why_here": st.why_here})
        out.append({"date": day.date.isoformat(), "label": chapter_label(route, ids), "chapters": chapters,
                    "stops": stops})
    return out


def template_guide(material: dict) -> str:
    """策展原文拼成的导览（模型不可用 / 不合格时用）：只有章节故事。
    当天的顺序就是下面的时间轴，每一站为什么在这里写在这一站自己下面，不在这里重复。"""
    if not material["stops"]:
        return ""
    parts = [c["story"] if c["story"] else f"接着「{c['title']}」往下走。" for c in material["chapters"]]
    return "\n".join(p for p in parts if p)


def _corpus(material: dict) -> str:
    bits = [c["title"] + c["story"] for c in material["chapters"]]
    for s in material["stops"]:
        bits += [s["name"], s["name_zh"], s["why_here"], *s["look_for"]]
    return "\n".join(bits)


def guide_ok(text: str, material: dict) -> bool:
    """模型写的导览能不能用：长度合适、没有网址 / 价格 / 钟点 / 开放表述，引号里的名字和数字都能在路线数据里找到。"""
    text = " ".join(text.split())
    if not _MIN_GUIDE <= len(text) <= _MAX_GUIDE or _FORBIDDEN.search(text):
        return False
    corpus = _corpus(material)
    if any(q not in corpus for q in _QUOTED.findall(text)):
        return False
    return all(n in corpus for n in re.findall(r"\d+", text))


def _narrate(material: list[dict], route: Route, client: LLMClient) -> tuple[dict[str, str], str | None]:
    """返回 ({日期: 导览}, 降级原因)；模型给的每一段单独检查，不合格的那天不在返回里。"""
    user = _USER.render(route=route.name_zh, thesis=route.thesis,
                        days=[m for m in material if m["stops"]])
    try:
        with trace_context(purpose="route-guide"):
            raw = client.complete_json(system=_SYSTEM, user=user, schema=GuideReply.model_json_schema())
        reply = GuideReply.model_validate_json(raw)
    except LLMError:
        return {}, "模型不可用，导览由策展原文拼成"
    except (ValidationError, json.JSONDecodeError):
        return {}, "模型返回的内容无法解析，导览由策展原文拼成"
    by_date = {m["date"]: m for m in material}
    out = {d.date.isoformat(): " ".join(d.guide.split()) for d in reply.days
           if d.date.isoformat() in by_date and guide_ok(d.guide, by_date[d.date.isoformat()])}
    if not out:
        return {}, "模型没有返回可用的导览，导览由策展原文拼成"
    return out, None


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def generate_route_trip(
    request: TravelRequest,
    *,
    client: LLMClient | None,
    library: PlaceLibrary | None = None,
    on_step: Callable[[str], None] | None = None,
):
    """按深度路线生成行程。返回 generator.GeneratedTrip（延迟导入，避免循环引用）。"""
    from app.generator import GeneratedTrip, _skeleton

    step = on_step or (lambda _name: None)
    step("library")
    lib = library or load_library(request.city_id)
    route = get_route(request.city_id, request.route or "")

    step("schedule")
    draft = schedule_route(request, route, lib)
    itinerary = _skeleton(request, draft, lib, route.name_zh)

    chapters_by_day = {d.date.isoformat(): d.chapters for d in draft.days}
    free_label = {d.date.isoformat(): d.label for d in draft.days if d.label}       # 路线之外的自由日
    stop_by_place = {s.place_id: s for _c, s in route.stops()}
    days: list[Day] = []
    for day in itinerary.days:
        ids = chapters_by_day.get(day.date.isoformat(), [])
        items = [it.model_copy(update={"look_for": list(stop_by_place[it.place_id].look_for),
                                       "why_here": stop_by_place[it.place_id].why_here})
                 if it.place_id in stop_by_place else it for it in day.items]
        titles = [c.title for c in route.chapters if c.id in ids]
        free = free_label.get(day.date.isoformat())
        if route.layout == "pick-one":                       # 一天的标题就是这天去的几家
            titles = [it.title for it in items if it.place_id in stop_by_place]
        days.append(day.model_copy(update={
            "items": items, "theme": " · ".join(titles) or free or day.theme,
            "chapter": chapter_label(route, ids) or free or None}))
    itinerary = itinerary.model_copy(update={"days": days})

    material = _day_material(route, lib, itinerary, chapters_by_day)
    guides = {m["date"]: template_guide(m) for m in material}

    step("validate")
    placed = {d.date.isoformat(): [it.place_id for it in d.items if it.place_id in stop_by_place]
              for d in itinerary.days}
    assert itinerary.generation is not None
    itinerary.generation.route = RouteInfo(
        id=route.id, name_zh=route.name_zh, thesis=route.thesis, baseline=placed, skipped=draft.route_skipped,
        notes=route.notes,
        reading=[RouteReading(title=r.title, kind=r.kind, creator=r.creator, year=r.year, note=r.note,
                              source_url=r.source.url) for r in route.reading])
    report = validate_itinerary(itinerary, request)

    step("narrate")
    note: str | None = "未启用模型，导览由策展原文拼成"
    narration = "template"
    if route.layout == "pick-one":
        note = None                                          # 每一站自带说明，没有导览要写
    elif client is not None:
        written, why = _narrate(material, route, client)
        if written:
            guides.update(written)
            narration, note = "llm", None
            if len(written) < sum(1 for m in material if m["stops"]):
                note = "部分天的导览没通过检查，已换成策展原文"
        else:
            note = why
    itinerary.generation.narration, itinerary.generation.narration_note = narration, note
    itinerary = itinerary.model_copy(update={"days": [
        d.model_copy(update={"guide": guides.get(d.date.isoformat()) or None}) for d in itinerary.days]})
    return GeneratedTrip(itinerary=itinerary, draft=draft, report=report)


def deviation(itinerary: Itinerary) -> bool:
    """你改过行程之后，路线站的安排和生成时不一样了（挪了天 / 删了 / 换了顺序 / 加了别的）。"""
    gen = itinerary.generation
    if gen is None or gen.route is None:
        return False
    now: dict[str, list[str]] = {}
    route_ids = {pid for ids in gen.route.baseline.values() for pid in ids}
    for d in itinerary.days:
        now[d.date.isoformat()] = [it.place_id for it in d.items if it.place_id in route_ids]
    return now != gen.route.baseline


def refresh_route_display(itinerary: Itinerary, city: str) -> Itinerary:
    """老的路线行程（10 月 27 日之前生成）补上「这一站为什么在这里」，并去掉策展原文拼的导览里的「今天的顺序」和逐站理由
    （它们现在写在每一站自己下面）。模型写的导览不动。读取时现算，返回新对象；不是路线行程、或没有要改的，原样返回。"""
    gen = itinerary.generation
    if gen is None or gen.route is None:
        return itinerary
    try:
        route = get_route(city, gen.route.id)
    except RouteError:
        return itinerary
    lib = load_library(city)
    stops = {s.place_id: s for _c, s in route.stops()}
    days: list[Day] = []
    changed = False
    for day in itinerary.days:
        items = []
        repeated = {f"{lib.places[i.place_id].name_zh}：{stops[i.place_id].why_here}"
                    for i in day.items if i.place_id in stops and i.place_id in lib.places}
        for it in day.items:
            if it.place_id in stops and not it.why_here and stops[it.place_id].why_here:
                it = it.model_copy(update={"why_here": stops[it.place_id].why_here})
                changed = True
            items.append(it)
        guide = day.guide
        if guide and "今天的顺序：" in guide:
            kept = [p for p in guide.split("\n") if not p.startswith("今天的顺序：") and p not in repeated]
            guide = "\n".join(kept) or None
            changed = True
        days.append(day.model_copy(update={"items": items, "guide": guide}))
    return itinerary.model_copy(update={"days": days}) if changed else itinerary
