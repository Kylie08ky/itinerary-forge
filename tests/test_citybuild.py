"""`app/citybuild/core.py`（规格 10）里不联网的部分：去重、去「一部分」、分主题、坐标核对、叠加精校、审阅表。"""

from __future__ import annotations

from datetime import date

import pytest

import app.citybuild.core as B
from app.cities import City
from app.library import _CITY_BBOX, Place, PlaceLibrary, ThemePack

TODAY = date(2026, 10, 29)
BBOX = {"lat": [39.65, 40.15], "lng": [116.05, 116.75]}


def cand(qid: str, name: str, links: int, lat: float = 39.9, lon: float = 116.4, cls: str = "Q570116") -> dict:
    return {"qid": qid, "name": name, "sitelinks": links, "lat": lat, "lon": lon, "cls": cls}


# --------------------------------------------------------------------------- #
# 清理候选
# --------------------------------------------------------------------------- #
def test_the_same_place_under_two_wikidata_items_keeps_the_better_known_one():
    out = B.dedupe_same_place([cand("Q1", "故宫", 105, 39.9163, 116.3972),
                               cand("Q2", "故宫博物院", 20, 39.9161, 116.3971),
                               cand("Q3", "天坛", 69, 39.88, 116.41)])
    assert [c["qid"] for c in out] == ["Q1", "Q3"]


def test_similar_names_far_apart_are_different_places():
    out = B.dedupe_same_place([cand("Q1", "中山公园", 20, 39.91, 116.39), cand("Q2", "中山公园南京", 12, 32.0, 118.8)])
    assert len(out) == 2


def test_a_part_of_another_candidate_is_dropped_but_a_part_of_something_outside_is_kept():
    cands = [cand("Q1", "故宫", 105), cand("Q2", "乾清宫", 13), cand("Q3", "石舫", 12), cand("Q4", "钟楼", 15)]
    parts = {"Q2": {"Q1"}, "Q3": {"Q999"}, "Q4": set()}
    assert [c["qid"] for c in B.drop_parts(cands, parts)] == ["Q1", "Q3", "Q4"]


def test_an_item_that_visitors_cannot_enter_is_dropped_by_the_exclude_list():
    cands = [cand("Q1", "中南海", 39), cand("Q2", "景山公园", 31)]
    assert [c["qid"] for c in B.drop_excluded(cands, {"Q1"})] == ["Q2"]


# --------------------------------------------------------------------------- #
# 坐标和分区
# --------------------------------------------------------------------------- #
def test_coordinates_agree_only_within_150_metres():
    assert B.coords_agree((39.9163, 116.3972), (39.9170, 116.3975)) is True       # 约 80 米
    assert B.coords_agree((39.9163, 116.3972), (39.9200, 116.3972)) is False      # 约 410 米
    assert B.coords_agree((39.9163, 116.3972), None) is False


def test_outside_the_city_box_is_a_day_trip():
    assert B.zone_for(39.9, 116.4, BBOX) == "core"
    assert B.zone_for(40.36, 116.02, BBOX) == "day_trip"


# --------------------------------------------------------------------------- #
# 主题
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("category, cls, want", [
    ("museum", "Q33506", "museum"), ("park", "Q22698", "nature"), ("activity", "Q43501", "family"),
    ("sight", "Q16560", "heritage"), ("sight", "Q44613", "religion"), ("sight", "Q174782", "district"),
    ("shopping", "", "district"), ("sight", "Q8502", "nature"), ("sight", "Q12280", "landmark"),
    ("food", "Q11707", "food"),
])
def test_the_category_decides_the_theme(category, cls, want):
    assert B.theme_for(category, cls) == want


def test_a_seed_can_override_the_theme():
    assert B.theme_for("sight", "Q570116", override="district") == "district"


def test_the_best_known_places_of_a_theme_are_anchors_and_the_rest_support():
    members = B.theme_members([f"p{i}" for i in range(9)])
    assert [m["role"] for m in members] == ["anchor"] * 6 + ["support"] * 3
    assert [m["weight"] for m in members] == [5, 5, 4, 4, 3, 3, 2, 2, 2]


