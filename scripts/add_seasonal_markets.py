"""把 data/seasonal/christmas-markets.json 里的圣诞市场加进各城市的地点库（规格 08 3.4 / R0-e）。

用法：
    python scripts/add_seasonal_markets.py
    python scripts/add_seasonal_markets.py --dry-run

每个市场变成该城市里的一个地点 `<城市>-weihnachtsmarkt`：带 `season`（举办期、来源、核对日期），不在期内当作闭馆，
所以只有行程日期落在举办期里才会排进去；坐标由 OSM（Nominatim）按 `osm_query` 搜出来，
必须落在城市中心 MARKET_RADIUS_M 米内，不手写。名字取官方的当地语言名；
中文名 = 城市中文名 + 「圣诞市场」（通用名词，不是翻译专名）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apply_verification import dump  # noqa: E402
from fetch_local_names import haversine_m  # noqa: E402

SEASONAL = ROOT / "data" / "seasonal" / "christmas-markets.json"
UA = "ai-travel-planner-agent-markets/0.1 (https://github.com/Kylie08ky; personal project)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
MARKET_RADIUS_M = 1500
THEME_ID = "daytrip"


def market_place(m: dict, city: dict, lat: float, lng: float, osm: str | None, today: date) -> dict:
    pid = f"{city['id']}-weihnachtsmarkt"
    name = m["name_local"]
    confirmed = m["confirmed_by"] == "official"
    place = {
        "id": pid, "name_zh": f"{city['name_zh']}圣诞市场", "name_en": name, "name_local": name,
        "name_local_check": {"source": "manual", "status": "verified", "checked_at": m["checked_at"], "wikidata": None},
        "category": "shopping", "setting": "outdoor", "zone": "core", "area": "市中心",
        "lat": round(lat, 6), "lng": round(lng, 6), "duration_min": 90, "open_access": True,
        "price_note": "入场免费；吃喝购物自费。" + ("" if confirmed else "日期来自活动聚合网站，"),
        "best_time": "evening", "closed_weekdays": [], "long_term_closed": False,
        "booking": {"required": False}, "ticketing": "none",
        "highlights": [], "note": f"当季活动：{m['start']} 到 {m['end']}。{m['hours']}",
        "interests": ["christmas"], "hours_verified": False, "coords_verified": False,
        "season": {"start": m["start"], "end": m["end"], "source_url": m["source_url"], "checked_at": m["checked_at"],
                   "confirmed_by": m["confirmed_by"],
                   "note": "日期以官方网站为准" if confirmed else "日期没能在官方网站核对，出发前请确认"},
        "data_status": "draft",
        "draft_source": {"osm": osm, "wikidata": None, "built_at": today.isoformat()},
    }
    place["price_note"] += "出发前请到官网确认"
    return place


def resolve_point(client: httpx.Client, query: str, centre: tuple[float, float]) -> tuple[float, float, str] | None:
    r = client.get(NOMINATIM, params={"q": query, "format": "jsonv2", "limit": "5"}, timeout=40)
    r.raise_for_status()
    time.sleep(1.0)
    best: tuple[float, float, float, str] | None = None
    for c in r.json():
        lat, lon = float(c["lat"]), float(c["lon"])
        d = haversine_m(centre[0], centre[1], lat, lon)
        if d <= MARKET_RADIUS_M and (best is None or d < best[0]):
            best = (d, lat, lon, f"{c.get('osm_type', 'node')}/{c.get('osm_id', '')}")
    return (best[1], best[2], best[3]) if best else None


def upsert(places: list[dict], theme: dict, place: dict) -> None:
    places[:] = [p for p in places if p["id"] != place["id"]] + [place]
    members = [x for x in theme["themes"][0]["members"] if x["place_id"] != place["id"]]
    theme["themes"][0]["members"] = [{"place_id": place["id"], "role": "anchor", "weight": 5}, *members]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args()
    today = date.fromisoformat(args.today)
    markets = json.loads(SEASONAL.read_text(encoding="utf-8"))["markets"]
    with httpx.Client(headers={"User-Agent": UA}) as client:
        for m in markets:
            cid = m["city"]
            city = json.loads((ROOT / "data" / "cities" / f"{cid}.json").read_text(encoding="utf-8"))
            pp = ROOT / "data" / "places" / f"{cid}.json"
            tp = ROOT / "data" / "themes" / f"{cid}.json"
            data = json.loads(pp.read_text(encoding="utf-8"))
            theme = json.loads(tp.read_text(encoding="utf-8"))
            pt = resolve_point(client, m["osm_query"], tuple(city["center"]))
            if pt is None:
                print(f"[--] {cid}: OSM 在市中心 {MARKET_RADIUS_M} 米内找不到「{m['osm_query']}」，没加")
                continue
            place = market_place(m, city, pt[0], pt[1], pt[2], today)
            print(f"[ok] {cid}: {place['name_zh']} · {place['name_local']} ({pt[2]}) {m['start']}→{m['end']}")
            if args.dry_run:
                continue
            upsert(data["places"], theme, place)
            dump(pp, data)
            tp.write_text(json.dumps(theme, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
