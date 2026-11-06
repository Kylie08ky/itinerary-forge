"""周边一日游的取数脚本（scripts/fetch_city_pairs.py、scripts/build_light_city.py）里不联网的部分：
选车站、算时间、挑景点、拼出能通过校验的城市 / 地点 / 主题数据。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_light_city as B  # noqa: E402
import fetch_city_pairs as P  # noqa: E402
from daytrip_destinations import DESTINATIONS, ORIGINS, by_id  # noqa: E402

from app.cities import City  # noqa: E402
from app.library import Place, PlaceLibrary, ThemePack  # noqa: E402

TODAY = date(2026, 10, 25)


# --------------------------------------------------------------------------- #
# fetch_city_pairs
# --------------------------------------------------------------------------- #
def test_local_winter_times_convert_to_utc_and_back():
    assert P.to_utc_iso(date(2026, 12, 12), "07:00") == "2026-12-12T06:00:00Z"
    assert P.local_hhmm("2026-12-12T21:39:00Z") == "22:39"


def test_the_station_pick_prefers_the_exact_name_then_rail_hints():
    results = [{"type": "PLACE", "name": "Ulm"}, {"type": "STOP", "name": "Ulm, ZOB"},
               {"type": "STOP", "name": "Ulm Hauptbahnhof"}]
    assert P.pick_stop(results, ("hbf", "hauptbahnhof"))["name"] == "Ulm Hauptbahnhof"
    assert P.pick_stop(results, ("hbf",), exact="Ulm, ZOB")["name"] == "Ulm, ZOB"
    assert P.pick_stop(results, ("hbf",), exact="Nirgendwo") is None
    assert P.pick_stop([{"type": "PLACE", "name": "x"}], ()) is None


def test_the_fastest_itinerary_and_the_bus_only_check_and_the_last_departure():
    its = [{"duration": 3600, "transfers": 1, "startTime": "2026-12-12T20:00:00Z", "legs": [{"mode": "RAIL"}]},
           {"duration": 3000, "transfers": 0, "startTime": "2026-12-12T21:10:00Z",
            "legs": [{"mode": "WALK"}, {"mode": "BUS"}]}]
    assert P.fastest(its)["duration"] == 3000
    assert P.is_bus_only(its[1]) is True and P.is_bus_only(its[0]) is False
    assert P.latest_departure(its) == "22:10"
    assert P.fastest([]) is None and P.latest_departure([]) is None


def test_station_names_in_chinese_follow_the_city_name():
    assert P.station_zh("蒂宾根", "Tübingen Hauptbahnhof") == "蒂宾根中央火车站"
    assert P.station_zh("蒂宾根", "Baden-Baden Bahnhof") == "蒂宾根火车站"
    assert P.station_zh(None, "Maastricht") == "Maastricht"


def test_every_destination_belongs_to_a_known_origin_and_has_a_unique_id():
    ids = [d.id for d in DESTINATIONS]
    assert len(ids) == len(set(ids))
    assert all(d.origin in ORIGINS for d in DESTINATIONS)
    assert by_id("tuebingen").language == "de" and by_id("strasbourg").language == "fr"


# --------------------------------------------------------------------------- #
# build_light_city
# --------------------------------------------------------------------------- #
def el(osm_id, name, q, lat=48.52, lon=9.05, osm_name=None, **tags):
    e = {"type": "wikidata", "id": osm_id, "lat": lat, "lon": lon, "tags": {"name": name, "wikidata": q, **tags}}
    return e | ({"osm": f"node/{osm_id}", "osm_name": osm_name} if osm_name else {})


INFO = {"Q1": {"sitelinks": {f"w{i}": {} for i in range(30)} | {"zhwiki": {"title": "某地"}},
                "labels": {"de": "Stiftskirche", "zh-hans": "教堂"}},
        "Q2": {"sitelinks": {f"w{i}": {} for i in range(25)}, "labels": {"de": "Schloss Hohentübingen"}},
        "Q3": {"sitelinks": {"dewiki": {}}, "labels": {}}}


def test_slugs_are_ascii_ids_the_library_accepts():
    assert B.slug("Hölderlinturm") == "holderlinturm"
    assert B.slug("Stiftskirche St. Georg") == "stiftskirche-st-georg"
    assert B.slug("Straße der Œuvre") == "strasse-der-oeuvre"


def test_classify_maps_osm_tags_to_categories_and_skips_non_sights():
    assert B.classify({"tourism": "museum"})[0] == "museum"
    assert B.classify({"historic": "castle"})[:2] == ("sight", "mixed")
    assert B.classify({"leisure": "park"})[0] == "park"
    assert B.classify({"place": "square"})[3] is True
    assert B.classify({"shop": "bakery"}) is None


def test_the_top_picks_follow_fame_chinese_wiki_bonus_dedupe_and_skip_junk():
    elements = [
        el(1, "Schloss", "Q2", tourism="attraction"), el(2, "Stiftskirche", "Q1", amenity="place_of_worship"),
        el(3, "Stiftskirche", "Q1", amenity="place_of_worship"),              # 同一个维基数据条目：去重
        el(4, "Accès Parking Schloss", "Q2", tourism="attraction"),            # 垃圾名字
        el(5, "Kleiner Brunnen", "Q3", tourism="attraction"),
        {"type": "node", "id": 6, "lat": 48.5, "lon": 9.0, "tags": {"name": "Ohne Wikidata", "tourism": "attraction"}},
    ]
    top = B.pick_top(elements, INFO, 3, (48.52, 9.05))
    assert [e["id"] for e in top] == [2, 1, 5]                    # 有中文维基的 Stiftskirche 排第一，没维基数据的不要


def test_wikipedia_disambiguation_brackets_are_not_part_of_the_name():
    assert B.clean_name("Frauenkirche (Esslingen)") == "Frauenkirche"
    assert B.clean_name("老桥 (海德堡)") == "老桥"
    assert B.clean_name("Alte Brücke") == "Alte Brücke"
    assert B.clean_name("(only)") == "(only)"                    # 整个名字都在括号里就别去掉


def test_first_sentence_cuts_at_the_full_stop_and_truncates():
    assert B.first_sentence("这是一座教堂。后面的不要。") == "这是一座教堂。"
    assert len(B.first_sentence("字" * 200)) <= 91


def test_a_built_place_city_and_theme_pass_the_real_validators():
    d = by_id("tuebingen")
    taken: set[str] = set()
    cands = [el(2, "Stiftskirche St. Georg", "Q1", osm_name="Stiftskirche St. Georg", amenity="place_of_worship",
                opening_hours="Mo off; Tu-Su 10:00-17:00", website="https://example.org/x",
                **{"addr:street": "Holzmarkt", "addr:housenumber": "1"}),
             el(1, "Schloss Hohentübingen", "Q2", historic="castle", lat=48.5230, lon=9.0610)]
    info = INFO | {"Q1": INFO["Q1"] | {"labels": {"de": "Stiftskirche St. Georg", "zh-hans": "圣乔治教堂"}}}
    places = [
        B.make_place(cands[0], d, info, TODAY, url="https://example.org/x", url_ok=True,
                     zh_note="一句话（中文维基百科）", taken=taken),
        B.make_place(cands[1], d, info, TODAY, url=None, url_ok=False, zh_note="", taken=taken),
    ]
    assert places[0]["name_zh"] == "圣乔治教堂" and places[0]["name_local_check"]["status"] == "verified"
    assert places[1]["name_zh"] == "Schloss Hohentübingen" and places[1]["name_local_check"]["status"] == "draft"
    assert places[0]["closed_weekdays"] == [0] and places[0]["official_url"] == "https://example.org/x"
    assert "official_url" not in places[1]
    city_json = B.make_city(d, "蒂宾根", (48.5216, 9.0576), [p["id"] for p in places])
    city = City.model_validate(city_json)
    assert city.tier == "light" and city.region == "suedwest-de" and city.local_language == "de"
    from app.library import _CITY_BBOX

    token = _CITY_BBOX.set((city.bbox, city.name_zh))
    try:
        built = [Place.model_validate(p) for p in places]
        theme = ThemePack.model_validate(B.make_theme("蒂宾根", [p.id for p in built])["themes"][0])
        lib = PlaceLibrary(city="tuebingen", places={p.id: p for p in built}, themes={theme.id: theme})
    finally:
        _CITY_BBOX.reset(token)
    assert all(p.data_status == "draft" and p.draft_source.wikidata for p in built)
    assert lib.theme_members("daytrip")
