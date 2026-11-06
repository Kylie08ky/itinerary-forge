"""P18 每个景点「中文名 · 当地语言名」（规格 07 P18）：数据模型、显示规则、搜索、行程 / 导出里的当地名。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.cities import load_city
from app.exports import render_ics, render_kml
from app.generator import generate_from_theme
from app.library import Place, load_library
from app.names import bilingual, fold, item_location, local_label, localize_itinerary
from app.renderer import render_markdown
from app.schemas import TravelRequest

LIB = load_library("paris")
CHECK = {"source": "osm+wikidata", "status": "verified", "checked_at": "2026-10-25", "wikidata": "Q19675"}


def _place(**over) -> Place:
    base = dict(id="x-place", name_zh="测试点", name_en="Test", category="sight", setting="outdoor", area="1区",
                lat=48.86, lng=2.34, duration_min=60)
    base.update(over)
    return Place.model_validate(base)


def request(days=3) -> TravelRequest:
    start = date(2026, 11, 14)
    return TravelRequest(destination="Paris", start_date=start, end_date=start + timedelta(days=days - 1),
                         budget_level="medium", travel_style="balanced", interests=["art"])


# --------------------------------------------------------------------------- #
# 数据模型：当地名是事实，要带核对记录
# --------------------------------------------------------------------------- #
def test_the_city_says_which_language_is_local():
    assert load_city("paris").local_language == "fr"


def test_a_local_name_must_carry_its_check_record():
    with pytest.raises(ValidationError, match="name_local_check"):
        _place(name_local="Musée du Louvre")
    p = _place(name_local="Musée du Louvre", name_local_check=CHECK)
    assert p.name_local == "Musée du Louvre" and p.name_local_check.status == "verified"


def test_the_check_record_has_a_known_source_and_a_wikidata_id_shape():
    with pytest.raises(ValidationError):
        _place(name_local="X", name_local_check={**CHECK, "source": "chatgpt"})           # 不由模型生成
    with pytest.raises(ValidationError):
        _place(name_local="X", name_local_check={**CHECK, "wikidata": "louvre"})


def test_a_place_may_have_no_local_name_yet_old_data_stays_valid():
    p = _place()
    assert p.name_local is None and p.name_local_check is None


# --------------------------------------------------------------------------- #
# 显示规则
# --------------------------------------------------------------------------- #
def test_fold_ignores_case_accents_and_punctuation():
    assert fold("Musée d'Orsay") == fold("MUSEE D’ORSAY") == "museedorsay"
    assert fold("Sacré-Cœur") == "sacrecoeur"
    assert fold("卢浮宫") == "卢浮宫"


def test_the_pair_is_shown_unless_the_local_name_adds_nothing():
    louvre = _place(name_zh="卢浮宫", name_en="Louvre", name_local="Musée du Louvre", name_local_check=CHECK)
    assert local_label(louvre) == "Musée du Louvre"
    assert bilingual(louvre) == "卢浮宫 · Musée du Louvre"
    same = _place(name_zh="Pompei", name_local="POMPEI", name_local_check=CHECK)      # 和中文名一样（忽略大小写）
    assert local_label(same) is None and bilingual(same) == "Pompei"
    none = _place(name_zh="卢浮宫")
    assert local_label(none) is None and bilingual(none) == "卢浮宫"


def test_the_itinerary_second_line_uses_the_local_name_and_falls_back_to_english():
    with_local = _place(name_en="Louvre", name_local="Musée du Louvre", name_local_check=CHECK, area="1区")
    assert item_location(with_local) == "Musée du Louvre · 1区"
    assert item_location(_place(name_en="Louvre", area="1区")) == "Louvre · 1区"


# --------------------------------------------------------------------------- #
# 真实数据：巴黎每个点都有法语名和核对记录
# --------------------------------------------------------------------------- #
# 没有官方名称、OSM / 维基数据里也查不到的点：不取当地名（当地名不由模型编），页面只显示中文名，行程第二行回退英文名
NO_LOCAL_NAME = {
    "seine-cruise": "游船不是一个有名字的地点",
    "st-germain-boutiques": "一条逛街路线，没有官方名称",
    "marais-walk": "一条街区散步路线：OSM 的街区对象离散步起点太远，旁边的博物馆不是它",
    "galerie-dior": "OSM 和维基数据里查不到这间展馆",
    "enfants-rouges": "OSM 在该坐标只有市场里的一家咖啡馆，已排除（scripts/fetch_local_names.py 的 REJECT）",
}


def test_every_paris_place_has_a_french_name_with_a_check_record_except_the_listed_few():
    missing = {p.id for p in LIB.places.values() if not p.name_local}
    assert missing == set(NO_LOCAL_NAME), f"没有当地名：{sorted(missing)}"
    assert all(p.name_local_check for p in LIB.places.values() if p.name_local)
    assert {p.name_local_check.source for p in LIB.places.values() if p.name_local} <= {
        "osm", "wikidata", "osm+wikidata", "manual"}


def test_most_paris_names_are_cross_checked_and_the_rest_are_marked_draft():
    named = [p for p in LIB.places.values() if p.name_local]
    verified = sum(p.name_local_check.status == "verified" for p in named)
    assert verified / len(named) >= 0.8, f"只有 {verified}/{len(named)} 个交叉核对一致"
    for p in named:
        assert p.name_local_check.checked_at.year == 2026


def test_the_louvre_reads_as_the_example_in_the_spec():
    assert bilingual(LIB.places["louvre"]) == "卢浮宫 · Musée du Louvre"


# --------------------------------------------------------------------------- #
# 搜索：当地名也能搜到（忽略重音和标点）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("query,pid", [
    ("Musée du Louvre", "louvre"),
    ("musee d orsay", "orsay"),
    ("MUSÉE D’ORSAY", "orsay"),
    ("tour eiffel", "eiffel-tower"),
])
def test_search_finds_a_place_by_its_local_name(query, pid):
    assert pid in [p.id for p in LIB.search(query)][:3]


# --------------------------------------------------------------------------- #
# 行程 / 导出
# --------------------------------------------------------------------------- #
def _generate():
    return generate_from_theme(request(), "museums", client=None, library=LIB).itinerary


def test_new_itineraries_write_the_local_name_on_the_second_line():
    it = _generate()
    seen = 0
    for d in it.days:
        for i in d.items:
            if i.place_id and i.place_id in LIB.places and i.location.endswith(" · " + LIB.places[i.place_id].area):
                assert i.location == item_location(LIB.places[i.place_id])
                seen += 1
    assert seen >= 4


def test_an_old_itinerary_gets_local_names_when_opened_without_touching_the_original():
    it = _generate()
    old = it.model_copy(deep=True)
    for d in old.days:
        for i in d.items:
            if i.place_id in LIB.places:
                i.location = f"{LIB.places[i.place_id].name_en} · {LIB.places[i.place_id].area}"     # 老格式：英文名
    upgraded = localize_itinerary(old, LIB)
    pairs = [(a, b) for da, db in zip(old.days, upgraded.days, strict=True)
             for a, b in zip(da.items, db.items, strict=True)]
    placed = [(a, b) for a, b in pairs if a.place_id in LIB.places]
    assert placed and all(b.location == item_location(LIB.places[a.place_id]) for a, b in placed)
    assert all(a.location.startswith(LIB.places[a.place_id].name_en) for a, _ in placed)     # 原件没被改
    free = [(a, b) for a, b in pairs if not a.place_id]
    assert all(a.location == b.location for a, b in free)                                    # 午餐 / 交通这类不动


def test_a_hand_edited_location_is_left_alone():
    it = _generate()
    item = next(i for d in it.days for i in d.items if i.place_id in LIB.places)
    item.location = "我自己改的地点说明"
    after = localize_itinerary(it, LIB)
    got = next(i for d in after.days for i in d.items if i.place_id == item.place_id and i.time == item.time)
    assert got.location == "我自己改的地点说明"


def test_the_calendar_location_carries_both_names():
    it = _generate()
    ics = render_ics(it, uid_prefix="p", library=LIB, now=datetime(2026, 10, 1, tzinfo=UTC))
    text = ics.replace("\r\n ", "")
    placed = [i.place_id for d in it.days for i in d.items if i.place_id in LIB.places]
    pid = next(p for p in placed if LIB.places[p].address)
    assert bilingual(LIB.places[pid]).replace(",", "\\,") in text or bilingual(LIB.places[pid]) in text


def test_the_map_file_names_every_stop_with_both_names():
    it = _generate()
    kml = render_kml(it)
    pids = {i.place_id for d in it.days for i in d.items if i.place_id in LIB.places}
    pairs = [bilingual(LIB.places[p]) for p in pids]
    assert any("·" in p for p in pairs)
    assert all(p.replace("&", "&amp;") in kml for p in pairs)


def test_the_markdown_export_shows_both_names_in_the_schedule_table():
    it = _generate()
    md = render_markdown(it, request())
    placed = [i.place_id for d in it.days for i in d.items if i.place_id in LIB.places]
    pid = next(p for p in placed if local_label(LIB.places[p]))
    assert bilingual(LIB.places[pid]) in md


# --------------------------------------------------------------------------- #
# 老行程：打开时换成当地名，已保存的版本不改
# --------------------------------------------------------------------------- #
def test_opening_a_plan_saved_with_english_second_lines_shows_local_names_and_keeps_the_stored_version(tmp_path):
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.versions import PlanStore

    req = request()
    it = _generate()
    for d in it.days:
        for i in d.items:
            if i.place_id in LIB.places:
                i.location = f"{LIB.places[i.place_id].name_en} · {LIB.places[i.place_id].area}"       # 老格式
    store = PlanStore(root=tmp_path / "plans")
    pid = store.create(req, it, note="old")
    client = TestClient(create_app(store=store))
    got = client.get(f"/plans/{pid}").json()["itinerary"]
    pairs = [(i["place_id"], i["location"]) for d in got["days"] for i in d["items"] if i.get("place_id") in LIB.places]
    assert pairs and all(loc == item_location(LIB.places[p]) for p, loc in pairs)
    stored = store.load(pid)
    assert any(i.location.startswith(LIB.places[i.place_id].name_en) for d in stored.days for i in d.items
               if i.place_id in LIB.places)                                               # 存的还是老样子


def test_the_route_data_carries_the_local_name_for_map_bubbles():
    from app.geo import build_route

    route = build_route(_generate(), LIB).as_dict()
    stops = [s for d in route["days"] for s in d["stops"]]
    assert stops and any(s["name_local"] for s in stops)
    for s in stops:
        assert s["name_local"] == local_label(LIB.places[s["place_id"]])


# --------------------------------------------------------------------------- #
# 目的地的官方语言就是中文（国内）：只显示中文，不再带英文名
# --------------------------------------------------------------------------- #
def test_a_domestic_place_shows_only_the_chinese_name_on_the_second_line():
    kaiyuan = _place(name_zh="开元寺", name_en="Kaiyuan Temple", area="鲤城区")
    assert item_location(kaiyuan, "zh") == "开元寺 · 鲤城区"
    assert item_location(kaiyuan) == "Kaiyuan Temple · 鲤城区"                     # 没说语言：老规则不变（国外）
    assert item_location(kaiyuan, "fr") == "Kaiyuan Temple · 鲤城区"


def test_a_domestic_trip_has_no_english_in_the_item_locations_and_old_ones_are_upgraded(monkeypatch, tmp_path):
    from datetime import date

    from app.cities import reload_cities
    from app.generator import generate_from_theme
    from app.library import load_library
    from app.schemas import TravelRequest

    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path))
    reload_cities()
    from city_fixtures import write_small_city

    write_small_city("kunming", "昆明", "Kunming", (25.0389, 102.7183), "CN", "Asia/Shanghai", "CNY",
                     date(2026, 10, 31))
    lib = load_library("kunming")
    req = TravelRequest(destination="昆明", start_date="2026-11-07", end_date="2026-11-08", budget_level="medium",
                        travel_style="balanced", interests=["均衡"])
    it = generate_from_theme(req, next(iter(lib.themes)), client=None, library=lib).itinerary
    placed = [i for d in it.days for i in d.items if i.place_id in lib.places]
    assert placed and all(i.location == f"{lib.places[i.place_id].name_zh} · {lib.places[i.place_id].area}"
                          for i in placed)
    old = it.model_copy(deep=True)
    for d in old.days:
        for i in d.items:
            if i.place_id in lib.places:
                i.location = f"{lib.places[i.place_id].name_en} · {lib.places[i.place_id].area}"
    new = localize_itinerary(old, lib)
    assert all(b.location == a.location for da, db in zip(it.days, new.days, strict=True)
               for a, b in zip(da.items, db.items, strict=True))
    reload_cities()
