"""建「轻量城市」（周边一日游目的地）的开放数据草稿（规格 08 3.4）。

建完之后要跑 scripts/simplify_chinese.py（把维基数据里的繁体中文转成简体，规格 08 3.7）。

用法：
    python scripts/build_light_city.py --city tuebingen
    python scripts/build_light_city.py --origin stuttgart      # 这个出发地的全部目的地
    python scripts/build_light_city.py --city tuebingen --dry-run

做什么（全部来自公开数据，不让模型编；每个点标「草稿」并带来源和日期）：
1. 维基数据：城市的中文名（简体优先）、坐标；SPARQL 找市中心 `centre_radius_m` 米内的
   景点 / 古迹 / 博物馆 / 教堂 / 公园。
2. 按语言版本数排序（越多越出名；有中文维基的加分，因为游客是中国人），取前 TOP_N 个。
3. 每个点：当地名 = 维基数据当地语言标签，再用 OSM（Nominatim）里同一个维基数据编号的对象核对；
   中文名 = 维基数据简体中文标签，没有就直接用当地名；官网 = OSM / 维基数据的官网，能打开才留；
   「每周闭馆日」来自 OSM 的 opening_hours；中文说明 = 中文维基百科摘要第一句。
   价格 / 开放时间一律写「出发前请到官网确认」。
4. 写 data/cities/<id>.json、data/places/<id>.json、data/themes/<id>.json（共用一个「一日游」主题）。
只发公开的只读请求，请求之间有停顿，User-Agent 里写明用途和联系方式。
（Overpass 公共服务常常满载，所以不用它；OSM 数据通过 Nominatim 取。）
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apply_verification import dump  # noqa: E402
from daytrip_destinations import DESTINATIONS, ORIGINS, Destination  # noqa: E402
from fetch_local_names import _capitalize, _consistent, _junk  # noqa: E402
from verify_library import check_url, parse_closed_days  # noqa: E402

UA = "ai-travel-planner-agent-build/0.1 (https://github.com/Kylie08ky; personal project)"
SPARQL = "https://query.wikidata.org/sparql"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
WIKIDATA = "https://www.wikidata.org/w/api.php"
ZH_SUMMARY = "https://zh.wikipedia.org/api/rest_v1/page/summary/{title}"
DATA = ROOT / "data"
TOP_N = 10
PAUSE_S = 1.0
OSM_MATCH_M = 300
ZH_LABELS = ("zh-hans", "zh-cn", "zh")
THEME_ID = "daytrip"

# 维基数据「是什么」→ 等价的 OSM 标签（给 classify 用）。景点类的类别，其余的（住宅、商店、大学楼……）不收。
CLASS_TAGS: dict[str, dict] = {
    "Q570116": {"tourism": "attraction"}, "Q33506": {"tourism": "museum"}, "Q16970": {"amenity": "place_of_worship"},
    "Q2977": {"amenity": "place_of_worship"}, "Q44613": {"amenity": "place_of_worship"},
    "Q160742": {"amenity": "place_of_worship"}, "Q23413": {"historic": "castle"}, "Q16560": {"historic": "palace"},
    "Q57831": {"historic": "fort"}, "Q543654": {"amenity": "townhall"}, "Q22698": {"leisure": "park"},
    "Q167346": {"leisure": "garden"}, "Q174782": {"place": "square"}, "Q12280": {"man_made": "bridge"},
    "Q12518": {"man_made": "tower"}, "Q4989906": {"historic": "monument"}, "Q483453": {"historic": "monument"},
    "Q109607": {"historic": "ruins"}, "Q43501": {"tourism": "zoo"}, "Q2416723": {"tourism": "theme_park"},
    "Q2281788": {"tourism": "aquarium"}, "Q751876": {"historic": "palace"}, "Q1762114": {"historic": "monument"},
}
SPARQL_TEMPLATE = """
SELECT ?item ?coord ?sitelinks (SAMPLE(?cls) AS ?class) WHERE {{
  SERVICE wikibase:around {{ ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point({lon} {lat})"^^geo:wktLiteral . bd:serviceParam wikibase:radius "{km}" . }}
  ?item wikibase:sitelinks ?sitelinks . FILTER(?sitelinks >= {minlinks})
  ?item wdt:P31/wdt:P279* ?cls .
  VALUES ?cls {{ {classes} }}
}}
GROUP BY ?item ?coord ?sitelinks ORDER BY DESC(?sitelinks) LIMIT 80
"""


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def clean_name(text: str) -> str:
    """维基百科的消歧括号（「Frauenkirche (Esslingen)」「老桥 (海德堡)」）不是名字的一部分，去掉。"""
    return re.sub(r"\s*[(（][^)）]*[)）]\s*$", "", text).strip() or text


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower().replace("ß", "ss").replace("œ", "oe").replace("æ", "ae"))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def point_of(el: dict) -> tuple[float, float] | None:
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    c = el.get("center")
    return (c["lat"], c["lon"]) if c else None


def classify(tags: dict) -> tuple[str, str, int, bool] | None:
    """OSM 标签 → (category, setting, 建议停留分钟, 是否公共开放空间)；不是景点 → None。"""
    tourism, historic = tags.get("tourism"), tags.get("historic")
    building, leisure, amenity = tags.get("building"), tags.get("leisure"), tags.get("amenity")
    free = tags.get("fee") == "no"
    if tourism in ("museum", "gallery"):
        return "museum", "indoor", 90, False
    if tourism in ("zoo", "theme_park"):
        return "activity", "outdoor", 180, False
    if tourism == "aquarium":
        return "activity", "indoor", 90, False
    if tourism == "viewpoint":
        return "sight", "outdoor", 30, True
    if historic in ("castle", "palace", "manor", "fort") or building in ("castle", "palace"):
        return "sight", "mixed", 90, False
    if historic in ("church",) or building in ("cathedral", "church") or amenity == "place_of_worship":
        return "sight", "indoor", 30, True
    if leisure in ("park", "garden"):
        return "park", "outdoor", 45, True
    if historic in ("city_gate", "ruins", "archaeological_site", "monument", "tower", "building"):
        return "sight", "outdoor", 25, True
    if building == "town_hall" or amenity == "townhall":
        return "sight", "outdoor", 20, True
    if tags.get("place") == "square" or tags.get("man_made") in ("tower", "bridge"):
        return "sight", "outdoor", 20, True
    if tourism == "attraction":
        return "sight", "outdoor", 30, free
    return None


def osm_ref(el: dict) -> str | None:
    return el.get("osm")                                  # 「node/123」；只有维基数据、OSM 里没对上的为 None


def score(sitelinks: dict) -> int:
    """越多语言版本越出名；有中文维基的加分（游客是中国人）。"""
    n = len(sitelinks)
    return n + (10 if "zhwiki" in sitelinks else 0)


def pick_top(elements: list[dict], info: dict[str, dict], n: int, centre: tuple[float, float]) -> list[dict]:
    """从候选里选前 n 个：有维基数据的按分数排，同名 / 同条目去重；名字是垃圾（停车场入口等）的不要。"""
    seen_q: set[str] = set()
    seen_name: set[str] = set()
    ranked: list[tuple[int, float, dict]] = []
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name")
        pt = point_of(el)
        q = tags.get("wikidata")
        if not name or pt is None or not q or _junk(name) or classify(tags) is None:
            continue
        ranked.append((-score(info.get(q, {}).get("sitelinks", {})), haversine_m(*centre, *pt), el))
    ranked.sort(key=lambda t: (t[0], t[1]))
    out: list[dict] = []
    for _s, _d, el in ranked:
        q, key = el["tags"]["wikidata"], slug(el["tags"]["name"])
        if q in seen_q or key in seen_name:
            continue
        seen_q.add(q)
        seen_name.add(key)
        out.append(el)
        if len(out) == n:
            break
    return out


def first_sentence(extract: str, limit: int = 90) -> str:
    s = extract.strip().replace("\n", " ")
    cut = s.find("。")
    s = s[: cut + 1] if cut != -1 else s
    return s if len(s) <= limit else s[:limit].rstrip("，、；：") + "…"


def zh_of(labels: dict[str, str]) -> str | None:
    return next((labels[k] for k in ZH_LABELS if k in labels), None)


def make_place(el: dict, city: Destination, info: dict, today: date, *, url: str | None, url_ok: bool,
               zh_note: str, taken: set[str]) -> dict:
    tags = el["tags"]
    q = tags["wikidata"]
    cat, setting, minutes, open_access = classify(tags)  # type: ignore[misc]
    lat, lng = point_of(el)  # type: ignore[misc]
    labels = info.get(q, {}).get("labels", {})
    osm_name = el.get("osm_name")                         # OSM 里同一个维基数据编号的对象的名字（没有 = 没对上）
    wd_name = labels.get(city.language) or tags["name"]
    verified = bool(osm_name and _consistent(osm_name, wd_name))
    if osm_name and verified:
        name_local = _capitalize(clean_name(wd_name if len(wd_name) >= len(osm_name) else osm_name))
    elif osm_name:
        name_local = _capitalize(clean_name(osm_name))
    else:
        name_local = _capitalize(clean_name(wd_name))
    base_id = f"{city.id}-{slug(name_local)}"[:60].strip("-")
    pid, k = base_id, 2
    while pid in taken:
        pid, k = f"{base_id}-{k}", k + 1
    taken.add(pid)
    street = tags.get("addr:street")
    address = None
    if street:
        address = f"{street} {tags.get('addr:housenumber', '')}".strip()
        city_part = " ".join(x for x in (tags.get("addr:postcode"), tags.get("addr:city")) if x)
        address = f"{address}, {city_part}" if city_part else address
    closed = parse_closed_days(tags["opening_hours"]) if tags.get("opening_hours") else None
    place: dict = {
        "id": pid, "name_zh": clean_name(zh_of(labels) or name_local),
        "name_en": clean_name(labels.get("en") or name_local),
        "name_local": name_local,
        "name_local_check": {"source": "osm+wikidata" if osm_name else "wikidata",
                             "status": "verified" if verified else "draft", "checked_at": today.isoformat(),
                             "wikidata": q},
        "category": cat, "setting": setting, "zone": "core", "area": tags.get("addr:suburb") or "市中心",
        "lat": round(lat, 6), "lng": round(lng, 6), "duration_min": minutes,
        "price_note": "价格和开放时间是开放数据草稿，尚未核对：出发前请到官网确认",
        "open_access": bool(open_access and tags.get("fee") != "yes"),
        "best_time": "any", "closed_weekdays": sorted(closed) if closed else [],
        "long_term_closed": False, "booking": {"required": False}, "ticketing": "none",
        "highlights": [], "note": zh_note, "interests": ["sightseeing"],
        "hours_verified": False, "coords_verified": False,
        "data_status": "draft",
        "draft_source": {"osm": osm_ref(el), "wikidata": q, "built_at": today.isoformat()},
    }
    if address:
        place["address"] = address
    if url and url_ok:
        place["official_url"] = url
        place["url_status"] = "ok"
        place["verified_at"] = today.isoformat()
    return place


def make_city(d: Destination, zh: str | None, centre: tuple[float, float], top_ids: list[str]) -> dict:
    lat, lng = centre
    names = {d.id, d.name_local.lower(), (zh or "").lower(), slug(d.name_local), d.name_local.split()[0].lower()}
    aliases = sorted(names - {""})
    return {
        "id": d.id, "name_zh": zh or d.name_local, "name_en": d.name_local, "local_language": d.language,
        "aliases": aliases, "country": d.country, "currency": "EUR", "timezone": d.timezone,
        "utc_offset": 1, "dst": "eu", "center": [round(lat, 4), round(lng, 4)],
        "bbox": {"lat": [round(lat - 0.04, 4), round(lat + 0.04, 4)],
                 "lng": [round(lng - 0.06, 4), round(lng + 0.06, 4)]},
        "suggested_days": {"min": 1, "max": 1, "quality_max": 1, "note": "一日游目的地：当天往返，建议 1 天"},
        "landmarks": top_ids[:3], "supported": True, "tier": "light", "region": d.region,
    }


def make_theme(city_zh: str, members: list[str]) -> dict:
    weights = [5, 5, 4, 4, 3] + [2] * 20
    return {"city": "", "themes": [{
        "id": THEME_ID, "name_zh": "一日游", "tagline": f"{city_zh}一天走一遍最值得看的地方",
        "description": "开放数据草稿：按维基数据的知名度排序挑出的景点，价格和开放时间出发前请到官网确认。",
        "recipe": {"anchors_per_day": {"relaxed": 2, "balanced": 3, "intense": 4}, "best_time_bias": [],
                   "max_big_venues_per_day": 2, "notes": "一日游：到站就开始，留够返程时间"},
        "members": [{"place_id": pid, "role": "anchor" if i < 5 else "support", "weight": weights[i]}
                    for i, pid in enumerate(members)],
        "tips": ["圣诞市场等当季活动只在举办期内排进行程", "返程末班车请提前在 DB Navigator 等官方渠道确认"],
    }]}


# --------------------------------------------------------------------------- #
# 取数据
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self, client: httpx.Client):
        self.c = client

    def _get(self, url: str, **kw) -> httpx.Response:
        r = self.c.get(url, timeout=90, **kw)
        r.raise_for_status()
        time.sleep(PAUSE_S)
        return r

    def wikidata(self, ids: list[str], langs: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 40):
            chunk = ids[i:i + 40]
            r = self._get(WIKIDATA, params={"action": "wbgetentities", "ids": "|".join(chunk),
                                            "props": "labels|sitelinks|claims", "languages": "|".join(langs),
                                            "format": "json"})
            for q, ent in (r.json().get("entities") or {}).items():
                labels = {k: v["value"] for k, v in (ent.get("labels") or {}).items()}
                claims = ent.get("claims") or {}
                pt = None
                for cl in claims.get("P625", []):
                    v = (cl.get("mainsnak", {}).get("datavalue") or {}).get("value")
                    if v:
                        pt = (float(v["latitude"]), float(v["longitude"]))
                        break
                site = next((cl["mainsnak"]["datavalue"]["value"] for cl in claims.get("P856", [])
                             if cl.get("mainsnak", {}).get("datavalue")), None)
                out[q] = {"labels": labels, "sitelinks": ent.get("sitelinks") or {}, "point": pt, "website": site}
        return out

    def candidates(self, lat: float, lon: float, radius_m: int, min_links: int = 3) -> list[dict]:
        """市中心周围的景点类条目：{qid, lat, lon, sitelinks, cls}，按语言版本数从多到少。"""
        classes = " ".join(f"wd:{c}" for c in CLASS_TAGS)
        q = SPARQL_TEMPLATE.format(lat=lat, lon=lon, km=radius_m / 1000, classes=classes, minlinks=min_links)
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self.c.get(SPARQL, params={"query": q, "format": "json"}, timeout=120,
                               headers={"Accept": "application/sparql-results+json"})
                r.raise_for_status()
                time.sleep(PAUSE_S)
                out = []
                for b in r.json()["results"]["bindings"]:
                    m = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", b["coord"]["value"])
                    if not m:
                        continue
                    out.append({"qid": b["item"]["value"].rsplit("/", 1)[1], "lon": float(m.group(1)),
                                "lat": float(m.group(2)), "sitelinks": int(b["sitelinks"]["value"]),
                                "cls": b["class"]["value"].rsplit("/", 1)[1]})
                return out
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last = exc
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"维基数据 SPARQL 连续失败：{last}")

    def osm_enrich(self, name: str, city: Destination, qid: str, lat: float, lon: float) -> dict | None:
        """在 OSM（Nominatim）里找同一个维基数据编号的对象，取名字 / 官网 / 开放时间 / 地址。没对上 → None。"""
        try:
            r = self._get(NOMINATIM, params={"q": f"{name}, {city.name_local}", "format": "jsonv2", "limit": "6",
                                             "namedetails": "1", "extratags": "1", "addressdetails": "1"})
        except httpx.HTTPError:
            return None
        for c in r.json():
            extra = c.get("extratags") or {}
            if extra.get("wikidata") != qid:
                continue
            if haversine_m(lat, lon, float(c["lat"]), float(c["lon"])) > OSM_MATCH_M:
                continue
            details, addr = c.get("namedetails") or {}, c.get("address") or {}
            return {
                "osm": f"{c.get('osm_type', 'node')}/{c.get('osm_id', '')}",
                "osm_name": details.get(f"name:{city.language}") or details.get("name"),
                "tags": {k: v for k, v in {
                    "opening_hours": extra.get("opening_hours"),
                    "website": extra.get("website") or extra.get("contact:website"),
                    "fee": extra.get("fee"),
                    "addr:street": addr.get("road"), "addr:housenumber": addr.get("house_number"),
                    "addr:postcode": addr.get("postcode"), "addr:city": addr.get("city") or addr.get("town"),
                    "addr:suburb": addr.get("suburb"),
                }.items() if v},
            }
        return None

    def zh_summary(self, title: str) -> str:
        try:
            r = self.c.get(ZH_SUMMARY.format(title=title.replace(" ", "_")), timeout=30)
            time.sleep(0.5)
            if r.status_code != 200:
                return ""
            return first_sentence(r.json().get("extract", ""))
        except httpx.HTTPError:
            return ""


def build_city(f: Fetcher, d: Destination, today: date) -> tuple[dict, list[dict], dict]:
    ent = f.wikidata([d.qid], [d.language, "en", *ZH_LABELS]).get(d.qid) or {}
    centre = ent.get("point")
    if centre is None:
        raise RuntimeError(f"{d.id}: 维基数据没有坐标")
    zh = zh_of(ent.get("labels", {}))
    info: dict[str, dict] = {}
    chosen: list[dict] = []
    for radius, min_links in ((d.centre_radius_m, 3), (d.centre_radius_m + 1000, 2), (d.centre_radius_m + 2000, 1)):
        cands = [c for c in f.candidates(centre[0], centre[1], radius, min_links) if c["qid"] != d.qid]
        todo = sorted({c["qid"] for c in cands} - set(info))
        if todo:
            info.update(f.wikidata(todo, [d.language, "en", *ZH_LABELS]))
        elements = []
        for c in cands:
            labels = info.get(c["qid"], {}).get("labels", {})
            name = labels.get(d.language) or labels.get("en")
            if name:
                elements.append({"type": "wikidata", "id": c["qid"], "lat": c["lat"], "lon": c["lon"],
                                 "tags": {**CLASS_TAGS[c["cls"]], "name": clean_name(name), "wikidata": c["qid"]}})
        chosen = pick_top(elements, info, TOP_N, centre)
        if len(chosen) >= TOP_N - 1:
            break
        print(f"  只找到 {len(chosen)} 个，放宽范围 / 门槛再找", flush=True)
    taken: set[str] = set()
    places: list[dict] = []
    for el in chosen:
        q = el["tags"]["wikidata"]
        extra = f.osm_enrich(el["tags"]["name"], d, q, el["lat"], el["lon"])
        if extra:
            el["osm"], el["osm_name"] = extra["osm"], extra["osm_name"]
            el["tags"] = {**extra["tags"], **el["tags"]}
        url = el["tags"].get("website") or info.get(q, {}).get("website")
        if url and url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        ok = False
        if url and url.startswith("https://"):
            try:
                ok = bool(check_url(f.c, url).get("ok"))
            except httpx.HTTPError:
                ok = False
        zh_title = (info.get(q, {}).get("sitelinks", {}).get("zhwiki") or {}).get("title")
        note = f"{f.zh_summary(zh_title)}（中文维基百科）" if zh_title else ""
        note = note if len(note) > len("（中文维基百科）") else ""
        places.append(make_place(el, d, info, today, url=url, url_ok=ok, zh_note=note, taken=taken))
        pl = places[-1]
        print(f"  {pl['id']}: {pl['name_zh']} · {pl['name_local']} [{pl['name_local_check']['status']}]"
              f"{' OSM✓' if el.get('osm') else ''}{' 官网✓' if ok else ''}", flush=True)
    city = make_city(d, zh, centre, [p["id"] for p in places])
    theme = make_theme(city["name_zh"], [p["id"] for p in places])
    theme["city"] = d.id
    return city, places, theme


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="")
    ap.add_argument("--origin", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args()
    if args.city:
        todo = [d for d in DESTINATIONS if d.id == args.city]
    elif args.origin:
        if args.origin not in ORIGINS:
            raise SystemExit(f"没有这个出发地：{args.origin}")
        todo = [d for d in DESTINATIONS if d.origin == args.origin]
    else:
        todo = list(DESTINATIONS)
    today = date.fromisoformat(args.today)
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        f = Fetcher(client)
        for d in todo:
            print(f"== {d.id} ({d.name_local})", flush=True)
            city, places, theme = build_city(f, d, today)
            if args.dry_run:
                continue
            write_json(DATA / "cities" / f"{d.id}.json", city)
            dump(DATA / "places" / f"{d.id}.json",
                 {"city": d.id, "_note": "一日游目的地（开放数据草稿）：规格 08 3.4。", "places": places})
            write_json(DATA / "themes" / f"{d.id}.json", theme)
    return 0


if __name__ == "__main__":
    sys.exit(main())
