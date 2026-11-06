"""认目的地（规格 10）：用户写的任何名字 → 一座城市 / 一组候选城市 / 认不出来。

`decide` 是纯规则（城市、国家、同名、找不到）；`Resolver` 负责去维基数据取候选；`seed_for` 把一个维基数据城市条目
变成现建需要的「种子」（中心点、国家、币种、时区、当地语言、范围、点数）。全部来自公开数据，不让模型编。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from importlib import resources
from typing import Literal

import httpx

from app.citybuild.common import ZH_LABELS, slug, zh_of
from app.citybuild.fetch import SPARQL, UA, WIKIDATA

SEARCH_LANGS = ("zh", "en", "de", "fr", "es", "it", "pt", "ja")
SAME_NAME_RATIO = 0.5            # 同名候选的名气达到最大者的这个比例，就让用户选
MAX_CHOICES = 5
MAX_COUNTRY_CHOICES = 12          # 国家里可以多选，多列几座（只列最常去的，其余可以直接在目的地里输入）
KINDS = ("settlement", "country", "region")
# 国家弹窗里固定在前面的城市（按旅行常见的顺序）：热门小城（比萨、锡耶纳）人口不到 10 万，按人口 / 名气排不进前几名。
# 其余的位置再按「最常去」补。QID 都用 Wikidata 核对过（罗马 Q220 …）。
PINNED_CITIES: dict[str, list[str]] = {
    # 意大利：罗马 佛罗伦萨 米兰 威尼斯 那不勒斯 博洛尼亚 比萨 锡耶纳 都灵 维罗纳
    "Q38": ["Q220", "Q2044", "Q490", "Q641", "Q2634", "Q1891", "Q13375", "Q2751", "Q495", "Q2028"],
}


@dataclass
class Candidate:
    qid: str
    label: str
    description: str = ""
    sitelinks: int = 0
    kind: str = "other"                     # settlement 城市 / 镇 / 村；country 国家；region 一级行政区；other
    country_qid: str = ""
    country_label: str = ""
    lat: float | None = None
    lng: float | None = None


@dataclass
class Resolution:
    kind: Literal["city", "choices", "country", "unknown"]
    city: Candidate | None = None
    choices: list[Candidate] = field(default_factory=list)
    top: Candidate | None = None             # kind == "country"：要去列这个国家 / 地区的城市
    reason: str = ""
    many: bool = False                        # choices 是「国家里最常去的城市」：可以多选、按点选顺序成多城市行程


def _same_name(label: str, text: str) -> bool:
    a, b = label.strip().lower(), text.strip().lower()
    return a == b or a.removesuffix("市") == b or a == b + "市"


def decide(cands: list[Candidate], text: str = "") -> Resolution:
    """候选 → 结果。只收有坐标、类别是城市 / 国家 / 一级行政区的；名字和输入一样的排前面，再按名气排
    （「北京」不能被一个别名叫「北京」、名气更大的国家抢走）。"""
    usable = sorted((c for c in cands if c.kind in KINDS and c.lat is not None),
                    key=lambda c: (not _same_name(c.label, text), -c.sitelinks))
    if not usable:
        return Resolution("unknown", reason="没找到叫这个名字的目的地")
    top = usable[0]
    if top.kind in ("country", "region"):
        return Resolution("country", top=top)
    rivals = [c for c in usable[1:] if c.kind == "settlement" and c.country_qid != top.country_qid
              and c.sitelinks >= SAME_NAME_RATIO * top.sitelinks]
    if rivals:
        return Resolution("choices", choices=[top, *rivals][:MAX_CHOICES], reason="有几个同名的地方，请选一个")
    return Resolution("city", city=top)


def zone_for_country(iso2: str, lat: float, lng: float) -> str | None:
    """国家里离这座城市最近的时区（tzdata 的 zone.tab：每个时区有一个代表坐标）。"""
    try:
        text = resources.files("tzdata.zoneinfo").joinpath("zone.tab").read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    best: tuple[float, str] | None = None
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        cc, coord, tz, *_ = line.split("\t")
        if cc != iso2:
            continue
        m = re.match(r"([+-]\d{2})(\d{2})(\d{2})?([+-]\d{3})(\d{2})(\d{2})?", coord)
        if not m:
            continue
        zlat = int(m.group(1)) + math.copysign(int(m.group(2)) / 60 + int(m.group(3) or 0) / 3600, int(m.group(1)))
        zlng = int(m.group(4)) + math.copysign(int(m.group(5)) / 60 + int(m.group(6) or 0) / 3600, int(m.group(4)))
        d = (zlat - lat) ** 2 + ((zlng - lng) * math.cos(math.radians(lat))) ** 2
        if best is None or d < best[0]:
            best = (d, tz)
    return best[1] if best else None


# 官方语言在维基数据里是「标准汉语」这类没有两字母代码（P218）的条目：按国家 / 地区补上
CHINESE_REGIONS = {"CN": "zh", "TW": "zh", "HK": "zh", "MO": "zh"}


def size_plan(population: int | None) -> dict:
    """城市大小 → 搜景点的半径、名气门槛、取多少个点。人口不知道按中等城市。"""
    pop = population or 300_000
    # 数量（规格 10 H19）：大城市 100、中等 60、小城 25；名气门槛放低，排在后面的点靠名气分排序，不会挤掉前面的
    if pop >= 1_000_000:
        return {"radius_km": 30, "min_links": 5, "top_n": 100, "extent": 0.25}
    if pop >= 150_000:
        return {"radius_km": 20, "min_links": 3, "top_n": 60, "extent": 0.15}
    return {"radius_km": 8, "min_links": 2, "top_n": 25, "extent": 0.08}


def make_seed(c: Candidate, *, name_en: str, name_zh: str, country_iso: str, currency: str, language: str,
              timezone: str, population: int | None, aliases: list[str]) -> dict:
    """现建用的种子（和 data/city-seeds/<id>.json 同一个格式）。"""
    plan = size_plan(population)
    dlat = plan["extent"]
    dlng = dlat / max(math.cos(math.radians(c.lat or 0)), 0.3)
    return {
        "id": slug(name_en) or c.qid.lower(), "name_zh": name_zh, "name_en": name_en, "qid": c.qid,
        "center": [round(c.lat or 0, 4), round(c.lng or 0, 4)], "radius_km": plan["radius_km"],
        "min_links": plan["min_links"], "top_n": plan["top_n"],
        "bbox": {"lat": [round((c.lat or 0) - dlat, 4), round((c.lat or 0) + dlat, 4)],
                 "lng": [round((c.lng or 0) - dlng, 4), round((c.lng or 0) + dlng, 4)]},
        "country": country_iso, "currency": currency, "local_language": language, "timezone": timezone,
        "aliases": sorted({a.lower() for a in aliases if a}), "include": [], "exclude": [],
    }


# --------------------------------------------------------------------------- #
# 取数据
# --------------------------------------------------------------------------- #
_DESCRIBE = """
SELECT DISTINCT ?item ?sitelinks ?coord ?country ?countryLabel ?type WHERE {{
  VALUES ?item {{ {ids} }}
  OPTIONAL {{ ?item wdt:P31/wdt:P279* ?type . VALUES ?type {{ wd:Q486972 wd:Q6256 wd:Q10864048 }} }}
  OPTIONAL {{ ?item wikibase:sitelinks ?sitelinks }}
  OPTIONAL {{ ?item wdt:P625 ?coord }}
  OPTIONAL {{ ?item wdt:P17 ?country }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "zh,en". }}
}}
"""
_TOP_CITIES = """
SELECT ?item ?sitelinks ?coord WHERE {{
  ?item {where} ; wdt:P1082 ?pop ; wikibase:sitelinks ?sitelinks ; wdt:P625 ?coord .
  FILTER(?pop > 100000 && ?sitelinks >= 15)
}} ORDER BY DESC(?sitelinks) LIMIT 60
"""


_TYPE_KIND = {"Q6256": "country", "Q10864048": "region", "Q486972": "settlement"}


def kind_of(types: set[str]) -> str:
    """一个条目同时是几种时：只要它也是城市（上海、东京、新加坡既是行政区又是城市）就当城市，不去列它里面的「城市」；
    否则国家 > 一级行政区。"""
    for kind in ("settlement", "country", "region"):
        if kind in types:
            return kind
    return "other"


_ADMIN_SUFFIX = ("广域市", "廣域市", "大都会", "都会区", "都會區")


def _is_admin_unit(label: str) -> bool:
    """「巴勒莫广域市」这类行政单位和城市本身重复，不当候选。"""
    return label.endswith(_ADMIN_SUFFIX)


def _why(exc: Exception | None) -> str:
    """给人看的失败原因：不带网址和查询语句。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"服务返回 {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "超时"
    return type(exc).__name__ if exc is not None else "未知"


