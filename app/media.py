"""地点图片与简介（规格 M02 S7b）：从 data/places/media.json 读，和地点库按地点编号合并。

- 图片只用 Wikimedia Commons 上授权在白名单里的实拍图；简介只用中文维基百科摘要的前两句。
- 作者 / 授权 / 来源链接 / 核对日期缺一项就不收（硬规则）；这里的校验就是那道闸。
- 不由模型挑图、不由模型写简介；脚本 `scripts/fetch_place_media.py` 只在手动运行时联网，应用运行时不访问 Wikimedia。
"""

from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.cities import DEFAULT_CITY, load_city
from app.citybuild.store import generated_dir

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "places"
PHOTOS_DIR = DATA_DIR / "photos"
HERO_DIR = DATA_DIR / "hero"                 # 封面大图（规格 M02 S7c）：≥ 1600 宽的横版实拍图
HERO_PATH = DATA_DIR / "hero.json"
MIN_HERO_WIDTH = 1600
MAX_HERO_BYTES = 900_000

MAX_INTRO_CHARS = 200
COMMONS_PREFIX = "https://commons.wikimedia.org/"
WIKIPEDIA_PREFIX = "https://zh.wikipedia.org/"

# 授权白名单：CC0、公有领域、CC BY / CC BY-SA 各版本。NC（禁止商用）、ND、GFDL、合理使用一律不收。
_LICENSE_OK = re.compile(
    r"^(CC0(?: 1\.0)?|Public domain|PD(?:[- ][\w.-]+)*|CC[- ]BY(?:-SA)?[- ]\d\.\d(?: [A-Za-z]+)?)$", re.IGNORECASE
)


def license_allowed(name: str | None) -> bool:
    """授权简称（Commons 的 LicenseShortName，如「CC BY-SA 4.0」）是否在白名单里。"""
    return bool(name and _LICENSE_OK.match(name.strip()))


class PlaceImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*\.(jpg|png)$")   # 在 data/places/photos/ 下
    title: str = Field(min_length=1)                   # Commons 文件名（File:xxx.jpg）
    source_url: str                                    # Commons 的文件页：署名里的「来源」
    author: str = Field(min_length=1)
    license: str
    license_url: str | None = None
    width: int = Field(ge=200)
    height: int = Field(ge=100)
    checked_at: date

    @field_validator("source_url")
    @classmethod
    def _commons(cls, v: str) -> str:
        if not v.startswith(COMMONS_PREFIX):
            raise ValueError(f"图片来源必须是 Wikimedia Commons 的文件页：{v}")
        return v

    @field_validator("license")
    @classmethod
    def _license(cls, v: str) -> str:
        if not license_allowed(v):
            raise ValueError(f"授权「{v}」不在白名单里（CC0 / 公有领域 / CC BY / CC BY-SA）")
        return v


class HeroImage(PlaceImage):
    """封面大图：和缩略图同样的署名要求，但必须是 JPEG 且宽 ≥ 1600。"""

    file: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*\.jpg$")
    width: int = Field(ge=MIN_HERO_WIDTH)


class PlaceIntro(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_INTRO_CHARS)
    source_url: str
    source_name: str = "中文维基百科"
    license: str = "CC BY-SA 4.0"
    license_url: str = "https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hans"
    checked_at: date

    @field_validator("source_url")
    @classmethod
    def _wikipedia(cls, v: str) -> str:
        if not v.startswith(WIKIPEDIA_PREFIX):
            raise ValueError(f"简介来源必须是中文维基百科的条目：{v}")
        return v


