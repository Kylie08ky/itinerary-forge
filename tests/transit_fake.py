"""测试用的假 Transitous：回放 tests/fixtures/transit/ 里录下来的真实回复（格式和线上一样），不联网。

斯图加特住址 → 任何德国目的地都回放「→ 海德堡」那份；→ 斯特拉斯堡（坐标 48.58…）回放跨境那份；
回程（arriveBy）回放「海德堡 → 斯图加特」当晚的那份。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import httpx

from app.transit import Transitous

FIX = Path(__file__).resolve().parent / "fixtures" / "transit"
HOME = {"city": "Stuttgart", "city_zh": "斯图加特", "country": "DE", "lat": 48.7788075, "lng": 9.1778844}


RECORDED_DAY = date(2026, 12, 12)                          # 夹具是这一天的时刻表
_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def fixture(name: str) -> dict | list:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def shifted(node, days: int):
    """把回复里所有 UTC 时间往后挪 days 天：用同一份夹具回放别的日期（班次的钟点不变）。"""
    if days == 0:
        return node
    if isinstance(node, str):
        if _ISO.match(node):
            d = date.fromisoformat(node[:10]) + timedelta(days=days)
            return f"{d.isoformat()}{node[10:]}"
        return node
    if isinstance(node, list):
        return [shifted(x, days) for x in node]
    if isinstance(node, dict):
        return {k: shifted(v, days) for k, v in node.items()}
    return node


def handler(request: httpx.Request) -> httpx.Response:
    q = dict(request.url.params)
    if request.url.path.endswith("/geocode"):
        return httpx.Response(200, json=fixture("geocode_strasbourg.json" if "Strasbourg" in q["text"]
                                                else "geocode_stuttgart.json"))
    regional = "transitModes" in q
    days = (date.fromisoformat(q["time"][:10]) - RECORDED_DAY).days
    if q.get("arriveBy") == "true":
        body = fixture("plan_heidelberg_stuttgart_last_regional.json" if regional
                       else "plan_heidelberg_stuttgart_last.json")
    elif q["toPlace"].startswith("48.58"):                     # 斯特拉斯堡车站
        body = {"itineraries": []} if regional else fixture("plan_stuttgart_strasbourg.json")
    else:
        body = fixture("plan_stuttgart_heidelberg_regional.json" if regional else "plan_stuttgart_heidelberg.json")
    return httpx.Response(200, json=shifted(body, days))


def fake_transitous() -> Transitous:
    return Transitous(client=httpx.Client(transport=httpx.MockTransport(handler)))


def broken_transitous() -> Transitous:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    return Transitous(client=httpx.Client(transport=httpx.MockTransport(boom)))
