"""取图片 / 简介的脚本（scripts/fetch_place_media.py）：选对地点的保护、授权白名单、简介截断、下载。

不联网：用 httpx.MockTransport 假装 Wikimedia。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_place_media as F  # noqa: E402

from app.media import license_allowed  # noqa: E402

LOUVRE = (48.8606, 2.3376)
TODAY = date(2026, 10, 21)
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20_000


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,ok", [
    ("CC BY-SA 4.0", True), ("CC BY 2.0", True), ("CC BY-SA 3.0 de", True), ("CC0", True), ("CC0 1.0", True),
    ("Public domain", True), ("PD-old-70", True), ("cc by-sa 2.5", True),
    ("CC BY-NC 4.0", False), ("CC BY-NC-SA 3.0", False), ("CC BY-ND 4.0", False), ("GFDL", False),
    ("Fair use", False), ("All rights reserved", False), ("", False), (None, False),
])
def test_only_free_licenses_are_allowed(name, ok):
    assert license_allowed(name) is ok


def test_clean_html_strips_tags_entities_and_whitespace():
    raw = '<a href="//commons.wikimedia.org/wiki/User:X" title="User:X">Jane &amp; Co</a>\n <span>Doe</span>'
    assert F.clean_html(raw) == "Jane & Co Doe"
    assert F.clean_html("") == "" and F.clean_html(None) == ""


def test_the_intro_is_the_first_two_sentences_cut_at_a_full_stop():
    text = "卢浮宫是位于巴黎的博物馆。它是世界上最大的博物馆之一。它收藏了三十万件艺术品。"
    assert F.trim_intro(text) == "卢浮宫是位于巴黎的博物馆。它是世界上最大的博物馆之一。"
    assert F.trim_intro("只有一句话。") == "只有一句话。"
    assert F.trim_intro("没有句号的一段话") is None
    assert F.trim_intro("") is None


def test_a_long_second_sentence_is_dropped_and_a_long_first_one_loses_its_brackets():
    long_second = "第一句很短。" + "字" * 250 + "。"
    assert F.trim_intro(long_second) == "第一句很短。"
    bracketed = "卢浮宫（法语：Musée du Louvre，" + "注" * 220 + "）是博物馆。"
    assert F.trim_intro(bracketed) == "卢浮宫是博物馆。"
    assert F.trim_intro("字" * 300 + "。") is None


def test_the_intro_never_exceeds_the_limit():
    for n in (50, 100, 190, 199):
        assert len(F.trim_intro("字" * n + "。")) <= F.MAX_INTRO_CHARS


# --------------------------------------------------------------------------- #
# 假的 Wikimedia
# --------------------------------------------------------------------------- #
def make_fetcher(handler) -> F.MediaFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": F.UA})
    return F.MediaFetcher(client, pause=0)


def api_response(request: httpx.Request, *, search, pages, imageinfo=None, summary=None):
    p = request.url.params
    host = request.url.host
    if host == "en.wikipedia.org" and p.get("list") == "search":
        return httpx.Response(200, json={"query": {"search": [{"title": t} for t in search]}})
    if host == "en.wikipedia.org":
        return httpx.Response(200, json={"query": {"pages": {str(i): pg for i, pg in enumerate(pages)}}})
    if host == "commons.wikimedia.org":
        return httpx.Response(200, json={"query": {"pages": {"1": imageinfo}}})
    if host == "zh.wikipedia.org":
        if summary is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=summary)
    if host == "upload.wikimedia.org":
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    raise AssertionError(f"没想到的请求：{request.url}")


def page(title, lat=None, lng=None, pageimage=None, zh=None):
    pg = {"title": title}
    if lat is not None:
        pg["coordinates"] = [{"lat": lat, "lon": lng}]
    if pageimage:
        pg["pageimage"] = pageimage
    if zh:
        pg["langlinks"] = [{"lang": "zh", "*": zh}]
    return pg


def good_imageinfo(**over):
    meta = {"LicenseShortName": {"value": "CC BY-SA 4.0"}, "Artist": {"value": '<a href="x">Jane Doe</a>'},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"}}
    meta.update(over.pop("meta", {}))
    ii = {"url": "https://upload.wikimedia.org/o.jpg", "thumburl": "https://upload.wikimedia.org/t.png",
          "descriptionurl": "https://commons.wikimedia.org/wiki/File:Louvre.png", "mime": "image/png",
          "width": 4000, "height": 3000, "thumbwidth": 640, "thumbheight": 480, "extmetadata": meta}
    ii.update(over)
    return {"title": "File:Louvre.png", "imageinfo": [ii]}


GOOD_SUMMARY = {"extract": "卢浮宫是位于巴黎的博物馆。它是世界上最大的博物馆之一。",
                "content_urls": {"desktop": {"page": "https://zh.wikipedia.org/wiki/卢浮宫"}}}


# --------------------------------------------------------------------------- #
# 选对地点
# --------------------------------------------------------------------------- #
def test_a_candidate_must_be_within_1000_metres_of_the_place():
    pages = [page("Louvre (disambiguation)"), page("Louvre, Val-d'Oise", 49.05, 2.5),
             page("Louvre", 48.8611, 2.3364, "Louvre.png", "卢浮宫")]
    f = make_fetcher(lambda r: api_response(r, search=[p["title"] for p in pages], pages=pages))
    hit = f.find_page("Louvre", *LOUVRE)
    assert hit["title"] == "Louvre" and hit["zh_title"] == "卢浮宫" and hit["pageimage"] == "Louvre.png"
    assert hit["distance_m"] < 1000


def test_a_namesake_elsewhere_or_without_coordinates_is_never_taken():
    pages = [page("Louvre, Val-d'Oise", 49.05, 2.5, "Far.jpg"), page("The Louvre (film)")]
    f = make_fetcher(lambda r: api_response(r, search=[p["title"] for p in pages], pages=pages))
    assert f.find_page("Louvre", *LOUVRE) is None


def test_the_first_close_candidate_in_search_order_wins():
    pages = [page("Louvre Near A", 48.8607, 2.3377, "a.jpg"), page("Louvre Near B", 48.8608, 2.3378, "b.jpg")]
    f = make_fetcher(lambda r: api_response(r, search=[p["title"] for p in pages], pages=pages))
    assert f.find_page("Louvre", *LOUVRE)["title"] == "Louvre Near A"


def test_a_nearby_but_differently_named_place_is_never_taken():
    """旁边的地铁站、别的景点，坐标再近也不能当成这个地点（真实发生过：圣心堂匹配到了 Anvers 地铁站）。"""
    pages = [page("Anvers station", 48.8831, 2.3444, "metro.jpg", "昂韦尔站"),
             page("Sacré-Cœur, Paris", 48.8867, 2.3431, "sacre.jpg", "圣心堂")]
    f = make_fetcher(lambda r: api_response(r, search=[p["title"] for p in pages], pages=pages))
    hit = f.find_page("Montmartre & Sacré-Cœur", 48.8860, 2.3430)
    assert hit["title"] == "Sacré-Cœur, Paris" and hit["zh_title"] == "圣心堂"
    only_station = [pages[0]]
    f2 = make_fetcher(lambda r: api_response(r, search=["Anvers station"], pages=only_station))
    assert f2.find_page("Montmartre & Sacré-Cœur", 48.8860, 2.3430) is None


@pytest.mark.parametrize("name,title,same", [
    ("Louvre", "Louvre Pyramid", True), ("Musée d'Orsay", "Musee d'Orsay", True),
    ("Pont Alexandre III", "Pont Alexandre-III", True), ("Jardin du Luxembourg", "Jardin des Tuileries", False),
    ("Musée Rodin", "Musée Picasso", False), ("Montmartre & Sacré-Cœur", "Anvers station", False),
    ("Eiffel Tower", "Eiffel Tower", True), ("Musée de Cluny", "Musée de Cluny", True),
])
def test_names_are_compared_by_their_distinctive_words(name, title, same):
    assert F.same_name(name, title) is same


def test_no_search_results_means_no_match():
    f = make_fetcher(lambda r: api_response(r, search=[], pages=[]))
    assert f.find_page("Nothing", *LOUVRE) is None


# --------------------------------------------------------------------------- #
# 图片的授权 / 尺寸把关
# --------------------------------------------------------------------------- #
def info_for(imageinfo, file_name="Louvre.png"):
    f = make_fetcher(lambda r: api_response(r, search=[], pages=[], imageinfo=imageinfo))
    return f.image_info(file_name)


def test_a_free_licensed_big_photo_is_accepted_with_cleaned_author_and_links():
    got = info_for(good_imageinfo())
    assert got["author"] == "Jane Doe" and got["license"] == "CC BY-SA 4.0"
    assert got["source_url"] == "https://commons.wikimedia.org/wiki/File:Louvre.png"
    assert got["license_url"] == "https://creativecommons.org/licenses/by-sa/4.0" and got["thumb_url"].endswith("t.png")


@pytest.mark.parametrize("over,why", [
    ({"meta": {"LicenseShortName": {"value": "CC BY-NC 4.0"}}}, "禁止商用"),
    ({"meta": {"LicenseShortName": {"value": "GFDL"}}}, "不在白名单"),
    ({"meta": {"Artist": {"value": ""}}}, "缺作者且不是公有领域"),
    ({"width": 600}, "原图太小"),
    ({"width": 5000, "height": 800}, "太扁，多半是全景 / 横幅"),
    ({"mime": "image/svg+xml"}, "矢量图"),
])
def test_photos_that_fail_a_check_are_rejected(over, why):
    assert info_for(good_imageinfo(**over)) is None, why


def test_a_public_domain_photo_without_an_author_is_accepted_with_a_placeholder():
    got = info_for(good_imageinfo(meta={"LicenseShortName": {"value": "Public domain"}, "Artist": {"value": ""}}))
    assert got["author"] == "未署名（公有领域）"


def test_file_names_that_are_not_photos_are_skipped_without_a_request():
    def boom(request):
        raise AssertionError("不该发请求")

    f = make_fetcher(boom)
    assert f.image_info("Map_of_Paris.svg") is None and f.image_info("Anim.gif") is None


# --------------------------------------------------------------------------- #
# 整个流程
# --------------------------------------------------------------------------- #
@pytest.fixture
def photos(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "PHOTOS_DIR", tmp_path)
    return tmp_path


def run(photos_dir, *, pages, imageinfo, summary):
    def handler(request):
        return api_response(request, search=[p["title"] for p in pages], pages=pages, imageinfo=imageinfo,
                            summary=summary)

    return make_fetcher(handler).fetch_place("louvre", "Louvre", *LOUVRE, today=TODAY)


def test_a_full_match_downloads_the_thumbnail_and_returns_image_and_intro(photos):
    media, note = run(photos, pages=[page("Louvre", 48.8611, 2.3364, "Louvre.png", "卢浮宫")],
                      imageinfo=good_imageinfo(), summary=GOOD_SUMMARY)
    assert media.image.file == "louvre.png" and (photos / "louvre.png").read_bytes() == PNG
    assert media.image.author == "Jane Doe" and media.image.license == "CC BY-SA 4.0"
    assert media.image.checked_at == TODAY and media.intro.checked_at == TODAY
    assert media.intro.text == "卢浮宫是位于巴黎的博物馆。它是世界上最大的博物馆之一。"
    assert media.intro.source_url == "https://zh.wikipedia.org/wiki/卢浮宫" and "图 + 简介" in note


def test_no_chinese_article_means_an_image_only_record(photos):
    media, note = run(photos, pages=[page("Louvre", 48.8611, 2.3364, "Louvre.png")],
                      imageinfo=good_imageinfo(), summary=None)
    assert media.image is not None and media.intro is None and "无简介" in note


def test_a_rejected_image_still_keeps_the_intro(photos):
    media, note = run(photos, pages=[page("Louvre", 48.8611, 2.3364, "Louvre.png", "卢浮宫")],
                      imageinfo=good_imageinfo(meta={"LicenseShortName": {"value": "CC BY-NC 4.0"}}),
                      summary=GOOD_SUMMARY)
    assert media.image is None and media.intro is not None and "无图" in note
    assert not list(photos.iterdir())                                   # 没有下载任何文件


def test_nothing_usable_means_no_record(photos):
    media, note = run(photos, pages=[page("Louvre", 48.8611, 2.3364, "Louvre.png")],
                      imageinfo=good_imageinfo(meta={"LicenseShortName": {"value": "GFDL"}}), summary=None)
    assert media is None and "没有可用的图片" in note
    far, note2 = run(photos, pages=[page("Elsewhere", 10.0, 10.0, "x.jpg", "别处")],
                     imageinfo=good_imageinfo(), summary=GOOD_SUMMARY)
    assert far is None and "没有坐标在附近" in note2


def test_a_tiny_download_is_not_kept(photos):
    def handler(request):
        if request.url.host == "upload.wikimedia.org":
            return httpx.Response(200, content=b"tiny")
        return api_response(request, search=["Louvre"], pages=[page("Louvre", 48.8611, 2.3364, "Louvre.png", "卢浮宫")],
                            imageinfo=good_imageinfo(), summary=GOOD_SUMMARY)

    media, _ = make_fetcher(handler).fetch_place("louvre", "Louvre", *LOUVRE, today=TODAY)
    assert media.image is None and media.intro is not None and not list(photos.iterdir())


def test_a_dry_run_downloads_nothing(photos):
    def handler(request):
        return api_response(request, search=["Louvre"], pages=[page("Louvre", 48.8611, 2.3364, "Louvre.png", "卢浮宫")],
                            imageinfo=good_imageinfo(), summary=GOOD_SUMMARY)

    media, _ = make_fetcher(handler).fetch_place("louvre", "Louvre", *LOUVRE, dry_run=True, today=TODAY)
    assert media.image is not None and not list(photos.iterdir())


def test_the_user_agent_names_a_contact_url_as_wikimedia_requires():
    assert "https://" in F.UA


def test_orphan_photos_are_pruned_but_referenced_ones_stay(tmp_path):
    (tmp_path / "keep.jpg").write_bytes(b"x")
    (tmp_path / "old.png").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("别的文件不动")
    current = {"places": {"keep": {"image": {"file": "keep.jpg"}}, "intro-only": {"intro": {"text": "x"}}}}
    assert F.prune_orphans(current, tmp_path) == ["old.png"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.jpg", "notes.txt"]


# --------------------------------------------------------------------------- #
# 真实跑一遍后发现的误配：地铁站、剧院、动物园；以及大图改要小一点的版本
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,title,same", [
    ("Tuileries Garden", "Tuileries station", False),
    ("Champs-Élysées", "Théâtre de l'Étoile (Champs-Élysées)", False),
    ("Jardin des Plantes", "Ménagerie du Jardin des Plantes", False),
    ("Café de Flore", "Café de Flore", True),
    ("Gare du Nord", "Gare du Nord", True),
    ("Musée du Quai Branly", "Musée du Quai Branly – Jacques Chirac", True),
    ("Tuileries Garden", "Tuileries Garden", True),
])
def test_titles_that_say_it_is_something_else_are_rejected_unless_the_place_is_that_thing(name, title, same):
    assert F.same_name(name, title) is same


def test_among_several_close_candidates_the_most_similar_name_wins_not_the_first_result():
    pages = [page("Louvre Museum Annex Gift Shop", 48.8607, 2.3377, "annex.jpg"),
             page("Louvre", 48.8611, 2.3364, "Louvre.png", "卢浮宫")]
    f = make_fetcher(lambda r: api_response(r, search=[p["title"] for p in pages], pages=pages))
    assert f.find_page("Louvre", *LOUVRE)["title"] == "Louvre"


def test_a_thumbnail_over_the_size_cap_is_retried_at_a_narrower_width(photos):
    widths = []

    def handler(request):
        host = request.url.host
        if host == "commons.wikimedia.org":
            if request.url.params.get("list"):
                return httpx.Response(200, json={"query": {}})
            w = int(request.url.params["iiurlwidth"])
            widths.append(w)
            info = good_imageinfo(thumburl=f"https://upload.wikimedia.org/t{w}.png", thumbwidth=w,
                                  thumbheight=w * 3 // 4)
            return httpx.Response(200, json={"query": {"pages": {"1": info}}})
        if host == "upload.wikimedia.org":
            big = request.url.path.endswith("t640.png")
            return httpx.Response(200, content=b"x" * (F.MAX_FILE_BYTES + 1 if big else 50_000))
        return api_response(request, search=["Louvre"], pages=[page("Louvre", 48.8611, 2.3364, "Louvre.png", "卢浮宫")],
                            summary=GOOD_SUMMARY)

    media, _ = make_fetcher(handler).fetch_place("louvre", "Louvre", *LOUVRE, today=TODAY)
    assert widths == [640, 480] and media.image.width == 480
    assert (photos / "louvre.png").stat().st_size == 50_000


def test_when_every_width_is_too_big_the_intro_is_kept_but_there_is_no_image(photos):
    def handler(request):
        host = request.url.host
        if host == "commons.wikimedia.org":
            if request.url.params.get("list"):                                  # 兜底搜索：什么也搜不到
                return httpx.Response(200, json={"query": {}})
            w = int(request.url.params["iiurlwidth"])
            info = good_imageinfo(thumburl=f"https://upload.wikimedia.org/t{w}.png", thumbwidth=w, thumbheight=w)
            return httpx.Response(200, json={"query": {"pages": {"1": info}}})
        if host == "upload.wikimedia.org":
            return httpx.Response(200, content=b"x" * (F.MAX_FILE_BYTES + 1))
        return api_response(request, search=["Louvre"], pages=[page("Louvre", 48.8611, 2.3364, "Louvre.png", "卢浮宫")],
                            summary=GOOD_SUMMARY)

    media, note = make_fetcher(handler).fetch_place("louvre", "Louvre", *LOUVRE, today=TODAY)
    assert media.image is None and media.intro is not None and not list(photos.iterdir()) and "无图" in note


# --------------------------------------------------------------------------- #
# 兜底：条目没有可用主图时，到 Commons 里直接搜（你验收后提的：没有图片的地点换别的方式找图）
# --------------------------------------------------------------------------- #
def commons_handler(*, search=(), geo=(), imageinfos=None, pages=None, summary=None):
    """假 Commons：全文搜索返回 search，附近搜索返回 geo；imageinfo 按文件名查 imageinfos 字典。"""
    imageinfos = imageinfos or {}

    def handler(request):
        host, p = request.url.host, request.url.params
        if host == "commons.wikimedia.org":
            if p.get("list") == "search":
                return httpx.Response(200, json={"query": {"search": [{"title": t} for t in search]}})
            if p.get("list") == "geosearch":
                return httpx.Response(200, json={"query": {"geosearch": [{"title": t} for t in geo]}})
            info = imageinfos.get(p["titles"])
            return httpx.Response(200, json={"query": {"pages": {"1": info or {"title": p["titles"]}}}})
        if host == "upload.wikimedia.org":
            return httpx.Response(200, content=PNG)
        return api_response(request, search=[x["title"] for x in (pages or [])], pages=pages or [], summary=summary)

    return handler


def paris_info(title, **meta):
    base = {"Categories": {"value": "Paris|Pastry shops"}, "ImageDescription": {"value": "A tea room in Paris"}}
    base.update(meta)
    d = good_imageinfo(meta=base)
    d["title"] = title
    return d


def test_the_fallback_picks_a_commons_photo_whose_name_matches_and_which_is_in_paris(photos):
    t = "File:Angelina Rue de Rivoli.jpg"
    f = make_fetcher(commons_handler(search=[t], imageinfos={t: paris_info(t)}))
    media, note = f.fetch_place("angelina", "Angelina", 48.8650, 2.3285, today=TODAY)
    assert media.image.file == "angelina.png" and media.intro is None
    assert "Commons 搜索" in note and (photos / "angelina.png").exists()
    assert media.image.source_url.startswith("https://commons.wikimedia.org/")


def test_the_fallback_needs_paris_in_the_title_categories_or_description(photos):
    t = "File:Angelina tea room.jpg"
    elsewhere = paris_info(t, Categories={"value": "Vienna"}, ImageDescription={"value": "A tea room in Vienna"})
    elsewhere["title"] = "File:Angelina tea room.jpg"
    f = make_fetcher(commons_handler(search=[t], imageinfos={t: elsewhere}))
    media, _ = f.fetch_place("angelina", "Angelina", 48.8650, 2.3285, today=TODAY)
    assert media is None and not list(photos.iterdir())


def test_the_fallback_ignores_logos_maps_other_names_and_vector_files(photos):
    bad = ["File:Angelina logo.png", "File:Paris map Angelina.png", "File:Ladurée Champs.jpg", "File:Angelina.svg",
           "File:Angelina menu.jpg", "File:Angelina old painting Paris.jpg"]
    f = make_fetcher(commons_handler(search=bad, imageinfos={t: paris_info(t) for t in bad}))
    media, _ = f.fetch_place("angelina", "Angelina", 48.8650, 2.3285, today=TODAY)
    assert media is None


def test_the_fallback_checks_the_license_like_everything_else(photos):
    t = "File:Angelina Paris.jpg"
    nc = paris_info(t, LicenseShortName={"value": "CC BY-NC 4.0"})
    f = make_fetcher(commons_handler(search=[t], imageinfos={t: nc}))
    assert f.fetch_place("angelina", "Angelina", 48.8650, 2.3285, today=TODAY)[0] is None


def test_nearby_geotagged_photos_are_used_only_when_their_name_also_matches(photos):
    right, wrong = "File:Angelina Paris interior.jpg", "File:Crow sunbathing Paris.jpg"
    f = make_fetcher(commons_handler(geo=[wrong, right], imageinfos={right: paris_info(right),
                                                                   wrong: paris_info(wrong)}))
    media, _ = f.fetch_place("angelina", "Angelina", 48.8650, 2.3285, today=TODAY)
    assert media.image.title == right


def test_the_fallback_does_not_run_when_the_article_already_gave_an_image(photos):
    calls = []

    def handler(request):
        if request.url.host == "commons.wikimedia.org" and request.url.params.get("list"):
            calls.append(request.url.params["list"])
        return api_response(request, search=["Louvre"], pages=[page("Louvre", 48.8611, 2.3364, "Louvre.png", "卢浮宫")],
                            imageinfo=good_imageinfo(), summary=GOOD_SUMMARY)

    media, note = make_fetcher(handler).fetch_place("louvre", "Louvre", *LOUVRE, today=TODAY)
    assert media.image is not None and calls == [] and "Commons 搜索" not in note


def test_an_intro_only_place_gets_an_image_from_the_fallback_and_keeps_its_intro(photos):
    t = "File:Musee Orsay facade Paris.jpg"
    pages = [page("Musée d'Orsay", 48.86, 2.3266, None, "奥赛博物馆")]

    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return commons_handler(search=[t], imageinfos={t: paris_info(t)})(request)
        return api_response(request, search=["Musée d'Orsay"], pages=pages, summary=GOOD_SUMMARY)

    media, note = make_fetcher(handler).fetch_place("orsay", "Musée d'Orsay", 48.86, 2.3266, today=TODAY)
    assert media.image is not None and media.intro is not None and "图（Commons 搜索） + 简介" in note


# --------------------------------------------------------------------------- #
# 兜底搜索的名字处理（验收时发现 Pierre Hermé 配到了圣叙尔皮斯喷泉）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,title,same", [
    ("Pierre Hermé - Saint-Germain", "Fontaine Saint-Sulpice, Paris 14 January 2019", False),
    ("Pierre Hermé - Saint-Germain", "Pierre Hermé macarons Paris", True),
    ("Notre-Dame", "Cathédrale Notre-Dame (exterior)", True),
    ("Notre-Dame", "Pont Notre-Dame", True),
    ("Sainte-Chapelle", "Sainte-Chapelle Upper level", True),
    ("Sainte-Chapelle", "Saint-Sulpice facade", False),
    ("Canal Saint-Martin", "Canal Saint-Martin bench", True),
    ("Canal Saint-Martin", "Saint-Ouen flea market", False),
])
def test_everyday_place_name_words_do_not_make_a_match(name, title, same):
    assert F.same_name(name, title) is same


@pytest.mark.parametrize("name,core", [
    ("Le Marais walk", "Le Marais"), ("Latin Quarter walk", "Latin Quarter"), ("Le Marais boutique walk", "Le Marais"),
    ("Seine cruise", "Seine"), ("Tour Montparnasse", "Montparnasse"), ("Louvre", "Louvre"), ("Walk", "Walk"),
    ("Chanel – 31 rue Cambon", "Chanel – 31 rue Cambon"),
])
def test_walk_and_shopping_suffixes_are_dropped_for_the_search(name, core):
    assert F.core_name(name) == core


def test_the_search_uses_the_core_name(photos):
    asked = []

    def handler(request):
        if request.url.host == "commons.wikimedia.org" and request.url.params.get("list") == "search":
            asked.append(request.url.params["srsearch"])
        t = "File:Le Marais Paris rue.jpg"
        return commons_handler(search=[t], imageinfos={t: paris_info(t)})(request)

    media, _ = make_fetcher(handler).fetch_place("marais-walk", "Le Marais walk", 48.857, 2.36, today=TODAY)
    assert asked == ['"Le Marais" Paris'] and media.image is not None


def test_a_photo_excluded_by_hand_is_skipped_for_that_place_only(photos):
    bad, good = "File:Le Marais estate agency front Paris.jpg", "File:Paris street Le Marais.jpg"
    infos = {bad: paris_info(bad), good: paris_info(good)}
    handler = commons_handler(search=[bad, good], imageinfos=infos)

    def fetcher(exclude):
        client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": F.UA})
        return F.MediaFetcher(client, pause=0, exclude=exclude)

    media, _ = fetcher({"marais-walk": {bad}}).fetch_place("marais-walk", "Le Marais walk", 48.857, 2.36, today=TODAY)
    assert media.image.title == good
    media, _ = fetcher({"other-place": {bad}}).fetch_place("marais-walk", "Le Marais walk", 48.857, 2.36, today=TODAY)
    assert media.image.title == bad


def test_the_exclusion_file_is_read_and_every_entry_gives_a_reason(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"a": {"files": ["One.jpg", "File:Two.jpg"], "reason": "r"}}', encoding="utf-8")
    assert F.load_exclusions(f) == {"a": {"File:One.jpg", "File:Two.jpg"}}
    assert F.load_exclusions(tmp_path / "missing.json") == {}
    import json
    real = json.loads((F.DATA_DIR / "media_exclude.json").read_text(encoding="utf-8"))
    assert real and all(v["files"] and v["reason"].strip() for v in real.values())
