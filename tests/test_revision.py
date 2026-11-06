import copy
import json
from pathlib import Path

from app.revision import (
    auto_repair,
    diff_itineraries,
    findings_to_instructions,
    revise_itinerary,
)
from app.schemas import Itinerary, TravelRequest
from app.validator import Finding, Severity, validate_itinerary

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "fixtures" / "stub-itinerary.json"
PERSONA = ROOT / "data" / "personas" / "persona-01-museum-foodie.json"


def base_dict() -> dict:
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))


def request_() -> TravelRequest:
    raw = json.loads(PERSONA.read_text(encoding="utf-8"))
    raw.pop("_meta", None)
    return TravelRequest.model_validate(raw)


class FakeClient:
    """按顺序返回给定的 JSON 字符串，用完后一直返回最后一个。"""

    def __init__(self, *payloads: str):
        self._payloads = list(payloads)
        self.calls = 0

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return payload


# --------------------------------------------------------------------------- #
def test_diff_detects_added_item():
    old = Itinerary.model_validate(base_dict())
    d = base_dict()
    d["days"][0]["items"].append({
        "time": "17:30", "title": "街角咖啡馆", "type": "food",
        "location": "9 区", "duration_minutes": 45, "booking_required": False,
    })
    new = Itinerary.model_validate(d)
    change = diff_itineraries(old, new)
    assert change.changed_days == ["2026-10-03"]
    assert any("街角咖啡馆" in a for a in change.added)
    assert change.removed == []


def test_diff_no_change():
    it = Itinerary.model_validate(base_dict())
    assert diff_itineraries(it, it).as_text() == "无结构性改动"


def test_findings_to_instructions_lists_messages():
    text = findings_to_instructions([
        Finding(code="x", severity=Severity.error, message="必去项「Trocadero」没有出现", day=None),
        Finding(code="y", severity=Severity.error, message="离开日结束太晚", day="2026-10-06"),
    ])
    assert "Trocadero" in text and "2026-10-06" in text


def test_revise_itinerary_applies_and_diffs():
    d = base_dict()
    d["days"][2]["items"].insert(1, {
        "time": "10:30", "title": "玛黑区咖啡馆", "type": "food",
        "location": "4 区", "duration_minutes": 40, "booking_required": False,
    })
    client = FakeClient(json.dumps(d, ensure_ascii=False))
    res = revise_itinerary(Itinerary.model_validate(base_dict()), request_(),
                           instructions="第 3 天上午加一个咖啡馆", client=client)
    assert res.itinerary.version == "v1"        # 版本号由 store 负责改
    assert "2026-10-05" in res.change.changed_days
    assert any("咖啡馆" in a for a in res.change.added)


def test_auto_repair_stops_when_clean():
    broken = base_dict()
    del broken["days"][0]["items"][2]          # 删掉塞纳河游船 -> must_go_missing
    broken["booking_checklist"] = [b for b in broken["booking_checklist"] if "游船" not in b["title"]]
    req = request_()
    assert not validate_itinerary(Itinerary.model_validate(broken), req).ok

    fixed_json = json.dumps(base_dict(), ensure_ascii=False)   # 修复版 = 完整 golden
    result = auto_repair(Itinerary.model_validate(broken), req,
                         client=FakeClient(fixed_json), max_rounds=2)
    assert result.ok
    assert len(result.rounds) == 1
    assert "must_go_missing" in result.rounds[0].fixed_codes


def test_auto_repair_gives_up_after_max_rounds():
    broken = base_dict()
    broken["trip"]["destination"] = "Berlin"   # destination_mismatch，改不掉
    req = request_()
    still_broken = json.dumps(broken, ensure_ascii=False)
    result = auto_repair(Itinerary.model_validate(broken), req,
                         client=FakeClient(still_broken), max_rounds=2)
    assert not result.ok
    assert len(result.rounds) == 2


# --------------------------------------------------------------------------- #
# 版本历史的「改动」要写清楚改了哪些地点
# --------------------------------------------------------------------------- #
def two_day_dict():
    d = base_dict()
    day2 = {**d["days"][0], "date": "2026-10-04", "items": [
        {"time": "10:00", "title": "奥赛博物馆", "type": "attraction", "location": "7 区",
         "duration_minutes": 120, "booking_required": False, "place_id": "orsay"}]}
    d["days"][0]["items"] = [
        {"time": "10:00", "title": "卢浮宫", "type": "attraction", "location": "1 区",
         "duration_minutes": 180, "booking_required": False, "place_id": "louvre"},
        {"time": "14:00", "title": "杜乐丽花园", "type": "attraction", "location": "1 区",
         "duration_minutes": 60, "booking_required": False, "place_id": "tuileries"}]
    d["days"] = [d["days"][0], day2]
    d["trip"]["date_range"]["end"] = "2026-10-04"
    return d


def changed(fn):
    d = two_day_dict()
    old = Itinerary.model_validate(d)
    fn(d)
    return diff_itineraries(old, Itinerary.model_validate(d)).as_text()


def test_summary_names_the_added_and_removed_places_by_day():
    def edit(d):
        d["days"][0]["items"].pop(1)
        d["days"][1]["items"].append({"time": "14:00", "title": "蓬皮杜中心", "type": "attraction",
                                      "location": "4 区", "duration_minutes": 120,
                                      "booking_required": False, "place_id": "pompidou"})
    text = changed(edit)
    assert "天有改动" in text
    assert "第 1 天：移除 杜乐丽花园" in text and "第 2 天：新增 蓬皮杜中心" in text


def test_summary_says_a_place_moved_instead_of_a_remove_and_an_add():
    def edit(d):
        moved = d["days"][0]["items"].pop(1)
        d["days"][1]["items"].append({**moved, "time": "14:00"})
    text = changed(edit)
    assert "杜乐丽花园 第 1 天 → 第 2 天" in text
    assert "新增" not in text and "移除" not in text


def test_summary_names_places_whose_time_changed_and_locks():
    def edit(d):
        d["days"][0]["items"][0]["time"] = "09:00"
        d["days"][0]["items"][1]["locked"] = True
    text = changed(edit)
    assert "卢浮宫 10:00 → 09:00" in text and "锁定 杜乐丽花园" in text
