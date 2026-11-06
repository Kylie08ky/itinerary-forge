"""M12 接口：/materials/parse，以及笔记来源随行程需求保存（S2 / S3）。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.llm import LLMError
from app.versions import PlanStore

SNIP = "Day1 上午奥赛博物馆，周一闭馆别走空！建议提前订票。独一无二的片段ZZ9"


class Model:
    def __init__(self, names):
        self.names = names

    def complete_json(self, *, system, user, schema):
        return json.dumps({"places": [{"name": n} for n in self.names]}, ensure_ascii=False)


class Dead:
    def complete_json(self, *, system, user, schema):
        raise LLMError("模型没启动")


def make_client(tmp_path, factory) -> TestClient:
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=factory))


@pytest.fixture
def client(tmp_path):
    return make_client(tmp_path, lambda: Model(["奥赛博物馆", "Berthillon 冰淇淋"]))


def body(**over):
    b = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
         "travel_style": "balanced", "interests": ["art"], "theme": "art", "arrival_time": "2026-10-03 10:30",
         "departure_time": "2026-10-06 18:00"}
    b.update(over)
    return b


# ---- /materials/parse ----
def test_parse_returns_matched_and_pending_rows(client):
    r = client.post("/materials/parse", json={"text": "上午奥赛博物馆，周一闭馆。Berthillon 冰淇淋排队40分钟。"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["model_used"] is True and out["source"] == "text"
    row = out["matched"][0]
    assert row["place_id"] == "orsay" and row["confidence"] == "high" and row["name_zh"] == "奥赛博物馆"
    assert row["tips"][0]["kind"] in ("closed", "queue", "booking")
    assert [p["name"] for p in out["pending"]] == ["Berthillon"]


def test_parse_works_without_a_model(tmp_path):
    c = make_client(tmp_path, Dead)
    out = c.post("/materials/parse", json={"text": "卢浮宫要订票，Berthillon 排队"}).json()
    assert out["model_used"] is False and [m["place_id"] for m in out["matched"]] == ["louvre"] and out["pending"] == []


def test_parse_works_when_the_client_cannot_even_be_built(tmp_path):
    def broken():
        raise LLMError("未知的 LLM_BACKEND")

    out = make_client(tmp_path, broken).post("/materials/parse", json={"text": "卢浮宫"}).json()
    assert out["model_used"] is False and out["matched"][0]["place_id"] == "louvre"


@pytest.mark.parametrize("payload, code", [
    ({}, "empty"), ({"text": "   "}, "empty"), ({"text": "卢" * 8001}, "too_long"),
    ({"text": "卢浮宫", "destination": "Atlantis"}, "destination"),
])
def test_parse_rejects_bad_input_with_a_clear_code(client, payload, code):
    r = client.post("/materials/parse", json=payload)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code and r.json()["detail"]["message"]


def test_parse_does_not_store_the_note(tmp_path):
    c = make_client(tmp_path, lambda: Model([]))
    c.post("/materials/parse", json={"text": SNIP})
    for f in (tmp_path / "plans").rglob("*"):
        if f.is_file():
            assert b"ZZ9" not in f.read_bytes(), f"{f.name} 里存了笔记正文"


# ---- 随行程保存 ----
SRC = {"place_id": "orsay", "snippet": SNIP[:60], "tips": [{"kind": "closed", "label": "周一闭馆", "text": "周一闭馆"}]}
PENDING = {"name": "Berthillon", "snippet": "Berthillon 冰淇淋排队40分钟"}


def make_plan(client) -> str:
    r = client.post("/plans", json=body(must_go_ids=["orsay"], note_sources=[SRC], note_pending=[PENDING]))
    return r.json()["plan_id"]


def test_note_sources_and_pending_are_saved_with_the_plan(client):
    r = client.post("/plans", json=body(must_go_ids=["orsay"], note_sources=[SRC], note_pending=[PENDING]))
    assert r.status_code == 201, r.text
    pid = r.json()["plan_id"]
    d = client.get(f"/plans/{pid}").json()
    assert d["note_sources"][0]["place_id"] == "orsay" and d["note_sources"][0]["tips"][0]["label"] == "周一闭馆"
    assert d["note_pending"] == [PENDING]


def test_pending_places_are_never_scheduled(client):
    pid = client.post("/plans", json=body(note_pending=[PENDING])).json()["plan_id"]
    it = client.get(f"/plans/{pid}").json()["itinerary"]
    titles = " ".join(i["title"] for d in it["days"] for i in d["items"])
    assert "Berthillon" not in titles


def test_sources_survive_shift_duplicate_and_restore(client):
    pid = make_plan(client)
    shifted = client.post(f"/plans/{pid}/shift", json={"start_date": "2026-10-10"})
    assert shifted.status_code == 201, shifted.text
    d = client.get(f"/plans/{pid}").json()
    assert d["latest_version"] == 2 and d["note_pending"] == [PENDING]
    assert d["note_sources"][0]["snippet"] == SRC["snippet"]
    new = client.post(f"/plans/{pid}/duplicate").json()["plan_id"]
    assert client.get(f"/plans/{new}").json()["note_sources"][0]["place_id"] == "orsay"
    client.post(f"/plans/{pid}/versions/1/restore")
    assert client.get(f"/plans/{pid}").json()["note_sources"][0]["place_id"] == "orsay"


def test_notes_never_leak_to_share_pdf_markdown_or_calendar(client):
    pid = make_plan(client)
    token = client.post(f"/plans/{pid}/share").json()["token"]
    shared = client.get(f"/share/{token}").text
    for leak in ("ZZ9", "Berthillon", "note_sources", "note_pending"):
        assert leak not in shared
    for path in ("itinerary.md", "itinerary.html", "itinerary.ics", "itinerary.kml"):
        text = client.get(f"/plans/{pid}/{path}").text
        assert "ZZ9" not in text and "Berthillon" not in text, path


def test_too_many_or_too_long_items_are_rejected(client):
    assert client.post("/plans", json=body(note_sources=[SRC] * 61)).status_code == 422
    assert client.post("/plans", json=body(note_sources=[{**SRC, "snippet": "字" * 201}])).status_code == 422
    assert client.post("/plans", json=body(note_pending=[{"name": "", "snippet": ""}])).status_code == 422


# ---- 粘贴链接（M12 S7）----
def test_parse_a_link_reads_the_page_then_parses_it_like_pasted_text(tmp_path, monkeypatch):
    import app.api as api
    from app.notes.fetch import FetchedPage

    seen = []

    def fake_fetch(url):
        seen.append(url)
        return FetchedPage(text="上午奥赛博物馆，周一闭馆别走空！建议提前订票。", host="notes.example", final_url=url)

    monkeypatch.setattr(api, "fetch_page", fake_fetch)
    c = make_client(tmp_path, Dead)
    out = c.post("/materials/parse", json={"url": "https://notes.example/a/1"}).json()
    assert seen == ["https://notes.example/a/1"]
    assert out["source"] == "url" and out["source_host"] == "notes.example"
    assert out["matched"][0]["place_id"] == "orsay" and out["matched"][0]["tips"]


def test_a_link_that_cannot_be_read_says_why_and_asks_for_the_text(tmp_path):
    c = make_client(tmp_path, Dead)
    for url, reason in [("http://127.0.0.1/health", "内网"), ("file:///etc/passwd", "http"),
                        ("http://169.254.169.254/latest/meta-data/", "内网")]:
        r = c.post("/materials/parse", json={"url": url})
        d = r.json()["detail"]
        assert r.status_code == 422 and d["code"] == "fetch_failed" and reason in d["reason"]
        assert "粘贴文本" in d["message"]


def test_text_and_url_together_or_neither_are_rejected(tmp_path):
    c = make_client(tmp_path, Dead)
    both = c.post("/materials/parse", json={"text": "卢浮宫", "url": "https://example.com/"})
    assert both.status_code == 422 and both.json()["detail"]["code"] == "both"
    assert c.post("/materials/parse", json={"url": "  "}).json()["detail"]["code"] == "empty"


def test_very_long_pages_are_cut_not_rejected(tmp_path, monkeypatch):
    import app.api as api
    from app.notes.fetch import FetchedPage

    big = FetchedPage(text="卢浮宫真大。" * 5000, host="h.example", final_url="https://h.example/")
    monkeypatch.setattr(api, "fetch_page", lambda url: big)
    out = make_client(tmp_path, Dead).post("/materials/parse", json={"url": "https://h.example/"})
    assert out.status_code == 200 and out.json()["matched"][0]["place_id"] == "louvre"


# ---- 笔记导入单独用 14B ----
def test_note_import_uses_its_own_model_and_everything_else_keeps_the_default(tmp_path, monkeypatch):
    import app.api as api
    from app.llm import LLMConfig

    built = []

    class Spy:
        def complete_json(self, *, system, user, schema):
            return json.dumps({"places": []})

    def fake_get_client(config=None):
        built.append((config or LLMConfig.from_env()).ollama_model)
        return Spy()

    monkeypatch.setattr(api, "get_client", fake_get_client)
    monkeypatch.delenv("NOTE_OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    c = TestClient(create_app(store=PlanStore(root=tmp_path / "plans")))          # 不注入客户端：走默认的模型选择
    c.post("/materials/parse", json={"text": "卢浮宫"})
    assert built[-1] == "qwen2.5:14b-instruct"
    monkeypatch.setenv("NOTE_OLLAMA_MODEL", "qwen2.5:3b-instruct")
    c.post("/materials/parse", json={"text": "卢浮宫"})
    assert built[-1] == "qwen2.5:3b-instruct"
    assert LLMConfig.from_env().ollama_model == "qwen2.5:7b-instruct"              # 别的功能不受影响
