"""为地点库取「当地语言名」（规格 07 P18）：OpenStreetMap 的 `name` 标签 + 维基数据的当地语言标签，两个来源交叉核对。

用法：
    python scripts/fetch_local_names.py                      # 补上还没有当地名的地点
    python scripts/fetch_local_names.py --only louvre,orsay
    python scripts/fetch_local_names.py --refresh            # 已有的也重新取
    python scripts/fetch_local_names.py --dry-run            # 只看会取到什么，不写文件

怎么保证「取对了、不是模型编的」：
- 当地名**只来自两个公开来源**：Nominatim（OSM）里离地点库坐标 MATCH_RADIUS_M 米内、有名字的最近候选的
  `name:<语言>` / `name`；  和它带的维基数据编号（Q…）对应的、当地语言的维基数据标签。模型不参与。
- 两边忽略大小写 / 重音 / 标点后一致 → `verified`（名字取带重音的维基数据写法）；只有一个来源、或两边对不上、
  或维基数据的中文名和库里的中文名完全对不上（可能匹配错了地点）→ `draft`，
  并写进 `<城市>.local_names.review.json` 等人看。
- 只发公开的只读请求，Nominatim 每次请求之间等 1 秒（他们的使用规定），User-Agent 里写明用途和联系方式。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apply_verification import dump  # noqa: E402

from app.cities import load_city  # noqa: E402
from app.names import fold  # noqa: E402

UA = "ai-travel-planner-agent-names/0.1 (https://github.com/Kylie08ky; personal project)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
WIKIDATA = "https://www.wikidata.org/w/api.php"
MATCH_RADIUS_M = 500
PAUSE_S = 1.0
PLACES_DIR = ROOT / "data" / "places"

# 有些地点的英文名是描述（「Le Marais walk」「Galeries Lafayette rooftop」），OSM 里搜不到。这里只覆盖**搜索词**——
# 要在 OSM 里找的那个真实对象；当地名仍然从 OSM / 维基数据读出来，不在这里写。
# 没有官方名称的通用活动（游船、购物 / 街区散步）不覆盖，留空由「没找到」清单列出，页面上只显示中文名。
QUERY_OVERRIDES: dict[str, str] = {
    "notre-dame": "Cathédrale Notre-Dame de Paris",
    "latin-quarter": "Quartier latin",
    "marais-boutiques": "Le Marais",
    "lafayette-rooftop": "Galeries Lafayette Haussmann",
    "galeries-lafayette-shopping": "Galeries Lafayette Haussmann",
    "marais-walk": "Le Marais",
    "printemps": "Printemps Haussmann",
    "lido": "Lido de Paris",
    "enfants-rouges": "Marché des Enfants Rouges",
    "galerie-dior": "Galerie Dior",
    "palais-garnier": "Opéra Garnier",
    "invalides": "Hôtel des Invalides",
    "pierre-herme-st-germain": "Pierre Hermé, rue Bonaparte",
}
# 查到的名字看过确认不对（别的对象、不是景点本身），不采用；重跑时也不会再采用。
REJECT: dict[str, str] = {
    "enfants-rouges": "OSM 在这个坐标上只有市场里的一家咖啡馆（Le Café du Marché des Enfants Rouges），不是市场本身",
}
# 街区这类大范围对象，OSM / 维基数据的坐标是区域中心，离地点库里的散步起点比较远：单独放宽半径（米）
RADIUS_M: dict[str, int] = {"latin-quarter": 1500, "marais-walk": 1500, "marais-boutiques": 1500}
# 维基数据的中文标签和库里的中文名对不上，但人工看过确认是同一个地点（繁体 / 译名不同 / 分店）：保持 verified。
IDENTITY_CONFIRMED: dict[str, str] = {
    "orangerie": "维基数据中文名是繁体「橘園美術館」",
    "picasso": "同一座博物馆，译名不同（毕加索 / 畢卡索）",
    "palais-de-tokyo": "维基数据中文名是繁体「東京宮」",
    "cite-des-sciences": "同一机构，中文名写法不同（法國科學工業城）",
    "laduree-champs": "库里写的是香榭丽舍分店，OSM 在该店坐标上，品牌名 Ladurée",
    "marais-walk": "玛黑区 = 玛莱区，同一个街区",
    "marais-boutiques": "玛黑区 = 玛莱区，同一个街区",
}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Result:
    id: str
    name_local: str | None
    check: dict | None
    review: str                # 空 = 不用人看


# --------------------------------------------------------------------------- #
# 取数据
# --------------------------------------------------------------------------- #
def search_osm(client: httpx.Client, query: str, country: str | None = None) -> list[dict]:
    params = {"q": query, "format": "jsonv2", "namedetails": "1", "extratags": "1", "limit": "6"}
    if country:
        params["countrycodes"] = country.lower()
    r = client.get(NOMINATIM, params=params, timeout=30)
    r.raise_for_status()
    time.sleep(PAUSE_S)
    return r.json()


ZH_VARIANTS = ("zh-hans", "zh-cn", "zh")           # 维基数据的中文标签：简体优先，没有才用泛中文（常常是繁体）


def _wikidata_get(client: httpx.Client, params: dict) -> dict:
    r = client.get(WIKIDATA, params={**params, "format": "json"}, timeout=30)
    r.raise_for_status()
    time.sleep(PAUSE_S)
    return r.json()


def wikidata_labels(client: httpx.Client, qid: str, langs: list[str]) -> dict[str, str]:
    data = _wikidata_get(client, {"action": "wbgetentities", "ids": qid, "props": "labels",
                                  "languages": "|".join(langs)})
    entity = (data.get("entities") or {}).get(qid) or {}
    return {k: v["value"] for k, v in (entity.get("labels") or {}).items()}


def wikidata_search(client: httpx.Client, text: str, language: str) -> list[str]:
    data = _wikidata_get(client, {"action": "wbsearchentities", "search": text, "language": language,
                                  "uselang": language, "type": "item", "limit": "5"})
    return [x["id"] for x in data.get("search", [])]


def wikidata_point(client: httpx.Client, qid: str) -> tuple[float, float] | None:
    """维基数据条目的坐标（P625）；没有 → None。"""
    data = _wikidata_get(client, {"action": "wbgetclaims", "entity": qid, "property": "P625"})
    for claim in data.get("claims", {}).get("P625", []):
        v = (claim.get("mainsnak", {}).get("datavalue") or {}).get("value")
        if v:
            return float(v["latitude"]), float(v["longitude"])
    return None


# --------------------------------------------------------------------------- #
# 选对、取名、核对
# --------------------------------------------------------------------------- #
def _junk(name: str) -> bool:
    """OSM 里挂在景点旁边的停车场入口、话题标签之类，不是景点本身的名字。"""
    low = name.strip().lower()
    return low.startswith("#") or low.startswith(("accès", "acces")) or "parking" in low


def pick_candidate(cands: list[dict], lat: float, lng: float, radius_m: float = MATCH_RADIUS_M) -> dict | None:
    """离地点库坐标最近、有名字、且在半径内的候选；没有 → None（同名的别处、没名字的都进不来）。"""
    best: tuple[float, dict] | None = None
    for c in cands:
        name = (c.get("namedetails") or {}).get("name")
        if not name or _junk(name):
            continue
        d = haversine_m(lat, lng, float(c["lat"]), float(c["lon"]))
        if d <= radius_m and (best is None or d < best[0]):
            best = (d, c)
    return best[1] if best else None


def osm_local_name(cand: dict, lang: str) -> str | None:
    details = cand.get("namedetails") or {}
    return details.get(f"name:{lang}") or details.get("name") or None


def _accents(s: str) -> int:
    return sum(ord(c) > 127 for c in s)


_STOP = {"de", "du", "des", "la", "le", "les", "l", "d", "et", "en", "sur", "the", "of", "di", "del", "della", "il"}


def _tokens(s: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", unicodedata.normalize("NFKD", s.replace("œ", "oe").replace("Œ", "oe")).lower())
    return {w for w in ("".join(c for c in x if not unicodedata.combining(c)) for x in words) if w and w not in _STOP}


# 地名里到处都有的词：只靠它们重合不算「说的是同一个地方」（「Musée Carnavalet」和「Le Marais」不算相关）
_GENERIC = {
    "musee", "jardin", "pont", "rue", "place", "palais", "chateau", "tour", "avenue", "cathedrale", "basilique",
    "eglise", "galerie", "galeries", "centre", "paris", "parc", "passage", "cafe", "hotel", "fondation", "marche",
    "quartier", "boulevard", "saint", "sainte", "restaurant", "walk", "shopping", "museum", "garden", "bridge",
}


def _relevant(osm_name: str, refs: list[str]) -> bool:
    """OSM 候选的名字和要找的东西（搜索词、库里的英文名）至少共有一个有区分度的词；否则找到的是旁边别的东西。"""
    mine = _tokens(osm_name) - _GENERIC
    for ref in refs:
        theirs = _tokens(ref) - _GENERIC
        if not theirs or mine & theirs:
            return True
    return False


def _consistent(a: str, b: str) -> bool:
    """两个写法指同一个名字：忽略大小写 / 重音 / 标点后相同，或一个的词全在另一个里（官方全称和常用简称）。"""
    if fold(a) == fold(b):
        return True
    ta, tb = _tokens(a), _tokens(b)
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return bool(small) and small <= big and sum(map(len, small)) >= 5


def _cjk(s: str) -> str:
    return "".join(c for c in s if "一" <= c <= "鿿")


def _zh_overlaps(wd_zh: str | None, name_zh: str) -> bool:
    """维基数据的中文名和库里的中文名对得上（确认匹配的是同一个地点）；库里的名字是外文品牌时不判断。"""
    mine = _cjk(name_zh)
    if not wd_zh or len(mine) < 2:
        return True
    theirs = _cjk(wd_zh)
    if mine in theirs or theirs in mine:
        return True
    return any(mine[i:i + 2] in theirs for i in range(len(mine) - 1))         # 至少共有两个相邻的字


def _pick_zh(labels: dict[str, str]) -> str | None:
    return next((labels[k] for k in ZH_VARIANTS if k in labels), None)


def _capitalize(name: str) -> str:
    """法语 / 意大利语的普通名词写法（维基数据「musée Rodin」、OSM「tour Eiffel」），首字母改大写，别的一概不动。"""
    return name[:1].upper() + name[1:]


def _from_osm(client: httpx.Client, place: dict, lang: str, query: str, city_name: str, country: str | None,
              today: date) -> Result | None:
    radius = RADIUS_M.get(place["id"], MATCH_RADIUS_M)
    cand = pick_candidate(search_osm(client, f"{query}, {city_name}", country), place["lat"], place["lng"], radius)
    if cand is None:                                                   # 换个问法再试一次
        cand = pick_candidate(search_osm(client, query, country), place["lat"], place["lng"], radius)
    if cand is None:
        return None
    osm_name = osm_local_name(cand, lang)
    if osm_name and not _relevant(osm_name, [query, place["name_en"]]):
        return None                                    # 找到的是旁边别的东西（比如街区旁边的博物馆）：不采用
    qid = (cand.get("extratags") or {}).get("wikidata")
    labels = wikidata_labels(client, qid, [lang, *ZH_VARIANTS]) if qid else {}
    wd_name = labels.get(lang)
    wd_zh = _pick_zh(labels)
    notes: list[str] = []

    if osm_name and wd_name and _consistent(osm_name, wd_name):
        # 两边只差大小写 / 重音：重音写全的优先；一样（或一个是另一个的简称）就取 OSM 的，它是路牌 / 官方标注
        name = wd_name if fold(osm_name) == fold(wd_name) and _accents(wd_name) > _accents(osm_name) else osm_name
        source, status = "osm+wikidata", "verified"
    elif osm_name and wd_name:
        name, source, status = osm_name, "osm+wikidata", "draft"
        notes.append(f"OSM「{osm_name}」≠ 维基数据「{wd_name}」，暂取 OSM 的")
    elif osm_name:
        name, source, status = osm_name, "osm", "draft"
        why = "OSM 没带维基数据编号" if not qid else f"维基数据 {qid} 没有 {lang} 标签"
        notes.append(f"只有 OSM 一个来源，没法交叉核对（{why}）")
    elif wd_name:
        name, source, status = _capitalize(wd_name), "wikidata", "draft"
        notes.append("OSM 没有名字，只有维基数据一个来源")
    else:
        return None
    if status == "verified" and not _zh_overlaps(wd_zh, place["name_zh"]) and place["id"] not in IDENTITY_CONFIRMED:
        status = "draft"
        notes.append(f"维基数据中文名「{wd_zh}」和库里的「{place['name_zh']}」对不上，请确认匹配的是同一个地点")
    return Result(place["id"], _capitalize(name), {"source": source, "status": status,
                                                    "checked_at": today.isoformat(), "wikidata": qid}, "；".join(notes))


def _from_wikidata(client: httpx.Client, place: dict, lang: str, today: date) -> Result | None:
    """OSM 找不到（或找到的像是别的地点）时：按英文名 / 中文名搜维基数据，坐标在半径内、中文名对得上的才算。
    身份由「坐标 + 中文名」两样独立确认，名字取这个条目的当地语言标签。"""
    seen: set[str] = set()
    for text, language in ((place["name_en"], "en"), (_cjk(place["name_zh"]), "zh")):
        if not text:
            continue
        for qid in wikidata_search(client, text, language):
            if qid in seen:
                continue
            seen.add(qid)
            pt = wikidata_point(client, qid)
            if pt is None or haversine_m(place["lat"], place["lng"], *pt) > RADIUS_M.get(place["id"], MATCH_RADIUS_M):
                continue
            labels = wikidata_labels(client, qid, [lang, *ZH_VARIANTS])
            name = labels.get(lang)
            if not name or not _zh_overlaps(_pick_zh(labels), place["name_zh"]) or not _pick_zh(labels):
                continue
            return Result(place["id"], _capitalize(name), {"source": "wikidata", "status": "verified",
                                                           "checked_at": today.isoformat(), "wikidata": qid}, "")
    return None


def resolve(client: httpx.Client, place: dict, lang: str, *, today: date, city_name: str = "Paris",
            country: str | None = None) -> Result:
    pid = place["id"]
    if pid in REJECT:
        return Result(pid, None, None, f"已排除：{REJECT[pid]}")
    query = QUERY_OVERRIDES.get(pid, place["name_en"])
    first = _from_osm(client, place, lang, query, city_name, country, today)
    if first is not None and first.check["status"] == "verified":
        return first
    second = _from_wikidata(client, place, lang, today)        # OSM 没找到 / 只是草稿：换维基数据按坐标 + 中文名再找
    if second is not None:
        return second
    if first is not None:
        return first
    return Result(pid, None, None, f"没找到：OSM 在 {MATCH_RADIUS_M} 米内没有叫「{query}」的有名字的地点，"
                                   "维基数据里也没有坐标和中文名都对得上的条目")


# --------------------------------------------------------------------------- #
# 写回
# --------------------------------------------------------------------------- #
def apply(places: list[dict], results: list[Result], *, refresh: bool) -> int:
    """把结果写进地点（原地改）；已有当地名的默认不动。返回改了几个。"""
    by_id = {p["id"]: p for p in places}
    changed = 0
    for r in results:
        p = by_id.get(r.id)
        if p is None:
            continue
        if r.name_local is None:
            if refresh and p.get("name_local") and r.review.startswith("已排除"):      # 看过确认不对的名字，清掉
                p.pop("name_local", None)
                p.pop("name_local_check", None)
                changed += 1
            continue
        if p.get("name_local") and not refresh:
            continue
        p["name_local"] = r.name_local
        p["name_local_check"] = r.check
        changed += 1
    return changed


def write_review(path: Path, results: list[Result], *, today: date) -> None:
    """待核对清单：只放需要人看的（草稿 / 没找到 / 已排除）。和已有的合并——这次处理过的按新结果更新，没处理的保留。"""
    items: dict[str, dict] = {}
    if path.exists():
        items = {i["id"]: i for i in json.loads(path.read_text(encoding="utf-8")).get("items", [])}
    for r in results:
        status = (r.check or {}).get("status", "missing")
        if status == "verified" and not r.review:
            items.pop(r.id, None)
        else:
            items[r.id] = {"id": r.id, "name_local": r.name_local, "status": status, "note": r.review}
    path.write_text(json.dumps({"checked_at": today.isoformat(), "items": list(items.values())},
                               ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="paris")
    ap.add_argument("--only", default="", help="只处理这些地点编号，逗号分隔")
    ap.add_argument("--refresh", action="store_true", help="已有当地名的地点也重新取")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args()

    city = load_city(args.city)
    path = PLACES_DIR / f"{args.city}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    today = date.fromisoformat(args.today)

    results: list[Result] = []
    with httpx.Client(headers={"User-Agent": UA}) as client:
        for p in data["places"]:
            if only and p["id"] not in only:
                continue
            if p.get("name_local") and not args.refresh:
                continue
            try:
                r = resolve(client, p, city.local_language, today=today, city_name=city.name_en, country=city.country)
            except httpx.HTTPError as exc:
                r = Result(p["id"], None, None, f"请求失败：{type(exc).__name__}: {exc}")
            results.append(r)
            mark = {"verified": "ok", "draft": "??"}.get((r.check or {}).get("status", ""), "--")
            print(f"[{mark}] {r.id}: {r.name_local or '-'} {r.review}", flush=True)

    if args.dry_run:
        return 0
    changed = apply(data["places"], results, refresh=args.refresh)
    dump(path, data)
    write_review(PLACES_DIR / f"{args.city}.local_names.review.json", results, today=today)
    print(f"写入 {changed} 个；待核对 {sum(1 for r in results if r.review or r.check is None)} 个"
          f"（见 data/places/{args.city}.local_names.review.json）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
