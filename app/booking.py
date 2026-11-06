"""预订记录与最晚订票日（规格 M06 S3 / S4）。

行程里的购票清单（`Itinerary.booking_checklist`）是「该订什么」，随行程版本变化；
预订记录（已订 / 确认码 / 入场时段）是用户自己的状态，单独存在 `reservations` 表里，按（行程号，地点 id）关联——
改一个确认码不该产生一个行程版本。这里把两者合成页面要的清单。

最晚订票日只对地点库里有明确提前天数（`Booking.lead_days`）的地点给，其余只说「建议尽早」，不编数字。
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from app.library import PlaceLibrary
from app.schemas import Itinerary

SOON_DAYS = 3                   # 离建议最晚订票日 ≤ 3 天：橙色「快到了」
LEAD_BASIS = "经验值：提前 {n} 天，非官方规定，以官网为准"


def today() -> date:
    """当天日期；单独成函数是为了测试里能固定「今天」。

    环境变量 ATP_TODAY=YYYY-MM-DD 可以把后端的「今天」固定住（浏览器验收用它，免得测试结果随真实日期漂移：
    「最晚订票日还有几天」「天气是预报还是去年同期」都和今天有关）。平时不设。
    """
    pinned = os.getenv("ATP_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()


def book_by(visit: date, lead_days: int | None) -> date | None:
    return None if lead_days is None else visit - timedelta(days=lead_days)


def urgency(deadline: date | None, now: date, *, booked: bool, visit: date | None = None) -> str:
    """booked 已订 / past 参观日已过（不再提醒）/ none 没有明确天数（只说建议尽早）/ late 已过建议日（马上订）
    / soon 快到了 / ok 还早。"""
    if booked:
        return "booked"
    if visit is not None and visit < now:
        return "past"
    if deadline is None:
        return "none"
    left = (deadline - now).days
    if left < 0:
        return "late"
    return "soon" if left <= SOON_DAYS else "ok"


def build_view(it: Itinerary, lib: PlaceLibrary, saved: dict[str, dict[str, Any]],
               now: date | None = None) -> list[dict[str, Any]]:
    """合成清单：行程里每个要订票的地点一行，再加上「已记录但不在当前行程里」的（确认码不能因为删了地点就丢）。"""
    now = now or today()
    first_visit: dict[str, date] = {}
    for day in it.days:
        for item in day.items:
            if item.place_id and item.place_id not in first_visit:
                first_visit[item.place_id] = day.date

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for b in it.booking_checklist:
        if not b.place_id or b.place_id in seen:
            continue
        seen.add(b.place_id)
        rows.append(_row(b.place_id, b.title, b.must_online, first_visit.get(b.place_id), lib, saved, now, True))
    for pid in saved:
        if pid in seen or pid not in lib.places:
            continue
        p = lib.places[pid]
        rows.append(_row(pid, p.name_zh, p.ticketing.value == "online_required", None, lib, saved, now, False))
    return rows


def _row(pid: str, title: str, must_online: bool, visit: date | None, lib: PlaceLibrary,
         saved: dict[str, dict[str, Any]], now: date, in_itinerary: bool) -> dict[str, Any]:
    p = lib.places.get(pid)
    lead = p.booking.lead_days if p else None
    deadline = book_by(visit, lead) if visit else None
    rec = saved.get(pid)
    status = rec["status"] if rec else "todo"
    booked_for = rec["visit_date"] if rec and status == "booked" else None
    return {
        "place_id": pid, "title": title, "must_online": must_online, "in_itinerary": in_itinerary,
        "visit_date": visit.isoformat() if visit else None,
        "lead_days": lead if visit else None, "lead_basis": LEAD_BASIS.format(n=lead) if lead and visit else None,
        "book_by": deadline.isoformat() if deadline else None,
        "urgency": urgency(deadline, now, booked=status == "booked", visit=visit) if in_itinerary else
                   ("booked" if status == "booked" else "none"),
        "status": status,
        "confirmation_code": rec["confirmation_code"] if rec else None,
        "slot": rec["slot"] if rec else None, "note": rec["note"] if rec else None,
        "entry_from": rec["entry_from"] if rec else None, "entry_to": rec["entry_to"] if rec else None,
        "locked": bool(rec["locked"]) if rec else False,
        "updated_at": rec["updated_at"] if rec else None,
        "booked_for": booked_for,
        "date_changed": bool(booked_for and visit and booked_for != visit.isoformat()),
        "url": p.public_url if p else None,
    }
