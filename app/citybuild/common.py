"""城市现建用的小工具（景点分类、名字清理、OSM 开放时间、官网核对）。

从 scripts/build_light_city.py 和 scripts/verify_library.py 里抽出来，让产品代码（app/）也能用；脚本里的原版暂时保留。
"""

from __future__ import annotations

import math
import re
import unicodedata

import httpx

ZH_LABELS = ("zh-hans", "zh-cn", "zh")


def is_junk(name: str) -> bool:
    """OSM 里挂在景点旁边的停车场入口、话题标签之类，不是景点本身的名字。"""
    low = name.strip().lower()
    return low.startswith("#") or low.startswith(("accès", "acces")) or "parking" in low


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
    # 中国的寺庙 / 清真寺 / 妈祖庙（泉州开元寺、清净寺、天后宫）：维基数据里不属于上面的「教堂」类
    "Q5393308": {"amenity": "place_of_worship"}, "Q32815": {"amenity": "place_of_worship"},
    "Q44539": {"amenity": "place_of_worship"}, "Q7245816": {"amenity": "place_of_worship"},
    # 中国的文物保护单位 / 文化遗产 / 国家 5A 级旅游景区：国内景点在维基数据里多半只有这几类标签、语言版本很少
    "Q10300916": {"historic": "monument"}, "Q210272": {"historic": "monument"}, "Q6838244": {"tourism": "attraction"},
    # 山水、高楼、市集、名餐厅（规格 10 H20）：山、湖、海滩、瀑布、洞、保护区、摩天楼、市场、餐厅
    "Q8502": {"natural": "peak"}, "Q23397": {"natural": "water"}, "Q40080": {"natural": "beach"},
    "Q34038": {"natural": "waterfall"}, "Q35509": {"natural": "cave_entrance"},
    "Q179049": {"leisure": "nature_reserve"},
    "Q11303": {"man_made": "tower"}, "Q330284": {"amenity": "marketplace"}, "Q11707": {"amenity": "restaurant"},
}
# 这几类要名气够大才算景点（无名的桥、小山、小湖、普通餐厅太多）：类别 → 最少语言版本数
CLASS_MIN_LINKS: dict[str, int] = {"Q12280": 8, "Q8502": 6, "Q23397": 6, "Q40080": 5, "Q34038": 4, "Q35509": 4,
                                   "Q11303": 8, "Q11707": 6, "Q330284": 4}
SPARQL_TEMPLATE = """
SELECT ?item ?coord ?sitelinks (SAMPLE(?cls) AS ?class)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?cls), "entity/"); separator=",") AS ?classes) WHERE {{
  SERVICE wikibase:around {{ ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point({lon} {lat})"^^geo:wktLiteral . bd:serviceParam wikibase:radius "{km}" . }}
  ?item wikibase:sitelinks ?sitelinks . FILTER(?sitelinks >= {minlinks})
  ?item wdt:P31/wdt:P279* ?cls .
  VALUES ?cls {{ {classes} }}
}}
GROUP BY ?item ?coord ?sitelinks ORDER BY DESC(?sitelinks) LIMIT 300
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
    if tags.get("natural") in ("peak", "water", "beach", "waterfall", "cave_entrance") or leisure == "nature_reserve":
        return "park", "outdoor", 120, True
    if amenity == "marketplace":
        return "shopping", "mixed", 60, True
    if amenity == "restaurant":
        return "food", "indoor", 60, False
    if historic in ("city_gate", "ruins", "archaeological_site", "monument", "tower", "building"):
        return "sight", "outdoor", 25, True
    if building == "town_hall" or amenity == "townhall":
        return "sight", "outdoor", 20, True
    if tags.get("place") == "square" or tags.get("man_made") in ("tower", "bridge"):
        return "sight", "outdoor", 20, True
    if tourism == "attraction":
        return "sight", "outdoor", 30, free
    return None


def first_sentence(extract: str, limit: int = 90) -> str:
    s = extract.strip().replace("\n", " ")
    cut = s.find("。")
    s = s[: cut + 1] if cut != -1 else s
    return s if len(s) <= limit else s[:limit].rstrip("，、；：") + "…"


def zh_of(labels: dict[str, str]) -> str | None:
    return next((labels[k] for k in ZH_LABELS if k in labels), None)


DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def check_url(client: httpx.Client, url: str) -> dict:
    try:
        r = client.get(url, follow_redirects=True, timeout=20)
    except httpx.HTTPError as exc:
        return {"ok": False, "status": None, "error": type(exc).__name__}
    m = TITLE_RE.search(r.text[:200_000]) if "text" in r.headers.get("content-type", "") else None
    title = re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""
    return {"ok": r.status_code < 400, "status": r.status_code, "final_url": str(r.url), "title": title}


_DAY_SEL = r"(?:[A-Z][a-z](?:-[A-Z][a-z])?)(?:\s*,\s*[A-Z][a-z](?:-[A-Z][a-z])?)*"


def _days_of(selector: str) -> set[int] | None:
    days: set[int] = set()
    for part in re.split(r"\s*,\s*", selector.strip()):
        if "-" in part:
            a, b = part.split("-")
            if a not in DAYS or b not in DAYS:
                return None
            i, j = DAYS.index(a), DAYS.index(b)
            days.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
        elif part in DAYS:
            days.add(DAYS.index(part))
        else:
            return None
    return days


def parse_closed_days(opening_hours: str) -> list[int] | None:
    """从 OSM opening_hours 里读出「哪几天不开」。只处理最常见的写法；读不懂返回 None。

    规则按顺序生效，后面的覆盖前面的（所以 "Mo-Su 09:00-18:00; Tu off" 周二不开）。
    """
    text = opening_hours.strip()
    if not text or any(tok in text for tok in ("sunrise", "sunset", "week ", "easter")):
        return None
    open_days: set[int] = set()
    for rule in text.split(";"):
        rule = rule.strip()
        if not rule or rule.startswith("PH") or re.match(r"^[A-Z][a-z]{2}\s+\d", rule):   # 节假日 / 具体日期
            continue
        # "Fr 08:00-12:00, Sa-Su 10:00-18:00"：逗号后紧跟「星期 + 时间」，拆成两条规则
        for sub in re.split(r",\s*(?=[A-Z][a-z](?:-[A-Z][a-z])?\s+\d)", rule):
            sub = sub.strip()
            off = re.match(r"^(" + _DAY_SEL + r")\s+off$", sub)
            if off:
                d = _days_of(off.group(1))
                if d is None:
                    return None
                open_days -= d
                continue
            if sub.endswith(" off"):          # 「Jan 01 off」之类，不影响每周规律
                continue
            m = re.match(r"^(" + _DAY_SEL + r")", sub)
            if not m:
                if re.match(r"^\d{2}:\d{2}", sub):        # 只有时间、没写星期 = 每天
                    open_days.update(range(7))
                    continue
                return None
            d = _days_of(m.group(1))
            if d is None:
                return None
            open_days |= d
    return sorted(set(range(7)) - open_days) if open_days else None
