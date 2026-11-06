"""查城际时间表（规格 08 3.4 / R0-c）：出发地 → 每座目的地的最快单程用时、换乘次数、当晚最晚一班返程。

用法：
    python scripts/fetch_city_pairs.py                 # 全部出发地
    python scripts/fetch_city_pairs.py --origin stuttgart
    python scripts/fetch_city_pairs.py --dry-run       # 只打印，不写文件

数据来源：Transitous（https://transitous.org，开源社区运营的免费公共交通路线服务，
数据是德国 DELFI、荷兰 NS、比利时 SNCB 等官方开放时刻表）。**只在开发时查一次**，结果连同来源、查询日期、样例日期
存进 data/cities/city-pairs.json，应用运行时不联网。查到的是「样例日期当天」时刻表上的最快用时，不是实时延误；
用 DB Navigator 等官方渠道订票前再确认。查不到的目的地不写（永远不让模型编）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrip_destinations import DESTINATIONS, ORIGINS  # noqa: E402

UA = "ai-travel-planner-agent-pairs/0.1 (https://github.com/Kylie08ky; personal project)"
API = "https://api.transitous.org/api/v1"
WIKIDATA = "https://www.wikidata.org/w/api.php"
SOURCE = "Transitous（开放时刻表：德国 DELFI、荷兰 NS、比利时 SNCB 等）"
SOURCE_URL = "https://transitous.org"
SAMPLE_DATE = date(2026, 12, 12)            # 周六：游客一日游最常见的日子
PAUSE_S = 0.6
# 样例日期在冬令时（中欧时间 = UTC+1）：德国、荷兰、比利时、法国同一个时区。
# 不用 zoneinfo（Windows 上没有时区库，不为它引新依赖）
TZ = timezone(timedelta(hours=1))
assert SAMPLE_DATE.month in (11, 12, 1, 2), "样例日期要在冬令时里"
OUT = ROOT / "data" / "cities" / "city-pairs.json"
ZH_LABELS = ("zh-hans", "zh-cn", "zh")


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def to_utc_iso(day: date, hhmm: str, tz: timezone = TZ) -> str:
    """当地时间 → Transitous 要的 UTC 时间串。"""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_hhmm(iso_utc: str, tz: timezone = TZ) -> str:
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(tz).strftime("%H:%M")


def pick_stop(results: list[dict], hints: tuple[str, ...], exact: str | None = None) -> dict | None:
    """搜车站：指定了确切站名就只认它；否则名字里带提示词（Hbf / Bahnhof / gare…）的优先；没有就取第一个站点。"""
    stops = [r for r in results if r.get("type") == "STOP"]
    if exact:
        return next((r for r in stops if r.get("name") == exact), None)
    for r in stops:
        low = f" {r.get('name', '').lower()}"
        if any(h in low for h in hints):
            return r
    return stops[0] if stops else None


def fastest(itineraries: list[dict]) -> dict | None:
    return min(itineraries, key=lambda i: (i["duration"], i["transfers"]), default=None)


def is_bus_only(itinerary: dict) -> bool:
    """整段没有铁路（只有步行 / 巴士 / 电车）→ 当巴士算。"""
    modes = {leg["mode"] for leg in itinerary["legs"]} - {"WALK"}
    return bool(modes) and not any("RAIL" in m or m in {"SUBWAY", "FERRY"} for m in modes)


def latest_departure(itineraries: list[dict], tz: timezone = TZ) -> str | None:
    """「晚上 23:59 前赶到」的方案里，最晚出发的那班的当地出发时间。"""
    best = max((i["startTime"] for i in itineraries), default=None)
    return local_hhmm(best, tz) if best else None


# --------------------------------------------------------------------------- #
# 取数据
# --------------------------------------------------------------------------- #
def geocode(client: httpx.Client, text: str) -> list[dict]:
    r = client.get(f"{API}/geocode", params={"text": text, "language": "de"}, timeout=40)
    r.raise_for_status()
    time.sleep(PAUSE_S)
    return r.json()


def plan(client: httpx.Client, a: dict, b: dict, when_utc: str, *, arrive_by: bool = False) -> list[dict]:
    params = {"fromPlace": f"{a['lat']},{a['lon']}", "toPlace": f"{b['lat']},{b['lon']}", "time": when_utc,
              "numItineraries": "6"}
    if arrive_by:
        params["arriveBy"] = "true"
    r = client.get(f"{API}/plan", params=params, timeout=60)
    r.raise_for_status()
    time.sleep(PAUSE_S)
    return r.json().get("itineraries", [])


def zh_label(client: httpx.Client, qid: str) -> str | None:
    r = client.get(WIKIDATA, params={"action": "wbgetentities", "ids": qid, "props": "labels",
                                     "languages": "|".join(ZH_LABELS), "format": "json"}, timeout=30)
    r.raise_for_status()
    time.sleep(PAUSE_S)
    labels = (r.json().get("entities", {}).get(qid) or {}).get("labels", {})
    return next((labels[k]["value"] for k in ZH_LABELS if k in labels), None)


def station_zh(city_zh: str | None, stop_name: str) -> str:
    low = stop_name.lower()
    if not city_zh:
        return stop_name
    return f"{city_zh}中央火车站" if ("hbf" in low or "hauptbahnhof" in low) else f"{city_zh}火车站"


def build(client: httpx.Client, origin_ids: list[str], today: date) -> dict:
    origins: dict[str, dict] = {}
    pairs: list[dict] = []
    for oid in origin_ids:
        o = ORIGINS[oid]
        so = pick_stop(geocode(client, o.station_query), o.station_hints)
        if so is None:
            print(f"[错误] 找不到出发车站 {o.station_query}")
            continue
        o_zh = zh_label(client, o.qid) or o.name_local
        origins[oid] = {"name_local": o.name_local, "name_zh": o_zh, "country": o.country,
                        "station": {"name": so["name"], "name_zh": station_zh(o_zh, so["name"]),
                                    "lat": so["lat"], "lng": so["lon"]}}
        print(f"出发地 {oid}: {so['name']}")
        for d in (x for x in DESTINATIONS if x.origin == oid):
            sd = pick_stop(geocode(client, d.station_query), d.station_hints, d.station_exact)
            if sd is None:
                print(f"[--] {d.id}: 找不到车站 {d.station_query}")
                continue
            out = plan(client, so, sd, to_utc_iso(SAMPLE_DATE, "07:00"))
            best = fastest(out)
            if best is None:
                print(f"[--] {d.id}: 时刻表里没有 {so['name']} → {sd['name']} 的方案")
                continue
            back = plan(client, sd, so, to_utc_iso(SAMPLE_DATE, "23:59"), arrive_by=True)
            last = latest_departure(back)
            if last is None:
                print(f"[--] {d.id}: 查不到当晚的返程班次")
                continue
            d_zh = zh_label(client, d.qid) or d.name_local
            pairs.append({
                "origin": oid, "destination": d.id, "mode": "bus" if is_bus_only(best) else "train",
                "minutes": best["duration"] // 60, "transfers": best["transfers"],
                "station": {"name": sd["name"], "name_zh": station_zh(d_zh, sd["name"]),
                            "lat": sd["lat"], "lng": sd["lon"]},
                "last_return": last, "source": SOURCE, "source_url": SOURCE_URL, "checked_at": today.isoformat(),
                "sample_date": SAMPLE_DATE.isoformat(),
                "note": "样例日期当天时刻表上的最快用时；订票和出发前请用 DB Navigator 等官方渠道确认",
            })
            print(f"[ok] {d.id}: {best['duration'] // 60} 分钟 换乘 {best['transfers']} 次 "
                  f"末班返程 {last}  ({sd['name']})")
    return {"_note": "城际时间表（规格 08 3.4）。由 scripts/fetch_city_pairs.py 用开放时刻表查得，来源和日期见每条。",
            "origins": origins, "pairs": pairs}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origin", default="", help="只查这个出发地")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args()
    ids = [args.origin] if args.origin else list(ORIGINS)
    with httpx.Client(headers={"User-Agent": UA}) as client:
        data = build(client, ids, date.fromisoformat(args.today))
    if args.dry_run:
        return 0
    if OUT.exists() and args.origin:                      # 只重查一个出发地：保留另一个的
        old = json.loads(OUT.read_text(encoding="utf-8"))
        data["origins"] = {**old["origins"], **data["origins"]}
        data["pairs"] = [p for p in old["pairs"] if p["origin"] != args.origin] + data["pairs"]
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {len(data['pairs'])} 条 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
