import json
from pathlib import Path

import pytest

from app.intake import run_intake

PERSONA_DIR = Path(__file__).resolve().parents[1] / "data" / "personas"


def load_persona(name: str) -> dict:
    return json.loads((PERSONA_DIR / name).read_text(encoding="utf-8"))


def test_missing_required_fields_produce_questions():
    result = run_intake({"destination": "Paris"})
    assert result.ok is False
    assert set(result.missing_fields) == {
        "start_date",
        "end_date",
        "budget_level",
        "travel_style",
        "interests",
    }
    assert len(result.questions) == len(result.missing_fields)


def test_persona_01_passes_cleanly():
    result = run_intake(load_persona("persona-01-museum-foodie.json"))
    assert result.ok is True
    assert result.request is not None
    assert result.request.trip_days == 4
    assert result.soft_questions == []


def test_persona_02_missing_hotel_area_triggers_soft_question():
    result = run_intake(load_persona("persona-02-family-relaxed.json"))
    assert result.ok is True
    assert any("地铁站" in q for q in result.soft_questions)


def test_persona_03_passes():
    result = run_intake(load_persona("persona-03-photographer-intense.json"))
    assert result.ok is True
    assert result.request.trip_days == 5


def test_invalid_value_reports_error_not_crash():
    result = run_intake(
        {
            "destination": "Paris",
            "start_date": "2026-10-06",
            "end_date": "2026-10-03",
            "budget_level": "medium",
            "travel_style": "balanced",
            "interests": ["food"],
        }
    )
    assert result.ok is False
    assert result.errors
    assert any("end_date" in e for e in result.errors)


@pytest.mark.parametrize(
    "name",
    [
        "persona-01-museum-foodie.json",
        "persona-02-family-relaxed.json",
        "persona-03-photographer-intense.json",
    ],
)
def test_all_personas_are_at_least_structurally_ok(name):
    assert run_intake(load_persona(name)).ok is True
