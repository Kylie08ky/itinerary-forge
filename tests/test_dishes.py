"""规格 10 H22：现建城市的当地名菜（场景 M45–M47）。"""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date

import pytest
from city_fixtures import write_small_city

from app.cities import reload_cities
from app.citybuild import dishes as dishes_mod
from app.citybuild.dishes import MAX_DISHES, build_dishes, is_dish_extract, pick_subcategories, rank
from app.citybuild.store import save_dishes
from app.food import food_overview, load_generated_dishes

TODAY = date(2026, 11, 4)


def test_only_extracts_that_say_it_is_food_count():
    assert is_dish_extract("土笋冻，是闽南著名海鲜类小吃，起源并盛产于闽南。制作原料是星虫。")
    assert is_dish_extract("咯摊， 是中餐的一种。以猪骨汤作底，加入猪肉等食材煮熟。")      # 第一句没有食物字眼，第二句有
    assert not is_dish_extract("1.5支路街区是成都的一处商业街区，有多家餐厅。")                    # 街区不是菜
    assert not is_dish_extract("古罗马饮食文化在古罗马兴盛的时代始终在变化。")                       # 古代饮食史不是现在吃什么
    assert not is_dish_extract("")


def test_only_dish_like_subcategories_are_read():
    subs = ["Category:泉州小吃", "Category:天津食品公司", "Category:天津白酒", "Category:泉州餐厅", "Category:川菜名菜"]
    assert pick_subcategories(subs) == ["Category:泉州小吃", "Category:川菜名菜"]


def test_dishes_are_ranked_by_fame_then_views_and_capped():
    rows = [{"name_zh": f"菜{i}", "sitelinks": i % 4, "views": i} for i in range(30)]
    top = rank(rows)
    assert len(top) == MAX_DISHES
    assert [d["sitelinks"] for d in top] == sorted((d["sitelinks"] for d in top), reverse=True)
    same = [d for d in top if d["sitelinks"] == 3]
    assert [d["views"] for d in same] == sorted((d["views"] for d in same), reverse=True)


class _Fetcher:
    """不联网的取数据：分类里 3 个条目（一个不是食物），名气 / 浏览量固定。"""

    c = None

    def wikidata(self, ids, langs):
        return {q: {"sitelinks": {"zhwiki": {}, "enwiki": {}}} for q in ids}

    def zh_extract(self, title):
        return {"土笋冻": "土笋冻，是闽南著名海鲜类小吃。原料是星虫，冻成后蘸酱食用。",
                "清源山": "清源山位于泉州市，是国家级风景名胜区，有老君岩等景点。"}.get(title, "")


@pytest.fixture
def fake_net(monkeypatch):
    def members(f, cat, kind):
        return ["土笋冻", "清源山"] if cat == "Category:泉州小吃" and kind == "page" else []

    monkeypatch.setattr(dishes_mod, "_members", members)
    monkeypatch.setattr(dishes_mod, "_wikibase_ids", lambda f, titles: {t: f"Q{i}" for i, t in enumerate(titles, 1)})
    monkeypatch.setattr(dishes_mod, "fetch_views", lambda c, title, today: 1234)
    monkeypatch.setattr(dishes_mod, "to_simplified", lambda c, strings: {s: s for s in strings})


def test_a_city_with_a_category_gets_its_dishes_with_a_source_link(fake_net):
    out = build_dishes(_Fetcher(), "泉州", TODAY)
    assert out["category"] == "泉州小吃" and [d["name_zh"] for d in out["dishes"]] == ["土笋冻"]   # 清源山不是食物
    assert out["category_note"] == "取自中文维基百科「泉州小吃」分类"
    d = out["dishes"][0]
    assert d["source"]["url"].startswith("https://zh.wikipedia.org/wiki/") and d["source"]["checked_at"] == "2026-11-04"
    assert d["sitelinks"] == 2 and d["views"] == 1234 and d["id"].startswith("dish-")


def test_a_city_without_a_category_has_no_dishes(monkeypatch):
    monkeypatch.setattr(dishes_mod, "_members", lambda f, cat, kind: [])
    assert build_dishes(_Fetcher(), "某城", TODAY) is None


@pytest.fixture
def small_city(tmp_path, monkeypatch):
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path))
    reload_cities()
    write_small_city("kunming", "昆明", "Kunming", (25.0389, 102.7183), "CN", "Asia/Shanghai", "CNY", TODAY)
    yield
    reload_cities()