# --------------------------------------------------------------------------- #
# 叠加精校
# --------------------------------------------------------------------------- #
OVERLAY = {"closed_weekdays": [0], "booking": {"required": True, "lead_time_hint": "提前 7 天 20:00 起订票，实名"},
           "ticketing": "online_required", "hours_verified": True,
           "sources": [{"url": "https://www.dpm.org.cn/", "checked_at": "2026-10-29", "what": "闭馆日、预约"}]}


def test_a_curated_overlay_replaces_the_unchecked_draft_values():
    draft = {"id": "forbidden-city", "closed_weekdays": [], "hours_verified": False, "booking": {"required": False},
             "ticketing": "none", "price_note": "待核对"}
    out = B.apply_overlay(draft, OVERLAY, TODAY)
    assert out["closed_weekdays"] == [0] and out["hours_verified"] is True
    assert out["booking"]["required"] is True and out["ticketing"] == "online_required"
    assert out["ticketing_checked_at"] == "2026-10-29"
    assert draft["closed_weekdays"] == []                                      # 不改原来的


def test_an_overlay_without_a_source_or_with_an_unknown_field_is_refused():
    with pytest.raises(ValueError, match="来源"):
        B.apply_overlay({"id": "x"}, {"closed_weekdays": [0]}, TODAY)
    with pytest.raises(ValueError, match="https"):
        B.apply_overlay({"id": "x"}, {"closed_weekdays": [0], "sources": [
            {"url": "http://x.cn", "checked_at": "2026-10-29", "what": "闭馆日"}]}, TODAY)
    with pytest.raises(ValueError, match="不允许"):
        B.apply_overlay({"id": "x"}, {**OVERLAY, "lat": 1.0}, TODAY)


# --------------------------------------------------------------------------- #
# 整体：造出的数据能通过地点库的校验
# --------------------------------------------------------------------------- #
def _raw(qid, name, en, cat, links, lat=39.9, lng=116.4, **kw):
    return {"qid": qid, "name_zh": name, "name_en": en, "category": cat, "setting": "outdoor", "minutes": 60,
            "open_access": False, "lat": lat, "lng": lng, "sitelinks": links, "cls": kw.pop("cls", "Q570116"),
            "theme": None, "zone": B.zone_for(lat, lng, BBOX), "osm": None, "coords": "wikidata", "url": None,
            "url_ok": False, "closed": None, "note": "", "area": "东城区", **kw}


def test_the_built_city_places_and_themes_pass_the_library_validation():
    raws = [_raw("Q1", "故宫", "Forbidden City", "sight", 105, cls="Q16560"),
            _raw("Q2", "国家博物馆", "National Museum of China", "museum", 45, cls="Q33506"),
            _raw("Q3", "景山公园", "Jingshan Park", "park", 31, cls="Q22698"),
            _raw("Q4", "八达岭", "Badaling", "sight", 30, 40.36, 116.02)]
    city = B.make_city("beijing", "北京", "Beijing", (39.9087, 116.3975), BBOX, ["forbidden-city"], today=TODAY)
    places, themes = B.build_data("beijing", "北京", raws, TODAY)
    cfg = City.model_validate(city)
    token = _CITY_BBOX.set((cfg.bbox, cfg.name_zh))                              # 校验坐标用这座城市的市区范围
    try:
        lib = PlaceLibrary(city="beijing", places={p["id"]: Place.model_validate(p) for p in places},
                           themes={t["id"]: ThemePack.model_validate(t) for t in themes["themes"]})
    finally:
        _CITY_BBOX.reset(token)
    assert {p.zone.value for p in lib.places.values()} == {"core", "day_trip"}
    assert set(lib.themes) == {"heritage", "museum", "nature"}                    # 没有成员的主题不建
    assert lib.places["forbidden-city"].data_status == "draft"
    assert "待核对" in (lib.places["forbidden-city"].price_note or "")


def test_the_city_config_is_a_standard_tier_chinese_city_that_is_not_supported_until_reviewed():
    city = B.make_city("beijing", "北京", "Beijing", (39.9087, 116.3975), BBOX, ["forbidden-city"], today=TODAY)
    c = City.model_validate(city)
    assert (c.tier, c.currency, c.timezone, c.dst, c.country, c.local_language) == (
        "standard", "CNY", "Asia/Shanghai", "none", "CN", "zh")
    assert c.supported is False and "beijing" in c.aliases and "北京" in c.aliases


