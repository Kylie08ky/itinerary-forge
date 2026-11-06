import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.renderer import find_browser
from app.versions import PlanStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "fixtures" / "stub-itinerary.json"
PERSONA = ROOT / "data" / "personas" / "persona-01-museum-foodie.json"

GOLDEN = FIXTURE.read_text(encoding="utf-8")


def persona_payload() -> dict:
    raw = json.loads(PERSONA.read_text(encoding="utf-8"))
    raw.pop("_meta", None)
    return raw


def mutated_json() -> str:
    d = copy.deepcopy(json.loads(GOLDEN))
    d["days"][2]["items"].insert(1, {
        "time": "10:30", "title": "玛黑区精品咖啡馆", "type": "food",
        "location": "4 区", "duration_minutes": 40, "booking_required": False,
    })
    return json.dumps(d, ensure_ascii=False)


class SequenceClient:
    def __init__(self, *payloads: str):
        self._payloads = list(payloads)
        self.calls = 0

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        p = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return p


@pytest.fixture
def client(tmp_path) -> TestClient:
    seq = SequenceClient(GOLDEN, mutated_json())
    app = create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=lambda: seq)
    return TestClient(app)


# --------------------------------------------------------------------------- #
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "llm_backend" in r.json()


def test_attractions_have_coordinates(client):
    r = client.get("/attractions")
    assert r.status_code == 200
    body = r.json()
    assert len(body) >= 20
    assert all("lat" in e and "lng" in e and "name_zh" in e for e in body)


def test_intake_incomplete_returns_questions(client):
    r = client.post("/intake", json={"destination": "Paris"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["questions"]


def test_intake_complete_ok(client):
    r = client.post("/intake", json=persona_payload())
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_create_plan_incomplete_422(client):
    r = client.post("/plans", json={"destination": "Paris"})
    assert r.status_code == 422
    assert r.json()["detail"]["questions"]


def test_create_plan_and_fetch(client):
    r = client.post("/plans", json=persona_payload())
    assert r.status_code == 201
    body = r.json()
    pid = body["plan_id"]
    assert body["version"] == 1
    assert len(body["itinerary"]["days"]) == 4
    assert "report" in body

    assert pid in client.get("/plans").json()

    detail = client.get(f"/plans/{pid}").json()
    assert detail["latest_version"] == 1
    assert len(detail["history"]) == 1
    assert detail["history"][0]["trigger"] == "initial"


def test_unknown_plan_404(client):
    assert client.get("/plans/nope-123").status_code == 404


def test_revision_creates_v2_with_diff(client):
    pid = client.post("/plans", json=persona_payload()).json()["plan_id"]
    r = client.post(f"/plans/{pid}/revisions", json={"feedback": "第 3 天上午加一个咖啡馆"})
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == 2
    assert "2026-10-05" in body["change"]["changed_days"]
    assert any("咖啡馆" in a for a in body["change"]["added"])

    hist = client.get(f"/plans/{pid}").json()["history"]
    assert [h["version"] for h in hist] == [1, 2]
    assert hist[1]["note"] == "第 3 天上午加一个咖啡馆"


def test_render_markdown(client):
    pid = client.post("/plans", json=persona_payload()).json()["plan_id"]
    r = client.get(f"/plans/{pid}/itinerary.md")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "# Paris · 4 天个性化行程" in r.text


@pytest.mark.skipif(find_browser() is None, reason="没有 Chrome / Edge")
def test_render_pdf(client):
    pid = client.post("/plans", json=persona_payload()).json()["plan_id"]
    r = client.get(f"/plans/{pid}/itinerary.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"


# --------------------------------------------------------------------------- #
# v0.1：按主题生成（规格 M01 S1/S2/S5）
# --------------------------------------------------------------------------- #
class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        from app.llm import LLMError
        raise LLMError("Ollama 没启动")


def theme_payload(theme="art", **over) -> dict:
    body = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06",
            "budget_level": "medium", "travel_style": "balanced", "interests": [theme], "theme": theme}
    body.update(over)
    return body


@pytest.fixture
def dead_llm_client(tmp_path) -> TestClient:
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=lambda: DeadLLM()))


