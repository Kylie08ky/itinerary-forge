# ruff: noqa: E501  （数据脚本：长句是文案，不折行）
"""现建城市的图片和简介（规格 10；规则同规格 M02 S7b / S7c）。

图片只用 Wikimedia Commons 上授权在白名单里的实拍图（地点的维基数据条目里的 P18「图像」）；作者 / 授权 / 来源链接 /
核对日期
缺一项就不收；简介只用中文维基百科摘要的前两句。图片下载到 `data/generated/photos/`（缩略图）和 `data/generated/hero/`
（封面大图，≥ 1600 宽的 JPEG，≤ 900KB）；应用运行时不再访问 Wikimedia。不由模型挑图、不由模型写简介。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx

from app.citybuild.store import generated_dir
from app.media import MAX_HERO_BYTES, MIN_HERO_WIDTH, HeroImage, PlaceImage, PlaceIntro, license_allowed

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
THUMB_WIDTH = 640
MAX_PHOTOS = 30                 # 一座城市最多下载这么多张（按名气从大到小）
HERO_TRIES = 4                  # 封面大图最多试前几个标志性地点
Progress = Callable[[str], None]


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def commons_image(client: httpx.Client, filename: str, width: int, *, pause: float = 0.3) -> dict | None:
    """Commons 文件页的缩略图地址、尺寸、作者、授权；授权不在白名单 / 没有作者 / 不是 JPEG、PNG → None。"""
    time.sleep(pause)
    try:
        r = client.get(COMMONS_API, timeout=30, params={
            "action": "query", "titles": f"File:{filename}", "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": width, "format": "json", "formatversion": 2})
        r.raise_for_status()
        info = r.json()["query"]["pages"][0]["imageinfo"][0]
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None
    meta = info.get("extmetadata") or {}
    lic = _plain((meta.get("LicenseShortName") or {}).get("value", ""))
    author = _plain((meta.get("Artist") or {}).get("value", ""))[:120]
    if (info.get("mime") not in ("image/jpeg", "image/png") or not license_allowed(lic) or not author
            or not info.get("thumburl") or not info.get("descriptionurl", "").startswith("https://commons.wikimedia.org/")):
        return None
    return {"thumb_url": info["thumburl"], "width": info.get("thumbwidth") or 0, "height": info.get("thumbheight") or 0,
            "orig_width": info.get("width") or 0, "mime": info["mime"], "source_url": info["descriptionurl"],
            "license": lic, "license_url": (meta.get("LicenseUrl") or {}).get("value"), "author": author,
            "title": f"File:{filename}"}


def _download(client: httpx.Client, url: str) -> bytes | None:
    try:
        r = client.get(url, timeout=60)
        r.raise_for_status()
        return r.content
    except httpx.HTTPError:
        return None


def two_sentences(extract: str, limit: int = 200) -> str:
    """摘要的前两句（超过 limit 就只留第一句再不行就截断）；没有 → 空。"""
    text = extract.strip().replace("\n", " ")
    parts = re.findall(r"[^。！？]+[。！？]", text)
    out = ""
    for s in parts[:2]:
        if len(out) + len(s) > limit:
            break
        out += s
    if not out and text:
        out = text[: limit - 1].rstrip("，、；：") + "…"
    return out


def _write(path: Path, data: bytes | dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data if isinstance(data, bytes) else (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    os.replace(tmp, path)


def fetch_media(client: httpx.Client, city_id: str, places: list[dict], raws: list[dict], today: date,
                say: Progress = lambda _m: None, *, pause: float = 0.3) -> tuple[int, list[str]]:
    """给城市里的地点下载图片、写简介，写 media 文件和封面大图文件；返回 (有图的地点数, 有封面大图的地点 id)。"""
    root = generated_dir()
    by_qid = {r["qid"]: r for r in raws}
    ranked = sorted((p for p in places if (p.get("draft_source") or {}).get("wikidata") in by_qid),
                    key=lambda p: -by_qid[p["draft_source"]["wikidata"]]["sitelinks"])
    media: dict[str, dict] = {}
    hero: dict[str, dict] = {}
    photos = 0
    for n, p in enumerate(ranked, 1):
        raw = by_qid[p["draft_source"]["wikidata"]]
        entry: dict = {}
        if photos < MAX_PHOTOS and raw.get("image"):
            say(f"图片 {n}/{len(ranked)}：{p['name_zh']}")
            img = commons_image(client, raw["image"], THUMB_WIDTH, pause=pause)
            data = _download(client, img["thumb_url"]) if img else None
            if img and data:
                ext = "png" if img["mime"] == "image/png" else "jpg"
                file = f"{p['id']}.{ext}"
                try:
                    pi = PlaceImage(file=file, title=img["title"], source_url=img["source_url"], author=img["author"],
                                    license=img["license"], license_url=img["license_url"], width=img["width"],
                                    height=img["height"], checked_at=today)
                except ValueError:
                    pi = None
                if pi is not None:
                    _write(root / "photos" / file, data)
                    entry["image"] = json.loads(pi.model_dump_json())
                    photos += 1
        if raw.get("intro") and raw.get("zh_title"):
            url = "https://zh.wikipedia.org/wiki/" + raw["zh_title"].replace(" ", "_")
            try:
                intro = PlaceIntro(text=raw["intro"], source_url=url, checked_at=today)
                entry["intro"] = json.loads(intro.model_dump_json())
            except ValueError:
                pass
        if entry:
            media[p["id"]] = entry
    # 封面大图：最有名的几个里，第一个有合格大图的；桥这类放最后（有别的就不用桥当封面）
    for p in sorted(ranked[:HERO_TRIES + 2], key=lambda p: by_qid[p["draft_source"]["wikidata"]].get("cls") == "Q12280")[:HERO_TRIES]:
        raw = by_qid[p["draft_source"]["wikidata"]]
        if not raw.get("image"):
            continue
        img = commons_image(client, raw["image"], MIN_HERO_WIDTH, pause=pause)
        if not img or img["mime"] != "image/jpeg" or img["orig_width"] < MIN_HERO_WIDTH or img["width"] < MIN_HERO_WIDTH:
            continue
        data = _download(client, img["thumb_url"])
        if not data or len(data) > MAX_HERO_BYTES:
            continue
        try:
            hi = HeroImage(file=f"{p['id']}.jpg", title=img["title"], source_url=img["source_url"], author=img["author"],
                           license=img["license"], license_url=img["license_url"], width=img["width"],
                           height=img["height"], checked_at=today)
        except ValueError:
            continue
        _write(root / "hero" / hi.file, data)
        hero[p["id"]] = {"image": json.loads(hi.model_dump_json())}
        break
    if media:
        _write(root / "media" / f"{city_id}.json", {"city": city_id, "places": media})
    if hero:
        _write(root / "media" / f"{city_id}.hero.json", {"city": city_id, "places": hero})
    return photos, list(hero)
