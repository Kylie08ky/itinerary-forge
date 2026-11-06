"""M04：行程编辑（移动、重排、锁定、删除、加入、换一个、重新排程）。

全是纯函数：输入「当前行程 + 一个操作」，返回新行程，不改输入、不碰网络和模型。

    编辑 ──► 只重排受影响的那几天（app.scheduler.plan_day）──► 条目 / 购票清单 / 风险提醒按地点库重算

原则：
- 手动放进来的就照放。落在闭馆日、排不进当天窗口、赶不上航班……这些交给质检去提示，不在这里拒绝（规格 S3）。
- 事实只信地点库。购票清单、官网链接、条目里的类型 / 位置，每次编辑后都从地点库重算。
- 锁定的点：不能被移动 / 删除 / 换掉；重新排程时日期、位置和开始时间都不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.generator import (
    _item,
    _skeleton,
    _template_theme,
    add_lunch_hints,
    build_checklist,
    build_risk_notes,
    free_day_item,
)
from app.library import Place, PlaceLibrary, Role, Setting, load_library
from app.names import item_location
from app.scheduler import DayPlan, plan_day, schedule
from app.scheduler.geo import haversine_km
from app.schemas import Day, ItemType, Itinerary, ItineraryItem, TravelRequest


class EditError(ValueError):
    """操作本身不合法（地点不在行程里、已锁定、日期不对……）。接口层转成 4xx，带着这句人话。"""


# --------------------------------------------------------------------------- #
# 操作
# --------------------------------------------------------------------------- #
class MoveToDay(BaseModel):
    op: Literal["move"] = "move"
    place_id: str
    to_date: date


class ReorderDay(BaseModel):
    op: Literal["reorder"] = "reorder"
    date: date
    place_ids: list[str]


class SetLock(BaseModel):
    op: Literal["lock"] = "lock"
    place_id: str
    locked: bool


class RemovePlace(BaseModel):
    op: Literal["remove"] = "remove"
    place_id: str


class AddPlace(BaseModel):
    op: Literal["add"] = "add"
    place_id: str
    date: date


class SwapPlace(BaseModel):
    op: Literal["swap"] = "swap"
    place_id: str
    new_place_id: str


class Reschedule(BaseModel):
    op: Literal["reschedule"] = "reschedule"


_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class SetEntryTime(BaseModel):
    """订好票后把景点排在入馆时间：锁定它，当天其余安排围绕它重排（规格 M06 S4）。
    end 有值 = 门票是一个进场时段：开始时间落在 [start, end] 内。"""

    op: Literal["entry"] = "entry"
    place_id: str
    start: str = Field(pattern=_HHMM)
    end: str | None = Field(default=None, pattern=_HHMM)

    @model_validator(mode="after")
    def _ordered(self) -> SetEntryTime:
        if self.end is not None and self.end <= self.start:
            raise ValueError("时段结束必须晚于开始")
        return self


Edit = Annotated[
    MoveToDay | ReorderDay | SetLock | RemovePlace | AddPlace | SwapPlace | Reschedule | SetEntryTime,
    Field(discriminator="op"),
]


@dataclass
class EditResult:
    itinerary: Itinerary
    touched: list[date]                       # 内容被重算过的日期
    notes: list[str] = field(default_factory=list)


class Alternative(BaseModel):
    place_id: str
    name_zh: str
    area: str
    address: str | None
    category: str
    duration_min: int
    ticketing: str
    reason: str


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _day_index(it: Itinerary, d: date) -> int:
    for i, day in enumerate(it.days):
        if day.date == d:
            return i
    raise EditError(f"{d} 不在这次行程的日期范围内")


def _place_ids(day: Day) -> list[str]:
    return [i.place_id for i in day.items if i.place_id]


def _locate(it: Itinerary, pid: str) -> tuple[int, ItineraryItem]:
    for idx, day in enumerate(it.days):
        for item in day.items:
            if item.place_id == pid:
                return idx, item
    raise EditError(f"「{pid}」不在行程里")


def _all_ids(it: Itinerary) -> set[str]:
    return {i.place_id for d in it.days for i in d.items if i.place_id}


def _need_place(lib: PlaceLibrary, pid: str) -> Place:
    try:
        return lib.get_place(pid)
    except KeyError:
        raise EditError(f"地点库里没有「{pid}」") from None


def _need_unlocked(item: ItineraryItem, verb: str) -> None:
    if item.locked:
        raise EditError(f"「{item.title}」已锁定，先解锁再{verb}")


def _check_editable(day: Day) -> None:
    for i in day.items:
        if not i.place_id and i.type not in (ItemType.meal, ItemType.logistics):
            raise EditError(f"{day.date} 里有不是来自地点库的条目「{i.title}」，这份行程暂不支持手动编辑")


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def entry_target(*, current: int, start: int, end: int | None) -> int:
    """入馆时间怎么落：单个时间就用它；时段则原开始时间在时段里就不动，早了移到时段开始，晚了移到时段结束。"""
    if end is None:
        return start
    return min(max(current, start), end)


def _locked_starts(day: Day, keep: list[str]) -> dict[str, int]:
    return {i.place_id: _minutes(i.time) for i in day.items if i.place_id and i.locked and i.place_id in keep}


# --------------------------------------------------------------------------- #
# 重算受影响的天
# --------------------------------------------------------------------------- #
def _replan(
    it: Itinerary, req: TravelRequest, lib: PlaceLibrary, layout: dict[date, list[str]], *,
    optimize: set[date], reason: str,
) -> tuple[Itinerary, list[str]]:
    old_items = {i.place_id: i for d in it.days for i in d.items if i.place_id}
    locked_ids = {pid for pid, i in old_items.items() if i.locked}
    theme_name = it.generation.theme_name if it.generation else ""
    out = it.model_copy(deep=True)
    notes: list[str] = []

    for d, ids in layout.items():
        idx = _day_index(out, d)
        _check_editable(it.days[idx])
        try:
            res = plan_day(req, d, ids, lib, optimize=d in optimize,
                           fixed=_locked_starts(it.days[idx], ids))
        except ValueError as exc:
            raise EditError(str(exc)) from exc
        for pid in res.broken_locks:
            notes.append(f"「{old_items[pid].title}」锁定的开始时间在这次调整后赶不上，已按新的顺序重排。")
        items = [_item(s, lib, k == 0, req, d) for k, s in enumerate(res.plan.stops)]
        if not items:
            items = [free_day_item()]
        for item in items:
            old = old_items.get(item.place_id or "")
            if old is not None:
                item.notes = old.notes                      # 保留原来的提醒和模型写的看点
            item.locked = bool(item.place_id) and item.place_id in locked_ids
        out.days[idx] = Day(date=d, theme=_template_theme(DayPlan(date=d, stops=res.plan.stops), theme_name),
                            items=items)
        if out.generation is not None:
            out.generation.day_reasons[d.isoformat()] = [reason]

    add_lunch_hints(out.days, lib)                      # 别的天可能因为这次调整多了 / 少了一家已排的店
    placed = [lib.get_place(pid) for day in out.days for pid in _place_ids(day)]
    out.booking_checklist = build_checklist(placed, req.paper_tickets)
    out.risk_notes = build_risk_notes(placed, out.generation.notes if out.generation else [])
    return out, notes


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def apply_edit(it: Itinerary, req: TravelRequest, edit: Edit, library: PlaceLibrary | None = None) -> EditResult:
    lib = library or load_library(req.city_id)

    if isinstance(edit, SetEntryTime):
        idx, item = _locate(it, edit.place_id)
        target = entry_target(current=_minutes(item.time), start=_minutes(edit.start),
                              end=_minutes(edit.end) if edit.end else None)
        work = it.model_copy(deep=True)
        for x in work.days[idx].items:
            if x.place_id == edit.place_id:
                x.time, x.locked = f"{target // 60:02d}:{target % 60:02d}", True
        out, notes = _replan(work, req, lib, {it.days[idx].date: _place_ids(it.days[idx])},
                             optimize={it.days[idx].date}, reason="按预订的入馆时间安排")
        got = next(x for x in out.days[idx].items if x.place_id == edit.place_id)
        if _minutes(got.time) != target:
            notes.append(f"「{item.title}」没能排在 {target // 60:02d}:{target % 60:02d}：当天时间排不下，"
                         f"现在排在 {got.time}，请检查。")
        return EditResult(out, touched=[it.days[idx].date], notes=notes)

    if isinstance(edit, SetLock):
        _locate(it, edit.place_id)
        out = it.model_copy(deep=True)
        for day in out.days:
            for item in day.items:
                if item.place_id == edit.place_id:
                    item.locked = edit.locked
        return EditResult(out, touched=[])

    if isinstance(edit, MoveToDay):
        src_i, item = _locate(it, edit.place_id)
        _need_unlocked(item, "移动")
        dst_i = _day_index(it, edit.to_date)
        src, dst = it.days[src_i].date, it.days[dst_i].date
        if src == dst:
            return EditResult(it.model_copy(deep=True), touched=[])
        layout = {src: [p for p in _place_ids(it.days[src_i]) if p != edit.place_id],
                  dst: [*_place_ids(it.days[dst_i]), edit.place_id]}
        out, notes = _replan(it, req, lib, layout, optimize={src, dst},
                             reason=f"这天由你手动调整过：把「{item.title}」移到了这里，顺序已按通勤最短重排。")
        return EditResult(out, touched=sorted(layout), notes=notes)

    if isinstance(edit, ReorderDay):
        idx = _day_index(it, edit.date)
        current = _place_ids(it.days[idx])
        if sorted(edit.place_ids) != sorted(current):
            raise EditError("天内重排必须正好包含当天已有的全部地点，不能多也不能少")
        out, notes = _replan(it, req, lib, {edit.date: list(edit.place_ids)}, optimize=set(),
                             reason="这天由你手动调整过：顺序按你拖拽的结果排，时间已重新计算。")
        return EditResult(out, touched=[edit.date], notes=notes)

    if isinstance(edit, RemovePlace):
        idx, item = _locate(it, edit.place_id)
        _need_unlocked(item, "删除")
        d = it.days[idx].date
        layout = {d: [p for p in _place_ids(it.days[idx]) if p != edit.place_id]}
        out, notes = _replan(it, req, lib, layout, optimize=set(),
                             reason=f"这天由你手动调整过：删掉了「{item.title}」，其余顺序不变，时间已重新计算。")
        return EditResult(out, touched=[d], notes=notes)

    if isinstance(edit, AddPlace):
        place = _need_place(lib, edit.place_id)
        if edit.place_id in _all_ids(it):
            raise EditError(f"「{place.name_zh}」已经在行程里了")
        idx = _day_index(it, edit.date)
        layout = {edit.date: [*_place_ids(it.days[idx]), edit.place_id]}
        out, notes = _replan(it, req, lib, layout, optimize={edit.date},
                             reason=f"这天由你手动调整过：加入了「{place.name_zh}」，顺序已按通勤最短重排。")
        return EditResult(out, touched=[edit.date], notes=notes)

    if isinstance(edit, SwapPlace):
        idx, item = _locate(it, edit.place_id)
        _need_unlocked(item, "换掉")
        new = _need_place(lib, edit.new_place_id)
        if edit.new_place_id in _all_ids(it):
            raise EditError(f"「{new.name_zh}」已经在行程里了")
        d = it.days[idx].date
        layout = {d: [edit.new_place_id if p == edit.place_id else p for p in _place_ids(it.days[idx])]}
        out, notes = _replan(it, req, lib, layout, optimize=set(),
                             reason=f"这天由你手动调整过：把「{item.title}」换成了「{new.name_zh}」，位置不变。")
        return EditResult(out, touched=[d], notes=notes)

    if isinstance(edit, Reschedule):
        return _reschedule(it, req, lib)

    raise EditError("不支持的操作")                                          # pragma: no cover


def _reschedule(it: Itinerary, req: TravelRequest, lib: PlaceLibrary) -> EditResult:
    """重新跑一遍排程：锁定的点日期、位置和开始时间不变，其余内容按主题重新选、重新排。"""
    if it.generation is None:
        raise EditError("这份行程不是按主题生成的，没法重新排程")
    pins: dict[str, tuple] = {}
    for day in it.days:
        for item in day.items:
            if item.place_id and item.locked:
                pins[item.place_id] = (day.date, None, _minutes(item.time))
    draft = schedule(req, it.generation.theme_id, lib, pinned=pins)
    fresh = _skeleton(req, draft, lib, it.generation.theme_name)
    fresh.generation.narration_note = "重新排程后，说明文字由模板生成"
    notes = list(draft.notes)
    for day in fresh.days:
        for item in day.items:
            item.locked = bool(item.place_id) and item.place_id in pins
    for pid, (d, _pos, start) in pins.items():
        ok = any(i.place_id == pid and _minutes(i.time) == start
                 for day in fresh.days if day.date == d for i in day.items)
        if not ok:
            notes.append(f"「{lib.get_place(pid).name_zh}」锁定的时间没能保持，请检查。")
    touched = [a.date for a, b in zip(it.days, fresh.days, strict=True) if a.items != b.items]
    return EditResult(fresh, touched=touched, notes=notes)


# --------------------------------------------------------------------------- #
# 换一个：同主题、同区域的备选
# --------------------------------------------------------------------------- #
def alternatives(it: Itinerary, req: TravelRequest, place_id: str, library: PlaceLibrary | None = None,
                 limit: int = 3, indoor_only: bool = False) -> list[Alternative]:
    lib = library or load_library(req.city_id)
    idx, _item_ = _locate(it, place_id)
    d = it.days[idx].date
    original = lib.get_place(place_id)
    used = _all_ids(it)
    avoid = {p.id for t in req.avoid if (p := lib.find(t))} | set(req.avoid_ids)
    gen = it.generation
    theme_ids = [t for t in (gen.themes or [gen.theme_id]) if t in lib.themes] if gen else []
    theme_name = it.generation.theme_name if it.generation else ""

    ranked: list[tuple[int, float, str, Place, str]] = []
    seen: set[str] = set()

    def consider(p: Place, weight: int, tier: int, source: str) -> None:
        if p.id in used or p.id in avoid or p.id in seen or p.zone != original.zone or lib.is_closed_on(p, d):
            return
        if indoor_only and p.setting != Setting.indoor:
            return
        seen.add(p.id)
        km = haversine_km((original.lat, original.lng), (p.lat, p.lng))
        score = weight - 0.7 * km + (1.0 if p.category == original.category else 0.0)
        ranked.append((tier, -score, p.name_zh, p, f"{source} · 距「{original.name_zh}」约 {km:.1f} 公里"))

    for theme_id in theme_ids:
        for p, m in lib.theme_members(theme_id):
            consider(p, m.weight, 0, f"{theme_name}主题")
    for other_id, other in lib.themes.items():
        if other_id in theme_ids:
            continue
        for p, m in lib.theme_members(other_id, Role.anchor):
            if m.weight >= 3:
                consider(p, m.weight - 1, 1, f"相邻的{other.name_zh}主题")

    ranked.sort(key=lambda r: r[:3])
    return [Alternative(place_id=p.id, name_zh=p.name_zh, area=p.area, address=p.address,
                        category=p.category.value, duration_min=p.duration_min,
                        ticketing=p.ticketing.value, reason=reason)
            for _t, _s, _n, p, reason in ranked[:limit]]


# --------------------------------------------------------------------------- #
# 保存前的整理：事实以地点库为准
# --------------------------------------------------------------------------- #
def normalize(it: Itinerary, req: TravelRequest, library: PlaceLibrary | None = None) -> Itinerary:
    """前端传来的行程在保存成版本前整理一遍：

    条目的类型 / 位置 / 是否需订票、购票清单、风险提醒，全部按地点库重算，不采信前端传来的内容；
    时间、时长、交通、备注、锁定标记保留。不认识的地点、不是地点库的普通条目一律拒绝。
    """
    lib = library or load_library(req.city_id)
    expected = [req.start_date.fromordinal(req.start_date.toordinal() + k) for k in range(req.trip_days)]
    if [d.date for d in it.days] != expected:
        raise EditError(f"行程应正好有 {req.trip_days} 天（{expected[0]} 到 {expected[-1]}），收到的日期对不上")
    out = it.model_copy(deep=True)
    for day in out.days:
        for i, item in enumerate(day.items):
            if item.place_id:
                p = _need_place(lib, item.place_id)
                fresh = _item_from_place(p, item, req.city.local_language)
                day.items[i] = fresh
            elif item.type not in (ItemType.meal, ItemType.logistics):
                raise EditError(f"「{item.title}」不是来自地点库的条目，不能保存")
    placed = [lib.get_place(pid) for day in out.days for pid in _place_ids(day)]
    out.booking_checklist = build_checklist(placed, req.paper_tickets)
    out.risk_notes = build_risk_notes(placed, out.generation.notes if out.generation else [])
    return out


def _item_from_place(p: Place, keep: ItineraryItem, language: str | None = None) -> ItineraryItem:
    from app.generator import _TYPE_BY_CATEGORY

    return keep.model_copy(update={
        "title": p.name_zh, "type": _TYPE_BY_CATEGORY[p.category], "location": item_location(p, language),
        "booking_required": p.booking.required,
    })