def test_the_overview_of_a_built_city_comes_from_its_generated_dishes(small_city, fake_net):
    assert food_overview("kunming") == []                                     # 还没有 = 空，不是错误
    save_dishes("kunming", build_dishes(_Fetcher(), "泉州", TODAY))
    rows = food_overview("kunming")
    assert len(rows) == 1 and rows[0]["generated"] and rows[0]["places"] == [] and rows[0]["kind_zh"] == "名菜"
    assert rows[0]["city_zh"] == "昆明" and len(rows[0]["near"]) == 2          # 前端拼「在高德里搜」用
    assert rows[0]["category"] == "泉州小吃"                                    # 写明来自哪个分类


def test_a_broken_dishes_file_is_ignored(small_city, tmp_path):
    path = tmp_path / "dishes" / "kunming.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"city": "kunming", "dishes": "oops"}', encoding="utf-8")
    assert load_generated_dishes("kunming") is None and food_overview("kunming") == []


def test_the_curated_paris_food_keeps_its_shape_and_gets_a_map_hint():
    rows = food_overview("paris")
    assert rows and not rows[0].get("generated") and rows[0]["city_zh"] == "巴黎"


def test_a_name_with_the_city_suffix_also_finds_the_category_without_it(fake_net):
    out = build_dishes(_Fetcher(), "泉州市", TODAY)                     # 城市配置里写的是「泉州市」，维基分类叫「泉州小吃」
    assert out and [d["name_zh"] for d in out["dishes"]] == ["土笋冻"]


def test_every_category_of_a_city_is_read_and_listed(monkeypatch):
    pages = {"Category:北京小吃": ["土笋冻"], "Category:北京饮食": ["土笋冻", "清源山"]}
    monkeypatch.setattr(dishes_mod, "_members", lambda f, cat, kind: pages.get(cat, []) if kind == "page" else [])
    monkeypatch.setattr(dishes_mod, "_wikibase_ids", lambda f, titles: {})
    monkeypatch.setattr(dishes_mod, "fetch_views", lambda c, title, today: None)
    monkeypatch.setattr(dishes_mod, "to_simplified", lambda c, strings: {s: s for s in strings})
    out = build_dishes(_Fetcher(), "北京", TODAY)
    assert out["category"] == "北京小吃、北京饮食" and len(out["dishes"]) == 1       # 同一个条目只算一次


def test_phonetic_brackets_are_removed_from_the_summary():
    raw = "米香，又称米通、大米香、米芳（ㆠㄧˋㄆㄤ），是水稻种植区常见之零嘴及小吃。类似玉米花。"
    assert dishes_mod._summary(raw) == "米香，又称米通、大米香、米芳，是水稻种植区常见之零嘴及小吃。类似玉米花。"
    keep = "豆汁（又称豆汁儿）是北京的传统小吃。味道酸。"
    assert dishes_mod._summary(keep) == keep                                   # 普通的括号不动


def test_a_network_failure_is_reported_not_mistaken_for_no_dishes():
    import httpx

    def refuse(request):
        raise httpx.ConnectError("offline")

    class Offline:
        c = httpx.Client(transport=httpx.MockTransport(refuse))
        pause = 0

    with pytest.raises(dishes_mod.FetchFailed):
        build_dishes(Offline(), "泉州", TODAY)


class _ItalianFetcher(_Fetcher):
    """外国城市：维基数据里有意大利语标签的，名菜带当地语言名。"""

    def wikidata(self, ids, langs):
        assert "it" in langs                                              # 要把当地语言一起取
        return {q: {"sitelinks": {"zhwiki": {}}, "labels": {"it": "tsukemen"} if q == "Q1" else {}} for q in ids}


def test_a_foreign_dish_gets_its_name_in_the_local_language(fake_net):
    out = build_dishes(_ItalianFetcher(), "泉州", TODAY, lang="it", city_qid=None)      # 没有城市编号就不读当地语言分类
    assert out["dishes"][0]["name_local"] == "tsukemen"


def test_a_dish_without_a_local_label_has_none_and_a_chinese_city_asks_for_no_local_name(fake_net):
    out = build_dishes(_Fetcher(), "泉州", TODAY)                       # 中文城市：不取当地语言名
    assert "name_local" not in out["dishes"][0] and "city_local" not in out


