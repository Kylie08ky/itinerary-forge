"""周边一日游，出发地是用户的住址（规格 08 3.7）：用 Transitous 现查「从你的住址到每座目的地」的路线，
区分「最快（可能要坐 ICE / IC，另买票）」和「只坐区间车（有 Deutschlandticket 通常不用另买票）」。

- 目的地仍是已收录的一日游城市（`data/cities/city-pairs.json` 里有车站的那些）；先按直线距离过滤再查。
- 「排一天」时再查一次回程（找当晚最晚一班），按选中的车型挑；返程比末班晚就拒绝并说明。
- 住址只用来查路线，**不进行程**：行程里只有出发地的城市名、国家和上车的车站。

纯算法，不用模型。联网部分都在 `app.transit.Transitous`，测试里注入假的。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.cities import City, city_of
from app.daytrips import (
    DAYTRIP_THEME,
    DEFAULT_BACK,
    DEFAULT_DEPART,
    MIN_STAY_MINUTES,
    SEASON_BACK,
    DayTripError,
    Station,
    describe_city,
    destinations_for,
    load_pairs,
)
from app.library import load_library
from app.schemas import DayTripOrigin, Transport, TransportLeg, TransportMode, TravelRequest
from app.transit import (
    DB_SEARCH_URL,
    DEUTSCHLANDTICKET_URL,
    SOURCE,
    SOURCE_URL,
    Option,
    TransitError,
    Transitous,
    choose_options,
    db_query_text,
    haversine_km,
    latest_return,
)

MAX_KM = 160                 # 直线距离超过这个的目的地不查（斯图加特到斯特拉斯堡直线约 108 公里）
MAX_MINUTES = 130            # 查回来超过这个用时的不显示
LAST_TRAIN_LIMIT = "23:59"   # 回程找「当晚这个时间之前到家」的班次


@dataclass(frozen=True)
class LiveOrigin:
    """出发地：城市（显示用）+ 住址坐标（只用来查路线，不保存）。"""

    city_local: str
    city_zh: str | None
    country: str
    lat: float
    lng: float

    @property
    def coords(self) -> str:
        return f"{self.lat:.6f},{self.lng:.6f}"


def zh_name_of(city: str) -> str | None:
    """城市的中文名：已收录的城市 / 预设出发地才知道；不知道就是 None（页面只写当地名，不让模型猜）。"""
    known = city_of(city)
    if known is not None:
        return known.name_zh
    for o in load_pairs().origins.values():
        if o.name_local.lower() == city.strip().lower():
            return o.name_zh
    return None


def zh_city_names() -> dict[str, str]:
    """中文城市名 → 当地语言名（斯图加特 → Stuttgart），用来读懂用户写的「斯图加特 Königstraße 28」。"""
    names: dict[str, str] = {o.name_zh: o.name_local for o in load_pairs().origins.values()}
    for city, _ in _stations().values():
        names[city.name_zh] = city.name_en
    return names


def make_origin(city: str, country: str, lat: float, lng: float, city_zh: str | None = None) -> LiveOrigin:
    city = city.strip()
    if not city or len(city) > 60:
        raise DayTripError("出发地要有城市名")
    if len(country) != 2:
        raise DayTripError("出发地要有国家代码（如 DE）")
    return LiveOrigin(city_local=city, city_zh=city_zh or zh_name_of(city), country=country.upper(), lat=lat, lng=lng)


def _stations() -> dict[str, tuple[City, Station]]:
    """每座目的地城市的车站（城市 id → (城市, 车站)）：取时刻表里第一条的，同一座城市的车站不随出发地变。"""
    out: dict[str, tuple[City, Station]] = {}
    for p in load_pairs().pairs:
        city = city_of(p.destination)
        if city is not None and city.id not in out:
            out[city.id] = (city, p.station)
    return out


def candidates(origin: LiveOrigin) -> list[tuple[City, Station]]:
    return [(c, s) for c, s in _stations().values() if haversine_km((origin.lat, origin.lng), (s.lat, s.lng)) <= MAX_KM]


def _at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())


def fare_note(option: Option, *, cross_border: bool) -> str:
    """乘车说明：乘什么、要不要另买票。不报价格（没有开放的票价数据）；覆盖范围以 DB 官方为准。"""
    ride = option.label()
    if cross_border:
        return f"去程乘 {ride}；跨境段另买票，价格以 DB 为准"
    if option.covered:
        return f"去程乘 {ride}（区间车；有 Deutschlandticket 通常不用另买票，以 DB 为准）"
    long_ = any(r.category == "long" for r in option.rides)
    return f"去程乘 {ride}（{'长途列车，' if long_ else ''}需另买票，价格请到 DB 查）"


def default_kind(options: dict[str, Option | None], dticket: bool) -> str:
    return "regional" if dticket and options.get("regional") else "fastest"


def _option_payload(option: Option, station: Station, *, cross_border: bool) -> dict[str, Any]:
    return {**option.as_dict(), "fare_note": fare_note(option, cross_border=cross_border).removeprefix("去程乘 "),
            "db_url": DB_SEARCH_URL, "db_query": db_query_text(option.station_from, station.name, option.depart)}


def list_destinations(origin: LiveOrigin, day: date, transit: Transitous, *, depart: str = DEFAULT_DEPART,
                      dticket: bool = False) -> dict[str, Any]:
    """从住址出发能一日游的目的地：每座城市给「最快」和「只坐区间车」两个方案（有就给），近的在前。"""
    when = _at(day, depart)
    cands = candidates(origin)
    results: list[tuple[City, Station, dict[str, Option | None]]] = []
    failures = 0
    missing: list[str] = []                                 # 这次没查到的城市（时刻表服务太忙）：页面提醒一句

    def one(item: tuple[City, Station]) -> tuple[City, Station, dict[str, Option | None]] | None:
        city, station = item
        domestic = origin.country == city.country == "DE"
        to = f"{station.lat:.6f},{station.lng:.6f}"
        for attempt in (1, 2):                              # 公共服务偶尔超时：每座城市再试一次
            try:
                its = transit.plan(origin.coords, to, when)
                fast = choose_options(its, when, domestic=domestic, city=origin.city_local)["fastest"]
                # 最快的不是纯区间车时，再单独查一次「只坐区间 / 本地交通」的（最快的班次里常常没有）
                need_local = domestic and fast is not None and not fast.covered
                local = transit.plan(origin.coords, to, when, local_only=True) if need_local else ()
                return city, station, choose_options(its, when, domestic=domestic, city=origin.city_local,
                                                     local_itineraries=local)
            except TransitError:
                if attempt == 2:
                    missing.append(city.id)
        return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        for r in pool.map(one, cands):
            if r is None:
                failures += 1
            else:
                results.append(r)
    if cands and failures == len(cands):
        raise TransitError("时刻表服务暂时连不上")

    out: list[dict[str, Any]] = []
    for city, station, options in results:
        fast = options["fastest"]
        if fast is None or fast.minutes > MAX_MINUTES:
            continue
        cross = origin.country != city.country
        kind = default_kind(options, dticket)
        out.append({
            **describe_city(city, day),
            "station": {"name": station.name, "name_zh": station.name_zh, "lat": station.lat, "lng": station.lng},
            "options": [_option_payload(o, station, cross_border=cross)
                        for o in (options["fastest"], options["regional"]) if o is not None],
            "default_kind": kind,
            "cross_border": cross,
        })
    out.sort(key=lambda d: (next(o["minutes"] for o in d["options"] if o["kind"] == d["default_kind"]),
                            d["city"]["id"]))
    return {"origin": {"city_local": origin.city_local, "city_zh": origin.city_zh, "country": origin.country},
            "date": day.isoformat(), "depart": depart, "dticket": dticket, "destinations": out,
            "missing": sorted(missing), "source": SOURCE, "source_url": SOURCE_URL,
            "dticket_url": DEUTSCHLANDTICKET_URL}


def offline_destinations(origin_id: str, day: date, depart: str = DEFAULT_DEPART) -> dict[str, Any]:
    """时刻表服务连不上时的回退（只有斯图加特 / 亚琛）：用之前存好的「市中心车站」用时，不区分车型，页面写明。"""
    t = load_pairs()
    home = t.origins[origin_id]
    out = []
    for d in destinations_for(origin_id, t):
        pair = d.pair
        start = _at(day, depart)
        cross = home.country != d.city.country
        out.append({
            **describe_city(d.city, day),
            "station": {"name": pair.station.name, "name_zh": pair.station.name_zh, "lat": pair.station.lat,
                        "lng": pair.station.lng},
            "options": [{"kind": "fastest", "minutes": pair.minutes, "transfers": pair.transfers,
                         "depart": depart, "arrive": f"{start + timedelta(minutes=pair.minutes):%H:%M}", "rides": [],
                         "station_from": home.station.name, "covered": False, "needs_ticket": None,
                         "long_distance": False, "fare_note": "没有区分车型：请到 DB 查实际班次和票价",
                         "db_url": DB_SEARCH_URL,
                         "db_query": db_query_text(home.station.name, pair.station.name, start)}],
            "default_kind": "fastest", "cross_border": cross,
        })
    return {"origin": {"city_local": home.name_local, "city_zh": home.name_zh, "country": home.country},
            "date": day.isoformat(), "depart": depart, "dticket": False, "destinations": out, "offline": True,
            "source": "之前存好的时刻表（从市中心车站出发）", "source_url": SOURCE_URL,
            "dticket_url": DEUTSCHLANDTICKET_URL}


def build_live_request(origin: LiveOrigin, destination: str, day: date, transit: Transitous, *,
                       depart: str = DEFAULT_DEPART, back: str | None = None,
                       kind: str = "fastest") -> tuple[TravelRequest, dict[str, Any]]:
    """「排一天」：现查去程（按你选的车型）和当晚最晚的回程，生成一份当天往返的需求。"""
    found = {c.id: (c, s) for c, s in candidates(origin)}
    if destination not in found:
        raise DayTripError(f"从你的住址出发没有这个目的地：{destination}")
    city, station = found[destination]
    domestic = origin.country == city.country == "DE"
    when = _at(day, depart)
    to = f"{station.lat:.6f},{station.lng:.6f}"
    its = transit.plan(origin.coords, to, when)
    local = transit.plan(origin.coords, to, when, local_only=True) if domestic and kind == "regional" else ()
    option = choose_options(its, when, domestic=domestic, city=origin.city_local, local_itineraries=local).get(kind)
    if option is None:
        raise DayTripError("这个时间之后没有这类班次：换一个出发时间，或换成另一种车型")

    returns = transit.plan(to, origin.coords, _at(day, LAST_TRAIN_LIMIT), arrive_by=True,
                           local_only=kind == "regional")
    last = latest_return(returns, regional_only=kind == "regional", domestic=domestic)
    if last is None or last.date() != day:
        raise DayTripError("时刻表上当晚没有回程班次，换一天或换一种车型试试")
    last_hhmm = f"{last:%H:%M}"
    if back is None:
        in_season = any(p.season and p.closed_reason(day) is None for p in load_library(city.id).places.values())
        back = min(SEASON_BACK if in_season else DEFAULT_BACK, last_hhmm)
    for label, value in (("出发时间", depart), ("返程时间", back)):
        if len(value) != 5 or value[2] != ":":
            raise DayTripError(f"{label}要写成 HH:MM（收到 {value!r}）")
    if back > last_hhmm:
        raise DayTripError(f"返程 {back} 比当晚最晚一班 {last_hhmm} 还晚，已经没车了：请改早一点"
                           "（出发前请到 DB 确认当天的班次）")
    leave = _at(day, back)
    if leave < option.arrive + timedelta(minutes=MIN_STAY_MINUTES):
        raise DayTripError(
            f"返程时间 {leave:%H:%M} 太早：{option.arrive:%H:%M} 到达，至少要在{city.name_zh}留 "
            f"{MIN_STAY_MINUTES // 60} 小时，最早 {(option.arrive + timedelta(minutes=MIN_STAY_MINUTES)):%H:%M} 返程")

    cross = origin.country != city.country
    transport = Transport(
        outbound=TransportLeg(mode=TransportMode.train, depart_at=option.depart, arrive_at=option.arrive,
                              from_place=option.station_from, to_place=station.name[:40],
                              number=option.short_label()),
        inbound=TransportLeg(mode=TransportMode.train, depart_at=leave, from_place=station.name[:40],
                             to_place=option.station_from),
    )
    req = TravelRequest(
        destination=city.id, start_date=day, end_date=day, budget_level="medium", travel_style="balanced",
        interests=[DAYTRIP_THEME], arrival_time=option.arrive, departure_time=leave, transport=transport,
        day_trip_from="address",
        day_trip_origin=DayTripOrigin(
            name_local=origin.city_local, name_zh=origin.city_zh, country=origin.country,
            station=option.station_from, station_lat=option.station_from_lat, station_lng=option.station_from_lng,
            ride_note=fare_note(option, cross_border=cross)),
    )
    info = {"arrive": f"{option.arrive:%H:%M}", "leave": f"{leave:%H:%M}", "last_return": last_hhmm,
            "minutes": option.minutes, "option": option.as_dict()}
    return req, info