class Resolver:
    def __init__(self, client: httpx.Client, pause: float = 0.4):
        self.c = client
        self.pause = pause

    def _get(self, url: str, **kw) -> dict:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self.c.get(url, timeout=90, **kw)
                r.raise_for_status()
                time.sleep(self.pause)
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"维基数据请求失败（{_why(last)}）")

    def search(self, text: str) -> list[tuple[str, str]]:
        """名字 → [(qid, 标签)]：用几种常见语言各搜一遍，合并去重。"""
        seen: dict[str, str] = {}
        for lang in SEARCH_LANGS:
            data = self._get(WIKIDATA, params={"action": "wbsearchentities", "search": text, "language": lang,
                                               "uselang": "zh-cn", "type": "item", "limit": 6, "format": "json"})
            for hit in data.get("search", []):
                seen.setdefault(hit["id"], hit.get("label") or hit["id"])
        return list(seen.items())

    def describe(self, ids: list[str]) -> list[Candidate]:
        if not ids:
            return []
        q = _DESCRIBE.format(ids=" ".join(f"wd:{i}" for i in ids))
        data = self._get(SPARQL, params={"query": q, "format": "json"})
        out: dict[str, Candidate] = {}
        types: dict[str, set[str]] = {}
        for b in data["results"]["bindings"]:
            qid = b["item"]["value"].rsplit("/", 1)[1]
            c = out.setdefault(qid, Candidate(qid=qid, label=qid))
            c.sitelinks = max(c.sitelinks, int(b["sitelinks"]["value"])) if "sitelinks" in b else c.sitelinks
            if "coord" in b and c.lat is None:
                m = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", b["coord"]["value"])
                if m:
                    c.lng, c.lat = float(m.group(1)), float(m.group(2))
            if "country" in b:
                c.country_qid = b["country"]["value"].rsplit("/", 1)[1]
                c.country_label = b.get("countryLabel", {}).get("value", "")
            if "type" in b:
                types.setdefault(qid, set()).add(_TYPE_KIND.get(b["type"]["value"].rsplit("/", 1)[1], "other"))
        for qid, c in out.items():
            c.kind = kind_of(types.get(qid, set()))
        return list(out.values())

    def labels(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 40):
            data = self._get(WIKIDATA, params={"action": "wbgetentities", "ids": "|".join(ids[i:i + 40]),
                                               "props": "labels|descriptions|claims",
                                               "languages": "zh-cn|zh-hans|zh|en", "format": "json"})
            out.update(data.get("entities") or {})
        return out

    def resolve(self, text: str) -> Resolution:
        hits = self.search(text.strip())
        cands = self.describe([q for q, _ in hits[:20]])
        names = self.labels([c.qid for c in cands])
        for c in cands:
            ent = names.get(c.qid, {})
            lab = {k: v["value"] for k, v in (ent.get("labels") or {}).items()}
            c.label = zh_of(lab) or lab.get("en") or c.label
            c.description = ((ent.get("descriptions") or {}).get("zh-cn") or (ent.get("descriptions") or {}).get("en")
                             or {}).get("value", "")
        res = decide(cands, text)
        if res.kind == "country" and res.top is not None:
            res = Resolution("choices", choices=self.top_cities(res.top), top=res.top,
                             reason=f"{res.top.label}最常去的城市，可以选几个，按点的顺序走", many=True)
            if not res.choices:
                return Resolution("unknown", reason="没能列出这个地方的城市，请直接写城市名")
        return res

    def top_cities(self, place: Candidate) -> list[Candidate]:
        """国家 / 地区里最常去的城市：先按「在这里、人口 > 10 万、语言版本多」粗筛（这个查询很快），
        再用类别（城市 / 镇）把省、都道府县这类行政区去掉。"""
        where = (f"wdt:P17 wd:{place.qid}" if place.kind == "country" else f"wdt:P131+ wd:{place.qid}")
        data = self._get(SPARQL, params={"query": _TOP_CITIES.format(where=where), "format": "json"})
        raw: dict[str, int] = {}
        for b in data["results"]["bindings"]:
            raw.setdefault(b["item"]["value"].rsplit("/", 1)[1], int(b["sitelinks"]["value"]))
        described = self.describe(list(raw))
        kinds = ("settlement", "region") if place.kind == "country" else ("settlement",)
        top = [c for c in described if c.kind in kinds and c.lat is not None]
        for c in top:
            c.sitelinks = raw.get(c.qid, c.sitelinks)
            c.country_label = c.country_label or place.label
        # 城市在前；西西里、撒丁这类地区只在城市不够时才补
        top.sort(key=lambda c: (c.kind != "settlement", -c.sitelinks))
        pinned = PINNED_CITIES.get(place.qid, [])
        have = {c.qid for c in top}
        extra = [q for q in pinned if q not in have]
        if extra:                                              # 固定的城市即使查询没排到也要列出来
            fixed = [c for c in self.describe(extra) if c.lat is not None]
            for c in fixed:
                c.country_label = c.country_label or place.label
            top = [*top, *fixed]
        by_pin = {q: n for n, q in enumerate(pinned)}
        # 固定的按给定顺序排前面；其余保持原来的名气顺序（稳定排序）
        top.sort(key=lambda c: by_pin.get(c.qid, len(by_pin)))
        top = top[: MAX_COUNTRY_CHOICES + 4]                  # 多取几个：去掉行政单位之后还够数
        names = self.labels([c.qid for c in top])
        for c in top:
            lab = {k: v["value"] for k, v in ((names.get(c.qid) or {}).get("labels") or {}).items()}
            c.label = zh_of(lab) or lab.get("en") or c.qid
        return [c for c in top if not _is_admin_unit(c.label)][:MAX_COUNTRY_CHOICES]

    def seed_for(self, c: Candidate, query: str = "") -> dict:
        """一座城市条目 → 现建用的种子：国家、币种、语言、时区、人口、名字都来自维基数据。"""
        ents = self.labels([c.qid])
        ent = ents.get(c.qid) or {}
        claims = ent.get("claims") or {}

        def first(cl: dict, pid: str) -> str | None:
            # 城市常有好几条「国家」「货币」：先取标成「首选」的，再取没有结束时间（P582）的，免得取到历史政权 / 旧货币
            ranked = sorted(cl.get(pid, []),
                            key=lambda x: (x.get("rank") != "preferred", "P582" in (x.get("qualifiers") or {})))
            for x in ranked:
                v = (x.get("mainsnak", {}).get("datavalue") or {}).get("value")
                if isinstance(v, dict) and "id" in v:
                    return v["id"]
                if isinstance(v, dict) and "amount" in v:
                    return v["amount"]
                if v is not None:
                    return str(v)
            return None

        labels = {k: v["value"] for k, v in (ent.get("labels") or {}).items()}
        name_en = labels.get("en") or c.label
        name_zh = zh_of(labels) or name_en
        pop = first(claims, "P1082")
        population = int(float(pop)) if pop else None
        country_q = first(claims, "P17") or c.country_qid
        cent = (self.labels([country_q]).get(country_q) or {}).get("claims", {}) if country_q else {}
        iso = first(cent, "P297") or ""
        cur_q, lang_q = first(cent, "P38"), first(cent, "P37")
        extra = self.labels([q for q in (cur_q, lang_q) if q])
        currency = first((extra.get(cur_q or "") or {}).get("claims", {}), "P498") or ""
        language = first((extra.get(lang_q or "") or {}).get("claims", {}), "P218") or CHINESE_REGIONS.get(iso, "en")
        tz = zone_for_country(iso, c.lat or 0, c.lng or 0) or ""
        if not (iso and currency and tz):
            raise RuntimeError(f"{name_zh}：没能从维基数据读出国家 / 币种 / 时区，没法建城市")
        return make_seed(c, name_en=name_en, name_zh=name_zh, country_iso=iso, currency=currency, language=language,
                         timezone=tz, population=population, aliases=[query, name_en, name_zh, c.label])


__all__ = ["Candidate", "Resolution", "Resolver", "ZH_LABELS", "UA", "decide", "make_seed", "size_plan",
           "zone_for_country"]