def test_the_review_table_lists_every_place_with_what_was_and_was_not_checked():
    raws = [_raw("Q1", "故宫", "Forbidden City", "sight", 105, cls="Q16560", coords="osm", url="https://www.dpm.org.cn/",
                 url_ok=True, osm="way/1"),
            _raw("Q2", "景山公园", "Jingshan Park", "park", 31, cls="Q22698", coords="mismatch")]
    places, _themes = B.build_data("beijing", "北京", raws, TODAY)
    table = B.review_table("北京", places, raws, TODAY)
    assert "故宫" in table and "景山公园" in table
    assert "坐标已交叉核对" in table and "坐标差得多" in table
    assert "官网能打开" in table


def test_big_areas_get_a_looser_coordinate_limit_than_ordinary_places():
    assert B.coord_limit("sight", "Q16560") == 500 and B.coord_limit("park", "") == 500
    assert B.coord_limit("museum", "Q33506") == 150


# --------------------------------------------------------------------------- #
# 认目的地（app/citybuild/resolve.py）
# --------------------------------------------------------------------------- #
from app.citybuild import resolve as R  # noqa: E402


def rc(qid, label, links, kind="settlement", country="Q142", lat=48.85, lng=2.35, **kw):
    return R.Candidate(qid=qid, label=label, sitelinks=links, kind=kind, country_qid=country, lat=lat, lng=lng, **kw)


def test_the_best_known_city_wins_when_there_is_no_rival():
    out = R.decide([rc("Q90", "巴黎", 300), rc("Q1", "巴黎镇", 5, country="Q30")])
    assert out.kind == "city" and out.city.qid == "Q90"


def test_a_same_name_city_in_another_country_with_similar_fame_asks_the_user():
    out = R.decide([rc("Q90", "巴黎", 300), rc("Q830149", "巴黎（得克萨斯）", 200, country="Q30")])
    assert out.kind == "choices" and [c.qid for c in out.choices] == ["Q90", "Q830149"]


def test_a_country_or_region_asks_for_its_cities():
    out = R.decide([rc("Q17", "日本", 400, kind="country", country="")])
    assert out.kind == "country" and out.top.qid == "Q17"
    assert R.decide([rc("Q1", "托斯卡纳", 90, kind="region")]).kind == "country"


def test_things_that_are_not_places_or_have_no_coordinates_are_ignored_and_nothing_found_is_unknown():
    assert R.decide([rc("Q5", "某本书", 50, kind="other")]).kind == "unknown"
    assert R.decide([rc("Q6", "某城", 50, lat=None)]).kind == "unknown"
    assert R.decide([]).kind == "unknown"


def test_the_nearest_time_zone_of_the_country_is_picked():
    assert R.zone_for_country("JP", 34.69, 135.5) == "Asia/Tokyo"
    assert R.zone_for_country("FR", 48.85, 2.35) == "Europe/Paris"
    assert R.zone_for_country("US", 40.7, -74.0) == "America/New_York"
    assert R.zone_for_country("US", 34.05, -118.24) == "America/Los_Angeles"
    assert R.zone_for_country("XX", 0, 0) is None


def test_the_search_size_follows_the_population():
    assert R.size_plan(14_000_000)["top_n"] == 100 and R.size_plan(300_000)["radius_km"] == 20
    assert R.size_plan(20_000)["top_n"] == 25 and R.size_plan(None)["radius_km"] == 20


def test_a_made_seed_has_the_city_config_fields_and_a_box_around_the_centre():
    c = rc("Q35765", "大阪", 120, country="Q17", lat=34.69, lng=135.5)
    seed = R.make_seed(c, name_en="Osaka", name_zh="大阪", country_iso="JP", currency="JPY", language="ja",
                       timezone="Asia/Tokyo", population=2_700_000, aliases=["Osaka", "大阪", ""])
    assert seed["id"] == "osaka" and seed["currency"] == "JPY" and seed["timezone"] == "Asia/Tokyo"
    assert seed["bbox"]["lat"][0] < 34.69 < seed["bbox"]["lat"][1]
    assert seed["bbox"]["lng"][0] < 135.5 < seed["bbox"]["lng"][1]
    assert seed["aliases"] == ["osaka", "大阪"] and seed["top_n"] == 100


