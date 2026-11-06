# ruff: noqa: E501  （数据脚本：长句是文案，不折行）
"""城市现建的取数据（规格 10）：维基数据、OpenStreetMap（Nominatim）、中文维基百科摘要、官网核对。

只发公开的只读请求；请求之间有停顿；User-Agent 里写明用途和联系方式。纯函数在 core.py，这里不做判断，只取和拼。
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from datetime import date

import httpx

from app.citybuild.common import (
    CLASS_MIN_LINKS,
    CLASS_TAGS,
    SPARQL_TEMPLATE,
    ZH_LABELS,
    check_url,
    classify,
    clean_name,
    first_sentence,
    is_junk,
    parse_closed_days,
    zh_of,
)
from app.citybuild.core import (
    best_class,
    coord_limit,
    coords_agree,
    dedupe_same_place,
    drop_excluded,
    drop_parts,
    zone_for,
)
from app.citybuild.media import two_sentences
from app.citybuild.voyage import Listing, chinese_name, fetch_views, fetch_voyage, window

UA = "ai-travel-planner-agent-build/0.1 (https://github.com/Kylie08ky; personal project)"
SPARQL = "https://query.wikidata.org/sparql"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
WIKIDATA = "https://www.wikidata.org/w/api.php"
ZH_SUMMARY = "https://zh.wikipedia.org/api/rest_v1/page/summary/{title}"
PAUSE_S = 1.0

Progress = Callable[[str], None]


def make_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA}, follow_redirects=True)


class CityFetcher:
    def __init__(self, client: httpx.Client, pause: float = PAUSE_S):
        self.c = client
        self.pause = pause

    def _get(self, url: str, **kw) -> httpx.Response:
        r = self.c.get(url, timeout=90, **kw)
        r.raise_for_status()
        time.sleep(self.pause)
        return r

    def wikidata(self, ids: list[str], langs: list[str]) -> dict[str, dict]:
        """条目的标签、语言版本、坐标、官网。"""
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 40):
            r = self._get(WIKIDATA, params={"action": "wbgetentities", "ids": "|".join(ids[i:i + 40]),
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
                image = next((cl["mainsnak"]["datavalue"]["value"] for cl in claims.get("P18", [])
                              if cl.get("mainsnak", {}).get("datavalue")), None)      # 维基共享资源上的文件名
                out[q] = {"labels": labels, "sitelinks": ent.get("sitelinks") or {}, "point": pt, "website": site,
                          "image": image if isinstance(image, str) else None}
        return out

    def candidates(self, lat: float, lon: float, radius_m: int, min_links: int) -> list[dict]:
        """中心点周围的景点类条目：{qid, lat, lon, sitelinks, cls}，按语言版本数从多到少。"""
        classes = " ".join(f"wd:{c}" for c in CLASS_TAGS)
        q = SPARQL_TEMPLATE.format(lat=lat, lon=lon, km=radius_m / 1000, classes=classes, minlinks=min_links)
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self.c.get(SPARQL, params={"query": q, "format": "json"}, timeout=120,
                               headers={"Accept": "application/sparql-results+json"})
                r.raise_for_status()
                time.sleep(self.pause)
                out = []
                for b in r.json()["results"]["bindings"]:
                    m = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", b["coord"]["value"])
                    if not m:
                        continue
                    sampled = b["class"]["value"].rsplit("/", 1)[1]
                    classes = [k for k in (b.get("classes", {}).get("value") or "").split(",") if k in CLASS_TAGS]
                    out.append({"qid": b["item"]["value"].rsplit("/", 1)[1], "lon": float(m.group(1)),
                                "lat": float(m.group(2)), "sitelinks": int(b["sitelinks"]["value"]),
                                "cls": best_class(classes) or sampled, "classes": classes or [sampled]})
                return out
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last = exc
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"维基数据查询连续失败（{type(last).__name__}）")

    def parts_of(self, ids: list[str]) -> dict[str, set[str]]:
        """每个条目「属于」（P361）哪些条目。"""
        out: dict[str, set[str]] = {}
        for i in range(0, len(ids), 40):
            r = self._get(WIKIDATA, params={"action": "wbgetentities", "ids": "|".join(ids[i:i + 40]),
                                            "props": "claims", "format": "json"})
            for q, ent in (r.json().get("entities") or {}).items():
                out[q] = {cl["mainsnak"]["datavalue"]["value"]["id"] for cl in (ent.get("claims") or {}).get("P361", [])
                          if cl.get("mainsnak", {}).get("datavalue")}
        return out

    def osm_lookup(self, name: str, city_zh: str, qid: str) -> dict | None:
        """OSM（Nominatim）里同一个维基数据编号的对象：坐标、官网、开放时间、区域。没对上 → None。"""
        try:
            r = self._get(NOMINATIM, params={"q": f"{name}, {city_zh}", "format": "jsonv2", "limit": "6",
                                             "extratags": "1", "addressdetails": "1"})
        except httpx.HTTPError:
            return None
        for c in r.json():
            extra = c.get("extratags") or {}
            if extra.get("wikidata") != qid:
                continue
            addr = c.get("address") or {}
            return {"osm": f"{c.get('osm_type', 'node')}/{c.get('osm_id', '')}",
                    "point": (float(c["lat"]), float(c["lon"])),
                    "opening_hours": extra.get("opening_hours"),
                    "website": extra.get("website") or extra.get("contact:website"),
                    "area": addr.get("city_district") or addr.get("suburb") or addr.get("district")}
        return None

    def osm_attractions(self, bbox: dict) -> list[dict]:
        """OSM（Nominatim）按类别在市区范围里找带维基数据编号的景点 / 博物馆 / 公园 / 观景台：
        中国的景点在维基数据里常常没有坐标或语言版本很少，OSM 里却有标注。返回 {qid, lat, lon, name, cls}。"""
        viewbox = f"{bbox['lng'][0]},{bbox['lat'][1]},{bbox['lng'][1]},{bbox['lat'][0]}"
        found: dict[str, dict] = {}
        for term, cls in (("attraction", "Q570116"), ("museum", "Q33506"), ("viewpoint", "Q570116"),
                          ("park", "Q22698"), ("historic", "Q4989906")):
            try:
                r = self._get(NOMINATIM, params={"q": term, "format": "jsonv2", "limit": "50", "viewbox": viewbox,
                                                 "bounded": "1", "extratags": "1", "namedetails": "1",
                                                 "accept-language": "zh"})
            except httpx.HTTPError:
                continue
            for x in r.json():
                extra = x.get("extratags") or {}
                qid = extra.get("wikidata")
                name = (x.get("namedetails") or {}).get("name:zh") or x.get("name")
                if not qid or not re.fullmatch(r"Q\d+", qid) or not name or is_junk(name) or qid in found:
                    continue
                found[qid] = {"qid": qid, "lat": float(x["lat"]), "lon": float(x["lon"]), "name": clean_name(name),
                              "cls": cls, "importance": float(x.get("importance") or 0)}
        return list(found.values())

    def zh_extract(self, title: str) -> str:
        """中文维基百科的摘要原文（没有 → 空）。"""
        try:
            r = self.c.get(ZH_SUMMARY.format(title=title.replace(" ", "_")), timeout=30)
            time.sleep(0.5)
            return (r.json().get("extract") or "") if r.status_code == 200 else ""
        except (httpx.HTTPError, ValueError):
            return ""

    def zh_summary(self, title: str) -> str:
        return first_sentence(self.zh_extract(title))


def collect(f: CityFetcher, seed: dict, say: Progress = lambda _m: None) -> list[dict]:
    """候选 → 清理 → 取前 N 个，再把种子里指定的加进来。每项带 name / sitelinks / 坐标 / 类别。"""
    lat, lng = seed["center"]
    langs = ["en", *ZH_LABELS]
    say("查找这座城市的景点")
    excluded = {e["qid"] for e in seed.get("exclude", [])}
    info: dict[str, dict] = {}
    auto: list[dict] = []
    for min_links in dict.fromkeys((seed["min_links"], 1)):       # 名气门槛先按城市大小，点不够就放到最低
        cands = f.candidates(lat, lng, int(seed["radius_km"] * 1000), min_links)
        info.update(f.wikidata(sorted({c["qid"] for c in cands} - set(info)), langs))
        auto = []
        for c in cands:
            labels = info.get(c["qid"], {}).get("labels", {})
            name = zh_of(labels) or labels.get("en")
            tags = {**CLASS_TAGS[c["cls"]], "name": name or ""}
            if (not name or is_junk(name) or classify(tags) is None
                    or zone_for(c["lat"], c["lon"], seed["bbox"]) != "core"
                    or all(c["sitelinks"] < CLASS_MIN_LINKS.get(k, 0)
                           for k in c.get("classes", [c["cls"]]))):      # 无名的桥 / 小山 / 小湖 / 普通餐厅不是景点
                continue
            auto.append({**c, "name": clean_name(name)})
        if len(auto) >= seed["top_n"] * 0.6:
            break
        say("景点不多，放宽名气门槛再找")
    if len(auto) < seed["top_n"]:
        say("用 OpenStreetMap 补充景点")
        have = {c["qid"] for c in auto}
        extra_osm = [c for c in f.osm_attractions(seed["bbox"]) if c["qid"] not in have]
        info.update(f.wikidata([c["qid"] for c in extra_osm if c["qid"] not in info], langs))
        for c in extra_osm:
            ent = info.get(c["qid"], {})
            auto.append({"qid": c["qid"], "name": c["name"], "lat": c["lat"], "lon": c["lon"], "cls": c["cls"],
                         "sitelinks": max(len(ent.get("sitelinks", {})), 1 + int(c["importance"] * 10))})
    auto = drop_excluded(auto, excluded)
    auto = dedupe_same_place(auto)
    say("清理重复和子景点")
    parts = f.parts_of([c["qid"] for c in auto])
    auto = sorted(drop_parts(auto, parts), key=lambda c: -c["sitelinks"])[: seed["top_n"]]
    chosen = {c["qid"] for c in auto}
    extra = [i for i in seed.get("include", []) if i["qid"] not in chosen and i["qid"] not in excluded]
    if extra:
        info.update(f.wikidata([i["qid"] for i in extra], langs))
    for i in extra:
        ent = info.get(i["qid"], {})
        pt = ent.get("point")
        if pt is None:
            continue
        auto.append({"qid": i["qid"], "name": i["name"], "sitelinks": len(ent.get("sitelinks", {})), "lat": pt[0],
                     "lon": pt[1], "cls": "", "seed": i})
    auto = add_voyage(f, seed, auto, info, excluded, langs, say)
    for c in auto:
        c["info"] = info.get(c["qid"], {})
    return auto


def _voyage_cls(name: str) -> str:
    """维基导游的条目没有「是什么」：按名字里的字粗分（博物馆 / 寺庙 / 其他景点），主题由它决定。"""
    if re.search(r"博物馆|纪念馆|科技馆|美术馆|展览馆", name):
        return "Q33506"
    if re.search(r"寺|庙|宫|观|祠|清真", name):
        return "Q5393308"
    return "Q570116"


def add_voyage(f: CityFetcher, seed: dict, auto: list[dict], info: dict[str, dict], excluded: set[str],
               langs: list[str], say: Progress = lambda _m: None) -> list[dict]:
    """维基导游（英文版）城市页：① 库里已有的点打上「维基导游推荐」；② 景点 / 活动条目里有维基数据编号、坐标在市区、
    还不在库里的，补进来。只取名字、类别、坐标、编号，不抄介绍文字；没有编号的条目、餐饮条目不收（H21）。"""
    page = fetch_voyage(f.c, seed.get("voyage_title") or seed["name_en"], pause=f.pause)
    if not page:
        say("维基导游里没有这座城市的页面，跳过")
        return auto
    say("读维基导游")
    mark = {"url": page["url"], "revid": page["revid"]}
    by_qid: dict[str, Listing] = {}
    by_name: dict[str, Listing] = {}
    for item in page["listings"]:
        if item.wikidata:
            by_qid.setdefault(item.wikidata, item)
        if zh := chinese_name(item):
            by_name.setdefault(zh, item)
    have = {c["qid"] for c in auto}
    new = [i for q, i in by_qid.items() if q not in have and q not in excluded and i.kind in ("see", "do")]
    if new:
        info.update(f.wikidata([i.wikidata for i in new if i.wikidata not in info], langs))
    for item in new:
        ent = info.get(item.wikidata, {})
        pt = ent.get("point")
        name = zh_of(ent.get("labels", {})) or chinese_name(item)
        if pt is None or not name or is_junk(name) or zone_for(pt[0], pt[1], seed["bbox"]) != "core":
            continue
        if any(clean_name(name) == c["name"] for c in auto):
            continue
        auto.append({"qid": item.wikidata, "name": clean_name(name), "sitelinks": len(ent.get("sitelinks", {})),
                     "lat": pt[0], "lon": pt[1], "cls": _voyage_cls(name), "classes": [_voyage_cls(name)]})
        say(f"维基导游补充：{name}")
    for c in auto:
        item = by_qid.get(c["qid"]) or by_name.get(c["name"])
        if item:
            c["voyage"] = {"kind": item.kind, **mark}
    return auto


def to_raws(f: CityFetcher, seed: dict, cands: list[dict], say: Progress = lambda _m: None,
            today: date | None = None) -> list[dict]:
    """逐点取 OSM / 官网 / 中文摘要 / 中文维基浏览量，拼成 core.build_data 要的 raws。"""
    today = today or date.today()
    period = "{}~{}".format(*[w[:4] + "-" + w[4:6] for w in window(today)])
    raws: list[dict] = []
    for n, c in enumerate(cands, 1):
        info = c["info"]
        labels = info.get("labels", {})
        seed_item = c.get("seed")
        say(f"核对 {n}/{len(cands)}：{c['name']}")
        osm = f.osm_lookup(c["name"], seed["name_zh"], c["qid"])
        if osm is None:
            coords = "wikidata"
        else:
            coords = "mismatch"
        if seed_item:
            category, setting, minutes, open_access = (
                seed_item.get("category", "sight"), seed_item.get("setting", "outdoor"),
                seed_item.get("minutes", 90), seed_item.get("open_access", False))
        else:
            category, setting, minutes, open_access = classify({**CLASS_TAGS[c["cls"]], "name": c["name"]})
        if osm is not None and coords_agree((c["lat"], c["lon"]), osm["point"], coord_limit(category, c.get("cls", ""))):
            coords = "osm"
        url = (osm or {}).get("website") or info.get("website")
        if url and url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        ok = False
        if url and url.startswith("https://"):
            try:
                ok = bool(check_url(f.c, url).get("ok"))
            except httpx.HTTPError:
                ok = False
        closed = parse_closed_days(osm["opening_hours"]) if osm and osm.get("opening_hours") else None
        title = (info.get("sitelinks", {}).get("zhwiki") or {}).get("title")
        extract = f.zh_extract(title) if title else ""
        summary = first_sentence(extract)
        views = fetch_views(f.c, title, today) if title else None
        raws.append({
            "qid": c["qid"], "name_zh": c["name"], "name_en": labels.get("en") or "", "category": category,
            "setting": setting, "minutes": minutes, "open_access": open_access, "lat": c["lat"], "lng": c["lon"],
            "sitelinks": c["sitelinks"], "cls": c.get("cls", ""), "theme": (seed_item or {}).get("theme"),
            "zone": zone_for(c["lat"], c["lon"], seed["bbox"]), "osm": (osm or {}).get("osm"), "coords": coords,
            "url": url, "url_ok": ok, "closed": closed, "note": f"{summary}（中文维基百科）" if summary else "",
            "area": (osm or {}).get("area") or "", "seed": bool(seed_item), "image": info.get("image"),
            "intro": two_sentences(extract), "zh_title": title or "", "voyage": c.get("voyage"),
            "views": views, "views_period": period if views is not None else None})
    return raws


def climate_stats(client: httpx.Client, lat: float, lng: float, year: int) -> dict | None:
    """去年当地每个月的平均最高 / 最低气温、降水量和海拔（Open-Meteo 历史数据，免费、不要 Key）；取不到 → None。"""
    try:
        r = client.get("https://archive-api.open-meteo.com/v1/archive", timeout=60, params={
            "latitude": lat, "longitude": lng, "start_date": f"{year}-01-01", "end_date": f"{year}-12-31",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum", "timezone": "auto"})
        r.raise_for_status()
        body = r.json()
        daily = body["daily"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    highs: dict[int, list[float]] = {}
    lows: dict[int, list[float]] = {}
    rain: dict[int, float] = {}
    for day, hi, lo, pr in zip(daily["time"], daily["temperature_2m_max"], daily["temperature_2m_min"],
                               daily["precipitation_sum"], strict=False):
        m = int(day[5:7])
        if hi is not None:
            highs.setdefault(m, []).append(hi)
        if lo is not None:
            lows.setdefault(m, []).append(lo)
        rain[m] = rain.get(m, 0.0) + (pr or 0.0)
    if len(highs) != 12 or len(lows) != 12:
        return None
    avg = {m: round(sum(v) / len(v), 1) for m, v in sorted(highs.items())}
    low = {m: round(sum(v) / len(v), 1) for m, v in sorted(lows.items())}
    out: dict = {"monthly_high_c": avg, "monthly_low_c": low,
                 "monthly_rain_mm": {m: round(v) for m, v in sorted(rain.items())}}
    if isinstance(body.get("elevation"), (int, float)):
        out["elevation_m"] = round(body["elevation"])
    return out


def build_raws(seed: dict, client: httpx.Client, say: Progress = lambda _m: None, pause: float = PAUSE_S,
               today: date | None = None) -> list[dict]:
    f = CityFetcher(client, pause)
    raws = to_raws(f, seed, collect(f, seed, say), say, today)
    simplify_raws(client, raws, say)
    return raws



# --------------------------------------------------------------------------- #
# 繁体 → 简体（规格 08 3.7：全部显示简体）
# --------------------------------------------------------------------------- #
_CJK = re.compile(r"[一-鿿]")
_P_TAG = re.compile(r"<p>(.*?)</p>", re.S)
WIKIPEDIA_API = "https://zh.wikipedia.org/w/api.php"


def _convert(client: httpx.Client, text: str) -> list[str]:
    for wait in (0, 5, 15, 40):
        time.sleep(wait)
        r = client.post(WIKIPEDIA_API, data={"action": "parse", "text": text, "contentmodel": "wikitext",
                                             "prop": "text", "variant": "zh-hans", "format": "json",
                                             "formatversion": 2, "disablelimitreport": 1}, timeout=60)
        if r.status_code == 429:
            continue
        r.raise_for_status()
        body = r.json()["parse"]["text"]
        return [html.unescape(re.sub(r"<[^>]+>", "", m)).strip() for m in _P_TAG.findall(body)]
    raise RuntimeError("中文维基百科请求太频繁")


def to_simplified(client: httpx.Client, strings: list[str], batch: int = 12) -> dict[str, str]:
    """一批中文 → 简体 {原文: 简体}。用中文维基百科自己的繁简转换；转换失败的原样保留（不让建城市因此失败）。"""
    todo = sorted({s for s in strings if s and _CJK.search(s)})
    out: dict[str, str] = {}
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            conv = _convert(client, "\n\n".join(chunk))
            if len(conv) != len(chunk):                                   # 段落数对不上：逐条转
                conv = [(_convert(client, s) or [s])[0] for s in chunk]
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError):
            continue
        for src, dst in zip(chunk, conv, strict=True):
            if dst and abs(len(dst) - len(src)) <= max(2, len(src) // 3):
                out[src] = dst
    return out


def simplify_raws(client: httpx.Client, raws: list[dict], say: Progress = lambda _m: None) -> None:
    say("统一成简体中文")
    names = to_simplified(client, [r["name_zh"] for r in raws] + [r["note"] for r in raws if r.get("note")]
                          + [r["intro"] for r in raws if r.get("intro")])
    for r in raws:
        r["name_zh"] = names.get(r["name_zh"], r["name_zh"])
        if r.get("note"):
            r["note"] = names.get(r["note"], r["note"])
        if r.get("intro"):                                              # 导出和地点卡显示的简介：不能是繁体
            r["intro"] = names.get(r["intro"], r["intro"])
