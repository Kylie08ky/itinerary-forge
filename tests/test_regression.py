"""回归测试：对固定用例集跑 validator，断言 finding 代码集合没有漂移。

用例在 tests/regression/cases/*.json，由 tests/regression/build_cases.py 生成。
改了规则、确认新行为正确后，重新跑 build_cases 冻结，再 review diff。
"""

import json
from pathlib import Path

import pytest

from app.schemas import Itinerary, TravelRequest
from app.validator import validate_itinerary

CASES = sorted((Path(__file__).resolve().parent / "regression" / "cases").glob("*.json"))


@pytest.mark.parametrize("case_path", CASES, ids=lambda p: p.stem)
def test_regression_case(case_path: Path):
    case = json.loads(case_path.read_text(encoding="utf-8"))
    itinerary = Itinerary.model_validate(case["itinerary"])
    request = TravelRequest.model_validate(case["request"])

    report = validate_itinerary(itinerary, request)
    got_errors = sorted({f.code for f in report.errors})
    got_warnings = sorted({f.code for f in report.warnings})

    exp = case["expect"]
    assert report.ok == exp["ok"], case["description"]
    assert got_errors == exp["error_codes"], f"{case['name']}: error 代码漂移"
    assert got_warnings == exp["warning_codes"], f"{case['name']}: warning 代码漂移"


def test_regression_set_is_not_empty():
    assert len(CASES) >= 6
    names = {p.stem for p in CASES}
    assert "clean-golden" in names