# --------------------------------------------------------------------------- #
# 繁体 → 简体
# --------------------------------------------------------------------------- #
def test_traditional_names_are_converted_through_the_wikipedia_variant_api_and_failures_keep_the_original():
    import httpx

    import app.citybuild.fetch as F

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        text = dict(x.split("=", 1) for x in request.content.decode().split("&"))["text"]
        calls.append(text)
        from urllib.parse import unquote_plus

        paras = unquote_plus(text).split("\n\n")
        fix = {"國立古代美術館": "国立古代美术馆", "羅西烏車站": "罗西乌车站"}
        html_ = "".join("<p>" + fix.get(p, p) + "</p>" for p in paras)
        return httpx.Response(200, json={"parse": {"text": html_}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = F.to_simplified(client, ["國立古代美術館", "羅西烏車站", "Rossio", ""], batch=12)
    assert out == {"國立古代美術館": "国立古代美术馆", "羅西烏車站": "罗西乌车站"} and len(calls) == 1
    raws = [{"name_zh": "國立古代美術館", "note": "", "x": 1}, {"name_zh": "Rossio", "note": "羅西烏車站"}]
    F.simplify_raws(client, raws)
    assert raws[0]["name_zh"] == "国立古代美术馆" and raws[1]["name_zh"] == "Rossio" and raws[1]["note"] == "罗西乌车站"
    broken = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert F.to_simplified(broken, ["國立古代美術館"]) == {}


def test_a_failed_wikidata_request_is_reported_without_the_url_and_admin_duplicates_are_not_candidates():
    import httpx

    request = httpx.Request("GET", "https://query.wikidata.org/sparql?query=%0ASELECT")
    err = httpx.HTTPStatusError("504", request=request, response=httpx.Response(504, request=request))
    assert R._why(err) == "服务返回 504" and "wikidata" not in R._why(err)
    assert R._why(httpx.ReadTimeout("x")) == "超时"
    assert R._is_admin_unit("巴勒莫廣域市") and R._is_admin_unit("佛罗伦萨大都会") and not R._is_admin_unit("东京都")


def test_an_entry_that_is_both_a_city_and_an_administrative_region_is_treated_as_a_city():
    assert R.kind_of({"settlement", "region"}) == "settlement"             # 上海、东京
    assert R.kind_of({"settlement", "country"}) == "settlement"            # 新加坡
    assert R.kind_of({"country"}) == "country" and R.kind_of({"region"}) == "region"
    assert R.kind_of({"country", "region"}) == "country" and R.kind_of(set()) == "other"


def test_a_candidate_whose_name_is_what_the_user_typed_beats_a_better_known_one_that_only_has_it_as_an_alias():
    china = rc("Q148", "中华人民共和国", 500, kind="country", country="")
    beijing = rc("Q956", "北京市", 305)
    out = R.decide([china, beijing], "北京")
    assert out.kind == "city" and out.city.qid == "Q956"
    assert R.decide([china, beijing]).kind == "country"                    # 没给输入时按名气


def test_the_wikipedia_intro_is_converted_to_simplified_too():
    """导出和地点卡会显示这段简介：以前只转了名字和备注，简介还是繁体（「位於中華人民共和國」）。"""
    from urllib.parse import unquote_plus

    import httpx

    import app.citybuild.fetch as F

    def handler(request: httpx.Request) -> httpx.Response:
        text = unquote_plus(dict(x.split("=", 1) for x in request.content.decode().split("&"))["text"])
        fix = {"東方明珠，位於中華人民共和國上海市。": "东方明珠，位于中华人民共和国上海市。"}
        body = "".join("<p>" + fix.get(p, p) + "</p>" for p in text.split("\n\n"))
        return httpx.Response(200, json={"parse": {"text": body}})

    raws = [{"name_zh": "东方明珠", "note": "", "intro": "東方明珠，位於中華人民共和國上海市。"},
            {"name_zh": "x", "note": ""}]
    F.simplify_raws(httpx.Client(transport=httpx.MockTransport(handler)), raws)
    assert raws[0]["intro"] == "东方明珠，位于中华人民共和国上海市。" and "intro" not in raws[1]


@pytest.mark.parametrize("area", ["Quartiere 1", "Municipio Roma I", "Municipio 1", "Circoscrizione 2"])
def test_administrative_boilerplate_is_not_shown_as_the_area(area):
    """意大利的行政区名（Quartiere 1、Municipio Roma I）对游客没有意义：区域写城市名（规格 12 S4）。"""
    places, _ = B.build_data("firenze", "佛罗伦萨", [_raw("Q1", "乌菲兹", "Uffizi", "museum", 50, area=area)], TODAY)
    assert places[0]["area"] == "佛罗伦萨"


def test_a_real_neighbourhood_name_is_kept():
    raws = [_raw("Q1", "乌菲兹", "Uffizi", "museum", 50, area="Oltrarno")]
    places, _ = B.build_data("firenze", "佛罗伦萨", raws, TODAY)
    assert places[0]["area"] == "Oltrarno"


def test_curated_names_replace_the_automatic_chinese_name():
    """种子的 names：维基数据的中文名不理想（鲍格才美术馆、没有中文名）时用手写的（规格 12 S4）。"""
    raws = [_raw("Q841506", "鲍格才美术馆", "Galleria Borghese", "museum", 50),
            _raw("Q9", "Torre Velasca", "Torre Velasca", "sight", 20)]
    B.apply_names(raws, {"names": {"Q841506": "博尔盖塞美术馆", "Q9": "韦拉斯卡塔楼"}})
    assert [r["name_zh"] for r in raws] == ["博尔盖塞美术馆", "韦拉斯卡塔楼"]
    assert raws[0]["name_en"] == "Galleria Borghese"


def test_curated_names_leave_other_places_alone():
    raws = [_raw("Q1", "乌菲兹", "Uffizi", "museum", 50)]
    B.apply_names(raws, {})
    assert raws[0]["name_zh"] == "乌菲兹"


def test_reapplying_overlays_updates_stored_places_without_rebuilding(tmp_path, monkeypatch):
    """改了精校（官网事实）后不必重新联网建城市：把精校重新叠到已存的地点上（规格 12）。"""
    import json

    from app.citybuild import build as BD
    from app.citybuild import store

    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path / "gen"))
    ov = tmp_path / "ov"
    ov.mkdir()
    monkeypatch.setattr(BD, "_OVERLAYS", ov)
    place = {"id": "uffizi", "closed_weekdays": [], "closed_dates": None, "hours_verified": False,
             "draft_source": {"wikidata": "Q51252"}}
    other = {"id": "x", "closed_weekdays": [], "hours_verified": False, "draft_source": {"wikidata": "Q1"}}
    store.save_generated({"id": "florence"}, [place, other], {"themes": []})
    src = [{"url": "https://www.uffizi.it/", "checked_at": "2026-11-03", "what": "闭馆日"}]
    fact = {"closed_weekdays": [0], "closed_dates": ["12-25"], "hours_verified": True, "sources": src}
    (ov / "florence.json").write_text(json.dumps({"places": {"Q51252": fact}}), encoding="utf-8")
    assert BD.reapply_overlays("florence", TODAY) == 1
    got = {p["id"]: p for p in json.loads(store.generated_path("places", "florence").read_text("utf-8"))["places"]}
    assert got["uffizi"]["closed_weekdays"] == [0] and got["uffizi"]["closed_dates"] == ["12-25"]
    assert got["uffizi"]["hours_verified"] is True
    assert got["x"]["hours_verified"] is False


