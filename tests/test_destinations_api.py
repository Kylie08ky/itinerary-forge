"""任意目的地的接口（规格 10）：认城市、现建任务、读现建的数据。不联网：认城市和建城市都换成假的。"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.cities import list_cities, load_city, reload_cities
from app.citybuild import resolve as R
from app.citybuild.core import build_data, make_city
from app.citybuild.jobs import BuildJobs
from app.citybuild.store import save_generated
from app.library import load_library
from app.llm import LLMError
from app.versions import PlanStore

TODAY = date(2026, 10, 29)


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


def cand(qid, label, links, kind="settlement", country="Q17", **kw):
    return R.Candidate(qid=qid, label=label, sitelinks=links, kind=kind, country_qid=country, country_label="日本",
                       lat=34.69, lng=135.5, **kw)


class FakeResolver:
    """按输入返回预设的结果；不联网。"""

    def __init__(self, answers: dict[str, R.Resolution]):
        self.answers = answers
        self.calls: list[str] = []

    def resolve(self, text: str) -> R.Resolution:
        self.calls.append(text)
        return self.answers.get(text, R.Resolution("unknown", reason="没找到叫这个名字的目的地"))

    def describe(self, ids):
        return [cand(i, "大阪", 197) for i in ids]

    def labels(self, ids):
        return {i: {"labels": {"zh-cn": {"value": "大阪"}, "en": {"value": "Osaka"}}} for i in ids}

    def seed_for(self, c, query=""):
        return R.make_seed(c, name_en="Osaka", name_zh="大阪", country_iso="JP", currency="JPY", language="ja",
                           timezone="Asia/Tokyo", population=2_700_000, aliases=[query, "Osaka", "大阪"])


def fake_build(seed, client, say):
    """假的现建：写一份两个点的城市数据到现建目录。"""
    say("核对 1/2")
    raws = [{"qid": "Q1", "name_zh": "大阪城", "name_en": "Osaka Castle", "category": "sight", "setting": "outdoor",
             "minutes": 90, "open_access": False, "lat": 34.687, "lng": 135.526, "sitelinks": 90, "cls": "Q23413",
             "theme": None, "zone": "core", "osm": None, "coords": "wikidata", "url": None, "url_ok": False,
             "closed": None, "note": "", "area": "", "seed": False},
            {"qid": "Q2", "name_zh": "道顿堀", "name_en": "Dotonbori", "category": "sight", "setting": "outdoor",
             "minutes": 60, "open_access": True, "lat": 34.669, "lng": 135.501, "sitelinks": 40, "cls": "Q570116",
             "theme": "district", "zone": "core", "osm": None, "coords": "wikidata", "url": None, "url_ok": False,
             "closed": None, "note": "", "area": "", "seed": False}]
    places, themes = build_data(seed["id"], seed["name_zh"], raws, TODAY)
    city = make_city(seed["id"], seed["name_zh"], seed["name_en"], tuple(seed["center"]), seed["bbox"],
                     [p["id"] for p in places], today=TODAY, seed=seed)
    save_generated(city, places, themes)
    reload_cities()
    return city


@pytest.fixture(autouse=True)
def generated_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path / "generated"))
    reload_cities()
    yield
    reload_cities()


def make_client(tmp_path, answers=None, builder=fake_build):
    resolver = FakeResolver(answers or {})
    app = create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM, resolver=resolver,
                     build_jobs=BuildJobs(builder=builder, inline=True, client_factory=_NoClient))
    return TestClient(app), resolver


class _NoClient:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def test_a_city_that_is_already_in_the_library_is_ready_without_asking_wikidata(tmp_path):
    client, resolver = make_client(tmp_path)
    for text in ("巴黎", "Paris", "paris"):
        r = client.post("/destinations/resolve", json={"text": text}).json()
        assert r == {"status": "ready", "city_id": "paris", "name_zh": "巴黎"}
    assert resolver.calls == []


def test_an_unknown_name_is_reported_as_not_found(tmp_path):
    client, _ = make_client(tmp_path)
    r = client.post("/destinations/resolve", json={"text": "xyzabc"}).json()
    assert r["status"] == "unknown" and "没找到" in r["reason"]


def test_a_known_but_unbuilt_city_says_build_and_a_country_or_a_namesake_says_choices(tmp_path):
    answers = {"大阪": R.Resolution("city", city=cand("Q35765", "大阪", 197)),
               "日本": R.Resolution("choices", choices=[cand("Q35765", "大阪", 197), cand("Q34600", "京都", 260)],
                                  reason="日本最常去的城市，请选一个")}
    client, _ = make_client(tmp_path, answers)
    r = client.post("/destinations/resolve", json={"text": "大阪"}).json()
    assert r["status"] == "build" and r["choice"]["qid"] == "Q35765"
    r = client.post("/destinations/resolve", json={"text": "日本"}).json()
    assert r["status"] == "choices" and [c["qid"] for c in r["choices"]] == ["Q35765", "Q34600"]


def test_building_a_city_runs_a_job_and_the_city_can_then_be_used_for_a_plan(tmp_path):
    client, _ = make_client(tmp_path)
    r = client.post("/destinations/build", json={"qid": "Q35765", "query": "大阪"})
    assert r.status_code == 202 and r.json()["status"] == "building"
    job = client.get(f"/destinations/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done" and job["name_zh"] == "大阪"
    assert load_city("osaka").currency == "JPY" and load_city("osaka").wikidata == "Q35765"
    assert "osaka" in {c.id for c in list_cities()} and load_library("osaka").places
    # 建好之后：按名字认得出，不用再建
    again = client.post("/destinations/resolve", json={"text": "大阪"}).json()
    assert again == {"status": "ready", "city_id": "osaka", "name_zh": "大阪"}
    assert client.post("/destinations/build", json={"qid": "Q35765"}).json()["status"] == "ready"
    # 用它生成行程
    plan = client.post("/plans", json={"destination": "Osaka", "start_date": "2026-12-12", "end_date": "2026-12-14",
                                       "budget_level": "medium", "travel_style": "balanced",
                                       "interests": ["sightseeing"],
                                       "theme": "heritage", "arrival_time": "2026-12-12 10:00",
                                       "departure_time": "2026-12-14 18:00"})
    assert plan.status_code == 201, plan.text
    days = plan.json()["itinerary"]["days"]
    assert len(days) == 3 and any(i.get("place_id") for d in days for i in d["items"])


def test_a_failing_build_is_reported_on_the_job_and_the_same_city_is_not_built_twice_at_once(tmp_path):
    def broken(seed, client, say):
        raise RuntimeError("维基数据连不上")

    client, _ = make_client(tmp_path, builder=broken)
    r = client.post("/destinations/build", json={"qid": "Q35765"}).json()
    job = client.get(f"/destinations/jobs/{r['job_id']}").json()
    assert job["status"] == "failed" and "维基数据连不上" in job["error"]
    assert client.get("/destinations/jobs/nope").status_code == 404


def test_running_jobs_for_the_same_city_share_one_job():
    seen: list[str] = []
    jobs = BuildJobs(builder=lambda seed, client, say: seen.append(seed["id"]) or {}, client_factory=_NoClient)
    seed = {"id": "osaka", "name_zh": "大阪"}
    first = jobs.start(seed)
    first.status = "running"                       # 假装还没跑完
    assert jobs.start(seed) is first
    jobs._pool.shutdown(wait=True)


def test_the_generated_files_are_written_atomically_and_listed_with_the_curated_cities(tmp_path):
    fake_build({"id": "osaka", "name_zh": "大阪", "name_en": "Osaka", "center": [34.69, 135.5],
                "bbox": {"lat": [34.4, 34.9], "lng": [135.2, 135.8]}, "country": "JP", "currency": "JPY",
                "local_language": "ja", "timezone": "Asia/Tokyo", "qid": "Q35765", "aliases": ["osaka", "大阪"],
                "supported": True}, None, lambda m: None)
    ids = [c.id for c in list_cities()]
    assert ids.index("paris") < ids.index("osaka")                            # 精校的在前
    path = tmp_path / "generated" / "cities" / "osaka.json"
    assert json.loads(path.read_text(encoding="utf-8"))["tier"] == "standard"
    assert not list((tmp_path / "generated").rglob("*.tmp"))


def test_a_plan_for_an_auto_built_city_says_the_data_is_automatic_and_a_curated_city_does_not(tmp_path):
    client, _ = make_client(tmp_path)
    client.post("/destinations/build", json={"qid": "Q35765", "query": "大阪"})
    body = {"start_date": "2026-12-12", "end_date": "2026-12-14", "budget_level": "medium", "travel_style": "balanced",
            "interests": ["sightseeing"], "arrival_time": "2026-12-12 10:00", "departure_time": "2026-12-14 18:00"}
    osaka = client.post("/plans", json={**body, "destination": "Osaka", "theme": "heritage"}).json()
    assert any("数据是自动建的" in n for n in osaka["itinerary"]["generation"]["notes"])
    paris = client.post("/plans", json={**body, "destination": "Paris", "theme": "art"}).json()
    assert not any("数据是自动建的" in n for n in paris["itinerary"]["generation"]["notes"])


def test_a_hand_written_seed_overrides_the_automatic_one_and_a_city_without_a_seed_is_untouched():
    from app.citybuild.build import with_curated_seed

    auto = {"id": "beijing", "name_zh": "北京", "include": [], "exclude": [], "bbox": {"lat": [0, 1], "lng": [0, 1]},
            "aliases": ["beijing"], "top_n": 40}
    merged = with_curated_seed(auto)
    assert merged["exclude"] and merged["include"] and merged["bbox"]["lat"] == [39.65, 40.15]
    assert "peking" in merged["aliases"] and "beijing" in merged["aliases"]
    plain = {"id": "lisbon", "name_zh": "里斯本", "include": [], "exclude": []}
    assert with_curated_seed(plain) == plain


PLAN_PAGES = ["", "/itinerary.md", "/itinerary.html", "/itinerary.ics", "/itinerary.kml", "/route", "/budget",
              "/budget-settings", "/weather", "/packing", "/todos", "/notes", "/reservations", "/stay-recommendation",
              "/attachments"]


def test_every_page_of_a_plan_for_an_auto_built_city_works_and_carries_no_paris_leftovers(tmp_path):
    client, _ = make_client(tmp_path)
    client.post("/destinations/build", json={"qid": "Q35765", "query": "大阪"})
    body = {"start_date": "2026-12-12", "end_date": "2026-12-14", "budget_level": "medium", "travel_style": "balanced",
            "interests": ["sightseeing"], "arrival_time": "2026-12-12 10:00", "departure_time": "2026-12-14 18:00",
            "destination": "Osaka", "theme": "heritage"}
    pid = client.post("/plans", json=body).json()["plan_id"]
    for suffix in PLAN_PAGES:
        r = client.get(f"/plans/{pid}{suffix}")
        want = 404 if suffix == "/budget" else 200                 # 现建的城市没有核对过价格：预算不估算，页面说明原因
        assert r.status_code == want, (suffix, r.status_code, r.text[:200])
    assert "没有核对" in client.get(f"/plans/{pid}/budget").json()["detail"]
    assert client.post(f"/plans/{pid}/packing/generate").status_code == 200
    packing = json.dumps(client.get(f"/plans/{pid}/packing").json(), ensure_ascii=False)
    todos = json.dumps(client.get(f"/plans/{pid}/todos").json(), ensure_ascii=False)
    for leftover in ("巴黎", "申根", "法国", "欧元", "france-visas", "C / E 型"):
        assert leftover not in packing and leftover not in todos, leftover
    assert "JPY" in packing and "步行鞋" in packing


def _city(**over):
    from app.cities import City

    base = load_city("paris").model_dump()
    return City.model_validate({**base, "id": "osaka", "country": "JP", "currency": "JPY", **over})


def test_the_generic_packing_list_has_no_paris_wording_and_a_domestic_trip_needs_an_id_card_not_a_passport():
    from app.companion import generic_packing, load_packing

    base = load_packing("paris")
    abroad = generic_packing(base, _city(monthly_high_c={m: 10.0 for m in range(1, 13)}))
    keys = {i.key for i in abroad.items}
    assert {"passport", "visa", "plug-adapter", "cards"} <= keys and "id-card" not in keys
    assert all("巴黎" not in i.text and "申根" not in i.text and "欧元" not in i.text for i in abroad.items)
    assert abroad.monthly_high_c[7] == 10.0 and "去年" in abroad.monthly_high_basis
    home = generic_packing(base, _city(country="CN", currency="CNY", id="beijing"))
    hkeys = {i.key for i in home.items}
    assert "id-card" in hkeys and not {"passport", "visa", "plug-adapter", "residence-permit"} & hkeys
    assert "没取到" in home.monthly_high_basis                         # 没有气温数据：按温和估算并说明


def test_suggested_todos_follow_the_city():
    from app.companion import SUGGESTED, suggested_for

    assert suggested_for(load_city("paris")) == SUGGESTED
    abroad = {k: t for k, t, _ in suggested_for(_city())}
    assert "法国" not in abroad["visa"] and "JPY" in abroad["money"]
    home = {k for k, _t, _ in suggested_for(_city(country="CN", currency="CNY"))}
    assert not {"passport-validity", "visa", "sim"} & home and "money" in home


def test_a_city_built_by_an_older_version_is_rebuilt_in_place_when_someone_asks_for_it_again(tmp_path):
    from app.cities import needs_rebuild

    client, _ = make_client(tmp_path, {"大阪": R.Resolution("city", city=cand("Q35765", "大阪", 197))})
    client.post("/destinations/build", json={"qid": "Q35765", "query": "大阪"})
    assert not needs_rebuild(load_city("osaka"))
    path = generated_dir_path() / "cities" / "osaka.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    old.pop("builder_version")                                                  # 假装是旧版建的
    path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    reload_cities()
    assert needs_rebuild(load_city("osaka"))
    assert client.post("/destinations/resolve", json={"text": "大阪"}).json()["status"] == "build"     # 旧版：提示重建
    again = client.post("/destinations/build", json={"qid": "Q35765", "query": "大阪"}).json()
    assert again["status"] == "building" and again["city_id"] == "osaka"        # 原地重建，没有多出一座城市
    assert not needs_rebuild(load_city("osaka")) and len([c for c in list_cities() if c.wikidata == "Q35765"]) == 1


def generated_dir_path():
    from app.citybuild.store import generated_dir

    return generated_dir()


def test_asking_for_an_outdated_auto_built_city_by_name_offers_a_rebuild_without_asking_wikidata(tmp_path):
    client, resolver = make_client(tmp_path)
    client.post("/destinations/build", json={"qid": "Q35765", "query": "大阪"})
    path = generated_dir_path() / "cities" / "osaka.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    old.pop("builder_version")
    path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    reload_cities()
    r = client.post("/destinations/resolve", json={"text": "大阪"}).json()
    assert r["status"] == "build" and r["choice"]["qid"] == "Q35765" and resolver.calls == []


def _plan_of(places: list[str], days: int = 3):
    from datetime import timedelta

    from app.schemas import Day, ItemType, Itinerary, ItineraryItem, Trip

    start = date(2026, 7, 10)
    items = [ItineraryItem(time="10:00", title=p, type=ItemType.museum, location="x", duration_minutes=90,
                           transport=None, notes="", place_id=p) for p in places]
    return Itinerary(trip=Trip.model_validate({"destination": "Paris", "date_range": {
        "start": start, "end": start + timedelta(days=days - 1)}, "traveler_profile": {
            "style": "balanced", "budget": "medium", "interests": []}}),
        days=[Day(date=start + timedelta(days=i), theme="t", items=items[i:i + 1] or items[:1]) for i in range(days)])


def test_packing_gets_a_few_lines_that_only_this_city_and_this_trip_need():
    from app.companion import context_items
    from app.library import load_library

    lib = load_library("paris")
    hot_wet = _city(monthly_high_c=dict.fromkeys(range(1, 13), 30.0), monthly_low_c=dict.fromkeys(range(1, 13), 16.0),
                    monthly_rain_mm=dict.fromkeys(range(1, 13), 180), elevation_m=2300, center=(10.0, 100.0))
    trip = _plan_of(["notre-dame", "eiffel-tower", "louvre"])
    texts = " ".join(i["text"] for i in context_items(trip, lib, hot_wet, [7]))
    assert "雨水多" in texts and "180" in texts and "温差大" in texts and "驱蚊" in texts and "海拔约 2300" in texts
    assert "教堂" in texts and "圣母院" in texts                                  # 行程里有教堂：写出是哪几个
    dry = _city(monthly_high_c=dict.fromkeys(range(1, 13), 20.0), monthly_low_c=dict.fromkeys(range(1, 13), 15.0),
                monthly_rain_mm=dict.fromkeys(range(1, 13), 30))
    plain = " ".join(i["text"] for i in context_items(_plan_of(["louvre"]), lib, dry, [7]))
    assert "雨水多" not in plain and "温差大" not in plain and "海拔" not in plain and "教堂" not in plain


def test_the_cover_ticket_total_is_to_be_filled_not_zero_when_a_city_has_no_prices(tmp_path):
    from app.renderer import cover_stats

    client, _ = make_client(tmp_path)
    client.post("/destinations/build", json={"qid": "Q35765", "query": "大阪"})
    body = {"start_date": "2026-12-12", "end_date": "2026-12-14", "budget_level": "medium", "travel_style": "balanced",
            "interests": ["sightseeing"], "arrival_time": "2026-12-12 10:00", "departure_time": "2026-12-14 18:00",
            "destination": "Osaka", "theme": "heritage"}
    pid = client.post("/plans", json=body).json()["plan_id"]
    assert client.get(f"/plans/{pid}/route").json()["ticket_eur"] is None
    assert "待补" in client.get(f"/plans/{pid}/itinerary.html").text
    from app.schemas import Itinerary

    it = Itinerary.model_validate(client.get(f"/plans/{pid}").json()["itinerary"])
    assert cover_stats(it)["ticket_eur"] is None
    paris = client.post("/plans", json={**body, "destination": "Paris", "theme": "art"}).json()["plan_id"]
    assert client.get(f"/plans/{paris}/route").json()["ticket_eur"] > 0


def test_a_student_on_a_paris_trip_gets_the_isic_card_in_the_real_packing_list(tmp_path):
    client, _ = make_client(tmp_path)
    body = {"start_date": "2026-12-12", "end_date": "2026-12-14", "budget_level": "medium", "travel_style": "balanced",
            "interests": ["art"], "arrival_time": "2026-12-12 10:00", "departure_time": "2026-12-14 18:00",
            "destination": "Paris", "theme": "art", "travelers": [{"age": 21, "student": True}]}
    pid = client.post("/plans", json=body).json()["plan_id"]
    client.post(f"/plans/{pid}/packing/generate")
    items = client.get(f"/plans/{pid}/packing").json()["items"]
    student = next(i for i in items if i["key"] == "student-card")
    assert "ISIC" in student["text"] and student["source_url"] == "https://www.isic.org/"
    plain = client.post("/plans", json={**body, "travelers": [{"age": 35}]}).json()["plan_id"]
    client.post(f"/plans/{plain}/packing/generate")
    assert not any(i["key"] == "student-card" for i in client.get(f"/plans/{plain}/packing").json()["items"])


def test_a_country_list_may_be_picked_from_many_times_but_a_namesake_list_only_once(tmp_path):
    """国家 → 最常去的城市可以多选（按点选顺序成多城市行程）；同名的地方只能选一个。"""
    two = [cand("Q35765", "大阪", 197), cand("Q34600", "京都", 260)]
    answers = {"日本": R.Resolution("choices", choices=two, reason="日本最常去的城市", many=True),
               "Springfield": R.Resolution("choices", choices=two, reason="有几个同名的地方，请选一个")}
    client, _ = make_client(tmp_path, answers)
    assert client.post("/destinations/resolve", json={"text": "日本"}).json()["many"] is True
    assert client.post("/destinations/resolve", json={"text": "Springfield"}).json()["many"] is False


def test_the_real_resolver_marks_a_country_listing_as_many():
    import inspect

    assert "many=True" in inspect.getsource(R.Resolver.resolve)
