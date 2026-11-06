"""随行清单：行李清单生成规则、待办紧急度与推荐、每日笔记、接口、导出、复制 / 删除 / 平移日期（规格 M10 S2–S4）。

不联网：天气用假的 Open-Meteo 响应，汇率桩由 conftest 提供。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import booking
from app import companion as C
from app.api import create_app
from app.companion import (
    PackingData,
    climate_class,
    climate_for,
    day_note_ref,
    generate_packing,
    load_packing,
    note_ref,
    suggest_todos,
    todo_summary,
    todo_urgency,
)
from app.llm import LLMError
from app.schemas import Itinerary, Residence, Traveler
from app.versions import PlanStore
from app.weather import WeatherService

DATA = load_packing("paris")


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


def T(age=30, residence="none", student=False) -> Traveler:
    return Traveler(age=age, residence=Residence(residence), student=student)


# --------------------------------------------------------------------------- #
# 模板数据
# --------------------------------------------------------------------------- #
def test_every_fact_in_the_template_has_an_official_source_and_a_check_date():
    assert set(DATA.facts) == {"passport", "plug", "visa"}
    for f in DATA.facts.values():
        assert f.source_url.startswith("https://") and f.checked_at.year >= 2026
    assert "3 个月" in DATA.facts["passport"].note and "10 年" in DATA.facts["passport"].note
    assert "230V" in DATA.facts["plug"].note and "C / E" in DATA.facts["plug"].note
    tied = {t.key: t.fact for t in DATA.items if t.fact}
    assert tied == {"passport": "passport", "plug-adapter": "plug", "visa": "visa"}  # 有事实的条目都挂着来源


def test_a_template_item_pointing_at_an_unregistered_fact_is_rejected():
    raw = json.loads(open(C._DATA_DIR / "packing.json", encoding="utf-8").read())
    raw["items"][0]["fact"] = "made-up"
    with pytest.raises(ValidationError, match="事实必须登记来源"):
        PackingData.model_validate(raw)
    raw = json.loads(open(C._DATA_DIR / "packing.json", encoding="utf-8").read())
    raw["items"].append(dict(raw["items"][0]))
    with pytest.raises(ValidationError, match="重复"):
        PackingData.model_validate(raw)
    del json.loads(open(C._DATA_DIR / "packing.json", encoding="utf-8").read())["facts"]["passport"]["source_url"]
    raw = json.loads(open(C._DATA_DIR / "packing.json", encoding="utf-8").read())
    del raw["facts"]["passport"]["source_url"]
    with pytest.raises(ValidationError):
        PackingData.model_validate(raw)


# --------------------------------------------------------------------------- #
# 气候
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("high,expected", [(5, "cold"), (9.9, "cold"), (10, "cool"), (16.9, "cool"), (17, "mild"),
                                           (23.9, "mild"), (24, "warm"), (35, "warm")])
def test_climate_classes(high, expected):
    assert climate_class(high) == expected


def test_a_forecast_beats_the_monthly_estimate_and_history_is_ignored():
    dates = [date(2026, 7, 10)]                                                       # 7 月常年很热
    cold_forecast = [{"source": "forecast", "t_max": 6.0}, {"source": "forecast", "t_max": 8.0}]
    cls, basis = climate_for(dates, cold_forecast, DATA)
    assert cls == "cold" and "天气预报" in basis
    history_only = [{"source": "history", "t_max": 5.0}]
    cls, basis = climate_for(dates, history_only, DATA)
    assert cls == "warm" and "常年平均最高气温" in basis                                  # 去年同期不算预报
    assert climate_for([date(2026, 12, 3)], None, DATA)[0] == "cold"
    assert climate_for([date(2026, 10, 3)], None, DATA)[0] == "cool"


# --------------------------------------------------------------------------- #
# 用真实生成的行程测清单规则
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(root=tmp_path / "plans")


def plan_body(*, theme="art", start="2026-10-14", end="2026-10-17", **extra) -> dict:
    body = {"destination": "Paris", "start_date": start, "end_date": end, "budget_level": "medium",
            "travel_style": "balanced", "interests": [theme], "theme": theme,
            "arrival_time": f"{start} 10:30", "departure_time": f"{end} 18:00"}
    body.update(extra)
    return body


def make_client(store, **kw) -> TestClient:
    return TestClient(create_app(store=store, client_factory=DeadLLM, **kw))


def make_plan(client, **kw) -> dict:
    r = client.post("/plans", json=plan_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def load(store, plan):
    return Itinerary.model_validate(plan["itinerary"]), store.request(plan["plan_id"])


def texts(res: dict) -> list[str]:
    return [i["text"] for i in res["items"]]


def keys(res: dict) -> set[str]:
    return {i["key"] for i in res["items"]}


def gen(store, plan, party=None, weather=None) -> dict:
    it, req = load(store, plan)
    return generate_packing(it, req, party or [T()], weather)


def test_an_october_trip_for_one_adult_has_the_documents_cool_weather_clothes_and_a_plug_adapter(store):
    res = gen(store, make_plan(make_client(store)))
    k = keys(res)
    assert {"passport", "visa", "tickets", "insurance", "cards", "plug-adapter", "rain-gear", "walking-shoes",
            "sweater", "light-jacket", "common-meds", "toiletries"} <= k
    assert "heavy-coat" not in k and "scarf-gloves" not in k and "sun-gear" not in k  # 10 月：偏凉，不用羽绒服 / 遮阳帽
    assert res["climate"] == "cool" and "常年平均最高气温" in res["basis"]
    cats = [i["category"] for i in res["items"]]
    assert cats == sorted(cats, key=C.CATEGORY_ORDER.index)                                 # 按分类排好
    assert "儿童" not in cats


def test_december_is_cold_and_july_is_warm(store):
    client = make_client(store)
    winter = gen(store, make_plan(client, start="2026-12-10", end="2026-12-13"))
    assert winter["climate"] == "cold" and {"heavy-coat", "scarf-gloves", "sweater"} <= keys(winter)
    assert not {"sun-gear", "sunscreen", "light-jacket"} & keys(winter)
    summer = gen(store, make_plan(client, start="2026-07-10", end="2026-07-13"))
    assert summer["climate"] == "warm" and {"sun-gear", "sunscreen"} <= keys(summer)
    assert not {"heavy-coat", "sweater", "scarf-gloves", "light-jacket"} & keys(summer)


def test_quantities_follow_the_number_of_nights_with_caps(store):
    client = make_client(store)

    def underwear(start, end):
        item = next(i for i in gen(store, make_plan(client, start=start, end=end))["items"] if i["key"] == "underwear")
        return item["text"]

    assert "× 3（行程 2 天" in underwear("2026-10-14", "2026-10-15")                        # 1 晚：最少 3 件
    assert "× 4（行程 4 天" in underwear("2026-10-14", "2026-10-17")                        # 3 晚 + 1
    assert "× 7（行程 10 天" in underwear("2026-10-14", "2026-10-23")                       # 封顶 7 件：中途洗


def test_children_get_kids_items_and_toddlers_get_baby_supplies(store):
    plan = make_plan(make_client(store))
    assert not {k for k in keys(gen(store, plan)) if k.startswith(("kids", "baby"))}
    kids = keys(gen(store, plan, [T(), T(8)]))
    assert {"kids-meds", "kids-snacks", "kids-toys", "kids-contact"} <= kids and "baby-supplies" not in kids
    assert "baby-supplies" in keys(gen(store, plan, [T(), T(2)]))
    assert "kids-meds" not in keys(gen(store, plan, [T(), T(13)]))                         # 13 岁不算儿童


def test_travelers_who_all_live_in_europe_get_a_residence_permit_instead_of_visa_and_plug_items(store):
    plan = make_plan(make_client(store))
    res = keys(gen(store, plan, [T(25, "fr", True), T(30, "eu")]))
    assert "residence-permit" in res and not {"passport", "visa", "plug-adapter"} & res
    mixed = keys(gen(store, plan, [T(25, "fr"), T(30)]))                                    # 有一个人不是长居：都要
    assert {"passport", "visa", "plug-adapter"} <= mixed and "residence-permit" not in mixed


def test_proof_of_age_or_residence_is_only_listed_when_someone_could_use_a_discount(store):
    plan = make_plan(make_client(store))
    assert "discount-proof" not in keys(gen(store, plan, [T(30)]))
    assert "discount-proof" in keys(gen(store, plan, [T(30), T(8)]))
    assert "discount-proof" in keys(gen(store, plan, [T(40, "fr")]))
    assert "discount-proof" in keys(gen(store, plan, [T(40, student=True)]))


def test_itinerary_content_adds_smart_clothes_and_camera_gear(store):
    client = make_client(store)
    ent = gen(store, make_plan(client, theme="entertainment"))
    assert "smart-outfit" in keys(ent)
    # 轻松没有晚上，不会排到歌剧院之类的晚场
    assert "smart-outfit" not in keys(gen(store, make_plan(client, theme="museums", travel_style="relaxed")))
    assert "camera" in keys(gen(store, make_plan(client, theme="photography")))
    assert "camera" not in keys(gen(store, make_plan(client, theme="art")))


def test_a_rainy_forecast_is_mentioned_on_the_rain_gear(store):
    plan = make_plan(make_client(store))
    weather = [{"source": "forecast", "t_max": 15, "rainy": False}, {"source": "forecast", "t_max": 14, "rainy": True},
               {"source": "forecast", "t_max": 15, "rainy": False}, {"source": "forecast", "t_max": 15, "rainy": True}]
    item = next(i for i in gen(store, plan, weather=weather)["items"] if i["key"] == "rain-gear")
    assert "预报第 2、4 天有雨" in item["text"]
    plain = next(i for i in gen(store, plan)["items"] if i["key"] == "rain-gear")
    assert "预报" not in plain["text"]


# --------------------------------------------------------------------------- #
# 待办：紧急度与推荐
# --------------------------------------------------------------------------- #
NOW = date(2026, 10, 14)


@pytest.mark.parametrize("due,done,level,days", [
    (None, False, "none", None), (date(2026, 10, 1), False, "overdue", -13), (date(2026, 10, 13), False, "overdue", -1),
    (NOW, False, "today", 0), (date(2026, 10, 15), False, "soon", 1), (date(2026, 10, 17), False, "soon", 3),
    (date(2026, 10, 18), False, "ok", 4), (date(2026, 10, 1), True, "done", None)])
def test_todo_urgency_levels(due, done, level, days):
    assert todo_urgency(due, done, NOW) == {"level": level, "days": days}


def test_the_todo_summary_counts_open_urgent_and_overdue_items():
    items = [{"urgency": todo_urgency(d, dn, NOW)} for d, dn in
             [(date(2026, 10, 1), False), (NOW, False), (date(2026, 10, 30), False), (None, False),
              (date(2026, 10, 1), True)]]
    assert todo_summary(items) == {"open": 4, "done": 1, "urgent": 2, "overdue": 1}


def test_suggested_todos_count_back_from_the_departure_date_and_say_they_are_only_suggestions():
    sug = {s["key"]: s for s in suggest_todos(date(2026, 12, 20), NOW, [], set())}
    assert sug["sug-flights"]["due"] == "2026-10-21" and sug["sug-insurance"]["due"] == "2026-12-06"
    assert sug["sug-checkin"]["due"] == "2026-12-19"
    assert "不是官方规定" in sug["sug-flights"]["why"]
    assert "france-visas.gouv.fr" in sug["sug-visa"]["text"]
    assert len(sug) == len(C.SUGGESTED)


def test_suggestions_already_in_the_past_are_clamped_to_today_and_marked_urgent():
    sug = {s["key"]: s for s in suggest_todos(date(2026, 10, 20), NOW, [], set())}
    assert sug["sug-flights"]["due"] == "2026-10-14" and "已临近，尽快" in sug["sug-flights"]["why"]
    assert sug["sug-checkin"]["due"] == "2026-10-19" and "已临近" not in sug["sug-checkin"]["why"]


def test_suggestions_skip_what_was_already_added():
    sug = suggest_todos(date(2026, 12, 20), NOW, [], {"sug-flights", "sug-hotel"})
    assert "sug-flights" not in {s["key"] for s in sug} and len(sug) == len(C.SUGGESTED) - 2


def test_ticket_deadlines_from_the_booking_checklist_become_suggested_todos():
    rows = [
        {"place_id": "louvre", "title": "卢浮宫", "book_by": "2026-11-01", "lead_days": 14, "status": "todo",
         "urgency": "ok", "in_itinerary": True},
        {"place_id": "orsay", "title": "奥赛博物馆", "book_by": "2026-11-02", "lead_days": 7, "status": "booked",
         "urgency": "booked", "in_itinerary": True},                                          # 已订：不再提醒
        {"place_id": "pantheon", "title": "先贤祠", "book_by": None, "lead_days": None, "status": "todo",
         "urgency": "none", "in_itinerary": True},                                            # 没有明确天数：不编日期
        {"place_id": "rodin", "title": "罗丹", "book_by": "2026-10-01", "lead_days": 7, "status": "todo",
         "urgency": "past", "in_itinerary": True},                                            # 参观日已过
        {"place_id": "gone", "title": "已不在行程", "book_by": "2026-11-01", "lead_days": 7, "status": "todo",
         "urgency": "ok", "in_itinerary": False},
    ]
    sug = {s["key"]: s for s in suggest_todos(date(2026, 12, 20), NOW, rows, set())}
    assert sug["book-louvre"]["text"] == "订「卢浮宫」门票" and sug["book-louvre"]["due"] == "2026-11-01"
    assert "建议最晚订票日" in sug["book-louvre"]["why"] and "提前 14 天" in sug["book-louvre"]["why"]
    assert not {"book-orsay", "book-pantheon", "book-rodin", "book-gone"} & set(sug)


# --------------------------------------------------------------------------- #
# 接口：行李
# --------------------------------------------------------------------------- #
def test_generate_fills_the_list_with_facts_carrying_their_sources(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    empty = client.get(f"/plans/{pid}/packing").json()
    assert empty["items"] == [] and empty["generated"] is False and empty["total"] == 0
    r = client.post(f"/plans/{pid}/packing/generate")
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == body["total"] > 20 and body["generated"] is True and body["packed"] == 0
    assert "常年平均最高气温" in body["basis"] and body["climate"] == "cool"
    assert body["categories"][:3] == ["证件", "衣物", "电子"]
    assert {"passport", "plug-adapter"} <= {i["key"] for i in body["items"]}
    sourced = {i["key"]: i for i in body["items"] if "source_url" in i}
    assert set(sourced) == {"visa"}  # 护照 / 插头只是提醒；只有官方规定（签证）给来源
    assert sourced["visa"]["source_url"] == "https://france-visas.gouv.fr/" and sourced["visa"]["checked_at"] >= "2026"
    assert client.get(f"/plans/{pid}/packing").json()["items"] == body["items"]


def test_only_items_flagged_as_official_rules_show_a_source(store):
    assert [t.key for t in DATA.items if t.show_source] == ["visa"]          # 现在只有入境签证这一条是官方规定
    raw = json.loads(open(C._DATA_DIR / "packing.json", encoding="utf-8").read())
    for item in raw["items"]:
        if item["key"] == "passport":
            item["show_source"] = True
    flagged = PackingData.model_validate(raw)
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    it, req = store.load(pid), store.request(pid)
    items = {i["key"]: i for i in generate_packing(it, req, req.travelers, None, data=flagged)["items"]}
    assert items["passport"]["source_url"].startswith("https://france-visas.gouv.fr")
    assert "visa" in items and "source_url" in items["visa"]
    assert items["passport"]["checked_at"] >= "2026" and "3 个月" in items["passport"]["source_note"]
    assert "source_url" not in items["plug-adapter"]                          # 没标记的照旧不显示


def test_checking_renaming_adding_and_deleting_items_and_the_progress(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    items = client.post(f"/plans/{pid}/packing/generate").json()["items"]
    first, second = items[0], items[1]
    r = client.patch(f"/plans/{pid}/packing/{first['id']}", json={"packed": True}).json()
    assert r["packed"] == 1 and next(i for i in r["items"] if i["id"] == first["id"])["packed"] is True
    r = client.patch(f"/plans/{pid}/packing/{second['id']}", json={"text": "护照 + 复印件", "category": "证件"}).json()
    assert next(i for i in r["items"] if i["id"] == second["id"])["text"] == "护照 + 复印件"
    r = client.post(f"/plans/{pid}/packing", json={"category": "其他", "text": "  给朋友的礼物  "})
    assert r.status_code == 201
    added = next(i for i in r.json()["items"] if i["text"] == "给朋友的礼物")
    assert added["key"] is None and r.json()["total"] == len(items) + 1
    r = client.post(f"/plans/{pid}/packing", json={"category": "给孩子", "text": "自定义分类"}).json()
    assert "给孩子" in r["categories"]                                                       # 自定义分类排在最后
    r = client.delete(f"/plans/{pid}/packing/{added['id']}").json()
    assert all(i["text"] != "给朋友的礼物" for i in r["items"])                              # 自己加的：真删
    with store.database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM packing_items WHERE text = '给朋友的礼物'").fetchone()[0] == 0


def test_regenerating_adds_only_what_is_missing_and_never_overwrites_or_resurrects_deleted_items(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    items = client.post(f"/plans/{pid}/packing/generate").json()["items"]
    keep, drop = items[0], items[3]
    client.patch(f"/plans/{pid}/packing/{keep['id']}", json={"packed": True, "text": "我改过的名字"})
    client.post(f"/plans/{pid}/packing", json={"category": "其他", "text": "自己加的"})
    after_delete = client.delete(f"/plans/{pid}/packing/{drop['id']}").json()
    assert drop["key"] not in keys(after_delete) and after_delete["hidden"] == 1
    again = client.post(f"/plans/{pid}/packing/generate").json()
    assert again["added"] == 0                                                              # 没有新推荐项
    kept = next(i for i in again["items"] if i["id"] == keep["id"])
    assert kept["packed"] is True and kept["text"] == "我改过的名字"                          # 勾过的、改过的不动
    assert any(i["text"] == "自己加的" for i in again["items"])
    assert drop["key"] not in keys(again)                                                   # 删掉的不会回来
    restored = client.post(f"/plans/{pid}/packing/restore").json()
    assert restored["restored"] == 1 and drop["key"] in keys(restored) and restored["hidden"] == 0


def test_a_longer_trip_adds_new_recommendations_without_touching_the_old_ones(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    before = client.post(f"/plans/{pid}/packing/generate").json()
    client.put(f"/plans/{pid}/budget-settings", json={"travelers": [{"age": 30}, {"age": 5}]})
    after = client.post(f"/plans/{pid}/packing/generate").json()
    assert after["added"] == 5 and after["total"] == before["total"] + 5  # 4 条儿童用品 + 证明年龄的证件（儿童享优惠）
    assert {"kids-meds", "kids-snacks", "kids-toys", "kids-contact", "discount-proof"} <= keys(after)


def test_packing_validation_and_unknown_items(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    bad = ({"category": "", "text": "x"}, {"category": "其他", "text": ""},
           {"category": "其他", "text": "字" * 121}, {"category": "字" * 21, "text": "x"}, {"text": "x"})
    for body in bad:
        assert client.post(f"/plans/{pid}/packing", json=body).status_code == 422
    assert client.patch(f"/plans/{pid}/packing/9999", json={"packed": True}).status_code == 404
    assert client.delete(f"/plans/{pid}/packing/9999").status_code == 404
    assert client.patch(f"/plans/{pid}/packing/1", json={"text": ""}).status_code == 422
    for path in ("packing", "todos", "notes"):
        assert client.get(f"/plans/nope/{path}").status_code == 404


def test_the_list_uses_the_weather_forecast_when_there_is_one(store):
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        start, end = date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"])
        n = (end - start).days + 1
        days = [(start + timedelta(days=k)).isoformat() for k in range(n)]
        return httpx.Response(200, json={"daily": {
            "time": days, "weather_code": [63] * n, "temperature_2m_max": [6.0] * n, "temperature_2m_min": [2.0] * n,
            "precipitation_sum": [4.0] * n, "precipitation_probability_max": [90] * n}})

    svc = WeatherService(httpx.Client(transport=httpx.MockTransport(handler)))
    client = make_client(store, weather=svc, today=lambda: date(2026, 10, 11))
    pid = make_plan(client)["plan_id"]
    body = client.post(f"/plans/{pid}/packing/generate").json()
    assert body["climate"] == "cold" and "天气预报" in body["basis"]  # 10 月本该偏凉，预报很冷就听预报的
    assert "heavy-coat" in keys(body)
    assert "预报第 1、2、3、4 天有雨" in next(i for i in body["items"] if i["key"] == "rain-gear")["text"]


# --------------------------------------------------------------------------- #
# 接口：待办
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(booking, "today", lambda: NOW)


def test_todos_add_edit_complete_delete_with_urgency_and_the_summary(store):
    client = make_client(store)
    pid = make_plan(client, start="2026-12-20", end="2026-12-23")["plan_id"]
    r = client.post(f"/plans/{pid}/todos", json={"text": "办申根签证", "due": "2026-10-16"})
    assert r.status_code == 201
    body = r.json()
    todo = body["items"][0]
    assert todo["text"] == "办申根签证" and todo["due"] == "2026-10-16" and todo["done"] is False
    assert todo["urgency"] == {"level": "soon", "days": 2}                                  # 2 天内：红色提醒
    client.post(f"/plans/{pid}/todos", json={"text": "太晚了", "due": "2026-10-10"})
    client.post(f"/plans/{pid}/todos", json={"text": "不急", "due": "2026-12-01"})
    client.post(f"/plans/{pid}/todos", json={"text": "没截止日"})
    body = client.get(f"/plans/{pid}/todos").json()
    levels = {t["text"]: t["urgency"]["level"] for t in body["items"]}
    assert levels == {"办申根签证": "soon", "太晚了": "overdue", "不急": "ok", "没截止日": "none"}
    assert body["summary"] == {"open": 4, "done": 0, "urgent": 2, "overdue": 1}
    assert [t["text"] for t in body["items"]][:2] == ["太晚了", "办申根签证"]  # 按截止日排，没截止日的在后
    done = client.patch(f"/plans/{pid}/todos/{todo['id']}", json={"done": True}).json()
    assert next(t for t in done["items"] if t["id"] == todo["id"])["urgency"] == {"level": "done", "days": None}
    assert done["summary"]["urgent"] == 1 and done["items"][-1]["id"] == todo["id"]          # 完成的沉到最后
    renamed = client.patch(f"/plans/{pid}/todos/{todo['id']}", json={"text": "办签证", "due": "2026-11-20"}).json()
    assert next(t for t in renamed["items"] if t["id"] == todo["id"])["text"] == "办签证"
    cleared = client.patch(f"/plans/{pid}/todos/{todo['id']}", json={"due": None}).json()
    assert next(t for t in cleared["items"] if t["id"] == todo["id"])["due"] is None      # 传 null = 清掉截止日
    gone = client.delete(f"/plans/{pid}/todos/{todo['id']}").json()
    assert all(t["id"] != todo["id"] for t in gone["items"])


def test_todo_validation_and_unknown_ids(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    for body in ({"text": ""}, {"text": "字" * 121}, {"text": "x", "due": "明天"}, {}):
        assert client.post(f"/plans/{pid}/todos", json=body).status_code == 422
    assert client.patch(f"/plans/{pid}/todos/9999", json={"done": True}).status_code == 404
    assert client.delete(f"/plans/{pid}/todos/9999").status_code == 404


def test_accepting_suggested_todos_adds_them_once_and_removes_them_from_the_suggestions(store):
    client = make_client(store)
    pid = make_plan(client, start="2026-12-20", end="2026-12-23")["plan_id"]
    body = client.get(f"/plans/{pid}/todos").json()
    sug = {s["key"]: s for s in body["suggestions"]}
    assert len(sug) >= len(C.SUGGESTED) and "sug-flights" in sug
    flights = sug["sug-flights"]
    client.post(f"/plans/{pid}/todos", json={"text": flights["text"], "due": flights["due"], "key": flights["key"]})
    again = client.post(f"/plans/{pid}/todos", json={"text": flights["text"], "due": flights["due"],
                                                     "key": flights["key"]}).json()           # 重复点：不重复加
    assert [t["key"] for t in again["items"]].count("sug-flights") == 1
    assert "sug-flights" not in {s["key"] for s in again["suggestions"]}
    assert next(t for t in again["items"] if t["key"] == "sug-flights")["due"] == flights["due"]


def test_suggested_todos_include_the_real_ticket_deadlines_of_this_trip(store):
    client = make_client(store)
    pid = make_plan(client, theme="museums", start="2026-12-20", end="2026-12-23")["plan_id"]
    rows = client.get(f"/plans/{pid}/reservations").json()["items"]
    due = [r for r in rows if r["book_by"] and r["status"] == "todo" and r["urgency"] not in ("past", "booked")]
    assert due, "博物馆主题里应该有带提前订票天数的景点（卢浮宫等）"
    sug = {s["key"]: s for s in client.get(f"/plans/{pid}/todos").json()["suggestions"]}
    for r in due:
        s = sug[f"book-{r['place_id']}"]
        assert s["text"] == f"订「{r['title']}」门票" and s["due"] in (r["book_by"], NOW.isoformat())


# --------------------------------------------------------------------------- #
# 接口：行程备注（每一条安排各自的备注）
# --------------------------------------------------------------------------- #
def ref_at(store, pid, day_index, nth=0) -> str:
    d = store.load(pid).days[day_index]
    return note_ref(d.date, d.items[nth])


def put(client, pid, ref, text):
    return client.put(f"/plans/{pid}/notes", json={"ref": ref, "text": text})


def test_a_note_belongs_to_one_item_cleared_by_emptying_and_limited_in_length(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    assert client.get(f"/plans/{pid}/notes").json() == {"notes": {}}
    busy = next(i for i, d in enumerate(store.load(pid).days) if len(d.items) >= 2)       # 凡尔赛那天只有一个点
    a, b = ref_at(store, pid, busy, 0), ref_at(store, pid, busy, 1)
    assert a != b
    r = put(client, pid, a, "  早点到，带上学生证  ")
    assert r.status_code == 200 and r.json()["notes"] == {a: "早点到，带上学生证"}
    put(client, pid, b, "旁边有家面包店")
    assert client.get(f"/plans/{pid}/notes").json()["notes"] == {a: "早点到，带上学生证", b: "旁边有家面包店"}
    put(client, pid, a, "改过")
    assert client.get(f"/plans/{pid}/notes").json()["notes"][a] == "改过"
    put(client, pid, b, "   ")                                                               # 清空 = 删除
    assert list(client.get(f"/plans/{pid}/notes").json()["notes"]) == [a]
    assert put(client, pid, a, "字" * 2000).status_code == 200
    assert put(client, pid, a, "字" * 2001).status_code == 422
    assert put(client, pid, "", "x").status_code == 422
    assert put(client, pid, "not-in-this-trip", "x").status_code == 422
    assert put(client, pid, "2026-11-30|不在行程里", "x").status_code == 422


def test_places_are_noted_by_place_id_and_other_items_by_date_and_title(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    it = store.load(pid)
    placed = next((d, i) for d in it.days for i in d.items if i.place_id)
    d2, i2 = placed
    assert note_ref(d2.date, i2) == i2.place_id
    assert note_ref(d2.date + timedelta(days=2), i2) == i2.place_id                   # 换一天也还是同一条备注
    free = next(((d, i) for d in it.days for i in d.items if not i.place_id), None)
    if free:
        assert note_ref(free[0].date, free[1]) == f"{free[0].date.isoformat()}|{free[1].title}"
        assert put(client, pid, note_ref(free[0].date, free[1]), "午餐想吃面").status_code == 200
    assert put(client, pid, i2.place_id, "先去排队").status_code == 200


def test_the_old_whole_day_notes_are_still_accepted_and_kept(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    day = store.load(pid).days[1].date
    assert put(client, pid, day_note_ref(day), "整天的提醒").status_code == 200
    assert client.get(f"/plans/{pid}/notes").json()["notes"] == {f"day|{day.isoformat()}": "整天的提醒"}


def test_item_notes_appear_under_their_own_item_in_the_markdown_and_html_exports(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    assert "备注（" not in client.get(f"/plans/{pid}/itinerary.md").text
    it = store.load(pid)
    d, item = it.days[1], it.days[1].items[0]
    put(client, pid, note_ref(d.date, item), "上午排队久一点\n记得带学生证")
    day = store.load(pid).days[0]
    put(client, pid, day_note_ref(day.date), "第一天的整天提醒")
    md = client.get(f"/plans/{pid}/itinerary.md").text
    day2 = md.split("## 第 2 天")[1].split("## 第 3 天")[0]
    assert f"- 备注（{item.time} {item.title}）：上午排队久一点" in day2 and "记得带学生证" in day2   # 在对应那一天里
    day1 = md.split("## 第 1 天")[1].split("## 第 2 天")[0]
    assert "备注：第一天的整天提醒" in day1 and "上午排队久一点" not in day1
    html = client.get(f"/plans/{pid}/itinerary.html").text
    assert html.count('class="item-note"') == 1 and "记得带学生证" in html
    assert html.count('class="day-note"') == 1 and "第一天的整天提醒" in html


def test_the_pdf_still_renders_with_notes(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    put(client, pid, ref_at(store, pid, 1), "带伞")
    r = client.get(f"/plans/{pid}/itinerary.pdf")
    if r.status_code == 503:
        pytest.skip("本机没有可用的 PDF 浏览器")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_shifting_the_dates_moves_date_based_notes_but_not_place_notes(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    it = store.load(pid)
    first, last = it.days[0], it.days[-1]
    place_item = next(i for i in it.days[1].items if i.place_id)
    put(client, pid, day_note_ref(first.date), "第一天")
    put(client, pid, day_note_ref(last.date), "最后一天")
    put(client, pid, place_item.place_id, "地点备注")
    titled = next((d, i) for d in it.days for i in d.items if not i.place_id)
    put(client, pid, note_ref(titled[0].date, titled[1]), "按名称的备注")
    r = client.post(f"/plans/{pid}/shift", json={"start_date": "2026-11-03"})
    assert r.status_code == 201
    notes = client.get(f"/plans/{pid}/notes").json()["notes"]
    assert notes["day|2026-11-03"] == "第一天" and notes["day|2026-11-06"] == "最后一天"
    assert notes[place_item.place_id] == "地点备注"                                    # 按地点记的不受影响
    moved = (titled[0].date + timedelta(days=20)).isoformat()
    assert notes[f"{moved}|{titled[1].title}"] == "按名称的备注"
    md = client.get(f"/plans/{pid}/itinerary.md").text
    assert md.split("## 第 1 天")[1].split("## 第 2 天")[0].count("备注：第一天") == 1
    assert "地点备注" in md


def test_old_databases_are_upgraded_keeping_the_whole_day_notes_and_share_defaults(tmp_path):
    import sqlite3

    from app.db import SCHEMA_VERSION, Database

    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE day_notes (plan_id TEXT NOT NULL, date TEXT NOT NULL, text TEXT NOT NULL,
                                updated_at TEXT NOT NULL, PRIMARY KEY (plan_id, date));
        CREATE TABLE shares (token TEXT PRIMARY KEY, plan_id TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);
        INSERT INTO day_notes VALUES ('p1', '2026-10-15', '旧的整天笔记', '2026-10-01T00:00:00');
        INSERT INTO shares VALUES ('tok', 'p1', '2026-10-01T00:00:00', NULL);
    """)
    con.commit()
    con.close()
    db = Database(path)
    with db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [dict(r) for r in c.execute("SELECT ref, text FROM day_notes")] == [
            {"ref": "day|2026-10-15", "text": "旧的整天笔记"}]
        assert dict(c.execute("SELECT share_packing, share_todos, share_notes FROM shares").fetchone()) == {
            "share_packing": 0, "share_todos": 0, "share_notes": 0}
    Database(path)                                                                        # 再开一次不会出错