class PlaceMedia(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: PlaceImage | None = None
    intro: PlaceIntro | None = None

    @model_validator(mode="after")
    def _something(self) -> PlaceMedia:
        if self.image is None and self.intro is None:
            raise ValueError("既没有图片也没有简介的地点不要写进 media.json")
        return self


def is_generated(city: str) -> bool:
    """现建的城市（规格 10）：地点库不在精校的 data/places/ 里，图片和封面放在 data/generated/。"""
    return not (DATA_DIR / f"{city}.json").exists() and (generated_dir() / "places" / f"{city}.json").exists()


def photo_path(filename: str) -> Path:
    """地点缩略图的位置：精校的 photos/ 里先找，再找现建的。"""
    path = PHOTOS_DIR / filename
    return path if path.is_file() else generated_dir() / "photos" / filename


def hero_path(filename: str) -> Path:
    path = HERO_DIR / filename
    return path if path.is_file() else generated_dir() / "hero" / filename


@lru_cache(maxsize=16)
def load_media(city: str = DEFAULT_CITY) -> dict[str, PlaceMedia]:
    """{地点编号: 图片 + 简介}；没有这个文件就是空（所有地点都当没有图片）。图片文件必须真的存在。"""
    path = generated_dir() / "media" / f"{city}.json" if is_generated(city) else DATA_DIR / "media.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, PlaceMedia] = {}
    for pid, entry in raw.get("places", {}).items():
        m = PlaceMedia.model_validate(entry)
        if m.image and not photo_path(m.image.file).is_file():
            raise ValueError(f"{pid}: 图片文件不存在：{m.image.file}")
        out[pid] = m
    return out


@lru_cache(maxsize=16)
def load_hero(city: str = DEFAULT_CITY) -> dict[str, HeroImage]:
    """{地点编号: 封面大图}；没有这个文件就是空。文件必须真的存在、体积不超过上限。"""
    path = generated_dir() / "media" / f"{city}.hero.json" if is_generated(city) else HERO_PATH
    if not path.exists():
        return {}
    out: dict[str, HeroImage] = {}
    for pid, entry in json.loads(path.read_text(encoding="utf-8")).get("places", {}).items():
        img = HeroImage.model_validate(entry["image"])
        file = hero_path(img.file)
        if not file.is_file():
            raise ValueError(f"{pid}: 封面大图文件不存在：{img.file}")
        if file.stat().st_size > MAX_HERO_BYTES:
            raise ValueError(f"{pid}: 封面大图超过 {MAX_HERO_BYTES // 1000}KB：{img.file}")
        out[pid] = img
    return out


def _image_json(i: PlaceImage, base: str) -> dict[str, Any]:
    return {"url": f"{base}/{i.file}", "title": i.title, "author": i.author, "license": i.license,
            "license_url": i.license_url, "source_url": i.source_url, "width": i.width, "height": i.height}


def pick_hero(city: str, place_ids: list[str] | None = None) -> dict[str, Any] | None:
    """一座城市（或这趟行程）的代表大图：先按城市配置里 landmarks 的顺序找（行程里没有的跳过），
    再按行程里出现的顺序；`place_ids` 不给 = 这座城市的第一张标志性大图。和行程页封面的选图规则一致。"""
    heroes = load_hero(city)
    landmarks = load_city(city).landmarks
    if place_ids is None:
        order = landmarks
    else:
        trip = list(dict.fromkeys(place_ids))
        order = [p for p in landmarks if p in trip] + [p for p in trip if p not in landmarks]
    for pid in order:
        if pid in heroes:
            return {"place_id": pid, **_image_json(heroes[pid], "/media/hero")}
    return None


def media_payload(place_id: str, city: str = DEFAULT_CITY) -> dict[str, Any] | None:
    """接口里 `media` 字段的样子：图片给本地地址 + 署名，简介给文字 + 来源；都没有返回 None。"""
    m = load_media(city).get(place_id)
    hero = load_hero(city).get(place_id)
    if m is None and hero is None:
        return None
    out: dict[str, Any] = {}
    if hero:
        out["hero"] = _image_json(hero, "/media/hero")          # 封面大图（M02 S7c）；只有标志性地点有
    if m is None:
        return out
    if m.image:
        out["image"] = _image_json(m.image, "/media/places")
    if m.intro:
        t = m.intro
        out["intro"] = {"text": t.text, "source_url": t.source_url, "source_name": t.source_name,
                        "license": t.license, "license_url": t.license_url}
    return out
