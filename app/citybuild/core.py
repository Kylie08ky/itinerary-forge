# ruff: noqa: E501  （数据脚本：长句是文案，不折行）
"""城市现建的内核（规格 10）：不联网的纯函数——去重、去「一部分」、分主题、坐标核对、精校叠加、拼数据、审阅表。"""

from __future__ import annotations

import copy
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.citybuild.common import clean_name, haversine_m, slug

BUILDER_VERSION = 3             # 现建流程改进后加一：版本低的现建城市下次有人输入时自动重建（v2：更多景点来源、图片、简介；v3：气候数据）
COORD_LIMIT_M = 150
SAME_PLACE_M = 600

THEMES: dict[str, tuple[str, str, list[str]]] = {
    "heritage": ("古迹与历史", "{city}的宫殿、古迹、遗址和老建筑", ["history", "sightseeing"]),
    "religion": ("寺庙与宗教建筑", "{city}的寺庙、教堂、清真寺和宗教古迹", ["history", "architecture"]),
    "museum": ("博物馆与艺术", "{city}的博物馆、美术馆和艺术区", ["museum", "art"]),
    "nature": ("山水与自然", "{city}的山、湖、海滩、公园和户外", ["nature", "relax"]),
    "district": ("老街与市集", "{city}的老街区、广场、步行街和市集", ["street", "shopping"]),
    "landmark": ("地标与建筑", "{city}的塔、桥、高楼和标志性建筑", ["landmark", "architecture"]),
    "family": ("亲子与乐园", "{city}的动物园、水族馆和主题乐园", ["family", "entertainment"]),
    "food": ("美食", "{city}有名的餐厅和小吃", ["food", "cafe"]),
}
# 维基数据「是什么」→ 主题；一个点有几种身份时按这个顺序取最前面的
THEME_PRIORITY = ["museum", "family", "religion", "food", "nature", "landmark", "district", "heritage"]
CLASS_THEME: dict[str, str] = {
    "Q33506": "museum",
    "Q43501": "family", "Q2416723": "family", "Q2281788": "family",
    "Q16970": "religion", "Q2977": "religion", "Q44613": "religion", "Q160742": "religion", "Q5393308": "religion",
    "Q32815": "religion", "Q44539": "religion", "Q7245816": "religion",
    "Q11707": "food",
    "Q22698": "nature", "Q167346": "nature", "Q8502": "nature", "Q23397": "nature", "Q40080": "nature",
    "Q34038": "nature", "Q35509": "nature", "Q179049": "nature",
    "Q12518": "landmark", "Q12280": "landmark", "Q11303": "landmark",
    "Q174782": "district", "Q330284": "district",
}
MIN_THEME_PLACES = 1          # 一个主题至少要这么多个点才单列（「均衡」要覆盖山水、地标这类，一个点的主题也留着）


def best_class(classes: list[str]) -> str | None:
    """一个点有几种「是什么」：按主题顺序取最前面的那个。"""
    return min(classes, key=lambda k: THEME_PRIORITY.index(CLASS_THEME.get(k, "heritage")), default=None)
HERITAGE_CLASSES = {"Q16560", "Q751876", "Q23413", "Q57831", "Q44613", "Q160742", "Q2977", "Q16970", "Q174782",
                    "Q570116", "Q4989906", "Q483453", "Q109607", "Q12518", "Q12280", "Q543654", "Q1762114"}
PRICE_TODO = "价格、预约渠道和优惠待核对（开放数据草稿）：出发前请以官网为准"
ALLOWED_OVERLAY = {"closed_weekdays", "closed_dates", "date_notes", "typical_hours", "booking", "ticketing",
                   "ticket_note", "price_note", "long_term_closed", "hours_verified", "official_url", "metro",
                   "highlights", "note", "sources"}


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def _norm(name: str) -> str:
    return re.sub(r"[\s·()（）]", "", name)


def dedupe_same_place(cands: list[dict]) -> list[dict]:
    """同一个地方的两个条目（故宫 / 故宫博物院）：名字一个包含另一个、而且离得近 → 只留名气大的。"""
    ranked = sorted(cands, key=lambda c: -c["sitelinks"])
    kept: list[dict] = []
    for c in ranked:
        n = _norm(c["name"])
        twin = any((n in _norm(k["name"]) or _norm(k["name"]) in n)
                   and haversine_m(c["lat"], c["lon"], k["lat"], k["lon"]) <= SAME_PLACE_M for k in kept)
        if not twin:
            kept.append(c)
    order = {c["qid"]: i for i, c in enumerate(cands)}
    return sorted(kept, key=lambda c: order[c["qid"]])


