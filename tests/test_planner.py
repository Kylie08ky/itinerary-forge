import json
from pathlib import Path

import pytest

from app.llm import LLMError, StubClient
from app.planner import (
    _enrich_booking_urls,
    _parse,
    attraction_catalog,
    build_prompts,
    format_attractions,
    generate_itinerary,
    load_knowledge,
)
from app.schemas import Itinerary, TravelRequest

PERSONA_DIR = Path(__file__).resolve().parents[1] / "data" / "personas"


def persona_request(name: str) -> TravelRequest:
    raw = json.loads((PERSONA_DIR / name).read_text(encoding="utf-8"))
    raw.pop("_meta", None)
    return TravelRequest.model_validate(raw)


def test_load_knowledge_is_the_planning_brief():
    text = load_knowledge()
    assert "闭馆日" in text and "卢浮宫" in text
    assert "### README" not in text


def _minimal_itinerary_dict(booking_title: str, url: str | None = None) -> dict:
    return {
        "trip": {
            "destination": "Paris",
            "date_range": {"start": "2026-10-03", "end": "2026-10-03"},
            "traveler_profile": {"style": "balanced", "budget": "medium", "interests": ["museum"]},
        },
        "days": [{
            "date": "2026-10-03", "theme": "test",
            "items": [{"time": "09:00", "title": booking_title, "type": "museum",
                       "location": "1 区", "duration_minutes": 60, "booking_required": True}],
        }],
        "booking_checklist": [{"title": booking_title, "reason": "需预约", "url": url}],
        "risk_notes": [], "version": "v1",
    }


def test_enrich_booking_urls_fills_known_official_link():
    it = Itinerary.model_validate(_minimal_itinerary_dict("卢浮宫"))
    enriched = _enrich_booking_urls(it)
    assert enriched.booking_checklist[0].url == "https://www.louvre.fr/en/visit"


def test_enrich_booking_urls_overrides_llm_hallucinated_link():
    """白名单里有的项目，永远信白名单，不信模型自己编的 url（哪怕看起来很像官网）。"""
    it = Itinerary.model_validate(
        _minimal_itinerary_dict("卢浮宫", url="https://www.louvre.fr/en/online-tickets")
    )
    enriched = _enrich_booking_urls(it)
    assert enriched.booking_checklist[0].url == "https://www.louvre.fr/en/visit"


def test_enrich_booking_urls_matches_alias_with_suffix():
    """标题是「迪士尼乐园门票」（带后缀、不是白名单精确名「迪士尼乐园巴黎」），也要对得上。"""
    it = Itinerary.model_validate(_minimal_itinerary_dict("迪士尼乐园门票"))
    enriched = _enrich_booking_urls(it)
    assert enriched.booking_checklist[0].url == "https://www.disneylandparis.com/en-int/"


def test_enrich_booking_urls_clears_unknown_to_null():
    it = Itinerary.model_validate(
        _minimal_itinerary_dict("某个不在白名单里的地方", url="https://模型编的.example")
    )
    enriched = _enrich_booking_urls(it)
    assert enriched.booking_checklist[0].url is None


def test_parse_reconciles_and_enriches():
    raw = json.dumps(_minimal_itinerary_dict("卢浮宫（需预约）"), ensure_ascii=False)
    it = _parse(raw)
    assert it.booking_checklist[0].url == "https://www.louvre.fr/en/visit"


def test_attraction_catalog_shape():
    cat = attraction_catalog()
    assert len(cat) >= 20
    for e in cat:
        assert {"name_zh", "arr", "type", "duration_min", "metro", "closed", "booking"} <= e.keys()


def test_format_attractions_ranks_by_interest():
    req = persona_request("persona-03-photographer-intense.json")  # interests: photography...
    table = format_attractions(req)
    lines = [ln for ln in table.splitlines() if ln.startswith("| ") and "名称" not in ln]
    # 摄影相关的点应排在没打摄影标签的点前面
    assert lines[0].split("|")[1].strip() in {"夏乐宫 / 特罗卡德罗广场", "蒙马特高地与圣心大教堂",
                                              "奥赛博物馆", "罗丹博物馆", "皇家宫殿花园",
                                              "圣马丁运河", "拉雪兹神父公墓", "老佛爷天台观景"}


def test_build_prompts_injects_request_and_dates():
    req = persona_request("persona-01-museum-foodie.json")
    system, user = build_prompts(req, knowledge="（资料略）")
    assert "只输出一个 JSON" in system
    assert "2026-10-03" in user and "2026-10-06" in user
    assert "正好这 4 个元素" in user  # 4 天行程
    assert "（资料略）" in user


def test_generate_with_stub_returns_valid_itinerary():
    req = persona_request("persona-01-museum-foodie.json")
    result = generate_itinerary(req, client=StubClient())
    assert result.attempts == 1
    assert result.warnings == []
    it = result.itinerary
    assert len(it.days) == req.trip_days
    assert [d.date.isoformat() for d in it.days] == [
        "2026-10-03",
        "2026-10-04",
        "2026-10-05",
        "2026-10-06",
    ]
    assert it.booking_checklist  # stub 样例里有预约清单


class _ScriptedClient:
    """先按 responses 顺序返回，然后一直返回最后一个。用来测重试。"""

    def __init__(self, responses: list[str]):
        self._responses = responses
        self.calls = 0

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


def test_retries_once_then_succeeds():
    good = StubClient()._fixture
    client = _ScriptedClient(["这不是 JSON", good])
    req = persona_request("persona-01-museum-foodie.json")
    result = generate_itinerary(req, client=client)
    assert client.calls == 2
    assert result.attempts == 2
    assert len(result.warnings) == 1


def test_gives_up_after_retries_exhausted():
    client = _ScriptedClient(["nope"])
    req = persona_request("persona-01-museum-foodie.json")
    with pytest.raises(LLMError, match="没拿到合法 Itinerary"):
        generate_itinerary(req, client=client, max_retries=1)
    assert client.calls == 2


@pytest.mark.integration
def test_ollama_end_to_end():
    """真实调用 Ollama。默认跳过；跑：pytest -m integration"""
    import httpx

    from app.llm import LLMConfig, OllamaClient

    cfg = LLMConfig.from_env()
    try:
        httpx.get(f"{cfg.ollama_base_url}/api/tags", timeout=3).raise_for_status()
    except Exception:
        pytest.skip("Ollama 不可用")

    req = persona_request("persona-01-museum-foodie.json")
    result = generate_itinerary(req, client=OllamaClient(cfg), max_retries=2)
    assert len(result.itinerary.days) == req.trip_days
