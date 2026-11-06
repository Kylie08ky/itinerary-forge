"""录几份真实的 Transitous 回复当测试夹具（tests/fixtures/transit/）：单元测试不联网，但用的是真实的数据格式。

    python scripts/record_transit_fixtures.py

录的是：住址查询（斯图加特、斯特拉斯堡）、斯图加特住址 → 海德堡车站（周六早上）、海德堡车站 → 斯图加特住址
（当晚最晚到家）、斯图加特住址 → 斯特拉斯堡车站（跨境）。夹具里是公共交通数据和一个公开的商业街地址，没有个人信息。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.transit import API, LOCAL_MODES_PARAM, USER_AGENT, to_utc_iso  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "transit"
HOME = "48.7788075,9.1778844"            # 斯图加特 Königstraße 28（市中心的商业街）
HD = "49.4035,8.6753"                    # 海德堡中央火车站
SX = "48.5850,7.7340"                    # 斯特拉斯堡火车站
DAY = datetime(2026, 12, 12, 9, 0)


def get(client: httpx.Client, path: str, **params) -> object:
    r = client.get(f"{API}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def slim(body: object) -> object:
    """只留下夹具里用得到的字段（原始回复带大量路径几何和实时信息，几百 KB 一份）。"""
    if isinstance(body, dict) and "itineraries" in body:
        stop = ("name", "lat", "lon", "departure", "arrival")
        legs = lambda it: [  # noqa: E731
            {**{k: leg[k] for k in ("mode", "routeShortName", "displayName", "duration") if k in leg},
             "from": {k: leg["from"][k] for k in stop if k in leg["from"]},
             "to": {k: leg["to"][k] for k in stop if k in leg["to"]}} for leg in it["legs"]]
        return {"itineraries": [{**{k: it[k] for k in ("duration", "startTime", "endTime", "transfers")},
                                 "legs": legs(it)} for it in body["itineraries"]]}
    if isinstance(body, list):
        keep = ("type", "name", "lat", "lon", "street", "houseNumber", "country", "zip", "areas")
        return [{k: row[k] for k in keep if k in row} for row in body[:6]]
    return body


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as c:
        files = {
            "geocode_stuttgart.json": get(c, "/geocode", text="Königstraße 28, Stuttgart"),
            "geocode_strasbourg.json": get(c, "/geocode", text="Rue du Faubourg National, Strasbourg"),
            "plan_stuttgart_heidelberg.json": get(c, "/plan", fromPlace=HOME, toPlace=HD, time=to_utc_iso(DAY),
                                                  numItineraries=8),
            "plan_stuttgart_heidelberg_regional.json": get(c, "/plan", fromPlace=HOME, toPlace=HD,
                                                           time=to_utc_iso(DAY), numItineraries=8,
                                                           transitModes=LOCAL_MODES_PARAM),
            "plan_heidelberg_stuttgart_last_regional.json": get(
                c, "/plan", fromPlace=HD, toPlace=HOME, time=to_utc_iso(datetime(2026, 12, 12, 23, 59)),
                arriveBy="true", numItineraries=8, transitModes=LOCAL_MODES_PARAM),
            "plan_heidelberg_stuttgart_last.json": get(c, "/plan", fromPlace=HD, toPlace=HOME,
                                                       time=to_utc_iso(datetime(2026, 12, 12, 23, 59)),
                                                       arriveBy="true", numItineraries=8),
            "plan_stuttgart_strasbourg.json": get(c, "/plan", fromPlace=HOME, toPlace=SX, time=to_utc_iso(DAY),
                                                  numItineraries=8),
        }
    for name, body in files.items():
        body = slim(body)
        (OUT / name).write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("写入", name)


if __name__ == "__main__":
    main()
