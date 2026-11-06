"""长图导出（规格 M08 S6）：内容范围（不含私人信息）、缓存、失败提示、真实渲染的尺寸。

不联网：天气 / 汇率桩由 conftest 提供。真实渲染的那条在没有 Playwright 浏览器时自动跳过。
"""

from __future__ import annotations

import struct

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
from app.api import create_app
from app.companion import note_ref
from app.llm import LLMError
from app.versions import PlanStore


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(root=tmp_path / "plans")


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(create_app(store=store, client_factory=DeadLLM))


def make_plan(client) -> str:
    r = client.post("/plans", json={
        "destination": "Paris", "start_date": "2026-10-14", "end_date": "2026-10-17", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "theme": "art",
        "arrival_time": "2026-10-14 10:30", "departure_time": "2026-10-17 18:00"})
    assert r.status_code == 201, r.text
    return r.json()["plan_id"]


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


@pytest.fixture
def fake_renderers(monkeypatch):
    """假的 PDF / PNG 渲染：记下收到的 HTML，写一个假文件。"""
    seen = {"pdf": [], "png": []}

    def fake_pdf(src, dst, **kw):
        seen["pdf"].append(src.read_text(encoding="utf-8"))
        dst.write_bytes(b"%PDF-1.4 fake")
        return dst

    def fake_png(src, dst, **kw):
        seen["png"].append(src.read_text(encoding="utf-8"))
        dst.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return dst

    monkeypatch.setattr(api_mod, "html_to_pdf", fake_pdf)
    monkeypatch.setattr(api_mod, "html_to_png", fake_png)
    return seen


def test_the_long_image_leaves_out_everything_private_that_the_pdf_has(client, store, fake_renderers):
    pid = make_plan(client)
    it = store.load(pid)
    day = it.days[1]
    client.put(f"/plans/{pid}/notes", json={"ref": note_ref(day.date, day.items[0]), "text": "私人备注QWE"})
    client.put(f"/plans/{pid}/budget-settings", json={"travelers": [{"age": 41}, {"age": 7}], "currency": "CNY"})
    place = next(i.place_id for d in it.days for i in d.items if i.place_id)
    client.put(f"/plans/{pid}/reservations/{place}", json={"status": "booked", "confirmation_code": "SECRET-998"})

    assert client.get(f"/plans/{pid}/itinerary.pdf").status_code == 200
    pdf_html = fake_renderers["pdf"][0]
    assert "私人备注QWE" in pdf_html and "SECRET-998" in pdf_html and "费用估算" in pdf_html     # PDF 是给自己的

    r = client.get(f"/plans/{pid}/itinerary.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    html = fake_renderers["png"][0]
    for private in ("私人备注QWE", "SECRET-998", "费用估算", "备注："):
        assert private not in html, private
    assert "max-width: 760px" in html                                    # 手机竖屏宽度、不分页
    assert 'page-break' not in html.split("<body", 1)[1]
    assert it.trip.destination in html and day.items[0].title in html


def test_the_long_image_is_cached_by_content_and_rebuilt_when_the_itinerary_changes(client, store, fake_renderers):
    pid = make_plan(client)
    first = client.get(f"/plans/{pid}/itinerary.png")
    assert client.get(f"/plans/{pid}/itinerary.png").content == first.content
    assert len(fake_renderers["png"]) == 1
    # 私人备注变了，长图里本来就没有它：不用重新生成
    day = store.load(pid).days[1]
    client.put(f"/plans/{pid}/notes", json={"ref": note_ref(day.date, day.items[0]), "text": "新备注"})
    client.get(f"/plans/{pid}/itinerary.png")
    assert len(fake_renderers["png"]) == 1
    # 行程内容变了（平移日期 = 新版本）：重新生成
    assert client.post(f"/plans/{pid}/shift", json={"start_date": "2026-11-03"}).status_code == 201
    client.get(f"/plans/{pid}/itinerary.png")
    assert len(fake_renderers["png"]) == 2
    # 老版本还能导
    old = client.get(f"/plans/{pid}/itinerary.png?version=1")
    assert old.status_code == 200 and len(fake_renderers["png"]) == 2     # 和第一次内容一样 → 命中缓存
    assert old.headers["content-disposition"].endswith(f'{pid}-v1.png"')


def test_unknown_plans_and_versions_are_404(client):
    assert client.get("/plans/nope/itinerary.png").status_code == 404
    pid = make_plan(client)
    assert client.get(f"/plans/{pid}/itinerary.png?version=9").status_code in (404, 422)


def test_a_failed_render_says_so_and_leaves_no_broken_file(client, store, monkeypatch):
    def boom(src, dst, **kw):
        raise RuntimeError("没有浏览器")

    monkeypatch.setattr(api_mod, "html_to_png", boom)
    pid = make_plan(client)
    r = client.get(f"/plans/{pid}/itinerary.png")
    assert r.status_code == 503 and "长图生成失败" in r.json()["detail"]
    cache = store.database.path.parent / "pdf_cache"
    assert not cache.exists() or not list(cache.glob("*.png"))


def test_the_real_render_is_a_tall_phone_width_png_within_the_size_cap(client):
    pid = make_plan(client)
    r = client.get(f"/plans/{pid}/itinerary.png")
    if r.status_code == 503:
        pytest.skip("本机没有可用的 Playwright 浏览器")
    assert r.status_code == 200
    width, height = png_size(r.content)
    assert width in (760, 1520)                                           # 1 倍或 2 倍
    assert height > width * 2                                             # 竖向长图
    assert max(width, height) <= 16000
    assert len(r.content) > 20_000
