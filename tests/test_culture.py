"""文化入门（规格 09 十三）：数据闸门、地点名的认法、接口。不联网（来源核对在 scripts/build_culture.py 里做）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import create_app
from app.culture import (
    KINDS,
    MIN_POPULARITY,
    CultureError,
    Work,
    culture_overview,
    get_work,
    load_culture,
    mentions,
    name_keys,
    recap,
    stories_by_place,
    stories_for_place,
    story_text,
    validate_against_library,
)
from app.library import load_library
from app.llm import LLMError
from app.versions import PlanStore

HISTORY = "history-thought-medieval-to-existentialism"

SRC = {"title": "x", "url": "https://example.org/x", "checked_at": "2026-10-26"}


def work(**over) -> Work:
    base = {"id": "w", "kind": "film", "title": "T", "creator": "C D", "year": 2000,
            "summary": "一部讲巴黎故事的电影，看它可以了解巴黎。",
            "sees": [{"place_id": "louvre", "note": "拍摄地点之一。"}], "source": SRC}
    return Work.model_validate({**base, **over})


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


def history_plan(client) -> str:
    payload = {"destination": "Paris", "start_date": "2026-12-17", "end_date": "2026-12-19", "budget_level": "medium",
               "travel_style": "balanced", "interests": ["history"], "theme": "museums", "route": HISTORY,
               "arrival_time": "2026-12-17 09:00", "departure_time": "2026-12-19 20:00"}
    r = client.post("/plans", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


# --------------------------------------------------------------------------- #
# 数据闸门
# --------------------------------------------------------------------------- #
def test_the_paris_works_load_and_every_one_is_sourced_and_linked_to_places():
    works = load_culture("paris")
    lib = load_library("paris")
    assert len(works) >= 12
    kinds = {w.kind for w in works.values()}
    assert {"film", "book"} <= kinds and kinds <= set(KINDS)
    assert all(w.popularity >= MIN_POPULARITY for w in works.values())       # 名气不大的不收
    for w in works.values():
        assert w.source.url.startswith("https://") and w.source.checked_at.year >= 2026
        assert w.sees and all(s.place_id in lib.places for s in w.sees), w.id
        assert not any(lib.places[s.place_id].long_term_closed for s in w.sees), w.id


def test_a_work_with_the_same_place_twice_is_rejected():
    with pytest.raises(ValidationError):
        work(sees=[{"place_id": "louvre", "note": "拍摄地点之一。"}, {"place_id": "louvre", "note": "又一处说明。"}])


def test_a_work_pointing_at_a_place_outside_the_library_is_rejected():
    with pytest.raises(CultureError, match="不在地点库"):
        validate_against_library(work(sees=[{"place_id": "no-such-place", "note": "拍摄地点之一。"}]),
                                 load_library("paris"))


def test_a_work_pointing_at_a_long_term_closed_place_is_rejected():
    lib = load_library("paris")
    closed = next((p for p in lib.places.values() if p.long_term_closed), None)
    if closed is None:
        pytest.skip("地点库里现在没有长期闭馆的点")
    with pytest.raises(CultureError, match="长期闭馆"):
        validate_against_library(work(sees=[{"place_id": closed.id, "note": "拍摄地点之一。"}]), lib)


def test_a_source_must_be_https_and_a_work_needs_at_least_one_place():
    with pytest.raises(ValidationError):
        work(source={**SRC, "url": "http://example.org/x"})
    with pytest.raises(ValidationError):
        work(sees=[])


def test_get_work_reports_unknown_ids():
    assert get_work("paris", "amelie").creator == "Jean-Pierre Jeunet"
    with pytest.raises(CultureError):
        get_work("paris", "no-such-work")


def test_a_city_without_a_culture_file_has_no_works():
    assert load_culture("no-such-city") == {}


# --------------------------------------------------------------------------- #
# 地点名在来源文字里怎么认（核对脚本和测试共用）
# --------------------------------------------------------------------------- #
def test_place_names_are_matched_without_accents_case_or_museum_prefixes():
    lib = load_library("paris")
    assert mentions(lib.places["orsay"], "She walks through the musee d'orsay at dusk.")
    assert mentions(lib.places["orsay"], "A scene in the Orsay.")
    assert mentions(lib.places["pont-alexandre-iii"], "the Opéra Pont Alexandre III the Sacré-Cœur")
    assert mentions(lib.places["montmartre-sacre-coeur"], "set in Montmartre")
    assert not mentions(lib.places["orsay"], "A scene at the Louvre.")


def test_short_names_never_count_as_a_match():
    lib = load_library("paris")
    assert all(len(k) >= 5 for p in lib.places.values() for k in name_keys(p))


# --------------------------------------------------------------------------- #
# 页面摘要和接口
# --------------------------------------------------------------------------- #
def test_the_overview_groups_by_kind_and_derives_the_routes_through_the_places():
    ov = culture_overview("paris")
    order = [list(KINDS).index(w["kind"]) for w in ov]
    assert order == sorted(order)
    midnight = next(w for w in ov if w["id"] == "midnight-in-paris")
    assert midnight["kind_zh"] == "电影" and midnight["title_zh"] == "午夜巴黎"
    assert {p["place_id"] for p in midnight["places"]} >= {"notre-dame", "pantheon", "versailles"}
    assert all(" · " in p["name"] or p["name"] == p["name_zh"] for p in midnight["places"])
    # 经过先贤祠和圣母院的路线：历史与思想（派生，不手工维护）
    assert "history-thought-medieval-to-existentialism" in [r["id"] for r in midnight["routes"]]


def test_culture_endpoint_lists_the_works_with_places_and_sources(client):
    r = client.get("/culture")
    assert r.status_code == 200
    works = r.json()
    assert len(works) >= 12
    amelie = next(w for w in works if w["id"] == "amelie")
    assert amelie["source"]["url"].startswith("https://") and amelie["places"]
    assert all({"place_id", "name", "name_zh", "note"} <= p.keys() for p in amelie["places"])


def test_every_work_note_and_summary_is_simplified_chinese_text():
    # 不含常见繁体字（和全站「全部简体」一致）；覆盖面不求全，只拦明显的混入
    traditional = set("與這個為從對點於後來開關門電學會經")
    for w in load_culture("paris").values():
        text = w.summary + w.title_zh + "".join(s.note for s in w.sees)
        assert not (traditional & set(text)), w.id


# --------------------------------------------------------------------------- #
# 地点 → 故事（规格 09 十五）
# --------------------------------------------------------------------------- #
def test_a_place_knows_which_works_it_appears_in_with_a_note_and_a_source():
    found = stories_for_place("paris", "pantheon")
    assert [s["work_id"] for s in found] == ["midnight-in-paris"]
    s = found[0]
    assert s["kind_zh"] == "电影" and s["title_zh"] == "午夜巴黎" and "先贤祠附近的街道" in s["note"]
    assert s["source"]["url"].startswith("https://en.wikipedia.org/")
    # 圣母院有电影也有书：名气大的（维基百科语言版本多的）在前
    notre = stories_for_place("paris", "notre-dame")
    assert {"film", "book"} <= {x["kind"] for x in notre}
    pops = [load_culture("paris")[x["work_id"]].popularity for x in notre]
    assert pops == sorted(pops, reverse=True) and pops[0] > 0


def test_places_without_stories_and_cities_without_culture_data_are_simply_empty():
    assert stories_for_place("paris", "citypharma") == []
    assert stories_for_place("no-such-city", "louvre") == []
    assert stories_by_place("paris", ["citypharma", "pantheon", "pantheon"]).keys() == {"pantheon"}
    assert recap("no-such-city", ["louvre"]) == [] and recap("paris", ["citypharma"]) == []


def test_every_curated_work_has_a_wikidata_id_and_a_popularity_that_orders_the_page():
    works = load_culture("paris")
    assert all(w.wikidata and w.popularity > 0 for w in works.values())
    assert works["les-miserables"].popularity > works["eiffel"].popularity
    ov = culture_overview("paris")
    for kind in KINDS:
        pops = [works[w["id"]].popularity for w in ov if w["kind"] == kind]
        assert pops == sorted(pops, reverse=True), kind


def test_the_recap_lists_only_works_whose_places_are_in_the_trip_most_visited_first():
    rows = recap("paris", ["notre-dame", "pantheon", "versailles", "citypharma"])
    ids = [r["id"] for r in rows]
    assert "midnight-in-paris" in ids and "amelie" not in ids
    assert ids[0] == "midnight-in-paris"                                     # 3 个点，经过最多
    top = rows[0]
    assert {v["place_id"] for v in top["visited"]} == {"notre-dame", "pantheon", "versailles"}
    assert all(r["visited"] and r["source"]["url"].startswith("https://") for r in rows)


def test_story_text_reads_as_one_sentence_with_the_original_title_only_when_translated():
    s = stories_for_place("paris", "pantheon")[0]
    assert story_text(s) == "电影《午夜巴黎》（Midnight in Paris）：拍摄地点包括先贤祠附近的街道。"
    bare = stories_for_place("paris", "palais-royal")[0]
    assert bare["title_zh"] is None and story_text(bare).startswith("电影《Mission: Impossible – Fallout》：")


def test_the_plan_carries_stories_by_place_and_a_recap_of_the_works_it_passes(client):
    pid = history_plan(client)
    detail = client.get(f"/plans/{pid}").json()
    placed = {i["place_id"] for d in detail["itinerary"]["days"] for i in d["items"] if i.get("place_id")}
    assert set(detail["stories"]) <= placed and "notre-dame" in detail["stories"]
    assert any(s["work_id"] == "hunchback" for s in detail["stories"]["notre-dame"])
    assert detail["culture"] and all(w["visited"] for w in detail["culture"])
    # 故事是读取时现算的：存下来的行程里没有它
    assert "stories" not in detail["itinerary"]["days"][0]["items"][0]


def test_exports_and_the_share_page_show_the_stories(client):
    pid = history_plan(client)
    md = client.get(f"/plans/{pid}/itinerary.md").text
    html = client.get(f"/plans/{pid}/itinerary.html").text
    assert "这里的故事（" in md and "《巴黎圣母院》" in md
    assert "这里的故事：" in html and "《巴黎圣母院》" in html
    token = client.post(f"/plans/{pid}/share").json()["token"]
    shared = client.get(f"/share/{token}").json()
    assert "notre-dame" in shared["stories"]


def test_the_place_card_lists_every_story_of_the_place(client):
    d = client.get("/places/notre-dame").json()
    assert {s["work_id"] for s in d["stories"]} >= {"hunchback", "midnight-in-paris"}
    assert client.get("/places/citypharma").json()["stories"] == []
