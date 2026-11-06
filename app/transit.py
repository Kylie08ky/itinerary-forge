"""实时查公共交通（规格 08 3.7）：Transitous（transitous.org，开源社区运营的免费公共交通路线服务，
数据来自德国 DELFI、荷兰 NS、比利时 SNCB 等官方开放时刻表）。

用来做三件事：把住址查成坐标（geocode）、从住址到目的地车站的路线（plan）、判断每条路线能不能用 Deutschlandticket。
不登录、不带密钥；请求带联系方式的 User-Agent，并发有上限，结果缓存 3 小时。**不保存也不记录住址**。
查到的是时刻表上的用时，不是实时延误；票价没有开放数据，不查不编（页面给去 DB 查询的链接）。

纯函数（分类、时间换算、挑选方案）和联网的 `Transitous` 分开，前者好测。
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

API = "https://api.transitous.org/api/v1"
USER_AGENT = "ai-travel-planner-agent/0.4 (+https://github.com/Kylie08ky; personal project)"
CACHE_TTL_SECONDS = 3 * 3600
TIMEOUT_SECONDS = 60.0
MAX_ADDRESSES = 6
SOURCE = "Transitous（开放时刻表：德国 DELFI、荷兰 NS、比利时 SNCB 等）"
SOURCE_URL = "https://transitous.org"
DEUTSCHLANDTICKET_URL = "https://www.bahn.de/angebot/regio/deutschland-ticket"

# 每一段交通属于哪一类：local = 区间 / 本地交通（Deutschlandticket 在德国境内覆盖）；
# long = DB 长途（ICE / IC / EC，要另买票）；其余（大巴、国外的普通火车、不认识的）一律当「另买票」，不替官方下结论
LOCAL_MODES = frozenset({"REGIONAL_RAIL", "SUBURBAN", "METRO", "SUBWAY", "TRAM", "BUS"})
LONG_MODES = frozenset({"HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL"})
LOCAL_MODES_PARAM = "REGIONAL_RAIL,SUBURBAN,METRO,SUBWAY,TRAM,BUS"
MODE_ZH = {
    "REGIONAL_RAIL": "区间车", "SUBURBAN": "城铁", "METRO": "地铁 / 城铁", "SUBWAY": "地铁", "TRAM": "有轨电车",
    "BUS": "公交", "HIGHSPEED_RAIL": "高速列车", "LONG_DISTANCE": "长途列车", "NIGHT_RAIL": "夜车", "COACH": "大巴",
    "RAIL": "火车",
}
SHOWN_RIDE_MIN = 5                      # 短于这个的一段（地铁坐一站）不在说明里列出来，只影响显示，不影响要不要买票
LOOKAHEAD_MIN = 120                     # 只看请求时间之后这么久内出发的班次


class TransitError(Exception):
    """时刻表服务连不上 / 返回了看不懂的东西；调用方告诉用户稍后再试，或回退到存好的时刻表。"""


# --------------------------------------------------------------------------- #
# 纯函数：时间
# --------------------------------------------------------------------------- #
def _last_sunday(year: int, month: int) -> date:
    d = date(year, month, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def central_europe_offset(day: date) -> int:
    """中欧时间相对 UTC 的小时数：夏令时（3 月最后一个周日到 10 月最后一个周日之前）+2，否则 +1。
    德国、荷兰、比利时、法国同一个时区；不用 zoneinfo（Windows 上没有时区库，不为它引新依赖）。"""
    return 2 if _last_sunday(day.year, 3) <= day < _last_sunday(day.year, 10) else 1


def to_utc_iso(local: datetime) -> str:
    """当地时间（没带时区）→ Transitous 要的 UTC 时间串。"""
    off = central_europe_offset(local.date())
    return (local - timedelta(hours=off)).replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_utc_iso(iso: str) -> datetime:
    """Transitous 的 UTC 时间串 → 当地时间（没带时区）。"""
    utc = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
    return utc + timedelta(hours=central_europe_offset(utc.date()))


# --------------------------------------------------------------------------- #
# 纯函数：把 Transitous 的路线整理成方案
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Ride:
    label: str                          # ICE 2090 / RE1 / S2
    mode: str
    minutes: int

    @property
    def category(self) -> str:
        return "local" if self.mode in LOCAL_MODES else "long" if self.mode in LONG_MODES else "other"


@dataclass(frozen=True)
class Option:
    """一个出行方案。kind：fastest = 时刻表上最快的；regional = 只坐区间 / 本地交通的最快方案。"""

    kind: str
    minutes: int                        # 门到站：含从住址走到车站
    transfers: int
    depart: datetime                    # 从住址出发（当地时间）
    arrive: datetime                    # 到目的地车站
    rides: tuple[Ride, ...]
    station_from: str                   # 上车的车站（当地写法，带城市名）
    station_from_lat: float
    station_from_lng: float
    covered: bool                       # Deutschlandticket 通常覆盖（全程区间 / 本地交通 + 全在德国）

    def label(self) -> str:
        """「ICE 2090」「RE1 → RE71」；车次类别写清楚，让人看出要不要另买票。"""
        shown = [r.label for r in self.rides if r.minutes >= SHOWN_RIDE_MIN] or [r.label for r in self.rides]
        return " → ".join(shown)

    def short_label(self, limit: int = 20) -> str:
        text = self.label()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "minutes": self.minutes, "transfers": self.transfers,
            "depart": f"{self.depart:%H:%M}", "arrive": f"{self.arrive:%H:%M}",
            "rides": [{"label": r.label, "mode": r.mode, "category": r.category,
                       "mode_zh": MODE_ZH.get(r.mode, "火车"), "minutes": r.minutes}
                      for r in self.rides if r.minutes >= SHOWN_RIDE_MIN or len(self.rides) == 1],
            "station_from": self.station_from, "covered": self.covered,
            "needs_ticket": not self.covered,
            "long_distance": any(r.category == "long" for r in self.rides),
        }


_ID_TAIL = re.compile(r"\s*\(\d+\)\s*$")


def ride_label(leg: dict[str, Any]) -> str:
    raw = leg.get("routeShortName") or leg.get("displayName") or MODE_ZH.get(leg.get("mode", ""), "火车")
    return _ID_TAIL.sub("", str(raw)).strip()


def _transit_legs(itinerary: dict[str, Any]) -> list[dict[str, Any]]:
    return [leg for leg in itinerary.get("legs", []) if leg.get("mode") != "WALK"]


def covered_by_deutschlandticket(itinerary: dict[str, Any], *, domestic: bool) -> bool:
    """每一段都是区间 / 本地交通，且出发地和目的地都在德国。跨境、长途、大巴、不认识的方式一律不算覆盖。"""
    legs = _transit_legs(itinerary)
    return bool(domestic and legs and all(leg.get("mode") in LOCAL_MODES for leg in legs))


def station_name(stop: dict[str, Any], city: str | None) -> str:
    """Transitous 的站名常常只有「Hauptbahnhof」：缺城市名时补上，如「Stuttgart Hauptbahnhof」。"""
    name = str(stop.get("name") or "火车站").strip()
    return name if not city or city.lower() in name.lower() else f"{city} {name}"


def to_option(itinerary: dict[str, Any], kind: str, *, domestic: bool, city: str | None) -> Option:
    legs = _transit_legs(itinerary)
    first_rail = next((leg for leg in legs if leg.get("mode") in LONG_MODES | {"REGIONAL_RAIL", "RAIL"}), legs[0])
    stop = first_rail["from"]
    return Option(
        kind=kind, minutes=round(itinerary["duration"] / 60), transfers=int(itinerary.get("transfers", 0)),
        depart=from_utc_iso(itinerary["startTime"]), arrive=from_utc_iso(itinerary["endTime"]),
        rides=tuple(Ride(label=ride_label(leg), mode=leg["mode"], minutes=round(leg.get("duration", 0) / 60))
                    for leg in legs),
        station_from=station_name(stop, city)[:40], station_from_lat=float(stop.get("lat", 0.0)),
        station_from_lng=float(stop.get("lon", 0.0)),
        covered=covered_by_deutschlandticket(itinerary, domestic=domestic),
    )


def choose_options(itineraries: Sequence[dict[str, Any]], not_before: datetime, *, domestic: bool,
                   city: str | None, local_itineraries: Sequence[dict[str, Any]] = ()) -> dict[str, Option | None]:
    """从一批路线里挑出「最快」和「只坐区间 / 本地交通的最快」两个方案。

    `itineraries` 是不限车型查到的；`local_itineraries` 是只允许区间 / 本地交通查到的（最快的班次里常常没有纯区间车）。
    只看 not_before 之后 LOOKAHEAD_MIN 分钟内出发的；没有路线 → 两个都是 None。
    最快本身就是纯区间车 → 只有 fastest（covered=True），regional 是 None（不重复）。
    """
    early, late = not_before - timedelta(minutes=1), not_before + timedelta(minutes=LOOKAHEAD_MIN)

    def ok(its: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [it for it in its if _transit_legs(it) and early <= from_utc_iso(it["startTime"]) <= late]

    usable = ok(itineraries)
    if not usable:
        return {"fastest": None, "regional": None}

    def key(it: dict[str, Any]) -> tuple[int, int]:
        return it["duration"], it.get("transfers", 0)

    fastest = min(usable, key=key)
    local = [it for it in [*usable, *ok(local_itineraries)] if covered_by_deutschlandticket(it, domestic=domestic)]
    out: dict[str, Option | None] = {"fastest": to_option(fastest, "fastest", domestic=domestic, city=city),
                                     "regional": None}
    if local and fastest not in local:                  # 最快的本身就是区间车：不重复列
        out["regional"] = to_option(min(local, key=key), "regional", domestic=domestic, city=city)
    return out


def latest_return(itineraries: Sequence[dict[str, Any]], *, regional_only: bool, domestic: bool) -> datetime | None:
    """一批「在某个时间之前到家」的路线里，最晚从目的地车站出发的那一班的出发时间；没有 → None。"""
    ok = [it for it in itineraries if _transit_legs(it)
          and (not regional_only or covered_by_deutschlandticket(it, domestic=domestic))]
    return max((_leg_departure(it) for it in ok), default=None)


def _leg_departure(it: dict[str, Any]) -> datetime:
    """第一段坐车的出发时间（不是走到车站的时间）。"""
    return from_utc_iso(_transit_legs(it)[0]["from"].get("departure") or it["startTime"])


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    from math import asin, cos, radians, sin, sqrt

    lat1, lng1, lat2, lng2 = map(radians, (*a, *b))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lng2 - lng1) / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(h))


DB_SEARCH_URL = "https://www.bahn.de/buchung/fahrplan/suche"


def db_query_text(origin: str, destination: str, when: datetime) -> str:
    """去 DB 查实时班次和票价时要填的内容：「Stuttgart Hauptbahnhof → Heidelberg Hauptbahnhof，12 月 12 日 09:56」。

    bahn.de 的搜索页不能只靠站名和时间预填（要带它自己的站点编号，我们没有；2026-10-25 在浏览器里试过：
    带站名和时间的链接打开后起点终点是空的、日期是今天），所以链接只到搜索页，要填什么写在旁边。"""
    return f"{origin} → {destination}，{when.month} 月 {when.day} 日 {when:%H:%M}"


# --------------------------------------------------------------------------- #
# 地址
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Address:
    label: str                          # 「Königstraße 28, 70173 Stuttgart」
    city: str
    country: str                        # DE / FR / NL / BE …
    lat: float
    lng: float
    kind: str = "ADDRESS"
    zip: str = ""


def _city_of(areas: Sequence[dict[str, Any]]) -> str:
    """地址所在的城市：默认区域里行政级别最细（但不超过 8）的那个；小地方没有这一级就退一步取任何非国家级的区域。"""
    best = [a for a in areas if a.get("default") and a.get("adminLevel", 0) <= 8]
    best = best or [a for a in areas if a.get("matched") and a.get("adminLevel", 0) <= 8]
    best = best or [a for a in areas if a.get("adminLevel", 0) > 2]
    return str(max(best, key=lambda a: a["adminLevel"])["name"]) if best else ""


# 房间 / 楼层 / 楼栋这类信息会让地址查询认错门牌（「Königstraße 28 3. OG Zimmer 305」被当成 Königstraße 3）
_UNIT = re.compile(
    r"(?:\b\d+\.?\s*(?:OG|EG|UG|DG|Etage|Stock)\b|\b(?:OG|EG|UG|DG|Etage)\s*\d*\b|"
    r"\b(?:Zimmer|Zi\.?|Raum|Apt\.?|Apartment|Wohnung|Whg\.?|Haus|Block|Stock)\s*[A-Za-z]?\d*[A-Za-z]?\b|"
    r"\b(?:Wohnheim|Studentenwohnheim)\b)", re.I)
_STR = re.compile(r"(?<=[a-zäöüß])str\.", re.I)
_CJK = re.compile("[\u3000-\u303f\u3400-\u9fff\uff00-\uffef]+")


def clean_address_query(text: str, zh_cities: dict[str, str] | None = None) -> str:
    """把用户写的地址整理成地址查询认得的样子：中文城市名换成当地语言名（斯图加特 → Stuttgart），
    去掉别的中文、楼层 / 房间号 / 楼栋，「Königstr.」展开成「Königstraße」。"""
    out = text
    for zh, local in (zh_cities or {}).items():
        out = out.replace(zh, f" {local} ")
    out = _CJK.sub(" ", out)
    out = _UNIT.sub(" ", out)
    out = _STR.sub("straße", out)
    out = re.sub(r"(?i)\bstr\.(?=\s|$|,)", "Straße", out)
    out = re.sub(r"\s*,\s*", ", ", out)          # 逗号后面补空格：「67,70176 Stuttgart」不补会被当成一个词，找不到门牌
    return re.sub(r"\s+", " ", out).strip(" ,;")


def address_score(a: Address, text: str) -> int:
    """0 = 城市和邮编都对得上你写的；越大越不像。"""
    low = text.lower()
    return (0 if a.city and a.city.lower() in low else 2) + (0 if a.zip and a.zip in low else 1)


def rank_addresses(found: Sequence[Address], text: str) -> list[Address]:
    """查询结果里城市 / 邮编出现在你输入里的排前面（「Weinbergstraße 5, Reutlingen」不要先给出别的小镇）。"""
    return sorted(found, key=lambda a: address_score(a, text))


def city_hint(text: str) -> str:
    """地址里最像城市的那一段：最后一个逗号后面的字（去掉邮编），没有逗号就取最后一个词。"""
    part = text.rsplit(",", 1)[-1] if "," in text else text.split()[-1] if text.split() else ""
    return re.sub(r"\b\d{4,5}\b", " ", part).strip()


def parse_address(row: dict[str, Any]) -> Address | None:
    if row.get("type") not in ("ADDRESS", "PLACE", "STOP") or "lat" not in row or "lon" not in row:
        return None
    city = _city_of(row.get("areas", []))
    zip_ = str(row.get("zip") or "").split(";")[0]
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    tail = " ".join(x for x in (zip_, city) if x)
    label = f"{name}, {tail}" if tail and city.lower() not in name.lower() else name
    return Address(label=label, city=city or zip_, country=str(row.get("country") or "").upper()[:2],
                   lat=float(row["lat"]), lng=float(row["lon"]), kind=row["type"], zip=zip_)


# --------------------------------------------------------------------------- #
# 联网
# --------------------------------------------------------------------------- #
@dataclass
class Transitous:
    """Transitous 客户端：缓存 + 并发上限 + 统一的错误。`client` 可注入（测试用 httpx.MockTransport）。"""

    client: httpx.Client | None = None
    ttl: float = CACHE_TTL_SECONDS
    clock: Callable[[], float] = time.time
    max_parallel: int = 3                  # 公共服务排队处理：并发开大了反而更慢（实测 3 个并发 19 秒，8 个并发 30 秒）
    _cache: dict[tuple, tuple[float, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _gate: threading.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.client = self.client or httpx.Client(headers={"User-Agent": USER_AGENT})
        self._gate = threading.Semaphore(self.max_parallel)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        key = (path, tuple(sorted(params.items())))
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        assert self.client is not None
        try:
            with self._gate:
                r = self.client.get(f"{API}{path}", params=params, timeout=TIMEOUT_SECONDS)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TransitError(f"时刻表服务暂时连不上（{type(exc).__name__}）") from exc
        with self._lock:
            self._cache[key] = (now, body)
        return body

    def geocode(self, text: str, zh_cities: dict[str, str] | None = None) -> list[Address]:
        cleaned = clean_address_query(text, zh_cities)
        if len(cleaned) < 3:
            return []
        rows = self._get("/geocode", {"text": cleaned})
        out = self._parse_rows(rows)
        hint = city_hint(cleaned)
        # 最像的结果城市和邮编都对不上（这座城市里没有这条街 / 门牌）：再按城市名查一次，用这座城市的车站 / 地点当近似
        if len(hint) >= 3 and (not out or min(address_score(a, cleaned) for a in out) >= 2):
            extra = [a for a in self._parse_rows(self._get("/geocode", {"text": hint}))
                     if a.city and (a.city.lower() in hint.lower() or hint.lower() in a.city.lower())]
            out.extend(a for a in extra[:3] if all(a.label != x.label for x in out))
        ranked = rank_addresses(out, cleaned)
        if ranked and address_score(ranked[0], cleaned) <= 1:     # 已经有城市对得上的：别的城市同名街道不再列出来凑数
            ranked = [a for a in ranked if address_score(a, cleaned) <= 1]
        return ranked[:MAX_ADDRESSES]

    @staticmethod
    def _parse_rows(rows: Any) -> list[Address]:
        if not isinstance(rows, list):
            raise TransitError("地址查询返回的数据对不上")
        out: list[Address] = []
        for row in rows:
            a = parse_address(row)
            if a and all(a.label != x.label for x in out):
                out.append(a)
        return out

    def plan(self, from_place: str, to_place: str, when: datetime, *, arrive_by: bool = False,
             local_only: bool = False, n: int = 8) -> list[dict[str, Any]]:
        """路线；from / to 是「纬度,经度」。when 是当地时间：默认找这个时间之后出发的；arrive_by 找这个时间之前到的；
        local_only 只允许区间 / 本地交通（找「有 Deutschlandticket 就不用另买票」的方案）。"""
        params = {"fromPlace": from_place, "toPlace": to_place, "time": to_utc_iso(when), "numItineraries": n}
        if arrive_by:
            params["arriveBy"] = "true"
        if local_only:
            params["transitModes"] = LOCAL_MODES_PARAM
        body = self._get("/plan", params)
        its = body.get("itineraries") if isinstance(body, dict) else None
        if not isinstance(its, list):
            raise TransitError("路线查询返回的数据对不上")
        return its