def test_the_local_city_name_is_saved_and_reaches_the_overview(small_city, fake_net, monkeypatch):
    class F(_ItalianFetcher):
        def wikidata(self, ids, langs):
            data = super().wikidata(ids, langs)
            data["Q999"] = {"sitelinks": {}, "labels": {"it": "Napoli"}}
            return data

    monkeypatch.setattr(dishes_mod, "_ancestry", lambda f, qid, lang: [])             # 这条测试不关心当地语言分类
    out = build_dishes(F(), "泉州", TODAY, lang="it", city_qid="Q999")
    assert out["city_local"] == "Napoli"
    save_dishes("kunming", out)
    row = food_overview("kunming")[0]
    assert row["name_local"] == "tsukemen" and row["city_local"] == "Napoli"


# --------------------------------------------------------------------------- #
# 当地语言维基的饮食分类（意大利语）：中文维基没有的城市（罗马）靠它
# --------------------------------------------------------------------------- #
def test_italian_category_names_come_from_the_demonyms():
    names = dishes_mod.local_category_names("it", ["romano", "romana", "romani"])
    assert names == ["Categoria:Cucina romana"]
    assert dishes_mod.local_category_names("it", ["milanesi"]) == ["Categoria:Cucina milanese"]
    assert dishes_mod.local_category_names("it", ["napoletani"]) == ["Categoria:Cucina napoletana"]
    assert dishes_mod.local_category_names("it", ["laziale", "laziali"]) == ["Categoria:Cucina laziale"]
    assert dishes_mod.local_category_names("it", ["fiorentini"]) == ["Categoria:Cucina fiorentina"]
    assert dishes_mod.local_category_names("it", ["senesi"]) == ["Categoria:Cucina senese"]
    assert dishes_mod.local_category_names("it", []) == []
    assert dishes_mod.local_category_names("de", ["Stuttgarter"]) == []              # 没写规则的语言不读，不猜


def test_french_category_names_come_from_the_demonyms():
    names = dishes_mod.local_category_names
    assert names("fr", ["Lyonnais", "Lyonnaise"]) == ["Catégorie:Cuisine lyonnaise"]
    assert names("fr", ["Parisien", "Parisienne"]) == ["Catégorie:Cuisine parisienne"]
    assert names("fr", ["Niçois"]) == ["Catégorie:Cuisine niçoise"]
    assert names("fr", ["Toulousains"]) == ["Catégorie:Cuisine toulousaine"]        # 复数 → 单数阴性
    assert names("fr", ["alsacien"]) == ["Catégorie:Cuisine alsacienne"]
    assert names("fr", ["breton"]) == ["Catégorie:Cuisine bretonne"]
    assert names("fr", ["corse"]) == ["Catégorie:Cuisine corse"]
    assert names("fr", []) == []


def test_the_note_names_the_french_category(monkeypatch, fake_net):
    monkeypatch.setattr(dishes_mod, "_ancestry", lambda f, qid, lang: [{"qid": "Q456", "demonyms": ["Lyonnais"]}])
    monkeypatch.setattr(dishes_mod, "_local_members", lambda f, lang, cat: ["Quenelle"] if cat == "Catégorie:Cuisine lyonnaise" else [])
    monkeypatch.setattr(dishes_mod, "_zh_titles", lambda f, lang, titles: ["土笋冻"])
    monkeypatch.setattr(dishes_mod, "_members", lambda f, cat, kind: [])
    out = build_dishes(_Fetcher(), "里昂", TODAY, lang="fr", city_qid="Q456")
    assert out["category_note"] == "取自法语维基百科「Cuisine lyonnaise」分类"


@pytest.fixture
def italy(monkeypatch, fake_net):
    """罗马式：中文维基没有分类；意大利语维基只有大区（拉齐奥）有「Cucina laziale」。"""
    chain = [{"qid": "Q220", "demonyms": ["romano", "romani"]}, {"qid": "Q1282", "demonyms": ["laziale", "laziali"]}]
    monkeypatch.setattr(dishes_mod, "_ancestry", lambda f, qid, lang: chain)
    monkeypatch.setattr(dishes_mod, "_local_members",
                        lambda f, lang, cat: ["Carbonara", "Colosseo"] if cat == "Categoria:Cucina laziale" else [])
    monkeypatch.setattr(dishes_mod, "_zh_titles", lambda f, lang, titles: ["土笋冻", "清源山"])   # 有中文条目的才留
    monkeypatch.setattr(dishes_mod, "_members", lambda f, cat, kind: [])                        # 中文维基没有分类


