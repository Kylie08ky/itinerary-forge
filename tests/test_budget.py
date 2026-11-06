"""预算：出行人逐人计价（欧盟居民 / 儿童 / 青年 / 学生 / 淡旺季）、交通取便宜、门票 / 餐饮 / 住宿、结算货币、
汇率缓存与降级、超预算建议、生成时按预算自动换、设置接口、导出（规格 M09 S1 / S2、M01 S4b）。

汇率用假的 Frankfurter 响应（httpx.MockTransport），测试里不联网。
"""

from __future__ import annotations

import json
import re
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import budget as B
from app.api import create_app
from app.budget import (
    BudgetData,
    BudgetSettings,
    estimate_budget,
    fit_to_budget,
    load_budget_data,
    ticket_price,
    transport_cost,
)
from app.fx import CACHE_TTL_SECONDS, FxRate, FxService
from app.library import Place, load_library
from app.llm import LLMError
from app.schemas import Itinerary, Residence, Traveler, TravelRequest
from app.versions import PlanStore

LIB = load_library("paris")
DATA = load_budget_data("paris")
RATE = FxRate("EUR", "CNY", 7.5, "2026-10-13")


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


def T(age=30, residence="none", student=False) -> Traveler:
    return Traveler(age=age, residence=Residence(residence), student=student)


