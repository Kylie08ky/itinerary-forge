"""从维基数据批量生成的作品（规格 09 十五，B）：纯函数、数据闸门、和人工策展的合并。不联网。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.culture as culture
from app.culture import AUTO_SUFFIX, KINDS, MIN_POPULARITY, CultureError, CultureFile, load_culture
from app.library import load_library
from scripts.build_culture_auto import BOTH, NOTE, claim_ids, claim_year, kind_of, slug

DATA = Path(culture._DIR)
AUTO_FILES = sorted(DATA.glob(f"*{AUTO_SUFFIX}.json"))


def ent(**claims) -> dict:
    return {"claims": {k: [{"mainsnak": {"datavalue": {"value": v}}} for v in vs] for k, vs in claims.items()}}


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_the_kind_comes_from_what_the_item_is_and_series_or_games_are_skipped():
    assert kind_of(["Q11424"]) == "film" and kind_of(["Q202866"]) == "film"
    assert kind_of(["Q93204", "Q11424"]) == "documentary"                    # 纪录片优先于电影
    assert kind_of(["Q7725634"]) == "book" and kind_of(["Q8261"]) == "book"
    assert kind_of(["Q5398426"]) is None and kind_of(["Q7889"]) is None      # 电视剧、游戏不收
    assert kind_of([]) is None


def test_claims_and_years():
    e = ent(P57=[{"id": "Q1"}, {"id": "Q2"}])
    assert claim_ids(e, "P57") == ["Q1", "Q2"] and claim_ids(e, "P50") == []
    dates = ent(P577=[{"time": "+2011-05-11T00:00:00Z"}, {"time": "+2010-01-01T00:00:00Z"}])
    assert claim_year(dates) == 2010                                         # 多个日期取最早的
    assert claim_year(ent(P571=[{"time": "+1938-01-01T00:00:00Z"}])) == 1938
    assert claim_year({}) is None


def test_ids_are_ascii_slugs_with_the_year_and_fall_back_to_the_wikidata_id():
    assert slug("Hôtel du Nord", 1938, "Q1") == "hotel-du-nord-1938"
    assert slug("Mission: Impossible – Fallout", 2018, "Q2") == "mission-impossible-fallout-2018"
    assert slug("盗梦空间", 2010, "Q25188") == "q25188"


# --------------------------------------------------------------------------- #
# 数据闸门：已生成的 <城市>.auto.json
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", AUTO_FILES, ids=lambda p: p.stem)
def test_every_auto_work_is_valid_sourced_famous_and_uses_only_the_fixed_sentences(path):
    data = CultureFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    lib = load_library(data.city)
    assert data.works
    for w in data.works:
        assert w.wikidata and w.popularity >= MIN_POPULARITY, w.id
        assert w.summary == "", w.id  # 自动收录的不写简介（页面上不重复导演和年份）
        assert w.kind in KINDS and w.source.url.startswith("https://en.wikipedia.org/wiki/"), w.id
        assert all(s.place_id in lib.places and s.note in {*NOTE.values(), BOTH} for s in w.sees), w.id


def test_auto_works_never_duplicate_a_curated_one():
    curated = json.loads((DATA / "paris.json").read_text(encoding="utf-8"))["works"]
    auto = json.loads((DATA / f"paris{AUTO_SUFFIX}.json").read_text(encoding="utf-8"))["works"]
    curated_ids = {w["wikidata"] for w in curated}
    merged = load_culture("paris")
    assert len(merged) == len(curated) + sum(1 for w in auto if w["wikidata"] not in curated_ids)
    assert {w["id"] for w in curated} <= set(merged)                          # 人工策展的都在


def test_the_merge_prefers_the_curated_work_with_the_same_wikidata_id(tmp_path, monkeypatch):
    base = {"kind": "film", "creator": "A B", "year": 2000, "summary": "一部讲巴黎故事的电影，看它可以了解巴黎。",
            "source": {"title": "x", "url": "https://example.org/x", "checked_at": "2026-10-27"}}
    (tmp_path / "paris.json").write_text(json.dumps({"city": "paris", "works": [
        {**base, "id": "hand", "title": "Hand", "wikidata": "Q1", "popularity": 30,
         "sees": [{"place_id": "louvre", "note": "手工写的说明。"}]}]}), encoding="utf-8")
    (tmp_path / f"paris{AUTO_SUFFIX}.json").write_text(json.dumps({"city": "paris", "works": [
        {**base, "id": "auto-dup", "title": "Dup", "wikidata": "Q1", "popularity": 90,
         "sees": [{"place_id": "orsay", "note": "拍摄地点之一。"}]},
        {**base, "id": "auto-new", "title": "New", "wikidata": "Q2", "popularity": 80,
         "sees": [{"place_id": "orsay", "note": "拍摄地点之一。"}]}]}), encoding="utf-8")
    monkeypatch.setattr(culture, "_DIR", tmp_path)
    load_culture.cache_clear()
    try:
        assert set(load_culture("paris")) == {"hand", "auto-new"}
        assert [s["work_id"] for s in culture.stories_for_place("paris", "orsay")] == ["auto-new"]
    finally:
        load_culture.cache_clear()


def test_a_work_below_the_popularity_bar_is_refused_when_loading(tmp_path, monkeypatch):
    base = {"kind": "film", "creator": "A B", "year": 2000, "summary": "一部讲巴黎故事的电影，看它可以了解巴黎。",
            "source": {"title": "x", "url": "https://example.org/x", "checked_at": "2026-10-27"}}
    (tmp_path / "paris.json").write_text(json.dumps({"city": "paris", "works": [
        {**base, "id": "obscure", "title": "Obscure", "wikidata": "Q9", "popularity": MIN_POPULARITY - 1,
         "sees": [{"place_id": "louvre", "note": "拍摄地点之一。"}]}]}), encoding="utf-8")
    monkeypatch.setattr(culture, "_DIR", tmp_path)
    load_culture.cache_clear()
    try:
        with pytest.raises(CultureError, match="名气不够"):
            load_culture("paris")
    finally:
        load_culture.cache_clear()
