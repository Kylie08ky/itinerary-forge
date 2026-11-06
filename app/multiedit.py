"""多城市行程的编辑（规格 11 第八节，批次 ②）：每个操作派给「被改的那座城市」的单城市视图，现有的编辑代码一行不改，
再把结果合回一份。

    换点 / 加 / 删 / 锁定 / 入馆时间  —— 看这个地点属于哪座城市的地点库
    移到另一天                         —— 目标日期必须在这个地点所在城市的日子里
    天内重排                           —— 换城市那天的两座城市各排各的
    重新排程                           —— 每座城市各自重排，锁定的点不动
    平移 / 改住宿 / 改换城市的交通     —— 改这份需求里每座城市的那一份，再按新需求重排或平移
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app import multicity
from app.editing import (
    AddPlace,
    Alternative,
    Edit,
    EditError,
    EditResult,
    MoveToDay,
    RemovePlace,
    ReorderDay,
    Reschedule,
    SetEntryTime,
    SetLock,
    SwapPlace,
    alternatives,
    apply_edit,
    normalize,
)
from app.library import load_library
from app.schemas import Itinerary, TransportLeg, TravelRequest
from app.shifting import shift_plan, shift_transport_json

Parts = list[tuple[TravelRequest, Itinerary]]


def _city_of_place(parts: Parts, place_id: str) -> int:
    for i, (req, _view) in enumerate(parts):
        if place_id in load_library(req.city_id).places:
            return i
    raise EditError(f"地点库里没有 {place_id!r}")


def _name(parts: Parts, i: int) -> str:
    return multicity.city_name(parts[i][0].city_id)


def _need_date(parts: Parts, i: int, day: date, what: str) -> None:
    view = parts[i][1]
    if day not in {d.date for d in view.days}:
        req = parts[i][0]
        raise EditError(f"{what}是{_name(parts, i)}的地点，只能放在{_name(parts, i)}的日子"
                        f"（{req.start_date:%m-%d} 到 {req.end_date:%m-%d}）里")


def _title(parts: Parts, i: int, place_id: str) -> str:
    place = load_library(parts[i][0].city_id).places.get(place_id)
    return f"「{place.name_zh}」" if place else "这个地点"


def apply_edit_multi(it: Itinerary, parent: TravelRequest, edit: Edit) -> EditResult:
    """把一个编辑操作用到多城市行程上；规则和单城市完全一样，只是先找到该动的那座城市。"""
    parts = multicity.split(parent, it)
    touched: list[date] = []
    notes: list[str] = []

    def run(i: int, op: Edit) -> None:
        req, view = parts[i]
        res = apply_edit(view, req, op)
        parts[i] = (req, res.itinerary)
        touched.extend(res.touched)
        notes.extend(f"{_name(parts, i)}：{n}" if len(parts) > 1 else n for n in res.notes)

    if isinstance(edit, Reschedule):
        for i in range(len(parts)):
            run(i, edit)
    elif isinstance(edit, ReorderDay):
        by_city: dict[int, list[str]] = {}
        for pid in edit.place_ids:
            by_city.setdefault(_city_of_place(parts, pid), []).append(pid)
        for i, ids in by_city.items():
            _need_date(parts, i, edit.date, _title(parts, i, ids[0]))
            run(i, ReorderDay(date=edit.date, place_ids=ids))
    else:
        i = _city_of_place(parts, edit.place_id)
        if isinstance(edit, MoveToDay):
            _need_date(parts, i, edit.to_date, _title(parts, i, edit.place_id))
        elif isinstance(edit, AddPlace):
            _need_date(parts, i, edit.date, _title(parts, i, edit.place_id))
        elif isinstance(edit, SwapPlace):
            j = _city_of_place(parts, edit.new_place_id)
            if j != i:
                raise EditError(f"{_title(parts, j, edit.new_place_id)}是{_name(parts, j)}的地点，"
                                f"不能换到{_name(parts, i)}的行程里")
        elif not isinstance(edit, (SetLock, RemovePlace, SetEntryTime)):
            raise EditError("不支持的操作")
        run(i, edit)
    return EditResult(multicity.merge(parent, parts), touched=sorted(set(touched)), notes=notes)


def alternatives_multi(it: Itinerary, parent: TravelRequest, place_id: str, *,
                       indoor_only: bool = False) -> list[Alternative]:
    parts = multicity.split(parent, it)
    req, view = parts[_city_of_place(parts, place_id)]
    return alternatives(view, req, place_id, indoor_only=indoor_only)


def normalize_multi(it: Itinerary, parent: TravelRequest) -> Itinerary:
    """保存前的整理：每座城市按自己的地点库重算事实。"""
    parts = [(req, normalize(view, req)) for req, view in multicity.split(parent, it)]
    return multicity.merge(parent, parts)


# --------------------------------------------------------------------------- #
# 平移
# --------------------------------------------------------------------------- #
def _rebuild(parent: TravelRequest, legs: list[Any], **updates: Any) -> TravelRequest:
    """用新的每座城市需求重建外层需求（走一遍校验）；住宿是每座城市的住宿加在一起。"""
    dumped = {**parent.model_dump(mode="json"), **updates,
              "legs": [{"request": r.model_dump(mode="json"), "theme": t} for r, t in legs],
              "stays": [s.model_dump(mode="json") for r, _t in legs for s in r.stays]}
    return TravelRequest.model_validate(dumped)


def shift_multi(it: Itinerary, parent: TravelRequest, new_start: date) -> tuple[Itinerary, TravelRequest]:
    """整趟平移：每座城市、住宿、来回程和换城市的交通都动同样的天数。"""
    delta = new_start - parent.start_date
    shifted: Parts = []
    for req, view in multicity.split(parent, it):
        view2, req2 = shift_plan(view, req, req.start_date + delta, load_library(req.city_id))
        shifted.append((req2, view2))
    themes = [leg.theme for leg in parent.legs]
    updates: dict[str, Any] = {"start_date": (parent.start_date + delta).isoformat(),
                               "end_date": (parent.end_date + delta).isoformat()}
    if parent.transport is not None:
        updates["transport"] = shift_transport_json(parent.transport, delta)
    new_parent = _rebuild(parent, [(r, t) for (r, _v), t in zip(shifted, themes, strict=True)], **updates)
    return multicity.merge(new_parent, shifted), new_parent


# --------------------------------------------------------------------------- #
# 住宿
# --------------------------------------------------------------------------- #
def leg_index_for_stay(parent: TravelRequest, check_in: date) -> int:
    """住宿属于哪座城市：入住日落在哪座城市的日期内（换城市那天算后一座）。"""
    hits = [i for i, leg in enumerate(multicity.legs_of(parent))
            if leg.request.start_date <= check_in <= leg.request.end_date]
    if not hits:
        raise EditError(f"入住日期 {check_in} 不在这趟旅行的任何一座城市的日期内")
    return hits[-1]


def set_stays_multi(it: Itinerary, parent: TravelRequest, raw: list[dict[str, Any]], *,
                    reschedule: bool) -> tuple[Itinerary, TravelRequest, list[str]]:
    """整体替换住宿：按入住日期分到各座城市；勾了重新排程就每座城市各排一遍。"""
    legs = multicity.legs_of(parent)
    per_leg: list[list[dict[str, Any]]] = [[] for _ in legs]
    for s in raw:
        check_in = s["check_in"] if isinstance(s["check_in"], date) else date.fromisoformat(str(s["check_in"]))
        per_leg[leg_index_for_stay(parent, check_in)].append(s)
    new_legs = []
    for leg, stays in zip(legs, per_leg, strict=True):
        req = TravelRequest.model_validate({**leg.request.model_dump(), "stays": stays})
        new_legs.append((req, leg.theme))
    new_parent = _rebuild(parent, new_legs)
    notes: list[str] = []
    itinerary = it
    if reschedule:
        res = apply_edit_multi(it, new_parent, Reschedule())
        itinerary, notes = res.itinerary, res.notes
    return itinerary, new_parent, notes


# --------------------------------------------------------------------------- #
# 换城市的交通
# --------------------------------------------------------------------------- #
def set_transfer_multi(it: Itinerary, parent: TravelRequest, position: int, *, mode: str, depart_at: datetime,
                       arrive_at: datetime, number: str | None) -> tuple[Itinerary, TravelRequest]:
    """改第 position 座 → 第 position+1 座的交通：前一座的返程和后一座的去程一起换，这两座城市按新时间重新排程
    （锁定的点不动）；别的城市不动。"""
    legs = multicity.legs_of(parent)
    if not 0 <= position < len(legs) - 1:
        raise EditError("没有这一次换城市")
    a, b = legs[position].request, legs[position + 1].request
    if depart_at.date() != a.end_date or arrive_at.date() != b.start_date:
        raise EditError(f"出发要在 {a.end_date}（{multicity.city_name(a.city_id)}的离开日），"
                        f"到达要在 {b.start_date}（{multicity.city_name(b.city_id)}的开始日）")
    if arrive_at <= depart_at:
        raise EditError("到达时间要晚于出发时间")
    shared = {"mode": mode, "depart_at": depart_at, "arrive_at": arrive_at, "number": number}
    sides = {position: ("inbound", TransportLeg(**shared, to_place=multicity.city_name(b.city_id))),
             position + 1: ("outbound", TransportLeg(**shared))}
    new_reqs: dict[int, TravelRequest] = {}
    for i, (side, leg) in sides.items():
        req = legs[i].request
        transport = {**req.transport.model_dump(mode="json", exclude_none=True),
                     side: leg.model_dump(mode="json", exclude_none=True)}
        new_reqs[i] = TravelRequest.model_validate({**req.model_dump(), "transport": transport})
    new_legs = [(new_reqs.get(i, leg.request), leg.theme) for i, leg in enumerate(legs)]
    new_parent = _rebuild(parent, new_legs)
    parts = multicity.split(parent, it)
    for i, req in new_reqs.items():
        res = apply_edit(parts[i][1], req, Reschedule())
        parts[i] = (req, res.itinerary)
    return multicity.merge(new_parent, parts), new_parent


__all__ = ["alternatives_multi", "apply_edit_multi", "leg_index_for_stay", "normalize_multi", "set_stays_multi",
           "set_transfer_multi", "shift_multi"]
