"""预算估算（规格 M09 S1 / S2、M01 S4b）：门票 / 餐饮 / 交通 / 住宿，逐人计价，结算货币 + 并列货币。

纯函数：不联网、不读数据库，汇率和设置由调用方传进来，所以好测。
价格规则：
- 门票只用地点库里核对过的价格和优惠规则；每个人按自己的年龄 / 长居身份 / 是否学生取**最低的适用价**，
  并按这个景点被排在哪一天取对应月份的价格（淡旺季）。没核对过优惠规则的景点一律按成人全价，并点名。
- 交通只用 `data/budget/<city>.json` 里带官方来源和核对日期的票价。
- 餐饮是**估算**（经验值，页面标「估算」，可被用户覆盖）；住宿用用户自己填的每晚价格。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.cities import DEFAULT_CITY
from app.editing import EditError, SwapPlace, alternatives, apply_edit
from app.fx import FxRate
from app.library import Place, PlaceLibrary, load_library
from app.schemas import Itinerary, Residence, Traveler, TravelRequest

_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "budget"
_TRANSIT_WORDS = ("地铁", "RER", "公交", "公共交通", "巴士", "火车")      # 行程里 transport 文字出现这些 = 要买票的一段
_LEVEL_ZH = {"low": "节省", "medium": "适中", "high": "轻奢"}
MAX_SUGGESTIONS = 3
MAX_FIT_STEPS = 12
RESIDENCE_ZH = {"none": "不长居欧洲", "fr": "长居法国", "eu": "长居其他欧盟国家"}


# --------------------------------------------------------------------------- #
# 策展数据
# --------------------------------------------------------------------------- #
class SingleTicket(BaseModel):
    name: str
    eur: float = Field(gt=0)
    source_url: str = Field(min_length=8)          # 价格必须带来源和核对日期（硬规则：价格只信策展数据）
    checked_at: date
    child_eur: float | None = Field(default=None, ge=0)
    child_ages: list[int] | None = None            # [起, 止]，含
    child_note: str | None = None
    child_source_url: str | None = None


class TouristPass(BaseModel):
    name: str
    eur_by_days: dict[int, float]
    child_eur_by_days: dict[int, float] | None = None      # 4–9 岁儿童半价
    child_note: str | None = None
    zones_note: str | None = None
    source_url: str = Field(min_length=8)
    checked_at: date

    @field_validator("eur_by_days", "child_eur_by_days")
    @classmethod
    def _ok(cls, v: dict[int, float] | None) -> dict[int, float] | None:
        if v is None:
            return v
        if not v or any(d <= 0 or p <= 0 for d, p in v.items()):
            raise ValueError("通票价格表不能为空，天数和价格都要大于 0")
        return v


class Transport(BaseModel):
    single_ticket: SingleTicket
    tourist_pass: TouristPass
    excluded: str = ""


class Meals(BaseModel):
    kind: str
    unit: str
    eur_per_day: dict[str, float]
    basis: str
    child_factor: dict[str, float] = Field(default_factory=lambda: {"under_4": 0.0, "under_12": 0.5})

    @field_validator("kind")
    @classmethod
    def _estimate(cls, v: str) -> str:
        if v != "estimate":
            raise ValueError("餐饮参考值必须标成 estimate：它不是核对过的价格")
        return v

    @field_validator("eur_per_day")
    @classmethod
    def _levels(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != {"low", "medium", "high"} or any(x < 0 for x in v.values()):
            raise ValueError("餐饮参考值要有 low / medium / high 三档，且不能为负")
        return v


class BudgetData(BaseModel):
    city: str
    transport: Transport
    meals: Meals


def has_budget_data(city: str) -> bool:
    """这座城市有没有核对过价格的预算数据（现建的城市没有：门票、交通价格没核对，不估算）。"""
    return (_DATA_DIR / f"{city}.json").exists()


@lru_cache(maxsize=4)
def load_budget_data(city: str = DEFAULT_CITY) -> BudgetData:
    return BudgetData.model_validate(json.loads((_DATA_DIR / f"{city}.json").read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# 用户设置（每份行程一份，不进行程版本）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BudgetSettings:
    travelers: tuple[Traveler, ...] = field(default_factory=lambda: (Traveler(),))
    currency: str = "CNY"                        # 结算货币：金额以它为主，另一种并列
    cap_amount: float | None = None
    cap_currency: str = "CNY"                    # 上限的货币（和结算货币分开存，切换结算货币不改变上限的含义）
    meal_eur_per_day: float | None = None        # 用户自己填的餐饮每人每天；None = 用档位参考值

    @classmethod
    def from_request(cls, request: TravelRequest) -> BudgetSettings:
        cur = getattr(request.currency, "value", request.currency)      # model_copy 出来的对象里可能是普通字符串
        return cls(travelers=tuple(request.travelers), currency=cur, cap_amount=request.budget_cap, cap_currency=cur)


# --------------------------------------------------------------------------- #
# 门票：逐人、按日期
# --------------------------------------------------------------------------- #
def eligible(who: str, residence: Residence | str) -> bool:
    """优惠的口径（和官网一致）：长居法国同时满足长居欧盟。"""
    r = residence.value if isinstance(residence, Residence) else residence
    if who == "any":
        return True
    if who == "resident_eu":
        return r in ("fr", "eu")
    if who == "resident_fr":
        return r == "fr"
    return False


@dataclass(frozen=True)
class TicketPrice:
    eur: float
    label: str                 # 命中的规则：成人价 / 欧盟居民价 / 免费（≤17 岁）…
    standard: float            # 这一天的成人标准价（算「省了多少」的基准）
    season_fallback: bool = False


def _standard(place: Place, visit: date) -> tuple[float, float | None, bool]:
    """(这一天的成人价, 这一天的欧盟居民价, 是不是月份没覆盖而回退)。"""
    for p in place.price_periods:
        if visit.month in p.months:
            return p.eur, p.eea_eur, False
    return float(place.price_eur or 0), None, bool(place.price_periods)


def ticket_price(place: Place, visit: date, t: Traveler) -> TicketPrice:
    """这个人在这一天进这个景点的最低适用价。place.price_eur 必须有值。"""
    std, period_eea, fallback = _standard(place, visit)
    best, label = std, "成人价" if std > 0 else "免费"
    c = place.concessions
    if c is None:
        return TicketPrice(best, label, std, fallback)
    eea = period_eea if period_eea is not None else c.eea_eur
    if eea is not None and eligible("resident_eu", t.residence) and eea < best:
        best, label = eea, "欧盟居民价"
    for b in c.bands:
        if b.min_age <= t.age <= b.max_age and eligible(b.who, t.residence) and b.eur < best:
            best = b.eur
            label = f"免费（{t.age} 岁）" if b.eur == 0 else f"优惠价（{b.min_age}–{b.max_age} 岁）"
    if t.student and c.student_eur is not None and eligible(c.student_who, t.residence) and c.student_eur < best:
        best, label = c.student_eur, "学生价"
    return TicketPrice(best, label, std, fallback)


def _known_price(p: Place) -> float | None:
    """地点的已知门票基准价：核对过的价格；公共开放空间 = 0；其余没查到 = None。"""
    if p.price_eur is not None:
        return p.price_eur
    return 0.0 if p.open_access else None


def _needs_price(p: Place) -> bool:
    """这个地点算不算「该有门票价格却还没查到」：公共开放空间、餐饮 / 购物点不收门票，不算。"""
    return not p.open_access and p.category.value not in ("food", "shopping")


def _price_for(p: Place, visit: date, t: Traveler) -> float | None:
    """这个人进这个地点的价格；没价格返回 None。"""
    if p.price_eur is not None:
        return ticket_price(p, visit, t).eur
    return 0.0 if p.open_access else None


def traveler_label(t: Traveler) -> str:
    bits = [f"{t.age} 岁"]
    if t.residence != Residence.none:
        bits.append(RESIDENCE_ZH[t.residence.value])
    if t.student:
        bits.append("学生")
    return " · ".join(bits)


# --------------------------------------------------------------------------- #
# 交通
# --------------------------------------------------------------------------- #
def _transit_legs(it: Itinerary) -> list[int]:
    """每天要买票的乘车段数：行程里有「地铁 / RER / 公交」文字的条目各算一段，当天有乘车再加一程回住宿。
    机场与住宿之间（logistics）不算——机场往返不在估算里。"""
    out = []
    for d in it.days:
        legs = sum(1 for i in d.items if i.type != "logistics" and i.transport
                   and any(w in i.transport for w in _TRANSIT_WORDS))
        out.append(legs + 1 if legs else 0)
    return out


def _best_transport(legs_per_day: list[int], single: float, passes: dict[int, float]) -> dict[str, Any]:
    """单程票 vs 通票（连续 k 天，k 取价格表里有的天数；通票盖住乘车最多的那几天，其余用单程票），取便宜的。"""
    total_legs = sum(legs_per_day)
    singles_cost = round(total_legs * single, 2)
    best_pass: tuple[float, int, int] | None = None            # (总价, 通票天数, 窗口起始下标)
    for k, price in sorted(passes.items()):
        window = min(k, len(legs_per_day))
        start = max(range(len(legs_per_day) - window + 1), key=lambda s: sum(legs_per_day[s:s + window]))
        covered = sum(legs_per_day[start:start + window])
        cost = round(price + (total_legs - covered) * single, 2)
        if best_pass is None or cost < best_pass[0]:
            best_pass = (cost, k, start)
    if total_legs == 0:
        return {"eur_person": 0.0, "detail": "行程里没有要乘地铁 / 公交的段（多是步行）", "choice": "none", "legs": 0}
    if best_pass is not None and best_pass[0] < singles_cost:
        cost, k, start = best_pass
        covered_days = min(k, len(legs_per_day))
        rest = total_legs - sum(legs_per_day[start:start + covered_days])
        extra = f"，其余 {rest} 段单程票" if rest else ""
        return {"eur_person": cost, "choice": "pass", "legs": total_legs,
                "detail": f"{k} 日通票（第 {start + 1}–{start + covered_days} 天）{extra}，比全买单程票"
                          f"（{total_legs} 段 × €{single:g} = €{singles_cost:g}）便宜 €{singles_cost - cost:.2f}"}
    return {"eur_person": singles_cost, "choice": "single", "legs": total_legs,
            "detail": f"单程票 {total_legs} 段 × €{single:g}，比通票便宜"}


def transport_cost(legs_per_day: list[int], data: Transport) -> dict[str, Any]:
    """一位 10 岁以上的人：单程票 vs Paris Visite 通票，取便宜的。"""
    return _best_transport(legs_per_day, data.single_ticket.eur, data.tourist_pass.eur_by_days)


def _traveler_transport(t: Traveler, legs_per_day: list[int], data: Transport) -> float:
    """4 岁以下免费；4–9 岁用官方儿童单程票或儿童通票（半价）取便宜的；10 岁起按成人。"""
    s = data.single_ticket
    lo, hi = s.child_ages or (4, 9)
    if t.age < lo:
        return 0.0
    if s.child_eur is not None and lo <= t.age <= hi:
        kid_passes = data.tourist_pass.child_eur_by_days or {}
        return _best_transport(legs_per_day, s.child_eur, kid_passes)["eur_person"]
    return transport_cost(legs_per_day, data)["eur_person"]


def _lodging(it: Itinerary, request: TravelRequest) -> tuple[float, list[str]]:
    """住宿总价（一间房）和待补说明。只算落在行程日期内的晚数。"""
    nights = [it.days[0].date + timedelta(days=k) for k in range(len(it.days) - 1)]
    unpriced = 0
    total = 0.0
    covered: set[date] = set()
    for s in request.stays:
        for k in range(s.nights):
            night = s.check_in + timedelta(days=k)
            if night not in nights:
                continue
            covered.add(night)
            if s.nightly_eur is None:
                unpriced += 1
            else:
                total += s.nightly_eur
    pending = []
    if unpriced:
        pending.append(f"{unpriced} 晚住宿没填价格")
    missing = len(nights) - len(covered)
    if missing:
        pending.append(f"{missing} 晚还没填住宿")
    return round(total, 2), pending


# --------------------------------------------------------------------------- #
# 总入口
# --------------------------------------------------------------------------- #
def _visit_dates(it: Itinerary, lib: PlaceLibrary) -> dict[str, tuple[date, str]]:
    """地点 id -> (它被排在哪一天, 行程里的标题)；同一个地点只算一次（第一次出现）。"""
    seen: dict[str, tuple[date, str]] = {}
    for d in it.days:
        for i in d.items:
            if i.place_id and i.place_id in lib.places and i.place_id not in seen:
                seen[i.place_id] = (d.date, i.title)
    return seen


def estimate_budget(it: Itinerary, request: TravelRequest, settings: BudgetSettings, fx: FxRate | None, *,
                    library: PlaceLibrary | None = None, data: BudgetData | None = None,
                    skip_ids: frozenset[str] = frozenset(), meal_days: int | None = None) -> dict[str, Any]:
    """meal_days：餐饮按几天算（默认 = 行程天数）；多城市里换城市那天被两座城市各算一次，后一座要少算一天。"""
    lib = library or load_library(request.city_id)
    data = data or load_budget_data(request.city_id)
    party = list(settings.travelers)
    n = len(party)
    days = len(it.days)
    seen = _visit_dates(it, lib)
    uses_concessions = any(t.age < 26 or t.residence != Residence.none or t.student for t in party)

    # ---- 门票：逐人逐景点 ----
    per_traveler_tickets = [0.0] * n
    saved = [0.0] * n
    saved_places: list[list[str]] = [[] for _ in party]
    free_places: list[list[str]] = [[] for _ in party]
    paid_places = set()
    unchecked: list[str] = []
    season_notes: list[str] = []
    ticket_pending: list[str] = []
    for pid, (visit, title) in seen.items():
        p = lib.places[pid]
        if p.price_eur is None:
            if _needs_price(p):
                ticket_pending.append(title)
            continue
        if p.price_eur > 0:
            paid_places.add(pid)
        if p.price_eur > 0 and uses_concessions and (p.concessions is None or not p.concessions.complete):
            unchecked.append(title + ("（只核对了一部分）" if p.concessions else ""))
        for i, t in enumerate(party):
            tp = ticket_price(p, visit, t)
            per_traveler_tickets[i] += tp.eur
            if tp.standard - tp.eur > 0.005:
                saved[i] += tp.standard - tp.eur
                (free_places if tp.eur == 0 else saved_places)[i].append(title)
            if tp.season_fallback and i == 0:
                season_notes.append(f"{title}：{visit.month} 月官网没写分月份价格，按 €{p.price_eur:g} 估算")
    tickets_total = round(sum(per_traveler_tickets), 2)
    discounts = []
    for i, t in enumerate(party):
        if saved[i] > 0.005:
            named = {"name": t.name} if t.name else {}                 # 填了姓名才带上（老行程的输出不变）
            discounts.append({"traveler": i + 1, **named, "who": traveler_label(t), "saved_eur": round(saved[i], 2),
                              "places": free_places[i] + saved_places[i]})
    free_count = sum(1 for pid in seen if lib.places[pid].price_eur == 0)
    tickets_detail = f"{len(paid_places)} 个收费景点" + (f"、{free_count} 个免费景点" if free_count else "")
    if discounts:
        tickets_detail += f"；已按出行人身份计入优惠，共省 €{sum(d['saved_eur'] for d in discounts):,.2f}"

    # ---- 餐饮：估算（或用户自己填的）----
    level = request.budget_level.value
    default_meal = data.meals.eur_per_day[level]
    meal_day = settings.meal_eur_per_day if settings.meal_eur_per_day is not None else default_meal
    is_override = settings.meal_eur_per_day is not None
    cf = data.meals.child_factor

    def meal_factor(t: Traveler) -> float:
        return cf.get("under_4", 0.0) if t.age < 4 else cf.get("under_12", 0.5) if t.age < 12 else 1.0

    meals_total = round(sum(meal_day * (days if meal_days is None else meal_days) * meal_factor(t) for t in party), 2)

    # ---- 交通：逐人 ----
    legs = _transit_legs(it)
    transport_total = round(sum(_traveler_transport(t, legs, data.transport) for t in party), 2)
    adult_tr = transport_cost(legs, data.transport)

    # ---- 住宿（一间房的总价）----
    lodging_total, lodging_pending = _lodging(it, request)

    rows = [
        {"key": "tickets", "label": "门票", "source": "library", "source_note": "地点库里核对过的价格和优惠规则",
         "eur_total": tickets_total, "pending": ticket_pending, "detail": tickets_detail},
        {"key": "meals", "label": "餐饮", "source": "user" if is_override else "estimate",
         "source_note": f"你自己填的每人每天 €{meal_day:g}" if is_override
         else f"估算，非实时价格：{_LEVEL_ZH[level]}档每人每天 €{default_meal:g} × {days} 天",
         "eur_total": meals_total, "pending": [],
         "detail": data.meals.basis if not is_override else "覆盖值只存在这份行程上；4 岁以下不计、12 岁以下按一半"},
        {"key": "transport", "label": "交通", "source": "official",
         "source_note": f"官方 2026 年票价（{data.transport.single_ticket.checked_at} 核对）",
         "eur_total": transport_total, "pending": [],
         "detail": (adult_tr["detail"] + "。" + data.transport.excluded) if data.transport.excluded
         else adult_tr["detail"]},
        {"key": "lodging", "label": "住宿", "source": "user", "source_note": "你填的每晚价格（一间房）",
         "eur_total": lodging_total, "pending": lodging_pending,
         "detail": "一间房的总价，人均 = 总价 ÷ 人数"},
    ]
    for r in rows:
        r["eur_person"] = round(r["eur_total"] / n, 2)
    total_eur = round(sum(r["eur_total"] for r in rows), 2)
    rate = fx.rate if fx else None

    def cny(v: float) -> float | None:
        return None if rate is None else round(v * rate, 2)

    for r in rows:
        r["cny_person"], r["cny_total"] = cny(r["eur_person"]), cny(r["eur_total"])
        r["share"] = round(r["eur_total"] / total_eur, 4) if total_eur else 0.0
        r["unknown"] = bool(r["pending"]) and r["eur_total"] == 0      # 一个价格都没有：显示「待补」，不是 0
    pending_count = sum(len(r["pending"]) for r in rows)

    out: dict[str, Any] = {
        "currency": settings.currency, "days": days,
        "travelers": [{**({"name": t.name} if t.name else {}), "age": t.age, "residence": t.residence.value,
                       "student": t.student, "label": traveler_label(t)}
                      for t in party],
        "cap": None if settings.cap_amount is None else {"amount": settings.cap_amount,
                                                          "currency": settings.cap_currency},
        "meal": {"value": meal_day, "default": default_meal, "is_override": is_override, "level": level,
                 "level_zh": _LEVEL_ZH[level]},
        "fx": None if fx is None else {"rate": fx.rate, "date": fx.date, "stale": fx.stale},
        "categories": rows,
        "discounts": discounts,
        "total": {"eur_person": round(total_eur / n, 2), "eur_total": total_eur,
                  "cny_person": cny(total_eur / n), "cny_total": cny(total_eur)},
        "pending_count": pending_count,
        "status": "none", "status_amount": None, "status_currency": None, "suggestions": [], "gap": None,
        "notes": [],
    }
    if fx is None:
        out["notes"].append("暂时拿不到汇率，只显示欧元")
    elif fx.stale:
        out["notes"].append(f"汇率为 {fx.date} 的旧数据（这次没拉到最新的）")
    if unchecked:
        out["notes"].append(f"{len(unchecked)} 个景点的优惠规则未核对，按成人全价：{'、'.join(unchecked)}")
    out["notes"].extend(season_notes)
    if pending_count:
        out["notes"].append(f"还有 {pending_count} 项待补，实际可能更高")

    _apply_cap(out, settings, rate, total_eur, tickets_total,
               lambda conv: cheaper_swaps(it, request, lib, party, conv, skip_ids=skip_ids))
    return out


def _apply_cap(out: dict[str, Any], settings: BudgetSettings, rate: float | None, total_eur: float,
               tickets_eur: float, swaps: Callable[[float], list[dict[str, Any]]]) -> None:
    """对照预算上限：算出「没超 / 超了多少」，超了就带替换建议（swaps(欧元换结算货币的系数)）和差距提示。"""
    if settings.cap_amount is None:
        return
    cap_eur = settings.cap_amount if settings.cap_currency == "EUR" else (
        None if rate is None else settings.cap_amount / rate)
    if cap_eur is None or (settings.currency == "CNY" and rate is None):
        out["status"] = "unknown"
        return
    diff_eur = total_eur - cap_eur
    out["status"] = "over" if diff_eur > 0.005 else "within"
    conv = rate if settings.currency == "CNY" else 1.0
    out["status_amount"] = round(abs(diff_eur) * conv, 2)
    out["status_currency"] = settings.currency
    if diff_eur > 0.005:
        out["suggestions"] = swaps(conv)
        out["gap"] = _gap(total_eur, tickets_eur, cap_eur, conv)


def _gap(total_eur: float, tickets_eur: float, cap_eur: float, conv: float) -> dict[str, Any] | None:
    """超出预算时，差距是不是大到「换景点也没用」：
    unreachable = 就算门票全免，门票以外的费用（餐饮 / 交通 / 住宿）也已经超过上限；large = 超出上限的一半以上。"""
    non_ticket = total_eur - tickets_eur
    over_ratio = (total_eur - cap_eur) / cap_eur if cap_eur > 0 else float("inf")
    level = "unreachable" if non_ticket > cap_eur else "large" if over_ratio >= 0.5 else None
    if level is None:
        return None
    return {"level": level, "non_ticket": round(non_ticket * conv, 2), "tickets": round(tickets_eur * conv, 2),
            "over_ratio": None if over_ratio == float("inf") else round(over_ratio, 2)}


def cheaper_swaps(it: Itinerary, request: TravelRequest, lib: PlaceLibrary, party: list[Traveler], conv: float, *,
                  limit: int = MAX_SUGGESTIONS, skip_ids: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """超预算时的替换建议：行程里每个收费景点，在「换一个」的候选里找**这批出行人合计更便宜**的（含免费），
    按省得多排序。候选没有价格的不推荐（不知道是不是更便宜）。conv = 欧元换成结算货币的系数。"""
    out: list[dict[str, Any]] = []
    seen = _visit_dates(it, lib)
    locked = {i.place_id for d in it.days for i in d.items if i.locked and i.place_id}
    for pid, (visit, title) in seen.items():
        p = lib.places[pid]
        if pid in skip_ids or pid in locked or p.price_eur is None:
            continue
        cost = sum(ticket_price(p, visit, t).eur for t in party)
        if cost <= 0:
            continue
        try:
            alts = alternatives(it, request, pid, lib, limit=200)
        except EditError:
            continue
        best = None
        for a in alts:
            ap = lib.places[a.place_id]
            if _known_price(ap) is None:
                continue
            alt_cost = sum(_price_for(ap, visit, t) or 0.0 for t in party)
            saving = cost - alt_cost
            if saving <= 0.005:
                continue
            same_theme = not a.reason.startswith("相邻")            # 优先同主题的备选；没有才用相邻主题的
            key = (same_theme, saving)
            if best is None or key > best[0]:
                best = (key, a, alt_cost, saving)
        if best is None:
            continue
        _key, alt, alt_cost, saving = best
        out.append({"place_id": pid, "title": title, "cost_eur": round(cost, 2),
                    "alt_place_id": alt.place_id, "alt_title": alt.name_zh, "alt_cost_eur": round(alt_cost, 2),
                    "saving_eur": round(saving, 2), "saving": round(saving * conv, 2)})
    out.sort(key=lambda s: -s["saving_eur"])
    return out[:limit]


def cat_total(b: dict[str, Any], key: str) -> float:
    return next(c["eur_total"] for c in b["categories"] if c["key"] == key)


def _cap_in_eur(settings: BudgetSettings, fx: FxRate | None) -> float | None:
    if settings.cap_amount is None:
        return None
    if settings.cap_currency == "EUR":
        return settings.cap_amount
    return None if fx is None else settings.cap_amount / fx.rate


# --------------------------------------------------------------------------- #
# 生成时按预算调整（规格 M01 S4b）
# --------------------------------------------------------------------------- #
def fit_to_budget(it: Itinerary, request: TravelRequest, fx: FxRate | None, *,
                  library: PlaceLibrary | None = None) -> tuple[Itinerary, list[str]]:
    """填了预算上限、估算超出时，把最贵的景点逐个换成同主题 / 同区域 / 当天开门的更便宜备选，直到不超或没得换。

    不动你锁定的点和「必去」的点。每一换都写一条说明；换不动写明还超多少；拿不到汇率又以人民币设上限时不调整、写明原因。
    """
    if request.budget_cap is None:
        return it, []
    lib = library or load_library(request.city_id)
    settings = BudgetSettings.from_request(request)
    must = set(request.must_go_ids)
    for name in request.must_go:
        hit = lib.find(name)
        if hit:
            must.add(hit.id)
    skip = frozenset(must)
    notes: list[str] = []
    cur = it
    first = estimate_budget(cur, request, settings, fx, library=lib, skip_ids=skip)
    if first["status"] == "over":
        fixed = first["total"]["eur_total"] - cat_total(first, "tickets")           # 餐饮 + 交通 + 住宿：换景点省不掉
        cap_eur = _cap_in_eur(settings, fx)
        if cap_eur is not None and fixed > cap_eur:
            unit = "¥" if settings.currency == "CNY" else "€"
            conv = (fx.rate if fx else 1.0) if settings.currency == "CNY" else 1.0
            return it, [f"门票以外的费用（餐饮 / 交通 / 住宿）估算已经 {unit}{fixed * conv:,.0f}，超出了预算上限，"
                        "换景点也省不回来，所以没有改动行程。可以放宽上限，或在预算卡里调整餐饮和出行人。"]
    for _ in range(MAX_FIT_STEPS):
        b = estimate_budget(cur, request, settings, fx, library=lib, skip_ids=skip)
        if b["status"] == "unknown":
            return cur, notes + ["预算上限是人民币，但现在拿不到汇率，没法按预算调整行程；可以在预算卡里改成欧元上限。"]
        if b["status"] != "over":
            return cur, notes
        if not b["suggestions"]:
            break
        s = b["suggestions"][0]
        try:
            res = apply_edit(cur, request, SwapPlace(place_id=s["place_id"], new_place_id=s["alt_place_id"]), lib)
        except EditError:
            skip = skip | {s["place_id"]}                 # 这个点换不了（比如排不下）：跳过，试下一个
            continue
        cur = res.itinerary
        unit = "¥" if settings.currency == "CNY" else "€"
        notes.append(f"为了预算，把「{s['title']}」换成了「{s['alt_title']}」，省 {unit}{s['saving']:,.0f}")
    b = estimate_budget(cur, request, settings, fx, library=lib, skip_ids=skip)
    if b["status"] == "over":
        unit = "¥" if b["status_currency"] == "CNY" else "€"
        notes.append(f"已经换不动了，估算仍超出预算 {unit}{b['status_amount']:,.0f}。"
                     "可以放宽上限，或在行程页手动删掉 / 换掉贵的点。")
    return cur, notes


def estimate_budget_multi(it: Itinerary, parent: TravelRequest, settings: BudgetSettings,
                          fx: FxRate | None) -> dict[str, Any]:
    """多城市：每座城市按自己的价格数据各算一份，再加在一起。

    没有核对过价格的城市（自动现建的）不计入，并在备注里点名；城市之间的大交通（飞机 / 火车）不在估算里。
    换城市那天的餐饮只算一次；预算上限对整趟旅行算。
    """
    from app import multicity  # 延迟导入：multicity 要用 trips，trips 不应在导入 budget 时被牵进来

    no_cap = replace(settings, cap_amount=None)
    priced: list[tuple[str, TravelRequest, Itinerary, int]] = []        # 城市名、需求、行程、餐饮算几天
    skipped: list[str] = []
    unpriced_days = 0
    prev_end = None
    for req, view in multicity.split(parent, it):
        name = multicity.city_name(req.city_id)
        days = len(view.days) - (1 if prev_end is not None and req.start_date == prev_end else 0)
        prev_end = req.end_date
        if has_budget_data(req.city_id):
            priced.append((name, req, view, days))
        else:
            skipped.append(name)
            unpriced_days += days
    if not priced:
        raise ValueError("这趟旅行的城市都没有核对过的价格数据")
    # 没有价格数据的城市，门票和交通没法估；餐饮是按档位的经验值，跟着第一座有数据的城市的标准一起算上
    parts: list[tuple[str, TravelRequest, Itinerary, dict[str, Any]]] = []
    for k, (name, req, view, days) in enumerate(priced):
        meal_days = days + (unpriced_days if k == 0 else 0)
        parts.append((name, req, view, estimate_budget(view, req, no_cap, fx, meal_days=meal_days)))

    first = parts[0][3]
    n = len(settings.travelers)
    rate = fx.rate if fx else None

    def cny(v: float) -> float | None:
        return None if rate is None else round(v * rate, 2)

    rows: list[dict[str, Any]] = []
    for k, base in enumerate(first["categories"]):
        mine = [(name, out["categories"][k]) for name, _r, _v, out in parts]
        total = round(sum(r["eur_total"] for _n, r in mine), 2)
        pending = [f"{name}：{p}" if base["key"] == "lodging" else p for name, r in mine for p in r["pending"]]
        details = [f"{name}：{r['detail']}" for name, r in mine]
        notes = list(dict.fromkeys(r["source_note"] for _n, r in mine))
        rows.append({**base, "eur_total": total, "pending": pending, "source_note": "；".join(notes),
                     "detail": details[0] if len(details) == 1 else "；".join(details)})
    total_eur = round(sum(r["eur_total"] for r in rows), 2)
    for r in rows:
        r["eur_person"] = round(r["eur_total"] / n, 2)
        r["cny_person"], r["cny_total"] = cny(r["eur_person"]), cny(r["eur_total"])
        r["share"] = round(r["eur_total"] / total_eur, 4) if total_eur else 0.0
        r["unknown"] = bool(r["pending"]) and r["eur_total"] == 0

    saved: dict[int, dict[str, Any]] = {}
    for _name, _r, _v, out in parts:
        for d in out["discounts"]:
            have = saved.setdefault(d["traveler"], {**d, "saved_eur": 0.0, "places": []})
            have["saved_eur"] = round(have["saved_eur"] + d["saved_eur"], 2)
            have["places"] = [*have["places"], *d["places"]]
    pending_count = sum(len(r["pending"]) for r in rows)
    out_all: dict[str, Any] = {
        **{k: first[k] for k in ("currency", "travelers", "meal", "fx")},
        "cap": None if settings.cap_amount is None else {"amount": settings.cap_amount,
                                                          "currency": settings.cap_currency},
        "days": len(it.days),
        "categories": rows,
        "discounts": list(saved.values()),
        "total": {"eur_person": round(total_eur / n, 2), "eur_total": total_eur,
                  "cny_person": cny(total_eur / n), "cny_total": cny(total_eur)},
        "pending_count": pending_count,
        "status": "none", "status_amount": None, "status_currency": None, "suggestions": [], "gap": None,
        "notes": [],
    }
    for name, _r, _v, out in parts:
        for note in out["notes"]:
            if note.startswith("还有 "):
                continue
            line = note if note.startswith(("暂时拿不到汇率", "汇率为")) or len(parts) == 1 else f"{name}：{note}"
            if line not in out_all["notes"]:
                out_all["notes"].append(line)
    if skipped:
        out_all["notes"].append(f"{'、'.join(skipped)}的门票和交通价格没有核对过，没有计入（餐饮按{parts[0][0]}的标准估算）")
    out_all["notes"].append("城市之间的飞机 / 火车票不在估算里，请按订好的票自己加上")
    if pending_count:
        out_all["notes"].append(f"还有 {pending_count} 项待补，实际可能更高")

    def swaps(conv: float) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for _name, req, view, _out in parts:
            found.extend(cheaper_swaps(view, req, load_library(req.city_id), list(settings.travelers), conv))
        found.sort(key=lambda s: -s["saving_eur"])
        return found[:MAX_SUGGESTIONS]

    tickets = next(r["eur_total"] for r in rows if r["key"] == "tickets")
    _apply_cap(out_all, settings, rate, total_eur, tickets, swaps)
    return out_all