def test_an_empty_query_never_becomes_an_alias():
    """从国家里挑的城市不带查询词：别名里不能有空串（否则空的目的地会认成这座城市）。"""
    bbox = {"min_lat": 0, "min_lng": 0, "max_lat": 1, "max_lng": 1}
    c = B.make_city("rome", "罗马", "Rome", (41.9, 12.5), bbox, [], today=date(2026, 1, 1),
                    seed={"aliases": ["", "  ", "罗马", "Rome"]})
    assert "" not in c["aliases"] and all(a.strip() for a in c["aliases"])


# --------------------------------------------------------------------------- #
# 泉州：开元寺、清净寺、天后宫这类寺庙清真寺要认得出；国内城市景点少也能建（缺的在高德里搜）
# --------------------------------------------------------------------------- #
def test_buddhist_temples_mosques_and_mazu_temples_count_as_sights():
    from app.citybuild.common import CLASS_TAGS, classify

    for qid in ("Q5393308", "Q32815", "Q44539", "Q7245816"):                  # 佛寺、清真寺、寺庙、妈祖庙
        assert qid in CLASS_TAGS, qid
        assert classify({**CLASS_TAGS[qid], "name": "开元寺"}) is not None


def test_a_mainland_city_with_few_open_data_sights_is_still_built(monkeypatch):
    """开放数据里国内小城的景点常常只有三五个：有高德可以搜，不再因为「名气太小」拒绝。"""
    import httpx

    from app.citybuild import build as BD

    raws = [{"qid": f"Q{i}", "name_zh": f"点{i}", "name_en": f"P{i}", "sitelinks": 20 - i, "lat": 24.9 + i / 1000,
             "lng": 118.58, "category": "sight", "setting": "outdoor", "minutes": 60, "open_access": True,
             "area": None, "zone": "core", "closed": [], "url": None, "url_ok": False, "coords": "wikidata"}
            for i in range(4)]
    monkeypatch.setattr(BD, "build_raws", lambda *_a, **_k: raws)
    monkeypatch.setattr(BD, "climate_stats", lambda *_a, **_k: {})
    seed = {"id": "xx", "name_zh": "某市", "name_en": "Xx", "center": [24.9, 118.58], "country": "CN",
            "bbox": {"lat": [24.6, 25.2], "lng": [118.3, 118.9]}, "radius_km": 30, "min_links": 10, "top_n": 40}
    BD.build_city(seed, httpx.Client(), today=date(2026, 11, 4), pause=0)           # 不抛错


