import copy
import json
from pathlib import Path

from app.schemas import Itinerary, TravelRequest
from app.validator import RULES, Severity, validate_itinerary

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "fixtures" / "stub-itinerary.json"
PERSONA = ROOT / "data" / "personas" / "persona-01-museum-foodie.json"


def base_dict() -> dict:
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))


def persona_request() -> TravelRequest:
    raw = json.loads(PERSONA.read_text(encoding="utf-8"))
    raw.pop("_meta", None)
    return TravelRequest.model_validate(raw)


def run(d: dict):
    return validate_itinerary(Itinerary.model_validate(d), persona_request())


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


# --------------------------------------------------------------------------- #
def test_at_least_ten_rules_registered():
    assert len(RULES) >= 10


def test_golden_itinerary_is_clean():
    report = run(base_dict())
    assert report.errors == [], [f.message for f in report.errors]
    assert report.warnings == [], [f.message for f in report.warnings]
    assert report.ok is True


def test_day_count_mismatch():
    d = base_dict()
    d["days"] = d["days"][:3]
    assert "day_count" in codes(run(d))


def test_date_out_of_range():
    d = base_dict()
    d["days"][2]["date"] = "2026-12-25"
    report = run(d)
    assert "date_out_of_range" in codes(report)
    assert not report.ok


def test_bad_time_format():
    d = base_dict()
    d["days"][0]["items"][1]["time"] = "25:99"
    assert "bad_time_format" in codes(run(d))


def test_items_unsorted():
    d = base_dict()
    d["days"][0]["items"][1]["time"] = "23:00"
    assert "items_unsorted" in codes(run(d))


def test_nonpositive_and_overlong_duration():
    d = base_dict()
    d["days"][0]["items"][1]["duration_minutes"] = 0
    d["days"][1]["items"][0]["duration_minutes"] = 999
    found = codes(run(d))
    assert "nonpositive_duration" in found
    assert "overlong_item" in found


def test_missing_transport():
    d = base_dict()
    d["days"][0]["items"][2]["transport"] = ""
    assert "missing_transport" in codes(run(d))


def test_day_overpacked():
    d = base_dict()
    for it in d["days"][1]["items"]:
        it["duration_minutes"] = 200
    assert "day_overpacked" in codes(run(d))


def test_must_go_missing():
    d = base_dict()
    del d["days"][0]["items"][2]        # 删掉塞纳河游船
    d["booking_checklist"] = [b for b in d["booking_checklist"] if "游船" not in b["title"]]
    report = run(d)
    assert "must_go_missing" in codes(report)
    assert not report.ok


def test_avoid_hit():
    d = base_dict()
    d["days"][2]["items"][0]["title"] = "老佛爷百货购物两小时"
    assert "avoid_hit" in codes(run(d))


def test_booking_not_listed():
    d = base_dict()
    d["days"][2]["items"][1]["booking_required"] = True  # 橘园美术馆，清单里没有
    assert "booking_not_listed" in codes(run(d))


def test_before_arrival():
    d = base_dict()
    d["days"][0]["items"][1]["time"] = "07:30"          # 观光项（sight），非 logistics
    d["days"][0]["items"].sort(key=lambda i: i["time"])
    report = run(d)
    assert "before_arrival" in codes(report)
    assert not report.ok


def test_before_arrival_ignores_logistics_checkin():
    d = base_dict()
    d["days"][0]["items"][0]["time"] = "07:00"          # 「办理入住」是 logistics，排在抵达时刻正常
    assert "before_arrival" not in codes(run(d))


def test_repeated_attraction_flagged():
    d = base_dict()
    d["days"][2]["items"].insert(0, {
        "time": "08:30", "title": "卢浮宫（补看）", "type": "museum", "location": "1 区",
        "duration_minutes": 30, "transport": "地铁 1 号线", "booking_required": False, "notes": None,
    })
    assert "repeated_attraction" in codes(run(d))   # golden 第 2 天已有卢浮宫


def test_repeated_attraction_ignores_food_and_same_day():
    d = base_dict()   # golden 每天都有「午餐」类 food，且不跨天算重复
    assert "repeated_attraction" not in codes(run(d))


def test_closed_day_detected():
    d = base_dict()
    d["days"][3]["items"][1]["title"] = "卢浮宫二刷"   # 2026-10-06 是周二，卢浮宫闭馆
    assert "closed_day" in codes(run(d))


def test_long_term_closed_flagged():
    d = base_dict()
    d["days"][2]["items"][2]["title"] = "蓬皮杜中心"   # 白名单里标了 long_term_closed
    report = run(d)
    assert "long_term_closed" in codes(report)
    assert not report.ok   # 是 error 级别


def test_long_term_closed_not_flagged_when_absent():
    assert "long_term_closed" not in codes(run(base_dict()))


def test_profile_mismatch():
    d = base_dict()
    d["trip"]["traveler_profile"]["style"] = "intense"
    assert "profile_style" in codes(run(d))


def test_destination_mismatch():
    d = base_dict()
    d["trip"]["destination"] = "Berlin"
    assert "destination_mismatch" in codes(run(d))


def test_report_sorted_errors_first():
    d = base_dict()
    d["days"][0]["items"][1]["time"] = "aa:bb"      # error
    d["days"][2]["items"][0]["title"] = "购物商场"   # warning
    report = run(d)
    severities = [f.severity for f in report.findings]
    assert severities == sorted(severities, key=lambda s: 0 if s == Severity.error else 1)
