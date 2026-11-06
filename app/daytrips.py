"""周边一日游（规格 08 3.4）：「我住在 A，附近哪些城市值得一日游」。

- 城际时间表 `data/cities/city-pairs.json`：出发地（斯图加特、亚琛）→ 目的地的单程用时、末班返程、来源和核对日期。
  数据由 scripts/fetch_city_pairs.py 用开放时刻表（Transitous）在开发时查好，应用运行时不联网。
- 目的地必须是已支持的城市（`data/cities/<id>.json`，tier=light）；没有城市配置的不出现。
- 「排一天」：用时间表里的用时算出到达时间，生成一份一天的 TravelRequest，交给现有的排程器。

纯算法，不用模型。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.cities import City, city_of, load_city
from app.library import load_library
from app.names import bilingual, local_label
from app.schemas import Transport, TransportLeg, TransportMode, TravelRequest

_PAIRS_PATH = Path(__file__).resolve().parents[1] / "data" / "cities" / "city-pairs.json"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DAYTRIP_THEME = "daytrip"            # 轻量城市共用的一个主题 id
DEFAULT_DEPART = "09:00"
DEFAULT_BACK = "18:30"
SEASON_BACK = "21:00"                # 当天有当季活动（圣诞市场天黑后才好看）：默认留到晚上
MIN_STAY_MINUTES = 120               # 到了目的地至少要能待 2 小时，不然不值得去


class DayTripError(ValueError):
    """请求的出发地 / 目的地 / 时间不成立；API 转成 4xx 并把原因告诉用户。"""


class Station(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str                                   # 当地写法：Stuttgart Hbf
    name_zh: str
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class Origin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name_local: str
    name_zh: str
    country: str = Field(min_length=2, max_length=2)
    station: Station


class Pair(BaseModel):
    """一条「出发地 → 目的地」：事实，必须带来源和核对日期。永远不让模型编。"""

    model_config = ConfigDict(extra="forbid")

    origin: str
    destination: str                            # 城市 id
    mode: Literal["train", "bus"]
    minutes: int = Field(ge=1, le=600)          # 时刻表上的最快单程用时
    transfers: int = Field(ge=0, le=5)
    station: Station                            # 目的地一侧的车站
    last_return: str                            # 样例日期当天，当晚能赶回出发地的最晚一班的出发时间（当地时间）
    source: str = Field(min_length=1)
    source_url: str
    checked_at: date
    sample_date: date                           # 用哪一天的时刻表查的（周末 / 平日会有差别）
    note: str | None = None

    @field_validator("last_return")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("last_return 要写成 HH:MM")
        return v

    @field_validator("source_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("source_url 必须以 https:// 开头")
        return v


class PairTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    origins: dict[str, Origin]
    pairs: list[Pair]

    @model_validator(mode="after")
    def _consistent(self) -> PairTable:
        seen: set[tuple[str, str]] = set()
        for p in self.pairs:
            if p.origin not in self.origins:
                raise ValueError(f"pairs 里的 origin {p.origin!r} 不在 origins 里")
            if (p.origin, p.destination) in seen:
                raise ValueError(f"{p.origin} → {p.destination} 重复")
            seen.add((p.origin, p.destination))
        return self


@lru_cache(maxsize=1)
def load_pairs() -> PairTable:
    return PairTable.model_validate(json.loads(_PAIRS_PATH.read_text(encoding="utf-8")))


def origin_ids(table: PairTable | None = None) -> list[str]:
    return sorted((table or load_pairs()).origins)


@dataclass(frozen=True)
class Destination:
    city: City
    pair: Pair


def destinations_for(origin: str, table: PairTable | None = None) -> list[Destination]:
    """从这个出发地能去的目的地，近的在前；没有城市配置（不是已支持的城市）的不出现。"""
    t = table or load_pairs()
    if origin not in t.origins:
        raise DayTripError(f"没有这个出发地：{origin}（现在支持：{'、'.join(origin_ids(t))}）")
    out: list[Destination] = []
    for p in t.pairs:
        if p.origin != origin:
            continue
        city = city_of(p.destination)
        if city is None:
            continue
        out.append(Destination(city=city, pair=p))
    return sorted(out, key=lambda d: (d.pair.minutes, d.city.id))


def _at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())


def build_day_request(origin: str, destination: str, day: date, *, depart: str = DEFAULT_DEPART,
                      back: str | None = None, table: PairTable | None = None) -> TravelRequest:
    """「排一天」：从 origin 当天出发去 destination 玩一天再回来。到达时间 = 出发时间 + 时刻表用时。"""
    t = table or load_pairs()
    dests = {d.city.id: d for d in destinations_for(origin, t)}
    if destination not in dests:
        raise DayTripError(f"从{t.origins[origin].name_zh}出发没有这个目的地：{destination}")
    d = dests[destination]
    if back is None:                     # 没填返程：平常 18:30；当天有当季活动留到 21:00；都不晚于样例日的末班
        in_season = any(p.season and p.closed_reason(day) is None for p in load_library(d.city.id).places.values())
        back = min(SEASON_BACK if in_season else DEFAULT_BACK, d.pair.last_return)
    for label, value in (("出发时间", depart), ("返程时间", back)):
        if not _HHMM.match(value):
            raise DayTripError(f"{label}要写成 HH:MM（收到 {value!r}）")
    if back > d.pair.last_return:
        raise DayTripError(f"返程 {back} 比时刻表样例日（{d.pair.sample_date}）的末班 {d.pair.last_return} 还晚，"
                           f"可能已经没车了：请改早一点，或先到 DB Navigator 等官方渠道确认当天的末班")
    start = _at(day, depart)
    arrive = start + timedelta(minutes=d.pair.minutes)
    leave = _at(day, back)
    if leave < arrive + timedelta(minutes=MIN_STAY_MINUTES):
        raise DayTripError(
            f"返程时间 {leave:%H:%M} 太早：{arrive:%H:%M} 到达，至少要在{d.city.name_zh}留 "
            f"{MIN_STAY_MINUTES // 60} 小时，最早 {(arrive + timedelta(minutes=MIN_STAY_MINUTES)):%H:%M} 返程")
    home = t.origins[origin]
    mode = TransportMode.coach if d.pair.mode == "bus" else TransportMode.train
    transport = Transport(
        outbound=TransportLeg(mode=mode, depart_at=start, arrive_at=arrive, from_place=home.station.name,
                              to_place=d.pair.station.name),
        inbound=TransportLeg(mode=mode, depart_at=leave, from_place=d.pair.station.name, to_place=home.station.name),
    )
    return TravelRequest(
        destination=d.city.id, start_date=day, end_date=day, budget_level="medium", travel_style="balanced",
        interests=[DAYTRIP_THEME], arrival_time=arrive, departure_time=leave, transport=transport,
        day_trip_from=origin,
    )


def home_for(req: TravelRequest) -> Origin | None:
    """这份一日游行程的出发地：住址出发的存在请求里（只有城市和车站），预设的取时刻表；不是一日游 → None。"""
    if not req.day_trip_from:
        return None
    o = req.day_trip_origin
    if o is not None:
        return Origin(name_local=o.name_local, name_zh=o.name_zh or o.name_local, country=o.country,
                      station=Station(name=o.station, name_zh=o.station, lat=o.station_lat, lng=o.station_lng))
    return load_pairs().origins.get(req.day_trip_from)


def origin_label(home: Origin) -> str:
    """「斯图加特 Stuttgart」；不知道中文名（或和当地名一样）就只写当地名。"""
    return home.name_local if home.name_zh == home.name_local else f"{home.name_zh} {home.name_local}"


def day_trip_labels(req: TravelRequest) -> tuple[str, str] | None:
    """一日游的「抵达」「返程」两个接驳项的标题；不是一日游 → None。当地名和中文名并列（规格 07 P18）。"""
    home = home_for(req)
    if home is None:
        return None
    if req.day_trip_origin is not None:
        station = req.transport.outbound.to_place if req.transport and req.transport.outbound.to_place else "火车站"
    else:
        t = load_pairs()
        pair = next((p for p in t.pairs if p.origin == req.day_trip_from and p.destination == req.city_id), None)
        station = pair.station.name if pair else "火车站"
    city = req.city
    return (f"从{origin_label(home)} 出发，抵达{city.name_zh} {city.name_en}（{station}）",
            f"前往 {station}，返回{origin_label(home)}")


def day_trip_info(req: TravelRequest) -> dict[str, str] | None:
    """行程页显示用的名字：city「海德堡 Heidelberg」、origin「斯图加特 Stuttgart」；不是一日游 → None。"""
    home = home_for(req)
    if home is None:
        return None
    city = req.city
    return {"city": f"{city.name_zh} {city.name_en}", "origin": origin_label(home)}


def crosses_border(req: TravelRequest) -> bool:
    """一日游的目的地和出发地不在同一个国家（斯图加特 → 斯特拉斯堡）：行程里要提醒带护照。"""
    home = home_for(req)
    return home is not None and home.country != req.city.country


def home_of(origin: str, table: PairTable | None = None) -> Origin:
    t = table or load_pairs()
    if origin not in t.origins:
        raise DayTripError(f"没有这个出发地：{origin}")
    return t.origins[origin]


def city_for(destination: str) -> City:
    return load_city(destination)


def _station(s: Station) -> dict:
    return {"name": s.name, "name_zh": s.name_zh, "lat": s.lat, "lng": s.lng}


def describe_city(city: City, day: date | None = None) -> dict:
    """目的地卡片里和「怎么去」无关的部分：城市、亮点、当季活动（在不在期内）、数据是不是草稿。"""
    lib = load_library(city.id)
    theme = lib.themes.get(DAYTRIP_THEME)
    ordered = [lib.places[m.place_id] for m in (theme.members if theme else []) if m.place_id in lib.places]
    seasonal = [p for p in ordered if p.season]
    regular = [p for p in ordered if not p.season]
    return {
        "city": {"id": city.id, "name_zh": city.name_zh, "name_local": city.name_en, "country": city.country,
                 "region": city.region},
        "highlights": [{"id": p.id, "name_zh": p.name_zh, "name_local": local_label(p), "label": bilingual(p)}
                       for p in regular[:3]],
        "seasonal": [{"id": p.id, "name_zh": p.name_zh, "name_local": local_label(p), "label": bilingual(p),
                      "start": p.season.start.isoformat(), "end": p.season.end.isoformat(),
                      "in_season": day is not None and p.closed_reason(day) is None,
                      "confirmed_by": p.season.confirmed_by,
                      "source_url": p.season.source_url, "note": p.season.note, "hours": p.note}
                     for p in seasonal],
        "places": len(ordered), "drafts": sum(p.data_status == "draft" for p in ordered),
    }


def describe_destination(d: Destination, day: date | None = None, *, depart: str = DEFAULT_DEPART) -> dict:
    """（离线时刻表）目的地列表里的一项：城市卡片 + 存好的单程用时和来源。"""
    pair = d.pair
    arrive = _at(day or date(2026, 12, 12), depart) + timedelta(minutes=pair.minutes)
    return {
        **describe_city(d.city, day),
        "minutes": pair.minutes, "transfers": pair.transfers, "mode": pair.mode, "station": _station(pair.station),
        "last_return": pair.last_return, "arrive": f"{arrive:%H:%M}",
        "source": pair.source, "source_url": pair.source_url, "checked_at": pair.checked_at.isoformat(),
        "sample_date": pair.sample_date.isoformat(), "note": pair.note,
    }