def test_a_city_without_a_chinese_category_gets_dishes_from_the_region_italian_category(italy):
    out = build_dishes(_ItalianFetcher(), "罗马", TODAY, lang="it", city_qid="Q220")
    assert out and [d["name_zh"] for d in out["dishes"]] == ["土笋冻"]                   # 清源山不是食物
    assert out["category"] == ""
    assert out["category_note"] == "取自意大利语维基百科「Cucina laziale」分类（所在大区的饮食）"


def test_the_city_own_italian_category_is_preferred_and_not_called_regional(italy, monkeypatch):
    monkeypatch.setattr(dishes_mod, "_local_members",
                        lambda f, lang, cat: ["Carbonara"] if cat in ("Categoria:Cucina romana", "Categoria:Cucina laziale") else [])
    out = build_dishes(_ItalianFetcher(), "罗马", TODAY, lang="it", city_qid="Q220")
    assert out["category_note"] == "取自意大利语维基百科「Cucina romana」分类"          # 找到城市自己的就不再往大区找


def test_zh_and_italian_categories_are_named_together_in_the_note(italy, monkeypatch):
    monkeypatch.setattr(dishes_mod, "_members", lambda f, cat, kind: ["土笋冻"] if cat == "Category:罗马饮食" and kind == "page" else [])
    out = build_dishes(_ItalianFetcher(), "罗马", TODAY, lang="it", city_qid="Q220")
    assert len(out["dishes"]) == 1                                                  # 同一个条目只算一次
    assert out["category_note"].startswith("取自中文维基百科「罗马饮食」和意大利语维基百科「Cucina laziale」")


def test_no_category_anywhere_means_no_dishes(italy, monkeypatch):
    monkeypatch.setattr(dishes_mod, "_local_members", lambda f, lang, cat: [])
    assert build_dishes(_ItalianFetcher(), "罗马", TODAY, lang="it", city_qid="Q220") is None


def test_the_note_reaches_the_overview(small_city, italy):
    save_dishes("kunming", build_dishes(_ItalianFetcher(), "罗马", TODAY, lang="it", city_qid="Q220"))
    row = food_overview("kunming")[0]
    assert row["category_note"].startswith("取自意大利语维基百科") and row["generated"]


def test_taiwan_terms_become_mainland_terms():
    assert dishes_mod.mainland("义大利炖饭") == "意大利炖饭"
    assert dishes_mod.mainland("拿坡里肉酱") == "那不勒斯肉酱"
    assert dishes_mod.mainland("义式脆饼，是义大利托斯卡纳的点心。") == "意式脆饼，是意大利托斯卡纳的点心。"
    assert dishes_mod.mainland("土笋冻") == "土笋冻"


def test_a_zh_category_that_gave_no_dish_is_not_named_in_the_note(italy, monkeypatch):
    monkeypatch.setattr(dishes_mod, "_members", lambda f, cat, kind: ["清源山"] if cat == "Category:罗马饮食" and kind == "page" else [])
    out = build_dishes(_ItalianFetcher(), "罗马", TODAY, lang="it", city_qid="Q220")
    assert out["category"] == "" and "中文维基百科" not in out["category_note"]       # 「罗马饮食」里只有古罗马饮食，没有一样菜


def test_a_connection_reset_midway_is_reported_not_a_crash(fake_net):
    import httpx

    class Flaky(_Fetcher):
        def wikidata(self, ids, langs):
            raise httpx.ReadError("connection reset")                         # 维基数据中途断连

    with pytest.raises(dishes_mod.FetchFailed):
        build_dishes(Flaky(), "泉州", TODAY)


def test_streets_are_not_dishes_and_sausage_and_cheese_are():
    street = "缝纫路 是法国里昂第二区科德利埃区的一条街道，以南北走向连接雅各宾广场及艾邦广场，联外公共交通包括里昂地铁A线。"
    assert not is_dish_extract(street)
    assert is_dish_extract("里昂香肠是法国里昂的一种传统香肠。通常用猪肉和猪油制成。")
    assert is_dish_extract("瓦什寒是一种法国软质奶酪。产于汝拉山区。")


def test_a_dish_born_in_a_street_district_is_still_a_dish():
    chengdu = "白家肥肠粉是成都小吃，发源于四川省双流县白家镇（今双流县西航港街道成白社区）。用猪大肠作为主要配料，红薯粉作成粉丝。"
    assert is_dish_extract(chengdu)                                       # 「西航港街道」是地名，不是说它自己是街道