def drop_parts(cands: list[dict], parts_of: dict[str, set[str]]) -> list[dict]:
    """「属于另一个候选的一部分」（乾清宫属于故宫）去掉；属于候选以外的东西不算。"""
    ids = {c["qid"] for c in cands}
    return [c for c in cands if not (parts_of.get(c["qid"], set()) & (ids - {c["qid"]}))]


def drop_excluded(cands: list[dict], excluded: set[str]) -> list[dict]:
    return [c for c in cands if c["qid"] not in excluded]


BIG_AREA_CLASSES = {"Q16560", "Q751876", "Q570116", "Q22698", "Q167346", "Q43501", "Q2416723", "Q2281788"}


def coord_limit(category: str, cls: str) -> int:
    """大景区（宫殿、公园、动物园）的中心点本来就会差几百米，放宽到 500 米；一般的点 150 米。"""
    return 500 if category == "park" or cls in BIG_AREA_CLASSES else COORD_LIMIT_M


def coords_agree(wd: tuple[float, float], osm: tuple[float, float] | None, limit_m: int = COORD_LIMIT_M) -> bool:
    return osm is not None and haversine_m(*wd, *osm) <= limit_m


def zone_for(lat: float, lng: float, bbox: dict) -> str:
    inside = bbox["lat"][0] <= lat <= bbox["lat"][1] and bbox["lng"][0] <= lng <= bbox["lng"][1]
    return "core" if inside else "day_trip"


def theme_for(category: str, cls: str = "", override: str | None = None) -> str:
    if override:
        return override
    if category == "museum":
        return "museum"
    if cls in CLASS_THEME:
        return CLASS_THEME[cls]
    if category == "food":
        return "food"
    if category in ("park", "activity"):
        return "nature"
    if category == "shopping":
        return "district"
    return "heritage"


def theme_members(place_ids: list[str]) -> list[dict]:
    """名气从大到小排好的地点：前 6 个是锚点（权重 5 5 4 4 3 3），其余是辅助点（权重 2）。"""
    weights = [5, 5, 4, 4, 3, 3]
    return [{"place_id": pid, "role": "anchor" if i < 6 else "support", "weight": weights[i] if i < 6 else 2}
            for i, pid in enumerate(place_ids)]


def apply_overlay(place: dict, overlay: dict, today: date) -> dict:
    """把有来源的精校事实叠到草稿上；没有来源、来源不是 https、改了不允许的字段，一律拒绝。"""
    bad = set(overlay) - ALLOWED_OVERLAY
    if bad:
        raise ValueError(f"{place.get('id')}: 精校里不允许改这些字段：{sorted(bad)}")
    sources = overlay.get("sources") or []
    if not sources:
        raise ValueError(f"{place.get('id')}: 精校必须带来源（sources：url、checked_at、what）")
    for s in sources:
        if not str(s.get("url", "")).startswith("https://"):
            raise ValueError(f"{place.get('id')}: 精校来源必须是 https 链接：{s.get('url')}")
        if not s.get("checked_at") or not s.get("what"):
            raise ValueError(f"{place.get('id')}: 精校来源要写核对日期和核对了什么")
    out = copy.deepcopy(place)
    for k, v in overlay.items():
        if k != "sources":
            out[k] = copy.deepcopy(v)
    checked = max(s["checked_at"] for s in sources)
    if "ticketing" in overlay or "ticket_note" in overlay or "booking" in overlay:
        out["ticketing_checked_at"] = checked
    if "official_url" in overlay:
        out["url_status"], out["verified_at"] = "ok", checked
    return out


# --------------------------------------------------------------------------- #
# 拼数据
# --------------------------------------------------------------------------- #
def _unique_id(raw: dict, taken: set[str]) -> str:
    base = slug(clean_name(raw["name_en"])) or f"place-{raw['qid'].lower()}"
    pid, k = base[:60].strip("-"), 2
    while pid in taken:
        pid, k = f"{base[:56]}-{k}", k + 1
    taken.add(pid)
    return pid


def apply_names(raws: list[dict], seed: dict) -> None:
    """种子里手写的中文名（seed["names"]，键是维基数据编号）盖过自动取的。"""
    names = seed.get("names") or {}
    for r in raws:
        if names.get(r["qid"]):
            r["name_zh"] = names[r["qid"]]


