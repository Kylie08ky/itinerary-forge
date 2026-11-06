# ruff: noqa: E501
"""现建城市的图片和简介（规格 10）：授权白名单、作者必填、下载到 data/generated/、读回来、对外的接口。不联网。"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.cities import reload_cities
from app.citybuild import media as M
from app.citybuild.store import generated_dir
from app.llm import LLMError
from app.media import load_hero, load_media, media_payload, pick_hero
from app.versions import PlanStore

TODAY = date(2026, 10, 29)
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 300


def info(license_="CC BY-SA 4.0", artist="<a href='x'>Jane Doe</a>", mime="image/jpeg", width=2400, thumb=1600):
    meta = {"LicenseShortName": {"value": license_}, "Artist": {"value": artist},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"}}
    return {"query": {"pages": [{"imageinfo": [{
        "thumburl": "https://upload.wikimedia.org/thumb/x.jpg", "thumbwidth": thumb, "thumbheight": thumb // 2,
        "width": width, "height": width // 2, "mime": mime,
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:X.jpg", "extmetadata": meta}]}]}}


def client_for(reply: dict | None, data: bytes = JPEG) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if "commons.wikimedia.org/w/api.php" in str(request.url):
            return httpx.Response(200, json=reply or {"query": {"pages": [{"missing": True}]}})
        return httpx.Response(200, content=data)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def generated(tmp_path, monkeypatch):
    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path / "generated"))
    reload_cities()
    yield
    reload_cities()


def test_a_commons_image_with_an_allowed_license_and_an_author_is_accepted_and_the_author_is_plain_text():
    got = M.commons_image(client_for(info()), "X.jpg", 640, pause=0)
    assert got and got["author"] == "Jane Doe" and got["license"] == "CC BY-SA 4.0" and got["width"] == 1600
    assert got["source_url"].startswith("https://commons.wikimedia.org/")


@pytest.mark.parametrize("reply", [info(license_="CC BY-NC 4.0"), info(artist=""), info(mime="image/gif"), None])
def test_an_image_with_a_forbidden_license_no_author_a_wrong_type_or_no_file_is_refused(reply):
    assert M.commons_image(client_for(reply), "X.jpg", 640, pause=0) is None


def test_the_intro_is_the_first_two_sentences_within_the_limit():
    text = "第一句。第二句。第三句。"
    assert M.two_sentences(text) == "第一句。第二句。"
    assert M.two_sentences("很长的一句" * 60 + "。第二句。", limit=50).endswith("…") and M.two_sentences("") == ""


def raws_and_places():
    raws = [{"qid": "Q1", "sitelinks": 90, "image": "X.jpg", "intro": "洪崖洞是重庆的一处景点。依山而建。", "zh_title": "洪崖洞"},
            {"qid": "Q2", "sitelinks": 40, "image": None, "intro": "", "zh_title": ""}]
    places = [{"id": "hongya-cave", "name_zh": "洪崖洞", "draft_source": {"wikidata": "Q1"}},
              {"id": "no-photo", "name_zh": "无图", "draft_source": {"wikidata": "Q2"}}]
    return raws, places


def test_photos_the_cover_and_intros_are_written_and_read_back_for_a_generated_city():
    raws, places = raws_and_places()
    photos, heroes = M.fetch_media(client_for(info()), "chongqing", places, raws, TODAY, pause=0)
    assert photos == 1 and heroes == ["hongya-cave"]
    root = generated_dir()
    assert (root / "photos" / "hongya-cave.jpg").read_bytes() == JPEG and (root / "hero" / "hongya-cave.jpg").is_file()
    media = json.loads((root / "media" / "chongqing.json").read_text(encoding="utf-8"))["places"]
    assert set(media) == {"hongya-cave"} and "依山而建" in media["hongya-cave"]["intro"]["text"]
    assert media["hongya-cave"]["intro"]["source_url"].startswith("https://zh.wikipedia.org/wiki/")
    (root / "places").mkdir(parents=True, exist_ok=True)
    (root / "places" / "chongqing.json").write_text("{}", encoding="utf-8")            # 让它算「现建的城市」
    load_media.cache_clear()
    load_hero.cache_clear()
    payload = media_payload("hongya-cave", "chongqing")
    assert payload["image"]["url"] == "/media/places/hongya-cave.jpg" and payload["hero"]["url"] == "/media/hero/hongya-cave.jpg"
    assert payload["image"]["author"] == "Jane Doe" and payload["intro"]["text"].startswith("洪崖洞")
    assert media_payload("no-photo", "chongqing") is None


def test_a_cover_needs_a_big_jpeg_under_the_size_limit():
    raws, places = raws_and_places()
    small = M.fetch_media(client_for(info(width=1000, thumb=1000)), "a", places, raws, TODAY, pause=0)
    assert small[1] == []                                                              # 原图不到 1600 宽：没有封面
    heavy = M.fetch_media(client_for(info(), data=b"0" * 1_000_000), "b", places, raws, TODAY, pause=0)
    assert heavy[1] == []                                                              # 超过 900KB：不收


def test_the_photo_and_cover_routes_serve_generated_files(tmp_path):
    raws, places = raws_and_places()
    M.fetch_media(client_for(info()), "chongqing", places, raws, TODAY, pause=0)

    class Dead:
        def complete_json(self, **kw):
            raise LLMError("x")

    client = TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=Dead))
    assert client.get("/media/places/hongya-cave.jpg").content == JPEG
    assert client.get("/media/hero/hongya-cave.jpg").status_code == 200
    assert client.get("/media/places/nope.jpg").status_code == 404
    assert pick_hero("paris") is not None                                              # 精校的城市不受影响
