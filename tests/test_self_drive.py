"""自驾（规格 M06 S6 / 12）：选了自驾，城市里点到点的通勤按开车算；填了出发地，给一个大致的路程估算。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.generator import generate_from_theme
from app.scheduler.geo import drive_estimate, travel
from app.schemas import TransportLeg, TravelRequest
from app.validator import validate_itinerary

SAT = date(2026, 10, 3)
A, B = (48.8606, 2.3376), (48.8738, 2.2950)                 # 卢浮宫 → 凯旋门，约 3 公里


def test_a_car_commute_is_driving_not_transit_and_short_hops_still_walk():
    minutes, mode = travel(A, B, car=True)
    assert mode == "自驾" and 0 < minutes < 40
    assert travel(A, (48.8611, 2.3380), car=True)[1] == "步行"          # 几十米还是走过去
    assert travel(A, B)[1] == "地铁"                                   # 没选自驾：和以前一样


def test_a_long_drive_estimate_from_the_straight_line_distance():
    km, minutes = drive_estimate(600)                                  # 比如上海宝山 → 泉州
    assert 700 <= km <= 800                                            # 实际路程比直线长，约 1.25 倍
    assert 9 * 60 <= minutes <= 12 * 60                                # 平均时速 70–80 公里 + 途中休息
    assert drive_estimate(0) == (0, 0)


def _req(mode="drive", out_km=None, back_km=None, **kw) -> TravelRequest:
    day0 = datetime.combine(SAT, datetime.min.time())
    out = TransportLeg(mode=mode, depart_at=day0 + timedelta(hours=6), arrive_at=day0 + timedelta(hours=12),
                       from_place="上海宝山", drive_km=out_km, drive_min=int(out_km * 1.2) if out_km else None)
    back = TransportLeg(mode=mode, depart_at=day0 + timedelta(days=3, hours=15), to_place="上海宝山",
                        drive_km=back_km, drive_min=int(back_km * 1.2) if back_km else None)
    return TravelRequest(destination="Paris", start_date=SAT, end_date=SAT + timedelta(days=3), budget_level="medium",
                         travel_style="balanced", interests=["art"], transport={"outbound": out, "inbound": back}, **kw)


def test_self_drive_trips_talk_about_driving_everywhere():
    trip = generate_from_theme(_req(), "art", client=None)
    items = [i for d in trip.itinerary.days for i in d.items]
    place_moves = [i.transport for i in items if i.place_id and "分钟" in i.transport]
    assert place_moves and all("自驾" in t or "步行" in t for t in place_moves), place_moves
    for i in items:
        assert "公共交通" not in i.transport and "地铁" not in i.transport, (i.title, i.transport)
        if i.type.value == "logistics":
            assert "自驾" in i.transport, i.transport
    assert validate_itinerary(trip.itinerary, _req()).ok


def test_without_self_drive_nothing_changes():
    trip = generate_from_theme(_req(mode="flight"), "art", client=None)
    assert not any("自驾" in i.transport for d in trip.itinerary.days for i in d.items)


def test_the_estimated_drive_from_the_origin_is_written_on_the_arrival_and_leaving_items():
    trip = generate_from_theme(_req(out_km=780, back_km=780), "art", client=None)
    first, last = trip.itinerary.days[0].items[0], trip.itinerary.days[-1].items[-1]
    assert "上海宝山" in first.notes and "780 公里" in first.notes and "估算" in first.notes
    assert "上海宝山" in last.notes and "780 公里" in last.notes


# ---- 接口：写了出发地就补上估算 ----
def test_the_api_adds_the_drive_estimate_when_the_origin_can_be_located(tmp_path):
    import time

    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.geocode import GeoHit
    from app.versions import PlanStore
    from tests.test_tasks import dead_client

    asked: list[str] = []

    def geo(text: str):
        asked.append(text)
        return GeoHit(31.40, 121.49, text) if "宝山" in text else None        # 上海宝山；其余定位不到

    client = TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=dead_client, geocoder=geo))
    body = {"destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
            "travel_style": "balanced", "interests": ["art"], "theme": "art",
            "transport": {"outbound": {"mode": "drive", "depart_at": "2026-10-03T06:00",
                                       "arrive_at": "2026-10-03T12:00", "from_place": "上海宝山"},
                          "inbound": {"mode": "drive", "depart_at": "2026-10-06T15:00", "to_place": "不存在的地方"}}}
    tid = client.post("/tasks", json=body).json()["task_id"]
    for _ in range(400):
        t = client.get(f"/tasks/{tid}").json()
        if t["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert t["status"] == "succeeded", t
    plan = client.get(f"/plans/{t['plan_id']}").json()
    days = plan["itinerary"]["days"]
    first = next(i for i in days[0]["items"] if i["type"] == "logistics")
    last = next(i for i in reversed(days[-1]["items"]) if i["type"] == "logistics")
    assert "公里" in first["notes"] and "上海宝山" in first["notes"]               # 上海到巴黎，直线约 9000 公里
    assert "公里" not in last["notes"]                                            # 定位不到：不估，也不报错
    assert asked == ["上海宝山", "不存在的地方"]
