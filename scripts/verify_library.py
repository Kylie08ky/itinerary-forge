"""自动核对地点库：官网链接、坐标、闭馆日（来源：HTTP 请求、OpenStreetMap Nominatim）。

用法：python scripts/verify_library.py [--city paris] [--out report.json]

只发公开的只读请求，参数里只有地点名；遵守 Nominatim 的 1 次/秒限速和 User-Agent 要求。
结果写成 JSON 报告，由调用方决定怎么修数据——脚本本身不改地点库。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
NOMINATIM_UA = "ai-travel-planner-agent-library-check/0.1 (personal project; contact via GitHub Kylie08ky)"

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


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


def format_address(a: dict) -> str | None:
    """把 Nominatim 的 address 拼成「门牌 街道, 邮编 城市」；缺街道或邮编就不要（宁缺毋滥）。"""
    road = a.get("road") or a.get("pedestrian") or a.get("footway") or a.get("path")
    postcode = a.get("postcode")
    if not road or not postcode:
        return None
    city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or "Paris"
    street = f"{a['house_number']} {road}" if a.get("house_number") else road
    return f"{street}, {postcode} {city}"


def geocode(client: httpx.Client, place: dict) -> dict:
    zone_hint = "Paris, France" if place["zone"] == "paris" else "France"
    q = f"{place['name_en']}, {zone_hint}"
    r = client.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": q, "format": "jsonv2", "limit": 3, "extratags": 1, "countrycodes": "fr",
                "addressdetails": 1, "accept-language": "fr"},
        headers={"User-Agent": NOMINATIM_UA}, timeout=30,
    )
    results = r.json() if r.status_code == 200 else []
    cands = []
    for it in results:
        lat, lng = float(it["lat"]), float(it["lon"])
        cands.append({
            "name": it.get("display_name", "")[:90], "lat": lat, "lng": lng,
            "dist_m": round(haversine_m(place["lat"], place["lng"], lat, lng)),
            "address": format_address(it.get("address") or {}),
            "opening_hours": (it.get("extratags") or {}).get("opening_hours"),
            "website": (it.get("extratags") or {}).get("website") or (it.get("extratags") or {}).get("contact:website"),
        })
    return {"query": q, "candidates": cands}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="paris")
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", default=None, help="只核对这些 id（逗号分隔）")
    args = ap.parse_args()

    data = json.loads((ROOT / "data" / "places" / f"{args.city}.json").read_text(encoding="utf-8"))["places"]
    only = set(args.only.split(",")) if args.only else None
    report = []
    with httpx.Client(headers={"User-Agent": BROWSER_UA, "Accept-Language": "en"}) as web, httpx.Client() as geo:
        for p in data:
            if only and p["id"] not in only:
                continue
            row: dict = {"id": p["id"], "name": p["name_zh"]}
            if p.get("official_url"):
                row["url"] = {"url": p["official_url"], **check_url(web, p["official_url"])}
            try:
                g = geocode(geo, p)
                row["geo"] = g
                for c in g["candidates"]:
                    if c["opening_hours"]:
                        c["closed_days_parsed"] = parse_closed_days(c["opening_hours"])
            except Exception as exc:  # noqa: BLE001
                row["geo"] = {"error": f"{type(exc).__name__}: {exc}"}
            report.append(row)
            time.sleep(1.1)           # Nominatim 限速
            print(f"{p['id']:30} url={row.get('url', {}).get('status')}  "
                  f"nearest={min((c['dist_m'] for c in row['geo'].get('candidates', [])), default=None)}m",
                  file=sys.stderr)

    out = Path(args.out) if args.out else ROOT / "data" / "places" / f"{args.city}.verification.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告已写入 {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
