"""多城市旅行的设置（规格 11，M17 S8–S10）：几座城市 + 每座城市的日期 + 城市之间的交通。

一趟旅行最终是**一份**行程（`app/multicity.py` 负责合并、拆分）；这里只管把设置拆成每座城市自己的需求：
- 每一段的日期、去程 / 返程交通：换城市那天两段共用——前一段只排到出发前，后一段从到达后开始，
  这正是排程器对「到达日 / 离开日」本来就会做的事；
- 没填交通时放一个**估算**的占位（按两城直线距离，写明「估算」）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.cities import City, city_of
from app.geo import haversine_km
from app.schemas import TransportLeg, TransportMode

MAX_LEGS = 8
MAX_LEG_DAYS = 14
OVERNIGHT_MAX_HOURS = 12      # 估算过夜交通：晚上 20:00 出发、第二天 08:00 到
# 没填交通时按直线距离估算：超过这个距离当飞机，否则高铁
FLIGHT_FROM_KM = 800
# 每一座城市自己的选项（深度路线、候选池里的必去 / 不去 / 权重、住宿）；其余的键不收
LEG_OPTION_KEYS = ("route", "route_stops", "must_go_ids", "avoid_ids", "place_weights", "stays")
COMMON_KEYS = ("budget_level", "travel_style", "travelers", "currency", "budget_cap", "restrictions", "paper_tickets",
               "interests", "avoid", "must_go")


class TripError(ValueError):
    """旅行的设置不合法（段数、天数、交通日期、城市没有数据……）；接口层转成 422，带这句人话。"""


class LegSpec(BaseModel):
    destination: str = Field(min_length=1, max_length=80)
    start_date: date                                  # 到达这座城市的那天
    end_date: date                                    # 离开这座城市的那天（可以和开始同一天）
    theme: str | None = None                          # 这座城市的主题；空 = 均衡
    themes: list[dict[str, Any]] | None = None        # 混选（权重）；和 theme 二选一
    interests: list[str] | None = None                # 兴趣文字（一般是主题名）；空 = 均衡
    options: dict[str, Any] = Field(default_factory=dict)             # LEG_OPTION_KEYS 里的几项，跟单城市生成一样


class TransferSpec(BaseModel):
    """换城市的交通（M06 S6）：方式 + 出发 / 到达时间（当地时间）；没填的由系统估算并标「估算」。"""

    mode: TransportMode | None = None
    depart_at: datetime | None = None
    arrive_at: datetime | None = None
    from_place: str | None = Field(default=None, max_length=40)
    to_place: str | None = Field(default=None, max_length=40)
    number: str | None = Field(default=None, max_length=20)


class TripSpec(BaseModel):
    title: str | None = Field(default=None, max_length=60)
    legs: list[LegSpec]
    outbound: TransportLeg                             # 到第一座城市
    inbound: TransportLeg                              # 离开最后一座城市
    transfers: list[TransferSpec] = Field(default_factory=list)       # 段与段之间；可以少给，缺的估算
    common: dict[str, Any] = Field(default_factory=dict)              # 出行人、预算、节奏等，每一段都一样

    @field_validator("legs")
    @classmethod
    def _legs(cls, v: list[LegSpec]) -> list[LegSpec]:
        if not 2 <= len(v) <= MAX_LEGS:
            raise ValueError(f"多城市旅行要 2–{MAX_LEGS} 座城市（收到 {len(v)} 座）")
        return v

    @model_validator(mode="after")
    def _dates(self) -> TripSpec:
        if len(self.transfers) > len(self.legs) - 1:
            raise ValueError("交通段比城市之间的空档多")
        for i, leg in enumerate(self.legs, 1):
            if leg.end_date < leg.start_date:
                raise ValueError(f"第 {i} 座城市的离开日期不能早于开始日期")
            if (leg.end_date - leg.start_date).days + 1 > MAX_LEG_DAYS:
                raise ValueError(f"第 {i} 座城市最多玩 {MAX_LEG_DAYS} 天")
        for i, (a, b) in enumerate(zip(self.legs, self.legs[1:], strict=False), 1):
            if (b.start_date - a.end_date).days not in (0, 1):
                raise ValueError(f"第 {i + 1} 座城市要在第 {i} 座离开的当天或第二天开始"
                                 "（当天换城市，或坐夜车 / 红眼航班）")
        if (self.legs[-1].end_date - self.legs[0].start_date).days < 1:
            raise ValueError("整趟旅行至少要 2 天")
        return self


def leg_dates(spec: TripSpec) -> list[tuple[date, date]]:
    """每一段的起止日期：用户填的；换城市当天两段共用同一天，坐夜车则第二座城市第二天才开始。"""
    return [(leg.start_date, leg.end_date) for leg in spec.legs]


def estimate_transfer(a: City, b: City, day: date, arrive_day: date | None = None,
                      not_before: datetime | None = None) -> tuple[TransferSpec, float]:
    """没填交通：按两城直线距离估算（飞机 / 高铁）。当天换城市：早上 10:00 出发；
    第二天到（夜车）：晚上 20:00 出发、第二天 08:00 到。not_before：在这座城市当天才到的（一天就走），出发要晚于它。
    返回 (交通, 直线公里)。"""
    km = haversine_km(tuple(a.center), tuple(b.center))
    if km > FLIGHT_FROM_KM:
        mode, hours = TransportMode.flight, 2.5 + km / 700            # 含去机场、安检、等行李
    else:
        mode, hours = TransportMode.high_speed_rail, 0.25 + km * 1.3 / 200
    if arrive_day is not None and arrive_day > day:
        if hours > OVERNIGHT_MAX_HOURS:
            raise TripError(f"{a.name_zh} → {b.name_zh} 太远（约 {km:.0f} 公里），一晚到不了：请自己填交通")
        depart = datetime(day.year, day.month, day.day, 20, 0)
        arrive = datetime(arrive_day.year, arrive_day.month, arrive_day.day, 8, 0)
        return TransferSpec(mode=mode, depart_at=depart, arrive_at=arrive), km
    depart = datetime(day.year, day.month, day.day, 10, 0)
    if not_before is not None and not_before.date() == day:
        later = not_before + timedelta(hours=3)                    # 在这座城市留 3 小时
        later = later.replace(second=0, microsecond=0) + timedelta(minutes=-later.minute % 30)   # 进到整半点
        depart = max(depart, later)
    arrive = depart + timedelta(hours=hours)
    if arrive.date() != day:
        raise TripError(f"{a.name_zh} → {b.name_zh} 太远（约 {km:.0f} 公里），当天到不了："
                        "请自己填交通，或者挑近一点的城市；坐夜车就把下一座城市的开始日期填成第二天")
    return TransferSpec(mode=mode, depart_at=depart, arrive_at=arrive.replace(second=0, microsecond=0)), km


def resolve_transfers(spec: TripSpec, cities: list[City]) -> list[tuple[TransferSpec, bool]]:
    """每个换城市点的交通：(交通, 是不是估算的)。用户填了出发和到达时间的用用户的，并检查：
    出发在前一座城市的离开那天，到达在后一座城市的开始那天。"""
    dates = leg_dates(spec)
    out: list[tuple[TransferSpec, bool]] = []
    came: datetime | None = None                       # 前一次换城市的到达时间
    for i in range(len(spec.legs) - 1):
        day, arrive_day = dates[i][1], dates[i + 1][0]
        given = spec.transfers[i] if i < len(spec.transfers) else TransferSpec()
        if given.depart_at is not None and given.arrive_at is not None:
            if given.depart_at.date() != day or given.arrive_at.date() != arrive_day:
                said = f"{day} 当天" if day == arrive_day else f"{day}，到达在 {arrive_day}"
                raise TripError(f"第 {i + 1} 次换城市的出发时间要在{said}"
                                f"（{cities[i].name_zh} → {cities[i + 1].name_zh}）")
            if given.arrive_at <= given.depart_at:
                raise TripError(f"第 {i + 1} 次换城市的到达时间要晚于出发时间")
            if came is not None and given.depart_at <= came:
                raise TripError(f"第 {i + 1} 次换城市的出发时间要晚于上一次到达（{cities[i].name_zh} 只待一天）")
            came = given.arrive_at
            out.append((given.model_copy(update={"mode": given.mode or TransportMode.other}), False))
            continue
        est, _km = estimate_transfer(cities[i], cities[i + 1], day, arrive_day, came)
        came = est.arrive_at
        out.append((est.model_copy(update={"from_place": given.from_place, "to_place": given.to_place,
                                           "number": given.number}), True))
    return out


def resolve_cities(spec: TripSpec) -> list[City]:
    cities: list[City] = []
    for leg in spec.legs:
        c = city_of(leg.destination)
        if c is None:
            raise TripError(f"「{leg.destination}」还没有数据：先在新建页输入它，建好之后再加进旅行")
        cities.append(c)
    return cities


def _leg_json(leg: TransportLeg | TransferSpec) -> dict[str, Any]:
    return leg.model_dump(mode="json", exclude_none=True)


def leg_payloads(spec: TripSpec, cities: list[City],
                 transfers: list[tuple[TransferSpec, bool]]) -> list[dict[str, Any]]:
    """每一段的生成需求（和 POST /tasks 收到的一样）：去程 = 到这座城市的交通，返程 = 离开这座城市的交通。"""
    dates = leg_dates(spec)
    common = {k: v for k, v in spec.common.items() if k in COMMON_KEYS}
    payloads: list[dict[str, Any]] = []
    for i, leg in enumerate(spec.legs):
        outbound = _leg_json(spec.outbound) if i == 0 else _leg_json(transfers[i - 1][0])
        inbound = _leg_json(spec.inbound) if i == len(spec.legs) - 1 else _leg_json(transfers[i][0])
        if i > 0 and transfers[i - 1][1]:
            outbound["estimated"] = True
        if i < len(spec.legs) - 1 and transfers[i][1]:
            inbound["estimated"] = True
        if i < len(spec.legs) - 1:                                  # 离开这座城市是去下一座，不是「返程」
            inbound.setdefault("to_place", cities[i + 1].name_zh)
        payload: dict[str, Any] = {
            "interests": ["均衡"], **common, "destination": cities[i].name_zh,
            "start_date": dates[i][0].isoformat(), "end_date": dates[i][1].isoformat(),
            "transport": {"outbound": outbound, "inbound": inbound},
        }
        if TransportMode.drive in (spec.outbound.mode, spec.inbound.mode):
            payload["car_in_city"] = True
        payload.update({k: v for k, v in leg.options.items() if k in LEG_OPTION_KEYS})
        if leg.interests:
            payload["interests"] = leg.interests
        if leg.themes:
            payload["themes"] = leg.themes
        if leg.theme:
            payload["theme"] = leg.theme
        payloads.append(payload)
    return payloads


def build_plan(spec: TripSpec) -> tuple[list[City], list[tuple[TransferSpec, bool]], list[dict[str, Any]]]:
    """旅行设置 → (每段的城市, 每个换城市点的交通, 每段的生成需求)。设置不合法抛 TripError。"""
    cities = resolve_cities(spec)
    if any(a.id == b.id for a, b in zip(cities, cities[1:], strict=False)):
        raise TripError("相邻的两段不能是同一座城市")
    transfers = resolve_transfers(spec, cities)
    return cities, transfers, leg_payloads(spec, cities, transfers)