def test_a_foreign_city_with_too_few_sights_is_still_refused(monkeypatch):
    import httpx

    from app.citybuild import build as BD

    monkeypatch.setattr(BD, "build_raws", lambda *_a, **_k: [])
    seed = {"id": "xx", "name_zh": "某市", "name_en": "Xx", "center": [48.0, 2.0], "country": "FR",
            "bbox": {"lat": [47.8, 48.2], "lng": [1.8, 2.2]}, "radius_km": 6, "min_links": 2, "top_n": 12}
    with pytest.raises(RuntimeError, match="只找到 0 个"):
        BD.build_city(seed, httpx.Client(), today=date(2026, 11, 4), pause=0)


def test_a_place_with_several_identities_takes_the_first_theme_in_the_priority_order():
    assert B.best_class(["Q10300916", "Q8502"]) == "Q8502"            # 文物保护单位 + 山 → 山水
    assert B.best_class(["Q5393308", "Q10300916"]) == "Q5393308"      # 佛寺 + 文物保护单位 → 寺庙
    assert B.best_class(["Q33506", "Q5393308"]) == "Q33506"
    assert B.best_class([]) is None


def test_a_theme_with_one_place_is_kept_so_that_balanced_can_cover_it(monkeypatch):
    entries = [{"id": "a", "theme": "religion", "zone": "core", "sitelinks": 14},
               {"id": "b", "theme": "religion", "zone": "core", "sitelinks": 9},
               {"id": "c", "theme": "nature", "zone": "core", "sitelinks": 11},
               {"id": "d", "theme": "heritage", "zone": "core", "sitelinks": 3}]
    by_id = {t["id"]: [m["place_id"] for m in t["members"]] for t in B.make_themes("x", "某城", entries)["themes"]}
    assert by_id == {"heritage": ["d"], "religion": ["a", "b"], "nature": ["c"]}      # 主题顺序固定，成员按名气排
    monkeypatch.setattr(B, "MIN_THEME_PLACES", 2)                                       # 要求至少 2 个：1 个的并进古迹
    merged = {t["id"]: [m["place_id"] for m in t["members"]] for t in B.make_themes("x", "某城", entries)["themes"]}
    assert merged == {"heritage": ["c", "d"], "religion": ["a", "b"]}
