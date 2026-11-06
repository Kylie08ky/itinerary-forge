"""K0 底线（规格 M17 S2）：去巴黎化之后，巴黎的行为必须和之前逐字一致。

用几份固定请求（死模型 = 模板说明、固定今天、固定汇率）跑完整接口，把行程、路线、预算、清单、预订、
各种导出的内容算成指纹，和 `tests/fixtures/k0/paris_baseline.json` 里 K0 之前录下的比。
指纹对不上，说明去巴黎化改变了巴黎的结果：先找原因，不要直接重录。

重录（只在你确认新行为就是想要的之后）：

    set UPDATE_K0_BASELINE=1 && .venv\\Scripts\\python -m pytest -q tests/test_k0_paris_baseline.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.fx import FxService
from app.llm import LLMError
from app.versions import PlanStore

BASELINE = Path(__file__).resolve().parent / "fixtures" / "k0" / "paris_baseline.json"
TODAY = "2026-09-01"


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


def _fixed_fx() -> FxService:
    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbols"]
        return httpx.Response(200, json={"amount": 1.0, "base": "EUR", "date": "2026-10-13",
                                         "rates": {symbol: 7.489 if symbol == "CNY" else 0.86}})
    return FxService(None, httpx.Client(transport=httpx.MockTransport(handler)))


BASE = {"destination": "Paris", "budget_level": "medium", "travel_style": "balanced"}
CASES: dict[str, dict] = {
    "art-4d": {**BASE, "start_date": "2026-10-03", "end_date": "2026-10-06", "interests": ["art"], "theme": "art",
               "arrival_time": "2026-10-03T10:30:00", "departure_time": "2026-10-06T18:00:00"},
    "mixed-family-cap": {
        **BASE, "start_date": "2026-11-10", "end_date": "2026-11-14", "interests": ["art", "food"], "theme": "art",
        "themes": [{"id": "art", "weight": 4}, {"id": "food", "weight": 2}], "travel_style": "relaxed",
        "travelers": [{"age": 38}, {"age": 21, "residence": "fr", "student": True}, {"age": 8}],
        "budget_cap": 2500, "currency": "CNY", "arrival_time": "2026-11-10T09:00:00",
        "departure_time": "2026-11-14T20:00:00"},
    "museums-stays-must-avoid": {
        **BASE, "start_date": "2026-10-20", "end_date": "2026-10-23", "interests": ["museums"], "theme": "museums",
        "hotel_area": "9区", "must_go": ["奥赛博物馆"], "avoid": ["卢浮宫"], "paper_tickets": True,
        "stays": [{"name": "测试酒店", "check_in": "2026-10-20", "check_out": "2026-10-23", "area": "9区",
                   "nightly_eur": 150}]},
    "intense-2d": {**BASE, "start_date": "2026-12-01", "end_date": "2026-12-02", "interests": ["shopping"],
                   "theme": "shopping", "travel_style": "intense"},
}

_PLAN_ID = re.compile(r"paris-\d{8}-[0-9a-f]{6}")
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(\+00:00|Z)?")


def _scrub(obj):
    """去掉每次都会变的东西：随机行程号、带秒的生成时间。"""
    if isinstance(obj, dict):
        return {k: ("<ts>" if k.endswith("_at") and isinstance(v, str) and _STAMP.fullmatch(v) else _scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, str):
        return _PLAN_ID.sub("<plan>", obj)
    return obj


def _fp(value) -> str:
    text = value if isinstance(value, str) else json.dumps(_scrub(value), ensure_ascii=False, sort_keys=True)
    # 唯一有意的改动（M17 S1）：地点的 zone 值 "paris" 改名 "core"（主城区）。基线是改名之前录的，这里按旧名比
    text = text.replace('"zone": "core"', '"zone": "paris"')
    text = _PLAN_ID.sub("<plan>", text)
    text = "\n".join(line for line in text.splitlines() if not line.startswith("DTSTAMP:"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def collect(tmp_path, monkeypatch) -> dict:
    monkeypatch.setenv("ATP_TODAY", TODAY)
    store = PlanStore(root=tmp_path / "plans")
    client = TestClient(create_app(store=store, client_factory=DeadLLM, fx=_fixed_fx()))
    out: dict = {}
    for name, body in CASES.items():
        r = client.post("/plans", json=body)
        assert r.status_code == 201, (name, r.text)
        pid = r.json()["plan_id"]
        got: dict[str, str] = {"create_response": _fp(r.json())}
        for label, path in {
            "detail": f"/plans/{pid}", "route": f"/plans/{pid}/route", "budget": f"/plans/{pid}/budget",
            "budget_settings": f"/plans/{pid}/budget-settings", "reservations": f"/plans/{pid}/reservations",
            "packing": f"/plans/{pid}/packing", "todos": f"/plans/{pid}/todos",
            "md": f"/plans/{pid}/itinerary.md", "html": f"/plans/{pid}/itinerary.html",
            "ics": f"/plans/{pid}/itinerary.ics", "kml": f"/plans/{pid}/itinerary.kml",
        }.items():
            resp = client.get(path)
            assert resp.status_code == 200, (name, label, resp.status_code, resp.text[:200])
            got[label] = _fp(resp.json() if resp.headers["content-type"].startswith("application/json") else resp.text)
        out[name] = got
    # 地点库、主题、知识库这些公共数据的对外形状
    lib: dict[str, str] = {}
    for label, path in {"places": "/places", "themes": "/themes", "attractions": "/attractions"}.items():
        lib[label] = _fp(client.get(path).json())
    place_ids = [p["id"] for p in client.get("/places").json()]
    lib["place_details"] = _fp([client.get(f"/places/{i}").json() for i in place_ids])
    lib["theme_details"] = _fp([client.get(f"/themes/{t['id']}").json() for t in client.get("/themes").json()])
    out["_library"] = lib
    return out


def test_paris_outputs_are_identical_to_the_k0_baseline(tmp_path, monkeypatch):
    got = collect(tmp_path, monkeypatch)
    if os.environ.get("UPDATE_K0_BASELINE"):
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("已重录 K0 基线")
    want = json.loads(BASELINE.read_text(encoding="utf-8"))
    diffs = [f"{case}.{key}" for case, parts in want.items() for key, fp in parts.items()
             if got.get(case, {}).get(key) != fp]
    assert not diffs, f"这些输出和 K0 之前不一样：{diffs}"


def test_the_baseline_itself_is_deterministic(tmp_path, monkeypatch):
    """同样的请求连跑两遍指纹必须一样，否则基线没有意义。"""
    a = collect(tmp_path / "a", monkeypatch)
    b = collect(tmp_path / "b", monkeypatch)
    assert a == b
