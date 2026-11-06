import json
from pathlib import Path

import pytest

from app.renderer import (
    _fmt_duration,
    find_browser,
    render_files,
    render_html,
    render_markdown,
)
from app.schemas import Itinerary, TravelRequest
from app.validator import validate_itinerary

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "fixtures" / "stub-itinerary.json"
PERSONA = ROOT / "data" / "personas" / "persona-01-museum-foodie.json"


@pytest.fixture
def itinerary() -> Itinerary:
    return Itinerary.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def request_() -> TravelRequest:
    raw = json.loads(PERSONA.read_text(encoding="utf-8"))
    raw.pop("_meta", None)
    return TravelRequest.model_validate(raw)


def test_fmt_duration():
    assert _fmt_duration(45) == "45 分钟"
    assert _fmt_duration(60) == "1 小时"
    assert _fmt_duration(150) == "2 小时 30 分钟"


def test_markdown_has_key_sections(itinerary, request_):
    report = validate_itinerary(itinerary, request_)
    md = render_markdown(itinerary, request_, report)
    assert "# Paris · 4 天个性化行程" in md
    assert "## 第 1 天 · 2026-10-03（周六）" in md
    assert "卢浮宫" in md and "🎫需预约" in md
    assert "## 购票清单" in md
    assert "- [ ] **塞纳河游船**" in md
    assert "## 风险提醒" in md
    assert "质检：0 error / 0 warning" in md
    assert "均衡节奏" in md and "中等预算" in md


def test_html_is_self_contained(itinerary, request_):
    html = render_html(itinerary, request_, validate_itinerary(itinerary, request_))
    assert html.lstrip().startswith("<!doctype html>")
    assert "<style>" in html and "http" not in html.split("<body>")[0].replace("lang", "")
    assert "第 1 天" in html
    assert "需预约" in html
    assert "@page" in html  # 打印优化


def test_render_files_writes_md_and_html(tmp_path, itinerary, request_):
    res = render_files(itinerary, tmp_path, name="trip", formats=("md", "html"),
                       request=request_)
    assert res.markdown.exists() and res.markdown.suffix == ".md"
    assert res.html.exists()
    assert res.pdf is None


browser_missing = find_browser() is None


@pytest.mark.skipif(browser_missing, reason="没有 Chrome / Edge，跳过 PDF 测试")
def test_render_pdf(tmp_path, itinerary, request_):
    res = render_files(itinerary, tmp_path, name="trip", formats=("html", "pdf"),
                       request=request_)
    assert res.pdf.exists()
    assert res.pdf.read_bytes()[:5] == b"%PDF-"
    assert res.pdf.stat().st_size > 2000
