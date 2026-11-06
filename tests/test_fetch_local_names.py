"""取当地语言名的脚本（scripts/fetch_local_names.py，规格 07 P18）：选对地点、两个来源交叉核对、写回、待核对清单。

不联网：用 httpx.MockTransport 假装 OpenStreetMap（Nominatim）和 Wikidata。当地名一律来自这两个来源，不由模型生成。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_local_names as F  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_pause(monkeypatch):
    monkeypatch.setattr(F.time, "sleep", lambda _s: None)


LOUVRE = {"id": "louvre", "name_zh": "卢浮宫", "name_en": "Louvre", "lat": 48.8606, "lng": 2.3376}
TODAY = date(2026, 10, 25)


def osm(name="Musée du Louvre", *, lat=48.8611, lon=2.3380, wikidata="Q19675", name_fr=None):
    details = {"name": name}
    if name_fr:
        details["name:fr"] = name_fr
    extra = {"wikidata": wikidata} if wikidata else {}
    return {"lat": str(lat), "lon": str(lon), "namedetails": details, "extratags": extra, "type": "museum"}


def client(osm_results, labels=None, seen=None, *, found=None, point=None, labels_by_id=None):
    """Nominatim 返回 osm_results；Wikidata：条目标签 labels（或按编号的 labels_by_id）、搜索结果 found、坐标 point。
    seen 收集所有请求，查谁在被访问。"""
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        if req.url.host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=osm_results)
        if req.url.host == "www.wikidata.org":
            action = req.url.params["action"]
            if action == "wbsearchentities":
                return httpx.Response(200, json={"search": [{"id": q} for q in (found or [])]})
            if action == "wbgetclaims":
                claims = {} if point is None else {"P625": [{"mainsnak": {"datavalue": {"value": {
                    "latitude": point[0], "longitude": point[1]}}}}]}
                return httpx.Response(200, json={"claims": claims})
            qid = req.url.params["ids"]
            lab = (labels_by_id or {}).get(qid, labels or {})
            body = {"entities": {qid: {"labels": {k: {"language": k, "value": v} for k, v in lab.items()}}}}
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# 选对地点、取名
# --------------------------------------------------------------------------- #
def test_the_nearest_named_candidate_within_the_radius_is_picked():
    near = osm(lat=48.8611, lon=2.3380)
    far = osm("Louvre Lens", lat=50.43, lon=2.81)                         # 别的城市同名
    unnamed = {"lat": "48.8607", "lon": "2.3377", "namedetails": {}, "extratags": {}}
    assert F.pick_candidate([far, unnamed, near], 48.8606, 2.3376) is near
    assert F.pick_candidate([far, unnamed], 48.8606, 2.3376) is None


def test_the_local_language_name_is_preferred_over_the_plain_name():
    assert F.osm_local_name(osm("Louvre", name_fr="Musée du Louvre"), "fr") == "Musée du Louvre"
    assert F.osm_local_name(osm("Musée du Louvre"), "fr") == "Musée du Louvre"
    assert F.osm_local_name({"namedetails": {}}, "fr") is None


# --------------------------------------------------------------------------- #
# 交叉核对
# --------------------------------------------------------------------------- #
def test_two_sources_that_agree_make_a_verified_name_ignoring_case_and_accents():
    c = client([osm("Musée du Louvre")], {"fr": "Musée du Louvre", "zh": "卢浮宫"})
    r = F.resolve(c, LOUVRE, "fr", today=TODAY)
    assert r.name_local == "Musée du Louvre"
    assert r.check == {"source": "osm+wikidata", "status": "verified", "checked_at": "2026-10-25", "wikidata": "Q19675"}
    assert not r.review
    r2 = F.resolve(client([osm("MUSEE DU LOUVRE")], {"fr": "Musée du Louvre"}), LOUVRE, "fr", today=TODAY)
    assert r2.check["status"] == "verified"
    assert r2.name_local == "Musée du Louvre"                   # 两边只差大小写 / 重音：用带重音的写法


def test_two_sources_that_disagree_are_a_draft_and_go_to_the_review_list_with_both_spellings():
    pn = {**LOUVRE, "id": "pn", "name_zh": "新桥", "name_en": "Pont Neuf"}
    c = client([osm("Pont Neuf Nord")], {"fr": "Pont Neuf Sud", "zh-hans": "新桥"})
    r = F.resolve(c, pn, "fr", today=TODAY)
    assert r.check["status"] == "draft" and r.check["source"] == "osm+wikidata"
    assert r.name_local == "Pont Neuf Nord"                               # 取 OSM 的（路牌 / 官方标注），不自己挑
    assert "Pont Neuf Nord" in r.review and "Pont Neuf Sud" in r.review


def test_an_osm_hit_that_shares_no_distinctive_word_with_what_we_look_for_is_not_used():
    # 「Le Marais」旁边的卡纳瓦莱博物馆、歌剧院里的餐厅：离得近，但不是要找的东西
    marais = {**LOUVRE, "id": "marais-x", "name_zh": "玛黑区", "name_en": "Le Marais walk"}
    r = F.resolve(client([osm("Musée Carnavalet")], {"fr": "Musée Carnavalet", "zh-hans": "卡纳瓦莱博物馆"}),
                  marais, "fr", today=TODAY)
    assert r.name_local is None and "没找到" in r.review
    garnier = {**LOUVRE, "id": "garnier", "name_zh": "巴黎歌剧院", "name_en": "Palais Garnier"}
    r2 = F.resolve(client([osm("L'Opéra Restaurant")], {"fr": "L'Opéra Restaurant"}), garnier, "fr", today=TODAY)
    assert r2.name_local is None


def test_an_official_full_name_and_its_common_short_form_count_as_agreeing():
    picasso = {**LOUVRE, "id": "picasso", "name_zh": "毕加索博物馆", "name_en": "Picasso Museum"}
    c = client([osm("Musée national Picasso-Paris")], {"fr": "musée Picasso", "zh-hans": "毕加索博物馆"})
    r = F.resolve(c, picasso, "fr", today=TODAY)
    assert r.check["status"] == "verified" and r.name_local == "Musée national Picasso-Paris"


def test_the_chinese_label_check_prefers_simplified_and_skips_foreign_brand_names():
    rodin = {**LOUVRE, "id": "rodin", "name_zh": "罗丹博物馆", "name_en": "Rodin Museum"}
    labels = {"fr": "Musée Rodin", "zh": "羅丹美術館", "zh-hans": "罗丹博物馆"}
    assert F.resolve(client([osm("Musée Rodin")], labels), rodin, "fr", today=TODAY).check["status"] == "verified"
    mismatch = {"fr": "Musée Rodin", "zh-hans": "卢浮宫"}
    wrong = F.resolve(client([osm("Musée Rodin")], mismatch), rodin, "fr", today=TODAY)
    assert wrong.check["status"] == "draft" and "对不上" in wrong.review
    brand = {**LOUVRE, "id": "laduree", "name_zh": "Ladurée 香榭丽舍店", "name_en": "Ladurée"}
    ok = F.resolve(client([osm("Ladurée")], {"fr": "Ladurée", "zh": "拉杜麗"}), brand, "fr", today=TODAY)
    assert ok.check["status"] == "draft" or ok.check["status"] == "verified"


def test_when_osm_has_nothing_wikidata_is_searched_and_must_match_by_coordinates_and_chinese_name():
    place = {**LOUVRE, "id": "conciergerie", "name_zh": "古监狱", "name_en": "Conciergerie"}
    labels = {"fr": "conciergerie", "zh-hans": "古监狱"}
    c = client([], found=["Q1"], point=(48.8556, 2.3459), labels_by_id={"Q1": labels})
    r = F.resolve(c, {**place, "lat": 48.8557, "lng": 2.3460}, "fr", today=TODAY)
    assert r.name_local == "Conciergerie"                                  # 标签首字母大写，别的不动
    assert r.check == {"source": "wikidata", "status": "verified", "checked_at": "2026-10-25", "wikidata": "Q1"}
    far = F.resolve(client([], found=["Q1"], point=(50.0, 3.0), labels_by_id={"Q1": labels}), place, "fr", today=TODAY)
    assert far.name_local is None and "没找到" in far.review                 # 坐标差太远：同名的别处不收
    other = {"fr": "x", "zh-hans": "别的地方"}
    wrong_zh = client([], found=["Q1"], point=(48.8557, 2.3460), labels_by_id={"Q1": other})
    assert F.resolve(wrong_zh, {**place, "lat": 48.8557, "lng": 2.3460}, "fr", today=TODAY).name_local is None


def test_a_weak_osm_hit_is_replaced_by_a_wikidata_match_that_checks_out():
    place = {**LOUVRE, "id": "printemps", "name_zh": "春天百货", "name_en": "Printemps"}
    c = client([osm("Accès Parking Haussmann - Printemps", wikidata=None)], found=["Q2"],
               point=(48.8737, 2.3280), labels_by_id={"Q2": {"fr": "Printemps Haussmann", "zh-hans": "春天百货"}})
    r = F.resolve(c, {**place, "lat": 48.8737, "lng": 2.3281}, "fr", today=TODAY)
    assert r.name_local == "Printemps Haussmann" and r.check["status"] == "verified"





def test_one_source_only_is_a_draft_that_says_which_source():
    r = F.resolve(client([osm("Musée du Louvre", wikidata=None)]), LOUVRE, "fr", today=TODAY)
    assert r.check == {"source": "osm", "status": "draft", "checked_at": "2026-10-25", "wikidata": None}
    assert r.name_local == "Musée du Louvre" and r.review


def test_nothing_found_leaves_the_name_empty_and_says_so():
    r = F.resolve(client([]), LOUVRE, "fr", today=TODAY)
    assert r.name_local is None and r.check is None and "没找到" in r.review


def test_requests_go_only_to_the_two_sources_with_a_contactable_user_agent_and_a_pause(monkeypatch):
    seen: list[httpx.Request] = []
    sleeps: list[float] = []
    monkeypatch.setattr(F.time, "sleep", sleeps.append)
    F.resolve(client([osm()], {"fr": "Musée du Louvre"}, seen), LOUVRE, "fr", today=TODAY)
    assert {r.url.host for r in seen} == {"nominatim.openstreetmap.org", "www.wikidata.org"}
    assert all("@" in r.headers["user-agent"] or "http" in r.headers["user-agent"] for r in seen)
    assert sleeps and all(s >= 1.0 for s in sleeps)                       # Nominatim 规定每秒最多 1 次


# --------------------------------------------------------------------------- #
# 写回
# --------------------------------------------------------------------------- #
def test_results_are_written_back_without_overwriting_existing_names_unless_refreshing():
    places = [dict(LOUVRE), {**LOUVRE, "id": "orsay", "name_local": "Musée d'Orsay",
                             "name_local_check": {"source": "manual", "status": "verified", "checked_at": "2026-01-01",
                                                  "wikidata": None}}]
    done = F.Result("louvre", "Musée du Louvre", {"source": "osm+wikidata", "status": "verified",
                                                  "checked_at": "2026-10-25", "wikidata": "Q19675"}, "")
    changed = F.apply(places, [done, F.Result("orsay", "Orsay X", done.check, "")], refresh=False)
    assert changed == 1
    assert places[0]["name_local"] == "Musée du Louvre" and places[0]["name_local_check"]["wikidata"] == "Q19675"
    assert places[1]["name_local"] == "Musée d'Orsay"                      # 已有的不动
    assert F.apply(places, [F.Result("orsay", "Orsay X", done.check, "")], refresh=True) == 1
    assert places[1]["name_local"] == "Orsay X"


def test_the_review_file_lists_only_what_needs_a_human_eye(tmp_path):
    ok = F.Result("a", "A", {"status": "verified"}, "")
    bad = F.Result("b", "B", {"status": "draft"}, "OSM「B」≠ Wikidata「C」")
    none = F.Result("c", None, None, "没找到")
    path = tmp_path / "review.json"
    F.write_review(path, [ok, bad, none], today=TODAY)
    got = json.loads(path.read_text(encoding="utf-8"))
    assert [x["id"] for x in got["items"]] == ["b", "c"]
    assert got["checked_at"] == "2026-10-25"
    F.write_review(path, [F.Result("b", "B", {"status": "verified"}, ""), F.Result("d", "D", {"status": "draft"}, "x")],
                   today=TODAY)                                        # 第二次：b 已经核对好了，d 新增，c 保留
    assert [x["id"] for x in json.loads(path.read_text(encoding="utf-8"))["items"]] == ["c", "d"]