_ADMIN_AREA = re.compile(r"^(quartiere|municipio|circoscrizione|distretto|bezirk|stadtbezirk)\b", re.I)


def _area_name(area: str | None, city_zh: str) -> str:
    """区域名：行政区划的编号名（Quartiere 1、Municipio Roma I）对游客没有意义，写城市名。"""
    if not area or _ADMIN_AREA.match(area.strip()):
        return city_zh
    return area


def build_data(city_id: str, city_zh: str, raws: list[dict], today: date,
               overlays: dict[str, dict] | None = None) -> tuple[list[dict], dict]:
    """raws（已经取好、清理好的候选，按名气从大到小）→ 地点列表 + 主题包。"""
    taken: set[str] = set()
    places: list[dict] = []
    entries: list[dict] = []
    for raw in sorted(raws, key=lambda r: -r["sitelinks"]):
        pid = _unique_id(raw, taken)
        theme = theme_for(raw["category"], raw.get("cls", ""), raw.get("theme"))
        place: dict = {
            "id": pid, "name_zh": clean_name(raw["name_zh"]), "name_en": clean_name(raw["name_en"] or raw["name_zh"]),
            "category": raw["category"], "setting": raw["setting"], "zone": raw["zone"],
            "area": _area_name(raw.get("area"), city_zh), "lat": round(raw["lat"], 6), "lng": round(raw["lng"], 6),
            "duration_min": raw["minutes"], "price_note": PRICE_TODO,
            "open_access": bool(raw["open_access"]), "best_time": "any",
            "closed_weekdays": sorted(raw["closed"]) if raw.get("closed") else [],
            "long_term_closed": False, "booking": {"required": False}, "ticketing": "none",
            "highlights": [], "note": raw.get("note", ""), "interests": list(THEMES[theme][2]),
            "hours_verified": False, "coords_verified": raw.get("coords") == "osm",
            "data_status": "draft",
            "draft_source": {"osm": raw.get("osm"), "wikidata": raw["qid"], "built_at": today.isoformat()},
            "fame": {"sitelinks": int(raw["sitelinks"]), "source": "wikidata", "checked_at": today.isoformat()},
        }
        if raw.get("views") is not None:
            place["fame"]["views"], place["fame"]["views_period"] = int(raw["views"]), raw.get("views_period") or ""
        if raw.get("voyage"):
            place["voyage"] = {**raw["voyage"], "checked_at": today.isoformat()}
        if raw.get("url") and raw.get("url_ok"):
            place["official_url"], place["url_status"], place["verified_at"] = raw["url"], "ok", today.isoformat()
        overlay = (overlays or {}).get(raw["qid"])
        if overlay:
            place = apply_overlay(place, overlay, today)
        raw["_id"], raw["_theme"] = pid, theme
        places.append(place)
        entries.append({"id": pid, "theme": theme, "zone": raw["zone"], "sitelinks": int(raw["sitelinks"])})
    return places, make_themes(city_id, city_zh, entries)


def make_themes(city_id: str, city_zh: str, entries: list[dict]) -> dict:
    """entries = [{id, theme, zone, sitelinks}] → 主题包。名气（语言版本数）从大到小排，前 6 个是锚点；
    点数不到 MIN_THEME_PLACES 的主题并进「古迹与历史」；市区范围外的点（一日游）做锚点放进各自的主题（≥ 4 天的行程才会选到）。"""
    core = [e for e in entries if e["zone"] == "core"]
    counts: dict[str, int] = {}
    for e in core:
        counts[e["theme"]] = counts.get(e["theme"], 0) + 1
    remap = {t: ("heritage" if n < MIN_THEME_PLACES else t) for t, n in counts.items()}
    by_theme: dict[str, list[str]] = {}
    for e in sorted(core, key=lambda e: -e["sitelinks"]):
        by_theme.setdefault(remap[e["theme"]], []).append(e["id"])
    themes = []
    for tid in [t for t in THEMES if t in by_theme]:
        name, tagline, _interests = THEMES[tid]
        themes.append({
            "id": tid, "name_zh": name, "tagline": tagline.format(city=city_zh),
            "description": "开放数据草稿：按维基数据的知名度排序挑出的点，价格、预约和部分闭馆日还没核对，出发前请以官网为准。",
            "recipe": {"anchors_per_day": {"relaxed": 2, "balanced": 2, "intense": 3}, "best_time_bias": [],
                       "max_big_venues_per_day": 2, "notes": ""},
            "members": theme_members(by_theme[tid]), "tips": [],
        })
    for e in entries:
        if e["zone"] != "day_trip":
            continue
        theme = next((t for t in themes if t["id"] == remap.get(e["theme"], e["theme"])), None)
        if theme is not None:
            theme["members"].append({"place_id": e["id"], "role": "anchor", "weight": 4})
    return {"city": city_id, "themes": themes}