def test_themes_endpoint_lists_all_eight_with_copy(client):
    body = client.get("/themes").json()
    assert {t["id"] for t in body} == {"art", "fashion", "shopping", "museums", "entertainment",
                                       "family", "photography", "food"}
    assert all(t["name_zh"] and t["tagline"] and t["anchors_per_day"] for t in body)


def test_places_endpoint_exposes_check_records(client):
    body = client.get("/places").json()
    assert len(body) >= 80
    louvre = next(p for p in body if p["id"] == "louvre")
    assert louvre["official_url"].startswith("https://") and louvre["url_status"] == "ok"
    assert louvre["verified_at"] == "2026-10-08"
    assert all(set(p["closed_weekdays"]) <= set(range(7)) for p in body)
    art = client.get("/places", params={"theme": "art"}).json()
    assert 0 < len(art) < len(body)


def test_places_endpoint_gives_links_only_for_places_with_a_ticket_channel(client):
    body = {p["id"]: p for p in client.get("/places").json()}
    assert body["louvre"]["official_url"].startswith("https://") and body["louvre"]["ticketing"] == "online_required"
    assert body["rodin"]["official_url"].startswith("https://") and body["rodin"]["ticketing"] == "online_or_walkin"
    assert body["tuileries-garden"]["official_url"] is None and body["tuileries-garden"]["ticketing"] == "none"
    assert "排队" in body["eiffel-tower"]["ticket_note"] or "售罄" in body["eiffel-tower"]["ticket_note"]
    assert all(p["address"] for p in body.values())
    for p in body.values():
        assert (p["official_url"] is not None) == (p["ticketing"] != "none"), p["id"]


def test_create_plan_accepts_the_paper_ticket_preference(client):
    r = client.post("/plans", json={**theme_payload("art"), "paper_tickets": True})
    assert r.status_code == 201, r.text


def test_create_plan_by_theme_works_without_a_model(dead_llm_client):
    r = dead_llm_client.post("/plans", json=theme_payload("art"))
    assert r.status_code == 201
    body = r.json()
    it = body["itinerary"]
    assert len(it["days"]) == 4
    assert it["generation"]["theme_id"] == "art"
    assert it["generation"]["narration"] == "template"
    assert it["generation"]["narration_note"]
    assert body["report"]["findings"] == [] or all(f["severity"] != "error" for f in body["report"]["findings"])
    # 周二没有卢浮宫、周一没有奥赛（M01 S2）
    by_date = {d["date"]: {i["place_id"] for i in d["items"]} for d in it["days"]}
    assert "louvre" not in by_date["2026-10-06"]
    assert "orsay" not in by_date["2026-10-05"]
    # 能取回，且保留生成信息
    pid = body["plan_id"]
    again = dead_llm_client.get(f"/plans/{pid}").json()
    assert again["itinerary"]["generation"]["theme_id"] == "art"


def test_stub_backend_text_is_not_mistaken_for_narration(client):
    """client 夹具里的 LLM 永远返回旧样例行程；它的格式对不上文案协议，必须降级为模板而不是被采用。"""
    it = client.post("/plans", json=theme_payload("art")).json()["itinerary"]
    assert it["generation"]["narration"] == "template"
    assert "塞纳河畔轻量漫步" not in " ".join(d["theme"] for d in it["days"])


def test_create_plan_unknown_theme_is_422(client):
    r = client.post("/plans", json=theme_payload("no-such-theme"))
    assert r.status_code == 422
    assert "art" in json.dumps(r.json(), ensure_ascii=False)


def test_legacy_create_plan_without_theme_still_works(client):
    r = client.post("/plans", json=persona_payload())
    assert r.status_code == 201
    assert r.json()["itinerary"]["generation"] is None
