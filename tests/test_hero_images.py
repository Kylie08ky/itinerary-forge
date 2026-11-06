"""封面大图（规格 M02 S7c）：数据闸门、取图脚本的检查、接口、真实数据的完整性。不联网：假的 Wikimedia。"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_hero_images as H  # noqa: E402

import app.media as media  # noqa: E402
from app.api import create_app  # noqa: E402
from app.llm import LLMError  # noqa: E402
from app.versions import PlanStore  # noqa: E402

TODAY = date(2026, 10, 22)
JPG = b"\xff\xd8\xff\xe0" + b"x" * 50_000
GOOD = {"file": "eiffel-tower.jpg", "title": "File:Eiffel.jpg", "source_url": "https://commons.wikimedia.org/wiki/File:Eiffel.jpg",
        "author": "Jane Doe", "license": "CC BY-SA 4.0", "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
        "width": 1920, "height": 1080, "checked_at": "2026-10-22"}


# --------------------------------------------------------------------------- #
# 数据闸门
# --------------------------------------------------------------------------- #
def test_a_valid_hero_image_is_accepted():
    assert media.HeroImage.model_validate(GOOD).width == 1920


@pytest.mark.parametrize("change", [
    {"width": 1599},                                  # 太小
    {"file": "eiffel-tower.png"},                     # 只收 JPEG
    {"file": "Eiffel Tower.jpg"},                     # 文件名不规范
    {"license": "CC BY-NC 4.0"},                      # 授权不在白名单
    {"author": ""},                                   # 缺作者
    {"source_url": "https://example.com/x.jpg"},      # 来源必须是 Commons 文件页
])
def test_bad_hero_images_are_refused(change):
    with pytest.raises(ValidationError):
        media.HeroImage.model_validate({**GOOD, **change})


@pytest.fixture
def hero_dir(tmp_path, monkeypatch):
    d = tmp_path / "hero"
    d.mkdir()
    monkeypatch.setattr(media, "HERO_DIR", d)
    monkeypatch.setattr(media, "HERO_PATH", tmp_path / "hero.json")
    media.load_hero.cache_clear()
    yield d
    media.load_hero.cache_clear()


def write_hero(hero_dir, entry=GOOD, pid="eiffel-tower", payload=JPG):
    (hero_dir / entry["file"]).write_bytes(payload)
    media.HERO_PATH.write_text(json.dumps({"city": "paris", "places": {pid: {"image": entry}}}), encoding="utf-8")
    media.load_hero.cache_clear()


def test_loader_reads_the_file_and_checks_it_exists_and_is_not_huge(hero_dir):
    assert media.load_hero() == {}                                              # 没有 hero.json：全都没有大图
    write_hero(hero_dir)
    assert media.load_hero()["eiffel-tower"].author == "Jane Doe"
    (hero_dir / GOOD["file"]).unlink()
    media.load_hero.cache_clear()
    with pytest.raises(ValueError, match="不存在"):
        media.load_hero()
    write_hero(hero_dir, payload=b"\xff\xd8\xff" + b"x" * (media.MAX_HERO_BYTES + 1))
    with pytest.raises(ValueError, match="超过"):
        media.load_hero()


def test_media_payload_carries_the_hero_with_credit_even_for_places_without_a_thumbnail(hero_dir):
    write_hero(hero_dir, pid="arc-de-triomphe")
    payload = media.media_payload("arc-de-triomphe")
    h = payload["hero"]
    assert h["url"] == "/media/hero/eiffel-tower.jpg" and h["author"] == "Jane Doe" and h["license"] == "CC BY-SA 4.0"
    assert h["source_url"].startswith("https://commons.wikimedia.org/") and h["width"] == 1920
    assert media.media_payload("no-such-place") is None


@pytest.fixture
def client(tmp_path, hero_dir) -> TestClient:
    class Dead:
        def complete_json(self, **kw):
            raise LLMError("x")

    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=Dead))


def test_hero_files_are_served_with_a_cache_header_and_nothing_else(client, hero_dir):
    write_hero(hero_dir)
    r = client.get("/media/hero/eiffel-tower.jpg")
    assert r.status_code == 200 and r.content == JPG and r.headers["content-type"] == "image/jpeg"
    assert "max-age" in r.headers["cache-control"]
    for bad in ("nope.jpg", "eiffel-tower.png", "..%2Fplans.db", "Eiffel Tower.jpg", "a.jpg.exe"):
        assert client.get(f"/media/hero/{bad}").status_code == 404, bad


def test_place_detail_exposes_the_hero(client, hero_dir):
    write_hero(hero_dir, pid="louvre")
    assert client.get("/places/louvre").json()["media"]["hero"]["url"] == "/media/hero/eiffel-tower.jpg"


# --------------------------------------------------------------------------- #
# 取图脚本：假的 Commons
# --------------------------------------------------------------------------- #
def commons(files: dict, *, hits: list[str] | None = None, size_by_width: dict | None = None):
    """files: {标题: imageinfo 字段覆盖}；hits: 搜索结果标题。"""

    def info(title, width):
        base = {"mime": "image/jpeg", "width": 4000, "height": 2500, "descriptionurl": f"https://commons.wikimedia.org/wiki/{title}",
                "thumburl": f"https://upload.wikimedia.org/thumb/{width}/{title}", "thumbwidth": width,
                "thumbheight": int(width * 0.625),
                "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"},
                                "Artist": {"value": "<a href='x'>Jane Doe</a>"},
                                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"}}}
        base.update(files[title])
        return {"query": {"pages": {"1": {"title": title, "imageinfo": [base]}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        if request.url.host == "upload.wikimedia.org":
            width = int(request.url.path.split("/")[2])
            return httpx.Response(200, content=b"\xff\xd8\xff" + b"x" * (size_by_width or {}).get(width, 300_000))
        if q.get("list") == "search":
            return httpx.Response(200, json={"query": {"search": [{"title": t} for t in (hits or [])]}})
        title = q["titles"]
        if title not in files:
            return httpx.Response(200, json={"query": {"pages": {"-1": {"title": title, "missing": ""}}}})
        return httpx.Response(200, json=info(title, int(q.get("iiurlwidth", 1920))))

    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": H.UA})


def fetcher(client) -> H.Fetcher:
    return H.Fetcher(client, pause=0)


@pytest.mark.parametrize("override, reason", [
    ({"mime": "image/png"}, "不是 JPEG"),
    ({"width": 2000, "height": 1300}, "小于"),
    ({"width": 2400, "height": 3600}, "不是横版"),
    ({"extmetadata": {"LicenseShortName": {"value": "CC BY-NC 4.0"}, "Artist": {"value": "x"}}}, "不在白名单"),
    ({"extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "Artist": {"value": ""}}}, "没有作者"),
])
def test_info_refuses_unusable_files_with_a_reason(override, reason):
    f = fetcher(commons({"File:A.jpg": override}))
    with pytest.raises(H.HeroError, match=reason):
        f.info("File:A.jpg")


def test_info_accepts_a_good_landscape_and_returns_credit():
    f = fetcher(commons({"File:A.jpg": {}}))
    info = f.info("A.jpg")                                                       # 不带 File: 前缀也行
    assert info["author"] == "Jane Doe" and info["license"] == "CC BY-SA 4.0" and info["original"] == (4000, 2500)
    assert info["thumb_url"].endswith("/File:A.jpg") and info["source_url"].endswith("File:A.jpg")
    with pytest.raises(H.HeroError, match="找不到"):
        f.info("File:Missing.jpg")


def test_candidates_keep_only_usable_jpegs_and_mark_featured_pictures():
    files = {"File:Good.jpg": {}, "File:Portrait.jpg": {"width": 2400, "height": 3600},
             "File:Small.jpg": {"width": 1000, "height": 600}}
    hits = ["File:Good.jpg", "File:Portrait.jpg", "File:Small.jpg", "File:Map.svg"]
    cands = fetcher(commons(files, hits=hits)).candidates("Eiffel Tower")
    assert [c["title"] for c in cands] == ["File:Good.jpg"]                      # 同一张在三轮搜索里只留一次
    assert cands[0]["featured"] is True


def test_fetch_pick_writes_the_file_and_a_valid_record(tmp_path):
    rec = fetcher(commons({"File:A.jpg": {}})).fetch_pick("eiffel-tower", "File:A.jpg", today=TODAY, hero_dir=tmp_path)
    assert (tmp_path / "eiffel-tower.jpg").stat().st_size == 300_003
    assert (rec.file, rec.width, rec.license, rec.author) == ("eiffel-tower.jpg", 1920, "CC BY-SA 4.0", "Jane Doe")


def test_fetch_pick_falls_back_to_a_narrower_size_when_the_file_is_too_big(tmp_path):
    f = fetcher(commons({"File:A.jpg": {}}, size_by_width={1920: 1_200_000, 1600: 400_000}))
    rec = f.fetch_pick("eiffel-tower", "File:A.jpg", today=TODAY, hero_dir=tmp_path)
    assert rec.width == 1600 and (tmp_path / "eiffel-tower.jpg").stat().st_size < 900_000
    f2 = fetcher(commons({"File:A.jpg": {}}, size_by_width={1920: 1_200_000, 1600: 1_100_000}))
    with pytest.raises(H.HeroError, match="换一张"):
        f2.fetch_pick("louvre", "File:A.jpg", today=TODAY, hero_dir=tmp_path)
    assert not (tmp_path / "louvre.jpg").exists()                               # 失败不留半截文件


def test_the_contact_sheet_lists_every_candidate_with_credit():
    cand = {"title": "File:A.jpg", "thumb_url": "https://u/x.jpg", "source_url": "https://c/A",
            "original": (4000, 2500), "license": "CC BY-SA 4.0", "author": "Jane <b>", "featured": True}
    html = H.sheet_html({"eiffel-tower": [cand]})
    assert "eiffel-tower" in html and "File:A.jpg" in html and "4000×2500" in html and "特色图片" in html
    assert "&lt;b&gt;" in html and "Jane <b>" not in html                         # 作者名里的标签被转义


# --------------------------------------------------------------------------- #
# 真实数据（挑好图之后）
# --------------------------------------------------------------------------- #
def test_real_hero_data_is_complete_and_licensed():
    path = media.DATA_DIR / "hero.json"
    if not path.exists():
        pytest.skip("还没取封面大图（scripts/fetch_hero_images.py）")
    media.load_hero.cache_clear()
    heroes = media.load_hero()               # 加载本身就校验：文件在、体积不超限、授权白名单、署名齐全
    assert heroes
    from app.library import load_library

    lib = load_library("paris")
    for pid, img in heroes.items():
        assert pid in lib.places, f"{pid} 不在地点库里"
        assert img.width >= 1600 and media.license_allowed(img.license) and img.author and img.source_url
        assert (media.HERO_DIR / img.file).stat().st_size <= media.MAX_HERO_BYTES
    on_disk = {p.name for p in media.HERO_DIR.glob("*.jpg")}
    assert on_disk == {i.file for i in heroes.values()}, "hero/ 里有没人引用的文件"