def _tz_rule(tz: str) -> tuple[float, str]:
    """标准时差（小时）和夏令时规则标记（现在只有「欧洲有夏令时」要用到；排程算日落时直接按时区算）。"""
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        return 0.0, "none"
    jan, jul = zone.utcoffset(datetime(2026, 1, 1, 12)), zone.utcoffset(datetime(2026, 7, 1, 12))
    if jan is None or jul is None:
        return 0.0, "none"
    std = min(jan, jul).total_seconds() / 3600
    return std, ("eu" if jan != jul and tz.startswith("Europe/") else "none")


def make_city(city_id: str, zh: str, en: str, center: tuple[float, float], bbox: dict, landmarks: list[str], *,
              today: date, seed: dict | None = None) -> dict:
    seed = seed or {}
    tz = seed.get("timezone", "Asia/Shanghai")
    std, dst = _tz_rule(tz)
    return {
        "id": city_id, "name_zh": zh, "name_en": en, "local_language": seed.get("local_language", "zh"),
        "aliases": sorted({city_id, zh.lower(), en.lower(), *[a.lower() for a in seed.get("aliases", []) if a.strip()]}),
        "country": seed.get("country", "CN"), "currency": seed.get("currency", "CNY"), "timezone": tz,
        "utc_offset": std, "dst": dst, "center": [round(center[0], 4), round(center[1], 4)], "bbox": bbox,
        "suggested_days": seed.get("suggested_days") or {
            "min": 3, "max": 5, "quality_max": 7, "note": "开放数据草稿：点是按名气自动选的，出发前请核对"},
        "landmarks": landmarks[:3], "supported": bool(seed.get("supported", False)), "tier": "standard",
        **({"wikidata": seed["qid"]} if seed.get("qid") else {}),
        **({"metro": seed["metro"]} if isinstance(seed.get("metro"), bool) else {}),
        **{k: seed[k] for k in ("monthly_high_c", "monthly_low_c", "monthly_rain_mm", "elevation_m") if seed.get(k)},
        "builder_version": BUILDER_VERSION,
    }


def review_table(city_zh: str, places: list[dict], raws: list[dict], today: date) -> str:
    by_id = {r.get("_id"): r for r in raws}
    lines = [f"# {city_zh}：地点审阅表（{today.isoformat()}，标准档草稿）", "",
             "> 请看一遍：不要的点告诉我（或写进种子的 exclude），想加的告诉我名字。「待核对」的事实不会编，页面上会写「以官网为准」。", "",
             "| # | 名字 | 主题 | 区域 | 维基语言版本数 | 坐标 | 官网 | 闭馆日 | 备注 |", "|---|---|---|---|---|---|---|---|---|"]
    coord_text = {"osm": "坐标已交叉核对（维基数据 + OSM）", "wikidata": "坐标只有维基数据",
                  "mismatch": "坐标差得多（>150 米），请看"}
    for i, p in enumerate(places, 1):
        r = by_id.get(p["id"], {})
        theme = THEMES.get(r.get("_theme", ""), ("", "", []))[0] or "—"
        zone = "一日游" if p["zone"] == "day_trip" else p["area"]
        url = "官网能打开" if p.get("official_url") else "官网待核对"
        closed = "、".join("一二三四五六日"[d] for d in p["closed_weekdays"]) or "待核对"
        if p["closed_weekdays"] and r.get("closed"):
            closed += "（OSM 社区数据）"
        notes = "种子补充" if r.get("seed") else ""
        if p.get("ticketing_checked_at"):
            notes = (notes + " 精校").strip()
        lines.append(f"| {i} | {p['name_zh']} | {theme} | {zone} | {r.get('sitelinks', '')} | "
                     f"{coord_text.get(r.get('coords', 'wikidata'), '')} | {url} | {closed} | {notes} |")
    lines += ["", f"共 {len(places)} 个点。"]
    return "\n".join(lines) + "\n"