# --------------------------------------------------------------------------- #
# 不产生版本；复制 / 删除；分享页不带
# --------------------------------------------------------------------------- #
def test_none_of_it_creates_a_version_is_copied_with_the_plan_and_removed_with_it(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    client.post(f"/plans/{pid}/packing/generate")
    client.post(f"/plans/{pid}/todos", json={"text": "办签证", "due": "2026-10-30"})
    ref = ref_at(store, pid, 1)
    put(client, pid, ref, "笔记")
    assert store.latest_version(pid) == 1
    copy = client.post(f"/plans/{pid}/duplicate").json()["plan_id"]
    assert client.get(f"/plans/{copy}/packing").json()["total"] == client.get(f"/plans/{pid}/packing").json()["total"]
    assert [t["text"] for t in client.get(f"/plans/{copy}/todos").json()["items"]] == ["办签证"]
    assert client.get(f"/plans/{copy}/notes").json()["notes"] == {ref: "笔记"}
    put(client, copy, ref, "副本改了")
    assert client.get(f"/plans/{pid}/notes").json()["notes"][ref] == "笔记"                   # 各改各的
    assert client.delete(f"/plans/{pid}").status_code == 204
    with store.database.connect() as con:
        for table in ("packing_items", "todos", "day_notes"):
            assert con.execute(f"SELECT COUNT(*) FROM {table} WHERE plan_id = ?", (pid,)).fetchone()[0] == 0
    assert client.get(f"/plans/{copy}/packing").json()["total"] > 0                           # 副本不受影响


# city_id 只是城市编号；stories 是策展库里「地点出现在哪些作品里」（公开资料，不含任何个人信息）
BASE_SHARE_KEYS = {"itinerary", "version", "route", "weather", "shared", "city_id", "stories"}


def test_the_public_share_page_carries_nothing_private_unless_the_owner_ticks_it(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    client.post(f"/plans/{pid}/packing/generate")
    client.post(f"/plans/{pid}/todos", json={"text": "私人待办XYZ", "due": "2026-10-30"})
    put(client, pid, ref_at(store, pid, 1), "私人笔记XYZ")
    link = client.post(f"/plans/{pid}/share").json()
    assert link["options"] == {"packing": False, "todos": False, "notes": False}           # 默认都不分享
    body = client.get(f"/share/{link['token']}")
    assert body.status_code == 200
    for needle in ("XYZ", "packing", "todos", "护照"):
        assert needle not in body.text, needle
    assert set(body.json()) == BASE_SHARE_KEYS and body.json()["shared"] == []


def test_share_options_need_a_live_link_apply_at_once_and_keep_the_same_link(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    assert client.put(f"/plans/{pid}/share/options", json={"packing": True}).status_code == 409    # 还没有链接
    assert client.put("/plans/nope/share/options", json={"packing": True}).status_code == 404
    link = client.post(f"/plans/{pid}/share").json()
    r = client.put(f"/plans/{pid}/share/options", json={"packing": True, "notes": True})
    assert r.status_code == 200 and r.json()["options"] == {"packing": True, "todos": False, "notes": True}
    assert r.json()["token"] == link["token"]                                              # 链接不变
    assert client.get(f"/plans/{pid}/share").json()["options"] == {"packing": True, "todos": False, "notes": True}
    assert client.post(f"/plans/{pid}/share").json()["options"]["packing"] is True         # 重复点还是同一个链接
    client.put(f"/plans/{pid}/share/options", json={})                                     # 全取消
    assert client.get(f"/plans/{pid}/share").json()["options"] == {"packing": False, "todos": False, "notes": False}


def test_only_the_ticked_things_are_in_the_share_response_and_only_the_safe_fields(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    client.post(f"/plans/{pid}/packing/generate")
    first = client.get(f"/plans/{pid}/packing").json()["items"][0]
    client.patch(f"/plans/{pid}/packing/{first['id']}", json={"packed": True})
    client.post(f"/plans/{pid}/todos", json={"text": "办签证", "due": "2026-10-30"})
    ref = ref_at(store, pid, 1)
    put(client, pid, ref, "排队久一点")
    token = client.post(f"/plans/{pid}/share").json()["token"]

    client.put(f"/plans/{pid}/share/options", json={"packing": True})
    body = client.get(f"/share/{token}").json()
    assert set(body) == BASE_SHARE_KEYS | {"packing"} and body["shared"] == ["packing"]
    pk = body["packing"]
    assert pk["total"] > 20 and pk["packed"] == 1 and pk["categories"][0] == "证件"
    assert set(pk["items"][0]) == {"category", "text", "packed"}                           # 没有编号 / 来源 / 排序
    dumped = json.dumps(body, ensure_ascii=False)
    assert "办签证" not in dumped and "排队久一点" not in dumped

    client.put(f"/plans/{pid}/share/options", json={"todos": True, "notes": True})
    body = client.get(f"/share/{token}").json()
    assert set(body) == BASE_SHARE_KEYS | {"todos", "notes"} and body["shared"] == ["todos", "notes"]
    assert body["todos"] == [{"text": "办签证", "due": "2026-10-30", "done": False}]
    assert body["notes"] == {ref: "排队久一点"}
    assert "packing" not in body                                                          # 取消勾选后立刻消失


def test_a_new_link_after_revoking_starts_with_nothing_shared_again(store):
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    old = client.post(f"/plans/{pid}/share").json()["token"]
    client.put(f"/plans/{pid}/share/options", json={"packing": True, "todos": True, "notes": True})
    assert client.delete(f"/plans/{pid}/share").status_code == 204
    assert client.get(f"/plans/{pid}/share").json()["options"] == {"packing": False, "todos": False, "notes": False}
    new = client.post(f"/plans/{pid}/share").json()
    assert new["token"] != old and new["options"] == {"packing": False, "todos": False, "notes": False}
    assert client.get(f"/share/{old}").status_code == 410
    assert client.get(f"/share/{new['token']}").json()["shared"] == []


# --------------------------------------------------------------------------- #
# PDF 缓存
# --------------------------------------------------------------------------- #
def test_the_pdf_is_cached_by_content_and_rebuilt_when_anything_in_it_changes(store, monkeypatch):
    import app.api as api_mod

    calls = []

    def fake_pdf(html_path, pdf_path, **kw):
        calls.append(html_path.read_text(encoding="utf-8"))
        pdf_path.write_bytes(b"%PDF-1.4 fake " + str(len(calls)).encode())
        return pdf_path

    monkeypatch.setattr(api_mod, "html_to_pdf", fake_pdf)
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    first = client.get(f"/plans/{pid}/itinerary.pdf")
    assert first.status_code == 200 and first.content.startswith(b"%PDF") and len(calls) == 1
    again = client.get(f"/plans/{pid}/itinerary.pdf")
    assert again.content == first.content and len(calls) == 1                              # 第二次直接取缓存
    assert client.get(f"/plans/{pid}/itinerary.pdf?version=1").content == first.content and len(calls) == 1
    put(client, pid, ref_at(store, pid, 1), "新备注")                                         # 内容变了 → 重新生成
    third = client.get(f"/plans/{pid}/itinerary.pdf")
    assert len(calls) == 2 and third.content != first.content and "新备注" in calls[1]
    client.get(f"/plans/{pid}/itinerary.pdf?page_per_day=false")                           # 版式变了也是新的一份
    assert len(calls) == 3
    assert (store.database.path.parent / "pdf_cache").is_dir()


def test_a_failed_pdf_render_says_so_and_leaves_no_broken_cache_file(store, monkeypatch):
    import app.api as api_mod

    def boom(html_path, pdf_path, **kw):
        raise RuntimeError("没有浏览器")

    monkeypatch.setattr(api_mod, "html_to_pdf", boom)
    client = make_client(store)
    pid = make_plan(client)["plan_id"]
    r = client.get(f"/plans/{pid}/itinerary.pdf")
    assert r.status_code == 503 and "PDF 生成失败" in r.json()["detail"]
    cache = store.database.path.parent / "pdf_cache"
    assert not cache.exists() or not list(cache.glob("*"))
