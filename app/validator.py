"""M3：确定性质检器。

在 LLM 产出 Itinerary 之后运行，对照 TravelRequest 逐条检查，产出结构化的 Finding 列表。
只做确定性规则（日期、密度、必去项、预约、闭馆日……）；偏好匹配度这类模糊判断留给以后的 LLM Judge。

新增规则 = 写一个 `_rule_xxx(itinerary, request, cfg) -> Iterable[Finding]` 函数并加进 RULES。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from app.cities import city_id_of, city_of
from app.library import WEEKDAY_ZH, Place, PlaceLibrary, Zone, load_library
from app.schemas import ItemType, Itinerary, ItineraryItem, TransportMode, TravelRequest

_ALIASES_PATH = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "place-aliases.json"

# 计入「当日强度」的活动类型（交通接驳和纯休息不算）。
_LOAD_TYPES = {ItemType.sight, ItemType.museum, ItemType.activity, ItemType.food}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class Severity(StrEnum):
    error = "error"      # 违反硬约束，必须改
    warning = "warning"  # 影响体验，建议改
    info = "info"        # 提示


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    day: str | None = None        # 出问题的日期（ISO），整体问题为 None
    item_index: int | None = None  # 当天第几个 item（从 0 起）


class ValidationReport(BaseModel):
    findings: list[Finding] = []

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.error]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.warning]

    @property
    def ok(self) -> bool:
        """没有 error 即视为通过（warning 不阻塞，交人工判断）。"""
        return not self.errors

    def summary(self) -> str:
        return f"{len(self.errors)} error / {len(self.warnings)} warning / {len(self.findings)} 条总计"


# --------------------------------------------------------------------------- #
# 别名 / 闭馆日
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlaceIndex:
    groups: list[list[str]]
    closed_weekday: dict[str, int]

    @classmethod
    def load(cls, path: Path | None = None) -> PlaceIndex:
        if path is not None:
            return cls._read(path)
        st = _ALIASES_PATH.stat()                     # 默认的别名表：文件没改就不重读（校验一次要取好几回）
        return cls._cached(_ALIASES_PATH, st.st_mtime_ns, st.st_size)

    @classmethod
    @lru_cache(maxsize=4)
    def _cached(cls, path: Path, _mtime_ns: int, _size: int) -> PlaceIndex:
        return cls._read(path)

    @classmethod
    def _read(cls, path: Path) -> PlaceIndex:
        data = json.loads(path.read_text(encoding="utf-8"))
        groups = [[s for s in g if not s.startswith("_")] for g in data.get("groups", [])]
        closed = {k: v for k, v in data.get("closed_weekday", {}).items() if not k.startswith("_")}
        return cls(groups=groups, closed_weekday=closed)

    def surfaces_for(self, term: str) -> set[str]:
        """把一个词扩展成「它自己 + 同组所有别名」，全部小写。"""
        low = term.strip().lower()
        out = {low}
        for group in self.groups:
            lows = {s.lower() for s in group}
            if low in lows:
                out |= lows
        return {s for s in out if s}

    def canonical_surfaces(self) -> dict[str, set[str]]:
        return {g[0]: {s.lower() for s in g} for g in self.groups if g}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ValidatorConfig:
    # 每种节奏下「当日活动分钟数」软上限
    max_active_minutes: dict[str, int] = field(
        default_factory=lambda: {"relaxed": 300, "balanced": 420, "intense": 540}
    )
    max_item_minutes: int = 300          # 单个 item 时长上限（超过大概率是模型瞎写）
    edge_day_load_ratio: float = 0.6     # 抵达/离开日强度不应超过软上限的这个比例
    arrival_buffer_minutes: int = 60     # 抵达后到首个活动的缓冲
    departure_buffer_minutes: int = 180  # 末个活动结束到离开的缓冲
    ground_arrival_buffer: int = 45      # 高铁 / 火车 / 大巴到站后：走出车站、去住宿（规格 11）
    ground_departure_buffer: int = 90    # 离开前提前到站（高铁 / 火车 / 大巴）
    drive_arrival_buffer: int = 30       # 自驾到达后：停车、把行李放到住宿
    drive_departure_buffer: int = 30     # 自驾离开前：取车、上路
    day_trip_arrival_buffer: int = 10    # 一日游：到站后走到第一个点的缓冲（火车站，不是机场）
    day_trip_departure_buffer: int = 30  # 一日游：最后一个点结束到返程车次出发的缓冲
    late_arrival_after: int = 14 * 60    # 抵达晚于 14:00 视为「晚到日」
    early_departure_before: int = 12 * 60  # 离开早于 12:00 视为「早走日」
    morning_end: int = 12 * 60
    afternoon_end: int = 18 * 60

    def active_cap(self, style: str) -> int:
        return self.max_active_minutes.get(style, 420)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _to_minutes(hhmm: str) -> int | None:
    if not _TIME_RE.match(hhmm.strip()):
        return None
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def _item_text(item: ItineraryItem) -> str:
    return " ".join(filter(None, [item.title, item.location, item.notes or ""])).lower()


def _itinerary_text(it: Itinerary) -> str:
    return "\n".join(_item_text(i) for d in it.days for i in d.items)


_NON_PLACE_TYPES = {ItemType.meal}      # 用餐占位里没有地点；其余类型沿用旧行为，按名字匹配


def _library_for(req: TravelRequest) -> PlaceLibrary | None:
    try:
        return load_library(req.city_id)
    except FileNotFoundError:
        return None


def edge_buffers(req: TravelRequest, cfg: ValidatorConfig) -> tuple[int, int]:
    """（抵达后的缓冲，离开前的缓冲）分钟：飞机要几小时；一日游坐火车，到站就能开始、提前半小时到站。"""
    if req.day_trip_from:
        return cfg.day_trip_arrival_buffer, cfg.day_trip_departure_buffer
    arrive, depart = cfg.arrival_buffer_minutes, cfg.departure_buffer_minutes
    if req.transport is not None:                       # 坐高铁 / 火车 / 大巴：不用像飞机那样提前几个小时
        if req.transport.outbound.mode in GROUND_MODES:
            arrive = cfg.ground_arrival_buffer
        if req.transport.inbound.mode in GROUND_MODES:
            depart = cfg.ground_departure_buffer
        if req.transport.outbound.mode == TransportMode.drive:
            arrive = cfg.drive_arrival_buffer
        if req.transport.inbound.mode == TransportMode.drive:
            depart = cfg.drive_departure_buffer
    return arrive, depart


GROUND_MODES = frozenset({TransportMode.high_speed_rail, TransportMode.train, TransportMode.coach})


def leave_place(req: TravelRequest) -> str:
    """离开那天「前往……」写什么：飞机 → 机场，高铁 / 火车 / 大巴 → 车站，自驾 / 其他 → 空（只写出发）。"""
    leg = req.transport.inbound if req.transport else None
    if leg is None or leg.mode == TransportMode.flight:
        return "机场"
    return "车站" if leg.mode in GROUND_MODES else ""


def leave_title(req: TravelRequest) -> str:
    """离开那天的接驳项标题。没写目的地、坐飞机时保持原来的「前往机场（返程）」；换城市时写明去哪里。"""
    place = leave_place(req)
    base = f"前往{place}" if place else "出发"
    leg = req.transport.inbound if req.transport else None
    return f"{base}，去{leg.to_place}" if leg and leg.to_place else f"{base}（返程）"


def _place_of(item: ItineraryItem, lib: PlaceLibrary) -> Place | None:
    """条目对应的地点库地点：先看 place_id，没有就按名字在标题 / 地点里找（兼容模型写的行程）。"""
    if item.place_id:
        return lib.places.get(item.place_id)
    if item.type in _NON_PLACE_TYPES:
        return None
    for text in (item.title.lower(), item.location.lower()):      # 标题优先，找不到才看地点字段
        best: tuple[int, Place] | None = None
        for p in lib.places.values():
            for name in (p.name_zh, p.name_en, p.name_local or ""):
                n = name.lower()
                if len(n) >= 3 and n in text and (best is None or len(n) > best[0]):
                    best = (len(n), p)
        if best:
            return best[1]
    return None


def _active_minutes(day) -> int:
    return sum(i.duration_minutes for i in day.items if i.type in _LOAD_TYPES)


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
def _rule_day_count(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """天数必须等于行程自然日数。"""
    if len(it.days) != req.trip_days:
        yield Finding(
            code="day_count",
            severity=Severity.error,
            message=f"行程应有 {req.trip_days} 天，实际 {len(it.days)} 天",
        )


def _rule_dates_valid(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """每天日期落在区间内、不重复、按升序。"""
    expected = [(req.start_date + timedelta(days=i)) for i in range(req.trip_days)]
    seen: set = set()
    prev = None
    for idx, day in enumerate(it.days):
        if day.date in seen:
            yield Finding(code="date_duplicate", severity=Severity.error,
                          message=f"日期 {day.date} 重复出现", day=day.date.isoformat())
        seen.add(day.date)
        if not (req.start_date <= day.date <= req.end_date):
            yield Finding(code="date_out_of_range", severity=Severity.error,
                          message=f"日期 {day.date} 不在 {req.start_date}~{req.end_date} 内",
                          day=day.date.isoformat())
        if prev and day.date <= prev:
            yield Finding(code="date_not_ascending", severity=Severity.warning,
                          message=f"第 {idx + 1} 天日期 {day.date} 未按升序", day=day.date.isoformat())
        prev = day.date
    if [d.date for d in it.days] != expected and len(it.days) == req.trip_days:
        yield Finding(code="date_mismatch", severity=Severity.error,
                      message=f"日期序列应为 {[d.isoformat() for d in expected]}")


def _rule_time_format(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """每个 item.time 必须是合法 HH:MM。"""
    for day in it.days:
        for j, item in enumerate(day.items):
            if _to_minutes(item.time) is None:
                yield Finding(code="bad_time_format", severity=Severity.error,
                              message=f"时间格式非法：{item.time!r}（{item.title}）",
                              day=day.date.isoformat(), item_index=j)


def _rule_items_sorted(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """每天 items 应按时间升序。"""
    for day in it.days:
        mins = [_to_minutes(i.time) for i in day.items]
        clean = [m for m in mins if m is not None]
        if clean != sorted(clean):
            yield Finding(code="items_unsorted", severity=Severity.warning,
                          message=f"{day.date} 的安排未按时间升序", day=day.date.isoformat())


def _is_day_trip(item: ItineraryItem, it: Itinerary, req: TravelRequest) -> bool:
    """郊区一日游（迪士尼、凡尔赛）本来就要大半天，不算「时长过长」。"""
    lib = _library_for(req)
    place = _place_of(item, lib) if lib else None
    return bool(place and place.zone == Zone.day_trip)


def _rule_duration_sane(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """单项时长必须为正，且不宜过长。"""
    for day in it.days:
        for j, item in enumerate(day.items):
            if item.duration_minutes <= 0:
                yield Finding(code="nonpositive_duration", severity=Severity.error,
                              message=f"「{item.title}」时长为 {item.duration_minutes} 分钟",
                              day=day.date.isoformat(), item_index=j)
            elif item.duration_minutes > cfg.max_item_minutes and not _is_day_trip(item, it, req):
                yield Finding(code="overlong_item", severity=Severity.warning,
                              message=f"「{item.title}」时长 {item.duration_minutes} 分钟，超过 "
                                      f"{cfg.max_item_minutes} 分钟，请核实",
                              day=day.date.isoformat(), item_index=j)


def _rule_transport_present(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """除每天第一项外，其余项应写明交通方式（避免路线断裂）。"""
    for day in it.days:
        for j, item in enumerate(day.items[1:], start=1):
            if not (item.transport or "").strip():
                yield Finding(code="missing_transport", severity=Severity.warning,
                              message=f"「{item.title}」缺少交通方式", day=day.date.isoformat(),
                              item_index=j)


def _is_day_trip_day(day, req: TravelRequest) -> bool:
    """这一天安排了郊区一日游（迪士尼、凡尔赛）：本来就是「一天只去一个点、玩大半天」。"""
    lib = _library_for(req)
    return bool(lib and any((p := _place_of(i, lib)) and p.zone == Zone.day_trip for i in day.items))


def _rule_daily_load(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """当日活动总时长不应超过该节奏的软上限（郊区一日游除外）。"""
    cap = cfg.active_cap(req.travel_style)
    for day in it.days:
        if _is_day_trip_day(day, req):
            continue
        total = _active_minutes(day)
        if total > cap:
            yield Finding(code="day_overpacked", severity=Severity.warning,
                          message=f"{day.date} 活动约 {total} 分钟，超过 {req.travel_style} 节奏建议的 "
                                  f"{cap} 分钟", day=day.date.isoformat())


def _rule_timeofday_coverage(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """非抵达/离开日应至少覆盖上午、下午、晚上里的两段。"""
    if len(it.days) < 3:
        return
    for day in it.days[1:-1]:
        if _is_day_trip_day(day, req):
            continue
        buckets = set()
        for i in day.items:
            m = _to_minutes(i.time)
            if m is None:
                continue
            buckets.add("上午" if m < cfg.morning_end else "下午" if m < cfg.afternoon_end else "晚上")
        if len(buckets) < 2:
            yield Finding(code="thin_day", severity=Severity.warning,
                          message=f"{day.date} 只覆盖了 {('、'.join(buckets)) or '零'} 时段，安排偏薄",
                          day=day.date.isoformat())


def _rule_must_go(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """用户 must_go 里的每一项都必须出现在行程中。"""
    places = PlaceIndex.load()
    blob = _itinerary_text(it)
    for term in req.must_go:
        surfaces = places.surfaces_for(term)
        if not any(s in blob for s in surfaces):
            yield Finding(code="must_go_missing", severity=Severity.error,
                          message=f"必去项「{term}」没有出现在任何一天")


def _rule_repeated_attraction(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """同一个景点 / 博物馆不应在多天重复出现。"""
    seen: dict[str, str] = {}   # 归一化标题 -> 首次出现的日期
    for day in it.days:
        for item in day.items:
            if item.type not in {ItemType.sight, ItemType.museum}:
                continue
            key = re.split(r"[（(]", item.title.strip().lower())[0].strip()
            if not key:
                continue
            if key in seen and seen[key] != day.date.isoformat():
                yield Finding(code="repeated_attraction", severity=Severity.warning,
                              message=f"「{item.title}」在 {seen[key]} 和 {day.date} 都出现了",
                              day=day.date.isoformat())
            seen.setdefault(key, day.date.isoformat())


def _rule_avoid(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """用户 avoid 里的项目不应出现（模糊匹配，故为 warning）。"""
    places = PlaceIndex.load()
    for day in it.days:
        for j, item in enumerate(day.items):
            text = _item_text(item)
            for term in req.avoid:
                if any(s in text for s in places.surfaces_for(term)):
                    yield Finding(code="avoid_hit", severity=Severity.warning,
                                  message=f"「{item.title}」疑似命中用户想避开的「{term}」",
                                  day=day.date.isoformat(), item_index=j)


def _rule_booking_checklist(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """凡是 booking_required 的项目，都要在 booking_checklist 里有对应条目。"""
    checklist_blob = " ".join(b.title for b in it.booking_checklist).lower()
    for day in it.days:
        for j, item in enumerate(day.items):
            if not item.booking_required:
                continue
            key = item.title.lower().split("（")[0].split("(")[0].strip()
            if key and key[:6] not in checklist_blob and item.title.lower() not in checklist_blob:
                yield Finding(code="booking_not_listed", severity=Severity.error,
                              message=f"「{item.title}」标了需预约，但预约清单里没有它",
                              day=day.date.isoformat(), item_index=j)
    if not it.booking_checklist and any(
        i.booking_required for d in it.days for i in d.items
    ):
        yield Finding(code="booking_checklist_empty", severity=Severity.error,
                      message="有需预约的项目，但 booking_checklist 为空")


def _rule_arrival_departure(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """抵达日不早于抵达时间+缓冲开始；离开日在离开时间-缓冲前结束；晚到/早走日降密度。"""
    if not it.days:
        return
    cap = cfg.active_cap(req.travel_style)

    first_day, last_day = it.days[0], it.days[-1]
    arrive_buf, depart_buf = edge_buffers(req, cfg)
    edge_days = req.day_trip_from is None            # 一日游坐火车往返，不存在「晚到日 / 早走日」

    if req.arrival_time and first_day.date == req.start_date:
        arr = req.arrival_time.hour * 60 + req.arrival_time.minute
        # 只看「真正的活动」——接驳、入住这类 logistics 项排在抵达时刻是正常的
        starts = [
            m for m in (_to_minutes(i.time) for i in first_day.items if i.type != ItemType.logistics)
            if m is not None
        ]
        if starts and min(starts) < arr + arrive_buf:
            yield Finding(code="before_arrival", severity=Severity.error,
                          message=f"抵达日首个活动 {min(starts) // 60:02d}:{min(starts) % 60:02d} "
                                  f"早于抵达时间 {req.arrival_time:%H:%M} + {arrive_buf} 分钟缓冲",
                          day=first_day.date.isoformat())
        if edge_days and arr >= cfg.late_arrival_after and _active_minutes(first_day) > cap * cfg.edge_day_load_ratio:
            yield Finding(code="late_arrival_overpacked", severity=Severity.warning,
                          message=f"晚到日（{req.arrival_time:%H:%M} 抵达）活动 {_active_minutes(first_day)} "
                                  f"分钟偏满，建议再压一压", day=first_day.date.isoformat())

    if req.departure_time and last_day.date == req.end_date:
        dep = req.departure_time.hour * 60 + req.departure_time.minute
        # 非 logistics 活动要在「离开时间 - 缓冲」前收尾；去机场的接驳项本身不算
        ends = [
            m + i.duration_minutes
            for i, m in ((i, _to_minutes(i.time)) for i in last_day.items if i.type != ItemType.logistics)
            if m is not None
        ]
        if ends and max(ends) > dep - depart_buf:
            yield Finding(code="after_departure", severity=Severity.error,
                          message=f"离开日最后一个活动约 {max(ends) // 60:02d}:{max(ends) % 60:02d} 结束，"
                                  f"晚于离开时间 {req.departure_time:%H:%M} - "
                                  f"{depart_buf} 分钟缓冲", day=last_day.date.isoformat())
        early = dep <= cfg.early_departure_before
        if edge_days and early and _active_minutes(last_day) > cap * cfg.edge_day_load_ratio:
            yield Finding(code="early_departure_overpacked", severity=Severity.warning,
                          message=f"早走日（{req.departure_time:%H:%M} 离开）还塞了 "
                                  f"{_active_minutes(last_day)} 分钟活动", day=last_day.date.isoformat())


def _rule_closed_days(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """已知闭馆日检查（卢浮宫周二、奥赛周一……），依据是地点库里的 closed_weekdays。"""
    lib = _library_for(req)
    if lib is None:
        return
    for day in it.days:
        wd = day.date.weekday()
        for j, item in enumerate(day.items):
            place = _place_of(item, lib)
            if place and wd in place.closed_weekdays:
                yield Finding(code="closed_day", severity=Severity.warning,
                              message=f"{day.date} 是周{WEEKDAY_ZH[wd]}，{place.name_zh} 当天闭馆",
                              day=day.date.isoformat(), item_index=j)
            elif place and (why := place.closed_reason(day.date, allow_long_term=True)):   # 固定闭馆日 / 不在当季期内
                yield Finding(code="closed_day", severity=Severity.warning,
                              message=f"{day.date}，{place.name_zh}：{why}",
                              day=day.date.isoformat(), item_index=j)


def _rule_long_term_closed(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """地点库里标了 long_term_closed（整修数年）的地点，不该排进行程。"""
    lib = _library_for(req)
    if lib is None:
        return
    for day in it.days:
        for j, item in enumerate(day.items):
            place = _place_of(item, lib)
            if place and place.long_term_closed:
                yield Finding(code="long_term_closed", severity=Severity.error,
                              message=f"「{item.title}」目前长期闭馆/整修中：{place.note}",
                              day=day.date.isoformat(), item_index=j)


def _rule_profile_matches(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """输出里的 traveler_profile 应与用户需求一致。"""
    p = it.trip.traveler_profile
    if str(p.style) != str(req.travel_style):
        yield Finding(code="profile_style", severity=Severity.warning,
                      message=f"traveler_profile.style={p.style}，用户填的是 {req.travel_style}")
    if str(p.budget) != str(req.budget_level):
        yield Finding(code="profile_budget", severity=Severity.warning,
                      message=f"traveler_profile.budget={p.budget}，用户填的是 {req.budget_level}")


def _rule_destination(it: Itinerary, req: TravelRequest, cfg: ValidatorConfig) -> Iterable[Finding]:
    """目的地要一致（大小写/中英文都算）。"""
    got = it.trip.destination.strip().lower()
    same_city = city_id_of(it.trip.destination) == req.city_id and city_of(req.destination) is not None
    if not same_city and req.destination.lower() not in got:
        yield Finding(code="destination_mismatch", severity=Severity.error,
                      message=f"目的地是 {it.trip.destination!r}，应为 {req.destination!r}")


RULES: list[Callable[[Itinerary, TravelRequest, ValidatorConfig], Iterable[Finding]]] = [
    _rule_day_count,
    _rule_dates_valid,
    _rule_time_format,
    _rule_items_sorted,
    _rule_duration_sane,
    _rule_transport_present,
    _rule_daily_load,
    _rule_timeofday_coverage,
    _rule_must_go,
    _rule_repeated_attraction,
    _rule_avoid,
    _rule_booking_checklist,
    _rule_arrival_departure,
    _rule_closed_days,
    _rule_long_term_closed,
    _rule_profile_matches,
    _rule_destination,
]


def _validate_multi(itinerary: Itinerary, request: TravelRequest, cfg: ValidatorConfig) -> ValidationReport:
    from app import multicity

    days = {d.date.isoformat(): d for d in itinerary.days}
    findings: list[Finding] = []
    for leg_req, leg_it in multicity.split(request, itinerary):
        cid = leg_req.city_id
        for f in validate_itinerary(leg_it, leg_req, config=cfg).findings:
            if f.item_index is not None and f.day in days:       # 单城市视图里的第几项 → 合并后那一天的第几项
                where = multicity.positions(days[f.day], cid)
                if f.item_index < len(where):
                    f = f.model_copy(update={"item_index": where[f.item_index]})
            findings.append(f)
    order = {Severity.error: 0, Severity.warning: 1, Severity.info: 2}
    findings.sort(key=lambda f: (order[f.severity], f.day or "", f.code))
    return ValidationReport(findings=findings)


def validate_itinerary(
    itinerary: Itinerary,
    request: TravelRequest,
    *,
    config: ValidatorConfig | None = None,
) -> ValidationReport:
    cfg = config or ValidatorConfig()
    if request.legs:                                      # 多城市：每座城市各查，再合并（规格 11 第八节）
        return _validate_multi(itinerary, request, cfg)
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(itinerary, request, cfg))
    order = {Severity.error: 0, Severity.warning: 1, Severity.info: 2}
    findings.sort(key=lambda f: (order[f.severity], f.day or "", f.code))
    return ValidationReport(findings=findings)
