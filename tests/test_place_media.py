"""地点图片与简介（规格 M02 S7b）：数据闸门（授权白名单 / 来源 / 署名）、真实数据的完整性、接口。

数据由 scripts/fetch_place_media.py 生成；这里的数据检查是「事实只信带来源的策展数据」那条硬规则的守门员：
哪天有人手改了 media.json 或换了图，缺署名 / 授权不对 / 文件丢了就会在这里失败。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import create_app
from app.library import load_library
from app.llm import LLMError
from app.media import (
    DATA_DIR,
    MAX_INTRO_CHARS,
    PHOTOS_DIR,
    PlaceImage,
    PlaceIntro,
    PlaceMedia,
    license_allowed,
    load_media,
    media_payload,
)
from app.versions import PlanStore


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM))


def good_image(**over) -> dict:
    d = {"file": "louvre.jpg", "title": "File:Louvre.jpg", "source_url": "https://commons.wikimedia.org/wiki/File:Louvre.jpg",
         "author": "Jane Doe", "license": "CC BY-SA 4.0", "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
         "width": 640, "height": 427, "checked_at": date(2026, 10, 21)}
    d.update(over)
    return d


def good_intro(**over) -> dict:
    d = {"text": "卢浮宫是位于巴黎的博物馆。", "source_url": "https://zh.wikipedia.org/wiki/卢浮宫",
         "checked_at": date(2026, 10, 21)}
    d.update(over)
    return d


# --------------------------------------------------------------------------- #
# 数据闸门
# --------------------------------------------------------------------------- #
def test_a_good_record_is_accepted_and_defaults_the_intro_license():
    m = PlaceMedia(image=PlaceImage(**good_image()), intro=PlaceIntro(**good_intro()))
    assert m.intro.license == "CC BY-SA 4.0" and m.intro.source_name == "中文维基百科"


@pytest.mark.parametrize("over,why", [
    ({"license": "CC BY-NC 4.0"}, "禁止商用"),
    ({"license": "GFDL"}, "不在白名单"),
    ({"author": ""}, "缺作者"),
    ({"source_url": "https://example.com/pic.jpg"}, "来源不是 Commons"),
    ({"file": "../evil.jpg"}, "文件名不合规"),
    ({"file": "Louvre.JPG"}, "文件名要小写"),
    ({"width": 100}, "太小"),
])
def test_an_image_record_missing_a_required_part_is_rejected(over, why):
    with pytest.raises(ValidationError):
        PlaceImage(**good_image(**over))


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        PlaceImage(**good_image(extra="x"))


@pytest.mark.parametrize("over", [{"text": "字" * (MAX_INTRO_CHARS + 1)}, {"text": ""},
                                  {"source_url": "https://en.wikipedia.org/wiki/Louvre"},
                                  {"source_url": "https://baike.baidu.com/item/卢浮宫"}])
def test_an_intro_must_be_short_and_come_from_chinese_wikipedia(over):
    with pytest.raises(ValidationError):
        PlaceIntro(**good_intro(**over))


def test_an_empty_media_record_is_rejected():
    with pytest.raises(ValidationError):
        PlaceMedia()
    assert PlaceMedia(intro=PlaceIntro(**good_intro())).image is None


@pytest.mark.parametrize("name", ["CC BY-SA 4.0", "CC BY 2.0", "CC0", "Public domain"])
def test_the_license_whitelist_lets_free_licenses_through(name):
    assert license_allowed(name)


# --------------------------------------------------------------------------- #
# 真实数据的完整性
# --------------------------------------------------------------------------- #
MEDIA = load_media("paris")
LIB = load_library("paris")


def test_every_media_entry_belongs_to_a_real_place():
    assert set(MEDIA) <= set(LIB.places)


def test_the_library_has_a_meaningful_amount_of_media():
    with_image = [p for p, m in MEDIA.items() if m.image]
    with_intro = [p for p, m in MEDIA.items() if m.intro]
    assert len(with_image) >= 20 and len(with_intro) >= 20, (len(with_image), len(with_intro))


def test_every_image_has_a_file_a_free_license_an_author_and_a_commons_source():
    for pid, m in MEDIA.items():
        if not m.image:
            continue
        i = m.image
        f = PHOTOS_DIR / i.file
        assert f.is_file() and f.stat().st_size >= 8_000, pid
        assert f.stat().st_size <= 600_000, f"{pid}: 缩略图太大（{f.stat().st_size} 字节）"
        assert i.file == f"{pid}{f.suffix}", f"{pid}: 文件名应该是地点编号"
        assert license_allowed(i.license) and i.author.strip() and i.source_url.startswith("https://commons.wikimedia.org/")
        head = f.read_bytes()[:8]
        assert head[:3] == b"\xff\xd8\xff" or head == b"\x89PNG\r\n\x1a\n", f"{pid}: 不是 JPEG / PNG 文件"
        assert i.checked_at.year >= 2026


def test_every_photo_file_is_referenced_by_a_record():
    referenced = {m.image.file for m in MEDIA.values() if m.image}
    on_disk = {p.name for p in PHOTOS_DIR.iterdir() if p.is_file()}
    assert on_disk == referenced, f"没有记录的文件：{on_disk - referenced}；有记录没文件：{referenced - on_disk}"


def test_every_intro_is_short_sourced_and_cut_at_a_sentence_end():
    for pid, m in MEDIA.items():
        if not m.intro:
            continue
        t = m.intro
        assert 0 < len(t.text) <= MAX_INTRO_CHARS and t.text[-1] in "。！？", f"{pid}: {t.text[-10:]}"
        assert t.source_url.startswith("https://zh.wikipedia.org/") and t.license == "CC BY-SA 4.0"
        assert t.checked_at.year >= 2026


def test_the_media_file_is_valid_json_with_a_city_and_places():
    raw = json.loads((DATA_DIR / "media.json").read_text(encoding="utf-8"))
    assert raw["city"] == "paris" and isinstance(raw["places"], dict)


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
def one_with_image() -> str:
    return next(p for p, m in MEDIA.items() if m.image and m.intro)


def test_the_payload_has_a_local_url_and_the_credit_fields():
    pid = one_with_image()
    p = media_payload(pid)
    assert p["image"]["url"] == f"/media/places/{MEDIA[pid].image.file}"
    assert {"author", "license", "license_url", "source_url", "title", "width", "height"} <= set(p["image"])
    assert {"text", "source_url", "source_name", "license", "license_url"} <= set(p["intro"])
    assert media_payload("no-such-place") is None


def test_places_and_place_detail_carry_the_media_field(client):
    pid = one_with_image()
    detail = client.get(f"/places/{pid}").json()
    assert detail["media"]["image"]["author"] == MEDIA[pid].image.author
    assert detail["media"]["intro"]["text"] == MEDIA[pid].intro.text
    listing = {p["id"]: p for p in client.get("/places").json()}
    assert listing[pid]["media"] == detail["media"]
    without = next(p for p in LIB.places if p not in MEDIA)
    assert listing[without]["media"] is None


def test_search_results_carry_the_media_field_too(client):
    pid = one_with_image()
    hit = next(p for p in client.get("/places/search", params={"q": LIB.places[pid].name_zh}).json() if p["id"] == pid)
    assert hit["media"]["image"]["url"].startswith("/media/places/")


def test_the_photo_is_served_with_the_right_type_and_a_cache_header(client):
    pid = one_with_image()
    img = MEDIA[pid].image
    r = client.get(f"/media/places/{img.file}")
    assert r.status_code == 200
    assert r.headers["content-type"] == ("image/png" if img.file.endswith(".png") else "image/jpeg")
    assert "max-age" in r.headers["cache-control"] and r.content == (PHOTOS_DIR / img.file).read_bytes()


@pytest.mark.parametrize("name", ["nope.jpg", "louvre.gif", "LOUVRE.jpg", "..%2Fmedia.json", "..%5Cmedia.json",
                                  "media.json", "louvre.jpg.exe", "-bad.jpg", ".jpg"])
def test_unknown_or_unsafe_file_names_are_404(client, name):
    assert client.get(f"/media/places/{name}").status_code == 404


def test_only_files_inside_the_photos_folder_can_be_read(client):
    assert client.get("/media/places/../places/paris.json").status_code in (404, 405)
