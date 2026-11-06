"""整体平移行程日期（规格 M04 S8）。纯函数：给定行程、需求、新的开始日期，返回平移后的行程和需求。

平移只动日期，不重新选点、不改顺序和时间。星期变了，闭馆日可能就撞上了——这件事交给质检规则查，
`new_conflicts` 把「平移前没有、平移后才有」的冲突挑出来给用户看，由用户决定要不要重新排程。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.library import PlaceLibrary
from app.schemas import Itinerary, TravelRequest
from app.validator import ValidationReport

# 这些质检规则的结果和「哪一天是星期几」有关：平移可能制造出新的冲突
_DATE_SENSITIVE = ("closed_day", "long_term_closed")


class ShiftError(ValueError):
    pass


def shift_transport_json(transport: Any, delta: timedelta) -> dict[str, Any]:
    """来回交通整体顺延 delta，返回能直接放进需求的字典。"""
    dumped = transport.model_dump(mode="json", exclude_none=True)
    for side in ("outbound", "inbound"):
        for key in ("depart_at", "arrive_at"):
            value = getattr(getattr(transport, side), key)
            if value is not None:
                dumped[side][key] = (value + delta).isoformat()
    return dumped


def shift_plan(
    it: Itinerary, req: TravelRequest, new_start: date, library: PlaceLibrary | None = None
) -> tuple[Itinerary, TravelRequest]:
    delta = new_start - req.start_date
    if delta.days == 0:
        raise ShiftError("日期没变：新的开始日期和现在一样")
    out = it.model_copy(deep=True)
    for day in out.days:
        day.date = day.date + delta
    out.trip.date_range.start = out.trip.date_range.start + delta
    out.trip.date_range.end = out.trip.date_range.end + delta
    if out.generation is not None:
        # 「为什么这样排」是按旧日期写的（周二卢浮宫闭馆……），平移后不再适用；留着会写出错的事实，所以清空并注明
        note = "日期平移后，原来的「为什么这样排」不再适用；重新排程后会重新生成。"
        out.generation = out.generation.model_copy(update={
            "day_reasons": {},
            "notes": list(dict.fromkeys([*out.generation.notes, note])),      # 平移多次也只留一条
        })

    updates: dict[str, Any] = {"start_date": req.start_date + delta, "end_date": req.end_date + delta}
    if req.arrival_time:
        updates["arrival_time"] = req.arrival_time + delta
    if req.departure_time:
        updates["departure_time"] = req.departure_time + delta
    if req.transport is not None:                            # 来回交通的日期也顺延，否则和新的开始 / 离开日期对不上
        updates["transport"] = shift_transport_json(req.transport, delta)
    updates["stays"] = [{**s.model_dump(), "check_in": s.check_in + delta, "check_out": s.check_out + delta}
                        for s in req.stays]                  # 住宿跟着顺延
    new_req = TravelRequest.model_validate({**req.model_dump(), **updates})
    return out, new_req


def _title_at(it: Itinerary, day: str | None, idx: int | None) -> str | None:
    if day is None or idx is None:
        return None
    for d in it.days:
        if d.date.isoformat() == day and 0 <= idx < len(d.items):
            return d.items[idx].title
    return None


def new_conflicts(
    before_it: Itinerary, before: ValidationReport, after_it: Itinerary, after: ValidationReport
) -> list[dict[str, Any]]:
    """平移后出现、平移前没有的日期相关冲突（按「规则 + 地点」比，不按日期比，因为日期本来就全变了）。"""
    had = {(f.code, _title_at(before_it, f.day, f.item_index)) for f in before.findings if f.code in _DATE_SENSITIVE}
    out = []
    for f in after.findings:
        if f.code not in _DATE_SENSITIVE:
            continue
        title = _title_at(after_it, f.day, f.item_index)
        if (f.code, title) in had:
            continue
        out.append({"code": f.code, "day": f.day, "title": title, "message": f.message})
    return out


def shift_summary(old_start: date, new_start: date) -> str:
    n = (new_start - old_start).days
    return f"整体平移 {n:+d} 天：{old_start:%m-%d} → {new_start:%m-%d}"

