"""现建一座城市（规格 10）：种子 → 取数据 → 清理 → 核对 → 拼出城市配置 / 地点 / 主题 → 存下来。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx

from app.citybuild import store
from app.citybuild.core import apply_names, build_data, make_city, review_table
from app.citybuild.fetch import PAUSE_S, build_raws, climate_stats
from app.citybuild.media import fetch_media

Progress = Callable[[str], None]
MIN_PLACES = 6                 # 找到的景点比这还少，就不建（建出来也只会是一堆空白的天）
MIN_PLACES_MAINLAND = 3        # 国内城市：开放数据里景点常常很少，缺的可以在高德里搜了直接加，所以放宽
_OVERLAYS = Path(__file__).resolve().parents[2] / "data" / "city-overlays"
_SEEDS = Path(__file__).resolve().parents[2] / "data" / "city-seeds"


def load_overlays(city_id: str) -> dict:
    path = _OVERLAYS / f"{city_id}.json"
    return json.loads(path.read_text(encoding="utf-8")).get("places", {}) if path.exists() else {}


def reapply_overlays(city_id: str, today: date | None = None) -> int:
    """把精校重新叠到已存的地点上（不联网、不重建）；返回叠了几个地点。

    改了 data/city-overlays/ 里的官网事实后用它，几秒钟；删掉某条事实要重建才会消失。
    """
    from app.citybuild.core import apply_overlay

    path = store.generated_path("places", city_id)
    doc = json.loads(path.read_text(encoding="utf-8"))
    overlays = load_overlays(city_id)
    n = 0
    for i, place in enumerate(doc["places"]):
        overlay = overlays.get((place.get("draft_source") or {}).get("wikidata"))
        if overlay:
            doc["places"][i] = apply_overlay(place, overlay, today or date.today())
            n += 1
    store._write(path, doc)
    return n


def with_curated_seed(seed: dict) -> dict:
    """data/city-seeds/<id>.json 里手写的东西（补充名单、排除名单、市区范围、半径）盖过自动算的；没有就原样。"""
    path = _SEEDS / f"{seed['id']}.json"
    if not path.exists():
        return seed
    curated = json.loads(path.read_text(encoding="utf-8"))
    merged = {**seed, **{k: curated[k] for k in ("include", "exclude", "bbox", "radius_km", "min_links", "top_n",
                                                  "suggested_days", "metro", "names") if k in curated}}
    merged["aliases"] = sorted({*seed.get("aliases", []), *curated.get("aliases", [])})
    return merged


def build_city(seed: dict, client: httpx.Client, say: Progress = lambda _m: None, *, today: date | None = None,
               pause: float = PAUSE_S) -> tuple[dict, list[dict], dict, str, list[dict]]:
    """返回 (城市配置, 地点, 主题包, 审阅表, 取好的原始候选)。不写文件。"""
    today = today or date.today()
    seed = with_curated_seed(seed)
    raws = build_raws(seed, client, say, pause, today)
    apply_names(raws, seed)
    least = MIN_PLACES_MAINLAND if seed.get("country") == "CN" else MIN_PLACES
    if len(raws) < least:
        raise RuntimeError(
            f"开放数据（维基数据 / OpenStreetMap）里「{seed['name_zh']}」只找到 {len(raws)} 个景点，"
            "排不出有意义的行程；可以换成附近更大的城市，或者先用它周边的城市")
    say("查当地的气温（行李清单用）")
    seed = {**seed, **(climate_stats(client, seed["center"][0], seed["center"][1], today.year - 1) or {})}
    say("整理成城市数据")
    places, themes = build_data(seed["id"], seed["name_zh"], raws, today, load_overlays(seed["id"]))
    city = make_city(seed["id"], seed["name_zh"], seed["name_en"], tuple(seed["center"]), seed["bbox"],
                     [p["id"] for p in places], today=today, seed=seed)
    return city, places, themes, review_table(seed["name_zh"], places, raws, today), raws


def build_and_store(seed: dict, client: httpx.Client, say: Progress = lambda _m: None, *, today: date | None = None,
                    pause: float = PAUSE_S) -> dict:
    """现建并存到 data/generated/，让下次直接用；返回城市配置。"""
    from app.cities import reload_cities

    city, places, themes, _table, raws = build_city(seed, client, say, today=today, pause=pause)
    if not themes["themes"]:
        raise RuntimeError(f"「{seed['name_zh']}」的景点数据太少，建不出主题")
    try:
        day = today or date.today()
        _photos, heroes = fetch_media(client, city["id"], places, raws, day, say, pause=min(pause, 0.5))
        if heroes:                                      # 有封面大图的地点排第一：首页 / 行程封面先用它
            city["landmarks"] = [*heroes, *[p for p in city["landmarks"] if p not in heroes]][:3]
    except Exception:  # noqa: BLE001 - 图片是锦上添花，下载失败不能让建城市失败
        pass
    store.save_generated(city, places, themes)
    reload_cities()
    try:                                                # 当地名菜也是锦上添花：取不到不影响建城市（H22）
        from app.citybuild.dishes import build_dishes
        from app.citybuild.fetch import CityFetcher

        dishes = build_dishes(CityFetcher(client, min(pause, 0.5)), seed["name_zh"], today or date.today(), say)
        if dishes:
            store.save_dishes(city["id"], dishes)
    except Exception:  # noqa: BLE001
        pass
    return city