# --------------------------------------------------------------------------- #
# 假的 Frankfurter
# --------------------------------------------------------------------------- #
class FakeFrankfurter:
    def __init__(self, rate=7.489, date="2026-10-13", *, fail: int | None = None, garbage=False):
        self.calls: list[httpx.Request] = []
        self.rate, self.date, self.fail, self.garbage = rate, date, fail, garbage

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail:
            return httpx.Response(self.fail, json={"message": "boom"})
        if self.garbage:
            return httpx.Response(200, json={"hello": "world"})
        symbol = request.url.params["symbols"]
        rate = self.rate if symbol == "CNY" else 0.86                                  # 欧元 -> 英镑
        return httpx.Response(200, json={"amount": 1.0, "base": "EUR", "date": self.date, "rates": {symbol: rate}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


# --------------------------------------------------------------------------- #
# 策展数据
# --------------------------------------------------------------------------- #
def test_every_transport_price_has_an_official_source_and_a_check_date():
    for item in (DATA.transport.single_ticket, DATA.transport.tourist_pass):
        assert item.source_url.startswith("https://") and item.checked_at.year >= 2026
    assert DATA.transport.single_ticket.eur == 2.55 and DATA.transport.single_ticket.child_eur == 1.3
    assert DATA.transport.tourist_pass.eur_by_days == {1: 30.6, 2: 45.4, 3: 63.8, 5: 78.0}
    assert DATA.transport.tourist_pass.child_eur_by_days == {1: 15.3, 2: 22.7, 3: 31.9, 5: 39.0}   # 儿童半价
    assert "没有区域 1–3" in DATA.transport.tourist_pass.zones_note                                  # 官方只有全区一种


def test_meal_reference_values_are_labelled_as_estimates_not_facts():
    raw = json.loads(open(B._DATA_DIR / "paris.json", encoding="utf-8").read())
    assert raw["meals"]["kind"] == "estimate"
    assert DATA.meals.eur_per_day["low"] < DATA.meals.eur_per_day["medium"] < DATA.meals.eur_per_day["high"]
    with pytest.raises(ValidationError, match="estimate"):
        BudgetData.model_validate({**raw, "meals": {**raw["meals"], "kind": "fact"}})
    no_src = json.loads(json.dumps(raw))
    del no_src["transport"]["single_ticket"]["source_url"]
    with pytest.raises(ValidationError):
        BudgetData.model_validate(no_src)


def test_every_curated_ticket_price_has_an_official_source_and_a_check_date():
    priced = [p for p in LIB.places.values() if p.price_eur is not None]
    assert len(priced) >= 25
    for p in priced:
        assert p.price_source_url and p.price_source_url.startswith("https://"), p.id
        assert p.price_checked_at and p.price_checked_at.year >= 2026, p.id
    assert LIB.places["louvre"].price_eur == 32 and LIB.places["eiffel-tower"].price_eur > 30


def test_every_concession_rule_has_an_official_source_and_a_check_date():
    with_rules = [p for p in LIB.places.values() if p.concessions]
    assert len(with_rules) >= 12                                                   # 最常去的景点都核对过
    for p in with_rules:
        c = p.concessions
        assert c.source_url.startswith("https://") and c.checked_at.year >= 2026, p.id
        assert p.price_eur is not None, p.id


def test_a_price_without_a_source_is_rejected_and_open_access_places_carry_no_price():
    base = dict(id="x-place", name_zh="测试点", name_en="Test", category="museum", setting="indoor", area="1区",
                lat=48.86, lng=2.34, duration_min=60)
    with pytest.raises(ValidationError, match="price_source_url"):
        Place.model_validate({**base, "price_eur": 10})
    ok = {**base, "price_eur": 10, "price_source_url": "https://example.org", "price_checked_at": "2026-10-14"}
    Place.model_validate(ok)
    with pytest.raises(ValidationError, match="open_access"):
        Place.model_validate({**ok, "open_access": True})
    Place.model_validate({**base, "open_access": True})
    with pytest.raises(ValidationError, match="price_eur"):
        Place.model_validate({**base, "price_periods": [{"months": [1], "eur": 5}]})       # 有分月份价就要有回退价
    with pytest.raises(ValidationError, match="月份"):
        Place.model_validate({**ok, "price_periods": [{"months": [1, 2], "eur": 5}, {"months": [2], "eur": 6}]})
    with pytest.raises(ValidationError):
        Place.model_validate({**ok, "concessions": {"checked_at": "2026-10-14", "bands": []}})   # 优惠规则缺来源


# --------------------------------------------------------------------------- #
# 出行人
# --------------------------------------------------------------------------- #
def make_request_obj(**over) -> TravelRequest:
    body = {"destination": "Paris", "start_date": "2026-10-20", "end_date": "2026-10-23", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["art"], "arrival_time": "2026-10-20T10:30:00",
            "departure_time": "2026-10-23T18:00:00"}
    body.update(over)
    return TravelRequest.model_validate(body)


def test_the_request_defaults_to_one_adult_who_does_not_live_in_europe():
    req = make_request_obj()
    assert req.travelers == [Traveler(age=30, residence=Residence.none, student=False)]
    assert req.budget_cap is None and req.currency.value == "CNY"


@pytest.mark.parametrize("bad", [{"travelers": []}, {"travelers": [{"age": -1}]}, {"travelers": [{"age": 130}]},
                                 {"travelers": [{"age": 20, "residence": "mars"}]}, {"budget_cap": -5},
                                 {"currency": "GBP"}, {"travelers": [{"age": 30}] * 21}])
def test_invalid_party_cap_or_currency_is_rejected(bad):
    with pytest.raises(ValidationError):
        make_request_obj(**bad)


# --------------------------------------------------------------------------- #
# 逐人、按日期取最低适用价
# --------------------------------------------------------------------------- #
OCT, NOV, JUL, DEC, APR = (date(2026, m, 20) for m in (10, 11, 7, 12, 4))


def eur(place: str, visit: date, t: Traveler) -> float:
    return ticket_price(LIB.places[place], visit, t).eur


def test_the_louvre_charges_non_eea_visitors_32_and_residents_22_with_free_entry_under_18_and_for_resident_youth():
    assert eur("louvre", OCT, T(30)) == 32
    assert eur("louvre", OCT, T(30, "eu")) == 22 and eur("louvre", OCT, T(30, "fr")) == 22
    assert eur("louvre", OCT, T(17)) == 0                                               # 18 岁以下所有人免费
    assert eur("louvre", OCT, T(20)) == 32                                              # 18–25 只对欧盟居民免费
    assert eur("louvre", OCT, T(20, "eu")) == 0 and eur("louvre", OCT, T(25, "fr")) == 0
    assert eur("louvre", OCT, T(26, "eu")) == 22
    assert ticket_price(LIB.places["louvre"], OCT, T(17)).label == "免费（17 岁）"


def test_french_residence_and_other_eu_residence_are_different_where_the_official_site_says_so():
    # 圣礼拜堂：欧盟居民价 €16；18–25 岁免费只给「欧盟公民和长居法国的非欧洲居民」
    assert eur("sainte-chapelle", OCT, T(30)) == 22
    assert eur("sainte-chapelle", OCT, T(30, "eu")) == 16
    assert eur("sainte-chapelle", OCT, T(21, "eu")) == 16  # 长居其他欧盟国家：只有欧盟居民价
    assert eur("sainte-chapelle", OCT, T(21, "fr")) == 0  # 长居法国的留学生：免费
    assert eur("conciergerie", OCT, T(21, "eu")) == 13 and eur("conciergerie", OCT, T(21, "fr")) == 0
    assert eur("conciergerie", OCT, T(10)) == 0


def test_prices_follow_the_month_the_place_is_visited():
    assert eur("versailles", OCT, T(30)) == 35 and eur("versailles", NOV, T(30)) == 25     # 旺季 4–10 月 / 淡季 11–3 月
    assert eur("versailles", OCT, T(30, "eu")) == 32 and eur("versailles", NOV, T(30, "eu")) == 22
    assert eur("versailles", OCT, T(24, "eu")) == 0 and eur("versailles", OCT, T(17)) == 0
    assert eur("arc-de-triomphe", JUL, T(30)) == 22 and eur("arc-de-triomphe", DEC, T(30)) == 16
    assert eur("pantheon", JUL, T(30)) == 16 and eur("pantheon", DEC, T(30)) == 13


def test_a_month_the_official_site_does_not_cover_falls_back_and_says_so():
    tp = ticket_price(LIB.places["pantheon"], APR, T(30))
    assert tp.eur == 13 and tp.season_fallback is True                                    # 4–5 月官网没写
    assert ticket_price(LIB.places["pantheon"], DEC, T(30)).season_fallback is False
    assert ticket_price(LIB.places["louvre"], APR, T(30)).season_fallback is False        # 没有分月份价的地点不算回退


def test_children_and_youth_bands_and_student_prices():
    assert [eur("eiffel-tower", OCT, T(a)) for a in (2, 8, 20, 30)] == [0, 9.2, 18.4, 36.7]
    assert [eur("fondation-louis-vuitton", OCT, T(a)) for a in (10, 22, 40)] == [5, 10, 18]
    assert eur("fondation-louis-vuitton", OCT, T(40, student=True)) == 10
    assert eur("palais-de-tokyo", OCT, T(35, student=True)) == 9 and eur("palais-de-tokyo", OCT, T(35)) == 13
    assert [eur("palais-garnier", OCT, T(a)) for a in (10, 15, 30)] == [0, 20, 25]
    assert eur("palais-garnier", OCT, T(15, "eu")) == 10 and eur("palais-garnier", OCT, T(30, "eu")) == 15
    assert eur("orsay", OCT, T(17)) == 0 and eur("orsay", OCT, T(20, "eu")) == 0 and eur("orsay", OCT, T(20)) == 16


def test_a_place_without_checked_rules_charges_everyone_the_adult_price():
    rodin = LIB.places["rodin"]
    assert rodin.price_eur == 14 and rodin.concessions is None
    for t in (T(5), T(20, "fr", True), T(70, "eu")):
        assert ticket_price(rodin, OCT, t).eur == 14


def test_eligibility_follows_the_official_wording():
    from app.budget import eligible
    assert eligible("any", "none") and not eligible("resident_eu", "none") and not eligible("resident_fr", "none")
    assert eligible("resident_eu", "fr") and eligible("resident_eu", "eu") and not eligible("resident_fr", "eu")
    assert eligible("resident_fr", "fr")


# --------------------------------------------------------------------------- #
# 交通：单程票 vs 通票，儿童票
# --------------------------------------------------------------------------- #
def test_no_transit_legs_costs_nothing():
    r = transport_cost([0, 0, 0], DATA.transport)
    assert r["eur_person"] == 0 and r["choice"] == "none"


def test_a_few_rides_stay_on_single_tickets_and_many_rides_switch_to_the_pass():
    r = transport_cost([3, 3, 3], DATA.transport)
    assert r["choice"] == "single" and r["eur_person"] == pytest.approx(22.95)
    r = transport_cost([13, 13, 13], DATA.transport)
    assert r["choice"] == "pass" and r["eur_person"] == pytest.approx(63.8) and "3 日通票" in r["detail"]


def test_the_pass_covers_the_busiest_consecutive_days_and_singles_cover_the_rest():
    r = transport_cost([2, 14, 14, 14, 2], DATA.transport)
    assert r["choice"] == "pass" and r["eur_person"] == pytest.approx(74.0)
    assert "第 2–4 天" in r["detail"] and "其余 4 段单程票" in r["detail"]
    r = transport_cost([13] * 6, DATA.transport)
    assert r["choice"] == "pass" and r["eur_person"] == pytest.approx(111.15)


def test_the_cheapest_option_is_never_more_than_all_singles():
    for legs in ([1], [5, 5], [9, 0, 9], [20] * 5, [0, 0, 4, 0], [14] * 4):
        total = sum(legs) * DATA.transport.single_ticket.eur
        assert transport_cost(legs, DATA.transport)["eur_person"] <= round(total, 2) + 1e-9


def test_children_use_the_official_child_fare_and_under_4s_ride_free():
    from app.budget import _traveler_transport
    legs = [4, 4, 4]
    assert _traveler_transport(T(3), legs, DATA.transport) == 0
    assert _traveler_transport(T(8), legs, DATA.transport) == pytest.approx(12 * 1.3)
    assert _traveler_transport(T(9), legs, DATA.transport) == pytest.approx(12 * 1.3)
    assert _traveler_transport(T(10), legs, DATA.transport) == pytest.approx(12 * 2.55)    # 10 岁起按成人
    assert _traveler_transport(T(40), legs, DATA.transport) == pytest.approx(30.6)         # 成人：单程 12 × 2.55 = 30.6


def test_a_busy_trip_makes_the_child_pass_cheaper_than_child_singles():
    from app.budget import _traveler_transport
    legs = [13, 13, 13]                                                                    # 39 段
    assert _traveler_transport(T(8), legs, DATA.transport) == pytest.approx(31.9)    # 儿童 3 日通票 < 39 × 1.3
    assert _traveler_transport(T(40), legs, DATA.transport) == pytest.approx(63.8)   # 成人 3 日通票
    assert _traveler_transport(T(3), legs, DATA.transport) == 0                      # 4 岁以下免费，不需要通票
    assert _traveler_transport(T(10), legs, DATA.transport) == pytest.approx(63.8)   # 10 岁起按成人


# --------------------------------------------------------------------------- #
# 估算（用真实生成的行程）
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(root=tmp_path / "plans")


def make_client(store, *, fx=None) -> TestClient:
    return TestClient(create_app(store=store, client_factory=DeadLLM, fx=fx))


def plan_body(*, level="medium", theme="art", start="2026-10-14", end="2026-10-17", **extra) -> dict:
    body = {"destination": "Paris", "start_date": start, "end_date": end, "budget_level": level,
            "travel_style": "balanced", "interests": [theme], "theme": theme,
            "arrival_time": f"{start} 10:30", "departure_time": f"{end} 18:00"}
    body.update(extra)
    return body


def make_plan(client, **kw) -> dict:
    r = client.post("/plans", json=plan_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def load(store, plan):
    return Itinerary.model_validate(plan["itinerary"]), store.request(plan["plan_id"])


def ids_in(it: Itinerary) -> list[str]:
    seen: list[str] = []
    for d in it.days:
        for i in d.items:
            if i.place_id in LIB.places and i.place_id not in seen:
                seen.append(i.place_id)
    return seen


def cat(b: dict, key: str) -> dict:
    return next(c for c in b["categories"] if c["key"] == key)


def settings(*party: Traveler, **kw) -> BudgetSettings:
    return BudgetSettings(travelers=tuple(party) or (T(),), **kw)


def test_tickets_are_summed_per_traveler_at_the_date_each_place_is_visited(store):
    plan = make_plan(make_client(store), theme="museums")
    it, req = load(store, plan)
    party = (T(30), T(8), T(21, "fr", True))
    b = estimate_budget(it, req, settings(*party), RATE)
    visits = B._visit_dates(it, LIB)
    expected = sum(ticket_price(LIB.places[pid], d, t).eur for pid, (d, _) in visits.items()
                   if LIB.places[pid].price_eur is not None for t in party)
    assert cat(b, "tickets")["eur_total"] == pytest.approx(expected, abs=0.02)
    solo = estimate_budget(it, req, settings(T(30)), RATE)
    assert cat(b, "tickets")["eur_total"] < 3 * cat(solo, "tickets")["eur_total"]          # 儿童 / 留学生有优惠


def test_discounts_are_itemised_per_traveler_with_what_they_saved(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    b = estimate_budget(it, req, settings(T(30), T(8), T(21, "fr")), RATE)
    assert [d["traveler"] for d in b["discounts"]] == [2, 3]                            # 成人没有优惠
    assert all(d["saved_eur"] > 0 and d["places"] for d in b["discounts"])
    assert "已按出行人身份计入优惠" in cat(b, "tickets")["detail"]
    assert estimate_budget(it, req, settings(T(30)), RATE)["discounts"] == []


def test_places_without_checked_rules_are_named_when_someone_could_get_a_discount(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    kids = estimate_budget(it, req, settings(T(30), T(8)), RATE)
    note = next(n for n in kids["notes"] if "优惠规则未核对" in n)
    unchecked = [i for i in ids_in(it) if LIB.places[i].price_eur and LIB.places[i].price_eur > 0
                 and (LIB.places[i].concessions is None or not LIB.places[i].concessions.complete)]
    assert unchecked and f"{len(unchecked)} 个景点" in note and "按成人全价" in note
    adult_only = estimate_budget(it, req, settings(T(30)), RATE)
    assert not any("优惠规则未核对" in n for n in adult_only["notes"])  # 成人非居民没有可享的优惠，不啰嗦


def test_meals_follow_the_budget_level_children_count_less_and_the_amount_can_be_overridden(store):
    client = make_client(store)
    it, req = load(store, make_plan(client, level="high"))
    day_rate = DATA.meals.eur_per_day["high"]
    b = estimate_budget(it, req, settings(T(30), T(10), T(3)), RATE)
    factor = 1 + 0.5 + 0                                                  # 成人 1 + 10 岁 0.5 + 3 岁 0
    assert cat(b, "meals")["eur_total"] == pytest.approx(day_rate * 4 * factor)
    assert cat(b, "meals")["source"] == "estimate" and "估算" in cat(b, "meals")["source_note"]
    o = estimate_budget(it, req, settings(T(30), T(10), meal_eur_per_day=40), RATE)
    assert cat(o, "meals")["eur_total"] == pytest.approx(40 * 4 * 1.5) and cat(o, "meals")["source"] == "user"
    assert o["meal"]["is_override"] and o["meal"]["default"] == day_rate


def test_transport_is_per_traveler_and_lodging_is_one_room_split_by_head_count(store):
    from app.schemas import Stay
    it, req = load(store, make_plan(make_client(store)))
    req = req.model_copy(update={"stays": [Stay.model_validate(dict(name="酒店", check_in="2026-10-14",
                                                                    check_out="2026-10-17", area="1区",
                                                                    nightly_eur=200))]})
    one = estimate_budget(it, req, settings(T(30)), RATE)
    three = estimate_budget(it, req, settings(T(30), T(30), T(2)), RATE)
    assert cat(three, "transport")["eur_total"] == pytest.approx(2 * cat(one, "transport")["eur_total"])    # 3 岁免费
    assert cat(one, "lodging")["eur_total"] == 600 == cat(three, "lodging")["eur_total"]
    assert cat(three, "lodging")["eur_person"] == pytest.approx(200)
    assert three["total"]["eur_total"] == pytest.approx(sum(c["eur_total"] for c in three["categories"]), abs=0.02)


def test_lodging_counts_priced_nights_and_flags_missing_prices_and_uncovered_nights(store):
    from app.schemas import Stay
    it, req = load(store, make_plan(make_client(store)))

    def lodging(stays):
        r = req.model_copy(update={"stays": [Stay.model_validate(s) for s in stays]})
        return cat(estimate_budget(it, r, settings(T()), RATE), "lodging")

    none = lodging([])
    assert none["eur_total"] == 0 and none["pending"] == ["3 晚还没填住宿"] and none["unknown"] is True
    priced = lodging([dict(name="A", check_in="2026-10-14", check_out="2026-10-17", area="1区", nightly_eur=150)])
    assert priced["eur_total"] == 450 and priced["pending"] == [] and priced["unknown"] is False
    mixed = lodging([dict(name="A", check_in="2026-10-14", check_out="2026-10-16", area="1区", nightly_eur=150),
                     dict(name="B", check_in="2026-10-16", check_out="2026-10-17", area="9区")])
    assert mixed["eur_total"] == 300 and mixed["pending"] == ["1 晚住宿没填价格"]


def test_squares_bridges_and_shops_are_never_listed_as_pending_tickets(store):
    it, req = load(store, make_plan(make_client(store), theme="photography"))
    b = estimate_budget(it, req, settings(T()), RATE)
    titles = {i.title: i.place_id for d in it.days for i in d.items if i.place_id}
    for t in cat(b, "tickets")["pending"]:
        place = LIB.places[titles[t]]
        assert place.price_eur is None and not place.open_access
        assert place.category.value not in ("food", "shopping")
    assert any(LIB.places[i].open_access for i in ids_in(it))


# --------------------------------------------------------------------------- #
# 结算货币、汇率缺失
# --------------------------------------------------------------------------- #
def test_the_settlement_currency_is_reported_and_both_currencies_are_always_available(store):
    it, req = load(store, make_plan(make_client(store)))
    cny = estimate_budget(it, req, settings(T(), currency="CNY"), RATE)
    eur = estimate_budget(it, req, settings(T(), currency="EUR"), RATE)
    assert cny["currency"] == "CNY" and eur["currency"] == "EUR"
    assert cny["total"] == eur["total"]                                                    # 数值相同，由前端决定谁在前
    assert cny["total"]["cny_total"] == pytest.approx(cny["total"]["eur_total"] * 7.5, abs=0.01)
    assert sum(c["share"] for c in cny["categories"]) == pytest.approx(1, abs=0.01)


def test_without_a_rate_only_euros_are_shown_and_a_yuan_cap_cannot_be_checked(store):
    it, req = load(store, make_plan(make_client(store)))
    b = estimate_budget(it, req, settings(T(), cap_amount=1000, cap_currency="CNY"), None)
    assert b["fx"] is None and b["total"]["cny_total"] is None and b["status"] == "unknown"
    assert "暂时拿不到汇率" in " ".join(b["notes"])
    eur_cap = estimate_budget(it, req, settings(T(), currency="EUR", cap_amount=50, cap_currency="EUR"), None)
    assert eur_cap["status"] == "over" and eur_cap["status_currency"] == "EUR"              # 欧元上限不需要汇率


def test_an_old_rate_is_labelled_with_its_date(store):
    it, req = load(store, make_plan(make_client(store)))
    b = estimate_budget(it, req, settings(T()), FxRate("EUR", "CNY", 7.4, "2026-09-30", stale=True))
    assert b["fx"]["stale"] is True and any("2026-09-30" in n for n in b["notes"])


# --------------------------------------------------------------------------- #
# 超预算
# --------------------------------------------------------------------------- #
def test_no_cap_means_no_status_and_no_suggestions(store):
    it, req = load(store, make_plan(make_client(store)))
    b = estimate_budget(it, req, settings(T()), RATE)
    assert b["status"] == "none" and b["suggestions"] == [] and b["cap"] is None


def test_a_cap_in_yuan_or_euro_is_compared_in_euros_and_reported_in_the_settlement_currency(store):
    it, req = load(store, make_plan(make_client(store)))
    total_eur = estimate_budget(it, req, settings(T()), RATE)["total"]["eur_total"]
    within = estimate_budget(it, req, settings(T(), cap_amount=(total_eur + 100) * 7.5, cap_currency="CNY"), RATE)
    assert within["status"] == "within" and within["status_currency"] == "CNY"
    assert within["status_amount"] == pytest.approx(750, abs=0.01)
    over = estimate_budget(it, req, settings(T(), currency="EUR", cap_amount=total_eur - 10, cap_currency="EUR"), RATE)
    assert over["status"] == "over" and over["status_amount"] == pytest.approx(10, abs=0.01)
    assert over["cap"] == {"amount": total_eur - 10, "currency": "EUR"}
    mixed = estimate_budget(it, req, settings(T(), currency="EUR", cap_amount=(total_eur + 100) * 7.5,
                                              cap_currency="CNY"), RATE)                    # 上限是人民币、结算用欧元
    assert mixed["status"] == "within" and mixed["status_currency"] == "EUR"
    assert mixed["status_amount"] == pytest.approx(100, abs=0.01)


def test_an_exceeded_cap_gives_at_most_three_cheaper_swaps_priced_for_this_party(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    party = (T(30), T(21, "fr"))
    b = estimate_budget(it, req, settings(*party, cap_amount=100, cap_currency="CNY"), RATE)
    assert b["status"] == "over"
    sug = b["suggestions"]
    assert 1 <= len(sug) <= 3
    in_trip = set(ids_in(it))
    visits = B._visit_dates(it, LIB)
    for s in sug:
        assert s["place_id"] in in_trip and s["alt_place_id"] not in in_trip
        day = visits[s["place_id"]][0]
        old = sum(ticket_price(LIB.places[s["place_id"]], day, t).eur for t in party)
        new = sum(B._price_for(LIB.places[s["alt_place_id"]], day, t) for t in party)
        assert s["saving_eur"] == pytest.approx(old - new, abs=0.01) and s["saving_eur"] > 0
        assert s["saving"] == pytest.approx(s["saving_eur"] * 7.5, abs=0.01)
    assert [s["saving_eur"] for s in sug] == sorted((s["saving_eur"] for s in sug), reverse=True)


def test_a_big_gap_is_flagged_and_an_unreachable_one_says_even_free_tickets_would_not_be_enough(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    base = estimate_budget(it, req, settings(T()), RATE)
    total = base["total"]["eur_total"]
    tickets = cat(base, "tickets")["eur_total"]
    non_ticket = total - tickets
    eur = dict(currency="EUR", cap_currency="EUR")
    near = estimate_budget(it, req, settings(T(), cap_amount=total - 5, **eur), RATE)
    assert near["status"] == "over" and near["gap"] is None                              # 只超一点：不用特别提示
    large = estimate_budget(it, req, settings(T(), cap_amount=total / 1.8, **eur), RATE)
    assert large["status"] == "over" and large["gap"]["level"] in ("large", "unreachable")
    assert large["gap"]["over_ratio"] >= 0.5 and large["gap"]["tickets"] == pytest.approx(tickets, abs=0.02)
    hopeless = estimate_budget(it, req, settings(T(), cap_amount=non_ticket - 1, **eur), RATE)
    assert hopeless["gap"]["level"] == "unreachable"
    assert hopeless["gap"]["non_ticket"] == pytest.approx(non_ticket, abs=0.02)
    within = estimate_budget(it, req, settings(T(), cap_amount=total + 50, **eur), RATE)
    assert within["status"] == "within" and within["gap"] is None
    assert estimate_budget(it, req, settings(T()), RATE)["gap"] is None                  # 没设上限


def test_locked_and_skipped_places_are_never_suggested_for_swapping(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    cap = settings(T(), cap_amount=100, cap_currency="CNY")
    first = estimate_budget(it, req, cap, RATE)["suggestions"][0]["place_id"]
    skipped = estimate_budget(it, req, cap, RATE, skip_ids=frozenset({first}))["suggestions"]
    assert first not in {s["place_id"] for s in skipped}
    locked = it.model_copy(deep=True)
    for d in locked.days:
        for i in d.items:
            if i.place_id == first:
                i.locked = True
    assert first not in {s["place_id"] for s in estimate_budget(locked, req, cap, RATE)["suggestions"]}


# --------------------------------------------------------------------------- #
# 生成时按预算调整
# --------------------------------------------------------------------------- #
def test_fitting_does_nothing_without_a_cap_or_when_already_within_budget(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    assert fit_to_budget(it, req, RATE) == (it, [])
    out, notes = fit_to_budget(it, req.model_copy(update={"budget_cap": 1_000_000}), RATE)
    assert out is it and notes == []


def test_over_budget_trips_get_their_priciest_places_swapped_and_every_swap_is_explained(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    before = estimate_budget(it, req, settings(T()), RATE)["total"]["eur_total"]
    tight = req.model_copy(update={"budget_cap": (before - 60) * 7.5})
    out, notes = fit_to_budget(it, tight, RATE)
    swaps = [n for n in notes if n.startswith("为了预算，把「")]
    assert swaps, notes
    assert all("换成了「" in n and "省 ¥" in n for n in swaps)
    after = estimate_budget(out, tight, BudgetSettings.from_request(tight), RATE)["total"]["eur_total"]
    assert after < before
    assert set(ids_in(out)) != set(ids_in(it))
    assert len(out.days) == len(it.days)                                                    # 天数不变，只换点


def test_when_the_non_ticket_costs_alone_exceed_the_cap_the_itinerary_is_left_alone_and_the_note_says_so(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    out, notes = fit_to_budget(it, req.model_copy(update={"budget_cap": 1}), RATE)
    assert out is it, "换景点救不了的预算，不该把行程改得面目全非"
    assert len(notes) == 1 and "门票以外的费用" in notes[0] and "没有改动行程" in notes[0]


def test_when_nothing_is_left_to_swap_the_note_says_how_much_is_still_over(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    b = estimate_budget(it, req, settings(T()), RATE)
    non_ticket = b["total"]["eur_total"] - cat(b, "tickets")["eur_total"]
    paid = [i for i in ids_in(it) if LIB.places[i].price_eur]
    tight = req.model_copy(update={"must_go_ids": paid, "budget_cap": (non_ticket + 20) * 7.5})   # 门票都是必去：没得换
    out, notes = fit_to_budget(it, tight, RATE)
    assert set(ids_in(out)) >= set(paid)
    assert any("已经换不动了" in n and "仍超出预算" in n for n in notes), notes


def test_swaps_prefer_a_cheaper_place_from_the_same_theme_over_a_bigger_saving_from_another_theme(store):
    from app.editing import alternatives
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    cap = settings(T(), cap_amount=1, cap_currency="EUR", currency="EUR")
    checked = 0
    for s in estimate_budget(it, req, cap, RATE)["suggestions"]:
        alts = alternatives(it, req, s["place_id"], LIB, limit=200)
        day = B._visit_dates(it, LIB)[s["place_id"]][0]
        cost = ticket_price(LIB.places[s["place_id"]], day, T()).eur
        same = [a for a in alts if not a.reason.startswith("相邻")
                and B._known_price(LIB.places[a.place_id]) is not None
                and (B._price_for(LIB.places[a.place_id], day, T()) or 0) < cost]
        chosen = next(a for a in alts if a.place_id == s["alt_place_id"])
        if same:
            checked += 1
            assert not chosen.reason.startswith("相邻"), f"{s['title']} 有同主题的更便宜备选，却推荐了相邻主题的"
    assert checked >= 1, "博物馆主题里应该有至少一个景点存在同主题的更便宜备选，否则这个测试没意义"


def test_must_go_places_are_never_swapped_away_for_the_budget(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    assert "louvre" in ids_in(it)
    out, _ = fit_to_budget(it, req.model_copy(update={"must_go_ids": ["louvre"], "budget_cap": 1}), RATE)
    assert "louvre" in ids_in(out)


def test_a_yuan_cap_without_a_rate_leaves_the_itinerary_alone_and_says_why(store):
    it, req = load(store, make_plan(make_client(store), theme="museums"))
    out, notes = fit_to_budget(it, req.model_copy(update={"budget_cap": 100}), None)
    assert out is it and any("拿不到汇率" in n for n in notes)
    out2, notes2 = fit_to_budget(it, req.model_copy(update={"budget_cap": 20, "currency": "EUR"}), None)
    assert notes2 and out2 is it                       # 欧元上限不需要汇率：照常判断（这里门票外的费用就超了）


def test_generation_with_a_cap_records_the_swaps_in_the_generation_notes(store):
    client = make_client(store, fx=FxService(store.database, FakeFrankfurter().client()))
    base = make_plan(client, theme="museums")
    it0, req0 = load(store, base)
    b0 = estimate_budget(it0, req0, settings(T()), RATE)
    cap_cny = round((b0["total"]["eur_total"] - cat(b0, "tickets")["eur_total"] / 2) * 7.489)   # 要省掉一半门票才够
    plan = make_plan(client, theme="museums", budget_cap=cap_cny, currency="CNY",
                     travelers=[{"age": 30, "residence": "none", "student": False}])
    notes = plan["itinerary"]["generation"]["notes"]
    assert any(n.startswith("为了预算") for n in notes), notes
    assert not any("为了预算" in n for n in base["itinerary"]["generation"]["notes"])


def test_generation_without_a_rate_still_succeeds_and_notes_that_the_budget_was_not_applied(store):
    client = make_client(store, fx=FxService(store.database, FakeFrankfurter(fail=500).client()))
    plan = make_plan(client, theme="museums", budget_cap=300, currency="CNY")
    assert any("拿不到汇率" in n for n in plan["itinerary"]["generation"]["notes"])


# --------------------------------------------------------------------------- #
# 汇率服务
# --------------------------------------------------------------------------- #
def test_the_rate_is_fetched_once_and_cached_for_24_hours():
    api, now = FakeFrankfurter(), [1000.0]
    svc = FxService(client=api.client(), clock=lambda: now[0])
    first = svc.get()
    assert first == FxRate("EUR", "CNY", 7.489, "2026-10-13") and svc.get() == first
    assert len(api.calls) == 1
    q = api.calls[0].url.params
    assert q["base"] == "EUR" and q["symbols"] == "CNY"
    now[0] += CACHE_TTL_SECONDS - 1
    svc.get()
    assert len(api.calls) == 1
    now[0] += 2
    svc.get()
    assert len(api.calls) == 2


def test_any_two_currencies_convert_through_the_euro():
    svc = FxService(client=FakeFrankfurter().client())
    assert svc.rate("EUR", "EUR").rate == 1 and svc.rate("CNY", "CNY").rate == 1
    assert svc.rate("EUR", "CNY").rate == pytest.approx(7.489)
    assert svc.rate("CNY", "EUR").rate == pytest.approx(1 / 7.489)
    assert svc.rate("CNY", "GBP").rate == pytest.approx(0.86 / 7.489)
    assert FxService(client=FakeFrankfurter(fail=500).client()).rate("CNY", "GBP") is None


@pytest.mark.parametrize("api", [FakeFrankfurter(fail=500), FakeFrankfurter(fail=429), FakeFrankfurter(garbage=True),
                                 FakeFrankfurter(rate=-1), FakeFrankfurter(rate=0)])
def test_a_broken_service_with_no_history_yields_none_not_an_error(api):
    assert FxService(client=api.client()).get() is None


def test_a_timeout_yields_none():
    def handler(request):
        raise httpx.ConnectTimeout("慢")
    assert FxService(client=httpx.Client(transport=httpx.MockTransport(handler))).get() is None


def test_the_last_good_rate_survives_a_restart_and_is_marked_stale_when_the_service_is_down(store):
    db = store.database
    assert FxService(db, FakeFrankfurter(rate=7.4, date="2026-10-01").client()).get().stale is False
    old = FxService(db, FakeFrankfurter(fail=503).client()).get()                            # 重启 + 接口挂了
    assert old == FxRate("EUR", "CNY", 7.4, "2026-10-01", stale=True)


def test_a_cached_rate_past_its_ttl_is_reused_as_stale_when_the_refresh_fails():
    state = {"down": False}
    api, now = FakeFrankfurter(rate=7.3, date="2026-10-02"), [0.0]

    def handler(request):
        return httpx.Response(503) if state["down"] else api(request)

    svc = FxService(client=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: now[0])
    svc.get()
    state["down"], now[0] = True, CACHE_TTL_SECONDS + 5
    again = svc.get()
    assert again.rate == 7.3 and again.stale is True and again.date == "2026-10-02"


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
def fx_client(store, **kw) -> TestClient:
    return make_client(store, fx=FxService(store.database, FakeFrankfurter(**kw).client()))


def test_the_budget_endpoint_starts_from_the_party_and_cap_given_when_the_plan_was_made(store):
    client = fx_client(store)
    party = [{"age": 30, "residence": "none", "student": False}, {"age": 8, "residence": "none", "student": False},
             {"age": 21, "residence": "fr", "student": True}]
    plan = make_plan(client, travelers=party, currency="EUR", budget_cap=5000)
    b = client.get(f"/plans/{plan['plan_id']}/budget").json()
    assert [t["age"] for t in b["travelers"]] == [30, 8, 21] and b["travelers"][2]["label"] == "21 岁 · 长居法国 · 学生"
    assert b["currency"] == "EUR" and b["cap"] == {"amount": 5000, "currency": "EUR"}
    assert b["fx"] == {"rate": 7.489, "date": "2026-10-13", "stale": False}
    assert [c["key"] for c in b["categories"]] == ["tickets", "meals", "transport", "lodging"]


def test_the_budget_endpoint_still_answers_when_the_rate_service_is_down(store):
    client = fx_client(store, fail=500)
    plan = make_plan(client)
    r = client.get(f"/plans/{plan['plan_id']}/budget")
    assert r.status_code == 200 and r.json()["fx"] is None and r.json()["total"]["eur_total"] > 0


def test_settings_save_merge_and_clear(store):
    client = fx_client(store)
    pid = make_plan(client)["plan_id"]
    assert client.get(f"/plans/{pid}/budget-settings").json() == {
        "travelers": [{"age": 30, "residence": "none", "student": False}], "currency": "CNY", "cap_amount": None,
        "cap_currency": "CNY", "meal_eur_per_day": None}
    party = [{"age": 40, "residence": "none", "student": False}, {"age": 9, "residence": "eu", "student": False}]
    r = client.put(f"/plans/{pid}/budget-settings", json={"travelers": party, "currency": "EUR", "cap_amount": 800,
                                                          "cap_currency": "EUR", "meal_eur_per_day": 45})
    assert r.status_code == 200 and r.json()["travelers"] == party and r.json()["currency"] == "EUR"
    b = client.get(f"/plans/{pid}/budget").json()
    assert (b["currency"], b["cap"], len(b["travelers"]), b["meal"]["value"]) == (
        "EUR", {"amount": 800, "currency": "EUR"}, 2, 45)
    r = client.put(f"/plans/{pid}/budget-settings", json={"currency": "CNY"})                   # 只改货币，别的不动
    assert r.json()["currency"] == "CNY" and r.json()["travelers"] == party and r.json()["cap_amount"] == 800
    r = client.put(f"/plans/{pid}/budget-settings", json={"cap_amount": None, "meal_eur_per_day": None})
    assert r.json()["cap_amount"] is None and r.json()["meal_eur_per_day"] is None and r.json()["currency"] == "CNY"


@pytest.mark.parametrize("body", [{"travelers": []}, {"travelers": [{"age": -3}]}, {"travelers": [{"age": 20}] * 21},
                                  {"travelers": None}, {"currency": "GBP"}, {"currency": None}, {"cap_amount": -1},
                                  {"meal_eur_per_day": -5}, {"meal_eur_per_day": 5000}, {"cap_amount": "lots"},
                                  {"cap_currency": None}])
def test_invalid_settings_are_refused_and_nothing_changes(store, body):
    client = fx_client(store)
    pid = make_plan(client)["plan_id"]
    assert client.put(f"/plans/{pid}/budget-settings", json=body).status_code == 422
    assert client.get(f"/plans/{pid}/budget-settings").json()["currency"] == "CNY"


def test_unknown_plans_are_404(store):
    client = make_client(store)
    assert client.get("/plans/nope/budget").status_code == 404
    assert client.get("/plans/nope/budget-settings").status_code == 404
    assert client.put("/plans/nope/budget-settings", json={"currency": "EUR"}).status_code == 404


def test_settings_do_not_create_versions_and_are_copied_with_the_plan_and_removed_with_it(store):
    client = fx_client(store)
    pid = make_plan(client)["plan_id"]
    client.put(f"/plans/{pid}/budget-settings", json={"cap_amount": 5000, "currency": "EUR", "cap_currency": "EUR",
                                                      "travelers": [{"age": 30}, {"age": 5}]})
    assert store.latest_version(pid) == 1
    copy = client.post(f"/plans/{pid}/duplicate").json()["plan_id"]
    cs = client.get(f"/plans/{copy}/budget-settings").json()
    assert cs["cap_amount"] == 5000 and cs["currency"] == "EUR" and len(cs["travelers"]) == 2
    client.put(f"/plans/{copy}/budget-settings", json={"cap_amount": 9000})
    assert client.get(f"/plans/{pid}/budget-settings").json()["cap_amount"] == 5000
    assert client.delete(f"/plans/{pid}").status_code == 204
    with store.database.connect() as con:
        assert con.execute("SELECT COUNT(*) AS n FROM budget_settings WHERE plan_id = ?", (pid,)).fetchone()["n"] == 0


def test_a_database_from_the_first_budget_version_is_upgraded_and_keeps_its_cap(tmp_path):
    import sqlite3

    from app.db import Database
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE plans (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);"
        "CREATE TABLE versions (plan_id TEXT, version INTEGER, body TEXT);"
        "CREATE TABLE budget_settings (plan_id TEXT PRIMARY KEY, cap_cny REAL, travelers INTEGER NOT NULL DEFAULT 1,"
        " meal_eur_per_day REAL, updated_at TEXT NOT NULL);"
        "INSERT INTO plans VALUES ('p1', 'x');"
        "INSERT INTO budget_settings VALUES ('p1', 8000, 3, 40, 'x');")
    con.commit()
    con.close()
    db = Database(path)
    with db.connect() as c:
        row = c.execute("SELECT party_json, currency, cap_amount, cap_currency, meal_eur_per_day FROM budget_settings "
                        "WHERE plan_id = 'p1'").fetchone()
        names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert len(json.loads(row["party_json"])) == 3 and row["currency"] == "CNY"
    assert row["cap_amount"] == 8000 and row["cap_currency"] == "CNY" and row["meal_eur_per_day"] == 40
    assert {"budget_settings", "fx_rates"} <= names


# --------------------------------------------------------------------------- #
# 导出带费用；分享页不带
# --------------------------------------------------------------------------- #
def test_exports_have_a_cost_section_in_the_settlement_currency_with_party_discounts_and_notes(store):
    client = fx_client(store)
    pid = make_plan(client, theme="museums")["plan_id"]
    client.put(f"/plans/{pid}/budget-settings", json={
        "travelers": [{"age": 30}, {"age": 8}, {"age": 21, "residence": "fr", "student": True}],
        "cap_amount": 200, "cap_currency": "CNY"})
    md = client.get(f"/plans/{pid}/itinerary.md").text
    section = md.split("## 费用估算")[1].split("\n## ")[0]
    assert "出行人：30 岁、8 岁、21 岁 · 长居法国 · 学生" in section
    for label in ("门票", "餐饮", "交通", "住宿"):
        assert f"| {label} |" in section
    assert "总计（3 人）" in section and "估算，非实时价格" in section
    assert re.search(r"¥[\d,]+ · €", section)                                                  # 人民币为主
    assert "汇率 1 欧元 = 7.489 元（欧洲央行参考汇率，2026-10-13）" in section
    assert "预算上限 ¥200：估算超出" in section and "第 2 位（8 岁）省 €" in section
    assert "优惠规则未核对" in section and "待补" in section
    client.put(f"/plans/{pid}/budget-settings", json={"currency": "EUR", "cap_amount": 100, "cap_currency": "EUR"})
    eur_md = client.get(f"/plans/{pid}/itinerary.md").text.split("## 费用估算")[1].split("\n## ")[0]
    assert re.search(r"€[\d,.]+ · ¥", eur_md) and "预算上限 €100：估算超出 €" in eur_md       # 欧元为主、人民币并列
    html = client.get(f"/plans/{pid}/itinerary.html").text
    assert "费用估算" in html and "出行人：" in html and "第 2 位" in html


def test_exports_without_a_rate_show_euros_only_and_never_fail(store):
    client = fx_client(store, fail=500)
    pid = make_plan(client)["plan_id"]
    md = client.get(f"/plans/{pid}/itinerary.md")
    section = md.text.split("## 费用估算")[1].split("\n## ")[0]
    assert md.status_code == 200 and "¥" not in section and "€" in section
    assert client.get(f"/plans/{pid}/itinerary.html").status_code == 200


def test_the_pdf_still_renders_with_the_cost_section(store):
    client = fx_client(store)
    pid = make_plan(client)["plan_id"]
    r = client.get(f"/plans/{pid}/itinerary.pdf")
    if r.status_code == 503:
        pytest.skip("本机没有可用的 PDF 浏览器")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_the_public_share_page_never_carries_budget_party_or_cap(store):
    client = fx_client(store)
    pid = make_plan(client, travelers=[{"age": 31, "residence": "fr"}, {"age": 7}], budget_cap=12345)["plan_id"]
    token = client.post(f"/plans/{pid}/share").json()["token"]
    body = client.get(f"/share/{token}")
    assert body.status_code == 200
    assert "budget" not in body.json() and "cap_amount" not in body.text and "12345" not in body.text
    assert "travelers" not in body.text and "residence" not in body.text
