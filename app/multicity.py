"""多城市行程的「逐城市适配层」（规格 11 第八节）：一份行程里有几座城市，现有的单城市代码一行不改。

做法：一份多城市行程 = 外层需求（`TravelRequest.legs`，每座城市一份自己的单城市需求 + 主题）+ 一份合并的行程
（每天、每个条目标了 `city_id`）。对它做任何操作都是：

    split —— 按城市拆成几份「单城市视图」（各自的需求 + 行程）
    对每一份用现有的单城市代码
    merge —— 把各城市的结果合回一份

换城市那天仍是一天：上午是前一座城市的条目，下午是后一座城市的条目，按条目的 `city_id` 分得开；这一天的
`theme_by_city` 记着两座城市各自的主题（拆回去时用）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TypeVar

from app.cities import load_city
from app.intake import run_intake
from app.library import load_library
from app.llm import LLMClient
from app.schemas import (
    Day,
    GenerationInfo,
    Itinerary,
    ItineraryItem,
    LegBlock,
    LegPlan,
    Transport,
    TravelRequest,
    Trip,
)
from app.trips import TransferSpec, TripError, TripSpec, build_plan

T = TypeVar("T")

ARROW = " → "


def city_name(city_id: str) -> str:
    return load_city(city_id).name_zh


def legs_of(request: TravelRequest):
    if request.legs is None:
        raise ValueError("这不是多城市行程")
    return request.legs


def leg_ids(request: TravelRequest) -> list[str]:
    return [leg.request.city_id for leg in legs_of(request)]


def title_of(request: TravelRequest) -> str:
    """「巴黎 → 里昂」。"""
    return ARROW.join(city_name(c) for c in leg_ids(request))


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
def _merged_generation(parts: list[tuple[TravelRequest, Itinerary]]) -> GenerationInfo | None:
    gens = [(req.city_id, it.generation) for req, it in parts if it.generation is not None]
    if not gens:
        return None
    first = gens[0][1]
    themes: list[str] = []
    for _, g in gens:
        for t in g.themes or [g.theme_id]:
            if t not in themes:
                themes.append(t)
    notes: list[str] = []
    reasons: dict[str, list[str]] = {}
    for cid, g in gens:
        for n in g.notes:
            line = f"{city_name(cid)}：{n}"
            if line not in notes:
                notes.append(line)
        for day, lines in g.day_reasons.items():
            reasons.setdefault(day, []).extend(lines)
    return GenerationInfo(
        theme_id=first.theme_id,
        theme_name=" · ".join(dict.fromkeys(g.theme_name for _, g in gens)),
        themes=themes,
        narration="llm" if any(g.narration == "llm" for _, g in gens) else "template",
        narration_note=next((g.narration_note for _, g in gens if g.narration_note), None),
        notes=notes,
        day_reasons=reasons,
        travel_estimated=any(g.travel_estimated for _, g in gens),
        route=None,
    )


def merge(parent: TravelRequest, parts: list[tuple[TravelRequest, Itinerary]]) -> Itinerary:
    """把每座城市的单城市行程合成一份（换城市那天并成一天）。"""
    by_date: dict[date, Day] = {}
    order: list[date] = []
    blocks: list[LegBlock] = []
    for req, it in parts:
        cid = req.city_id
        blocks.append(LegBlock(city_id=cid, trip=it.trip, generation=it.generation,
                               booking_checklist=it.booking_checklist, risk_notes=it.risk_notes))
        for day in it.days:
            items = [i.model_copy(update={"city_id": cid}) for i in day.items]
            have = by_date.get(day.date)
            if have is None:
                by_date[day.date] = day.model_copy(update={"items": items, "city_id": cid, "theme_by_city": {}})
                order.append(day.date)
                continue
            # 换城市那天：两座城市各一半，按时间排在一起（前一座在出发前，后一座在到达后）
            merged_items = sorted([*have.items, *items], key=lambda i: i.time)
            themes = {**(have.theme_by_city or {have.city_id or "": have.theme}), cid: day.theme}
            by_date[day.date] = have.model_copy(update={"items": merged_items, "theme_by_city": themes})
    trip = Trip(
        destination=ARROW.join(city_name(r.city_id) for r, _ in parts),
        date_range={"start": parent.start_date, "end": parent.end_date},
        traveler_profile=parts[0][1].trip.traveler_profile,
    )
    risk: list[str] = []
    for _, it in parts:
        for r in it.risk_notes:
            if r not in risk:
                risk.append(r)
    return Itinerary(
        trip=trip,
        days=[by_date[d] for d in order],
        booking_checklist=[b for _, it in parts for b in it.booking_checklist],
        risk_notes=risk,
        version=parts[0][1].version,
        generation=_merged_generation(parts),
        legs=blocks,
    )


# --------------------------------------------------------------------------- #
# 拆分
# --------------------------------------------------------------------------- #
def _is_shared(day: Day) -> bool:
    return bool(day.theme_by_city)


def positions(day: Day, city_id: str) -> list[int]:
    """这一天里属于这座城市的条目在合并后那一天里的位置（单城市视图里第 j 项 = 合并后第 positions[j] 项）。"""
    if not _is_shared(day):
        return list(range(len(day.items)))
    return [k for k, i in enumerate(day.items) if i.city_id == city_id]


def split(parent: TravelRequest, itinerary: Itinerary) -> list[tuple[TravelRequest, Itinerary]]:
    """按城市拆成几份单城市视图：每份 = (这座城市自己的需求, 这座城市自己的行程)。"""
    legs = legs_of(parent)
    blocks = itinerary.legs or []
    if len(blocks) != len(legs):
        raise ValueError("多城市行程里的城市数和需求对不上")
    out: list[tuple[TravelRequest, Itinerary]] = []
    for leg, block in zip(legs, blocks, strict=True):
        req = leg.request
        cid = req.city_id
        days: list[Day] = []
        for day in itinerary.days:
            if not req.start_date <= day.date <= req.end_date:
                continue
            shared_day = _is_shared(day)
            if shared_day:
                items = [i.model_copy(update={"city_id": None}) for i in day.items if i.city_id == cid]
                theme = day.theme_by_city.get(cid, day.theme)
                keep_guide = day.city_id == cid
            else:
                items = [i.model_copy(update={"city_id": None}) for i in day.items]
                theme, keep_guide = day.theme, True
            if not items and shared_day and cid not in day.theme_by_city:
                continue                                    # 换城市那天这座城市没排任何东西，也没有这一天
            days.append(day.model_copy(update={
                "items": items, "theme": theme, "city_id": None, "theme_by_city": {},
                "chapter": day.chapter if keep_guide else None, "guide": day.guide if keep_guide else None}))
        if not days:
            raise ValueError(f"{city_name(cid)}没有任何一天的内容")
        out.append((req, Itinerary(
            trip=block.trip, days=days, booking_checklist=block.booking_checklist, risk_notes=block.risk_notes,
            version=itinerary.version, generation=block.generation)))
    return out


def map_legs(parent: TravelRequest, itinerary: Itinerary,
             fn: Callable[[TravelRequest, Itinerary], tuple[TravelRequest, Itinerary]]
             ) -> tuple[TravelRequest, Itinerary]:
    """对每座城市的视图用 fn（现有的单城市代码），再合回一份；需求里每座城市的那一份也换成 fn 返回的。"""
    parts = [fn(req, it) for req, it in split(parent, itinerary)]
    new_legs = [leg.model_copy(update={"request": req}) for leg, (req, _) in zip(legs_of(parent), parts, strict=True)]
    new_parent = parent.model_copy(update={"legs": new_legs})
    return new_parent, merge(new_parent, parts)


def leg_index_of_day(parent: TravelRequest, day: date, city_id: str | None = None) -> int:
    """这一天（和这座城市）属于第几座城市；换城市那天要给 city_id 才分得清。"""
    hits = [i for i, leg in enumerate(legs_of(parent)) if leg.request.start_date <= day <= leg.request.end_date]
    if not hits:
        raise ValueError(f"{day} 不在这趟旅行里")
    if city_id is not None:
        for i in hits:
            if parent.legs[i].request.city_id == city_id:
                return i
    return hits[0]


def item_city(day: Day, index: int) -> str | None:
    it: ItineraryItem = day.items[index]
    return it.city_id or day.city_id


# --------------------------------------------------------------------------- #
# 建需求 / 生成
# --------------------------------------------------------------------------- #
def build_request(spec: TripSpec) -> tuple[TravelRequest, list[tuple[TransferSpec, bool]]]:
    """旅行设置 → 外层需求（每座城市一份自己的单城市需求 + 主题）；设置不合法抛 TripError（人话）。"""
    cities, transfers, payloads = build_plan(spec)
    legs: list[LegPlan] = []
    for i, (city, payload) in enumerate(zip(cities, payloads, strict=True)):
        who = f"第 {i + 1} 段（{city.name_zh}）"
        lib = load_library(city.id)
        payload = dict(payload)
        if not (payload.get("theme") or payload.get("themes")):                  # 没选主题 = 均衡
            payload["themes"] = [{"id": t, "weight": 3} for t in lib.themes]
        theme = payload.pop("theme", None)
        result = run_intake(payload)
        if not result.ok or result.request is None:
            detail = "；".join([*result.errors, *result.questions]) or "需求不完整"
            raise TripError(f"{who}：{detail}")
        req = result.request
        if req.themes:                                                           # 混选：权重最大的当主主题
            unknown = [t.id for t in req.themes if t.id not in lib.themes]
            if unknown:
                raise TripError(f"{who}没有这个主题：{'、'.join(unknown)}")
            theme = max(req.themes, key=lambda t: t.weight).id
        if theme not in lib.themes:
            raise TripError(f"{who}没有这个主题：{theme}")
        legs.append(LegPlan(request=req, theme=str(theme)))
    first, last = legs[0].request, legs[-1].request
    parent = TravelRequest(
        destination=first.destination, start_date=first.start_date, end_date=last.end_date,
        budget_level=first.budget_level, travel_style=first.travel_style, interests=first.interests,
        transport=Transport(outbound=first.transport.outbound, inbound=last.transport.inbound),
        restrictions=first.restrictions, must_go=first.must_go, avoid=first.avoid, language=first.language,
        paper_tickets=first.paper_tickets, travelers=first.travelers, budget_cap=first.budget_cap,
        currency=first.currency, stays=[s for leg in legs for s in leg.request.stays], legs=legs)
    return parent, transfers


def generate(parent: TravelRequest, *, client: LLMClient | None,
             on_step: Callable[[str], None] | None = None,
             post: Callable[[TravelRequest, Itinerary], Itinerary] | None = None) -> Itinerary:
    """每座城市各排一份（现有的单城市排程，一行不改），再合成一份。"""
    from app.generator import generate_from_theme  # 延迟导入：generator 也要用 schemas

    parts: list[tuple[TravelRequest, Itinerary]] = []
    for leg in legs_of(parent):
        name = city_name(leg.request.city_id)
        step = (lambda n, _name=name: on_step(f"{_name}：{n}")) if on_step else None
        trip = generate_from_theme(leg.request, leg.theme, client=client, on_step=step)
        itinerary = post(leg.request, trip.itinerary) if post else trip.itinerary
        parts.append((leg.request, itinerary))
    return merge(parent, parts)


def combine(parts: list[tuple[TravelRequest, Itinerary]]) -> tuple[TravelRequest, Itinerary]:
    """把已经存在的几份单城市行程（相邻、日期首尾相接）并成一份多城市行程；一次性迁移旧的「一座城市一份」用。"""
    if len(parts) < 2:
        raise TripError("至少要两座城市才能合成一份")
    for (a, _), (b, _) in zip(parts, parts[1:], strict=False):
        if a.city_id == b.city_id:
            raise TripError("相邻的两份是同一座城市")
        if b.start_date != a.end_date:
            raise TripError(f"{city_name(a.city_id)}的离开日（{a.end_date}）和{city_name(b.city_id)}的开始日"
                            f"（{b.start_date}）对不上")
    legs = [LegPlan(request=req, theme=(it.generation.theme_id if it.generation else "")) for req, it in parts]
    first, last = legs[0].request, legs[-1].request
    parent = TravelRequest(
        destination=first.destination, start_date=first.start_date, end_date=last.end_date,
        budget_level=first.budget_level, travel_style=first.travel_style, interests=first.interests,
        transport=Transport(outbound=first.transport.outbound, inbound=last.transport.inbound),
        restrictions=first.restrictions, must_go=first.must_go, avoid=first.avoid, language=first.language,
        paper_tickets=first.paper_tickets, travelers=first.travelers, budget_cap=first.budget_cap,
        currency=first.currency, stays=[s for leg in legs for s in leg.request.stays], legs=legs)
    return parent, merge(parent, parts)
