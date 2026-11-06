"""天气：预报 / 去年同期的选择、天气代码、雨天阈值、缓存、失败降级、室外点与室内备选（规格 M10 S1a–S1f）。

全部用假的 Open-Meteo 响应（httpx.MockTransport），测试里不联网。
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import weather as W
from app.api import create_app
from app.editing import alternatives
from app.library import Setting, load_library
from app.llm import LLMError
from app.schemas import Itinerary, TravelRequest
from app.versions import PlanStore
from app.weather import WeatherService, fetch_days, same_day_last_year, source_for, wmo

TODAY = date(2026, 10, 11)
LIB = load_library("paris")


class DeadLLM:
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        raise LLMError("模型没启动")


# --------------------------------------------------------------------------- #
# 假的 Open-Meteo
# --------------------------------------------------------------------------- #
def daily(dates: list[date], codes, tmax=15.0, tmin=8.0, prob=None, mm=0.0) -> dict:
    n = len(dates)
    body = {"time": [d.isoformat() for d in dates], "weather_code": codes if isinstance(codes, list) else [codes] * n,
            "temperature_2m_max": [tmax] * n, "temperature_2m_min": [tmin] * n,
            "precipitation_sum": [mm] * n}
    if prob is not None:
        body["precipitation_probability_max"] = prob if isinstance(prob, list) else [prob] * n
    return {"daily": body}


def span(start: date, end: date) -> list[date]:
    return [start + timedelta(days=k) for k in range((end - start).days + 1)]


class FakeMeteo:
    """记录请求；按网址返回预报 / 历史。"""

    def __init__(self, *, forecast_prob=10, forecast_code=3, fail: int | None = None, garbage=False):
        self.calls: list[httpx.Request] = []
        self.forecast_prob, self.forecast_code, self.fail, self.garbage = forecast_prob, forecast_code, fail, garbage

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail:
            return httpx.Response(self.fail, json={"error": True})
        if self.garbage:
            return httpx.Response(200, json={"hello": "world"})
        q = request.url.params
        ds = span(date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"]))
        if "archive" in request.url.host:
            return httpx.Response(200, json=daily(ds, 61, mm=3.9))
        return httpx.Response(200, json=daily(ds, self.forecast_code, prob=self.forecast_prob, mm=0.4))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_weather_codes_are_translated_and_unknown_ones_do_not_crash():
    assert wmo(0) == ("晴", "sun")
    assert wmo(61) == ("小雨", "rain")
    assert wmo(95)[1] == "storm"
    assert wmo(12345) == ("天气不明", "cloud")


def test_dates_within_15_days_use_the_forecast_and_further_ones_use_last_year():
    assert source_for(TODAY, TODAY) == "forecast"
    assert source_for(TODAY + timedelta(days=15), TODAY) == "forecast"
    assert source_for(TODAY + timedelta(days=16), TODAY) == "history"
    assert source_for(TODAY - timedelta(days=10), TODAY) == "forecast"          # 行程已经开始
    assert same_day_last_year(date(2026, 11, 20)) == date(2025, 11, 20)
    assert same_day_last_year(date(2028, 2, 29)) == date(2027, 2, 28)


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #
def test_forecast_days_carry_label_temperatures_and_rain_probability():
    meteo = FakeMeteo(forecast_prob=[10, 85], forecast_code=[3, 63])
    days = fetch_days(meteo.client(), 48.85, 2.35, [TODAY + timedelta(days=3), TODAY + timedelta(days=4)], TODAY)
    assert [d.source for d in days] == ["forecast", "forecast"]
    assert (days[0].label, days[1].label) == ("阴", "中雨")
    assert days[0].t_max == 15.0 and days[0].t_min == 8.0
    assert (days[0].rain_prob, days[1].rain_prob) == (10, 85)
    assert (days[0].rainy, days[1].rainy) == (False, True)
    assert len(meteo.calls) == 1                                               # 一段连续日期只请求一次


@pytest.mark.parametrize("prob,rainy", [(59, False), (60, True), (100, True), (0, False)])
def test_a_day_is_rainy_only_from_60_percent(prob, rainy):
    meteo = FakeMeteo(forecast_prob=prob)
    [d] = fetch_days(meteo.client(), 48.85, 2.35, [TODAY + timedelta(days=2)], TODAY)
    assert d.rainy is rainy


def test_far_away_dates_show_last_years_weather_without_probability_and_never_count_as_rainy():
    far = date(2026, 11, 20)
    meteo = FakeMeteo()
    [d] = fetch_days(meteo.client(), 48.85, 2.35, [far], TODAY)
    assert d.source == "history" and d.rain_prob is None and d.rainy is False
    assert d.label == "小雨" and d.rain_mm == 3.9                                  # 去年那天确实下雨了，但只作参考
    q = meteo.calls[0].url.params
    assert q["start_date"] == "2025-11-20" and "archive" in meteo.calls[0].url.host


def test_a_trip_spanning_the_horizon_mixes_forecast_and_history_in_date_order():
    ds = [TODAY + timedelta(days=k) for k in (14, 15, 16, 17)]
    days = fetch_days(FakeMeteo().client(), 48.85, 2.35, ds, TODAY)
    assert [d.date for d in days] == ds
    assert [d.source for d in days] == ["forecast", "forecast", "history", "history"]


def test_missing_values_in_the_response_are_tolerated():
    def handler(request):
        return httpx.Response(200, json={"daily": {"time": ["2026-10-12", "2026-10-13"], "weather_code": [1, None],
                                                   "temperature_2m_max": [None, 12],
                                                   "precipitation_probability_max": [None, 70]}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    days = fetch_days(client, 48.85, 2.35, [date(2026, 10, 12), date(2026, 10, 13)], TODAY)
    assert len(days) == 1                                                       # 没有天气代码的那天直接略过
    assert days[0].t_max is None and days[0].rain_prob is None and days[0].rainy is False


# --------------------------------------------------------------------------- #
# 服务：缓存和降级
# --------------------------------------------------------------------------- #
def test_results_are_cached_for_three_hours():
    meteo, now = FakeMeteo(), [1000.0]
    svc = WeatherService(meteo.client(), clock=lambda: now[0])
    ds = [TODAY + timedelta(days=2)]
    first = svc.get("Paris", ds, TODAY)
    svc.get("paris", ds, TODAY)
    assert len(meteo.calls) == 1 and first
    now[0] += W.CACHE_TTL_SECONDS - 1
    svc.get("Paris", ds, TODAY)
    assert len(meteo.calls) == 1
    now[0] += 2
    svc.get("Paris", ds, TODAY)
    assert len(meteo.calls) == 2


@pytest.mark.parametrize("meteo", [FakeMeteo(fail=500), FakeMeteo(fail=429), FakeMeteo(garbage=True)])
def test_an_unavailable_or_garbled_service_yields_nothing_instead_of_an_error(meteo):
    assert WeatherService(meteo.client()).get("Paris", [TODAY + timedelta(days=2)], TODAY) is None


def test_a_timeout_yields_nothing():
    def handler(request):
        raise httpx.ConnectTimeout("慢")
    svc = WeatherService(httpx.Client(transport=httpx.MockTransport(handler)))
    assert svc.get("Paris", [TODAY + timedelta(days=2)], TODAY) is None


def test_stale_cache_is_used_when_the_service_goes_down_later():
    state = {"down": False}
    meteo = FakeMeteo()

    def handler(request):
        return httpx.Response(503) if state["down"] else meteo(request)

    now = [0.0]
    svc = WeatherService(httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: now[0])
    ds = [TODAY + timedelta(days=2)]
    good = svc.get("Paris", ds, TODAY)
    state["down"], now[0] = True, W.CACHE_TTL_SECONDS + 5
    assert svc.get("Paris", ds, TODAY) == good


def test_a_destination_without_known_coordinates_gets_no_weather_and_no_request():
    meteo = FakeMeteo()
    assert WeatherService(meteo.client()).get("Atlantis", [TODAY], TODAY) is None
    assert meteo.calls == []


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
def make_client(tmp_path, meteo: FakeMeteo) -> TestClient:
    svc = WeatherService(meteo.client())
    return TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=DeadLLM, weather=svc,
                                 today=lambda: TODAY))


def new_plan(client, start="2026-10-14", end="2026-10-17", theme="photography") -> dict:
    r = client.post("/plans", json={"destination": "Paris", "start_date": start, "end_date": end,
                                    "budget_level": "medium", "travel_style": "balanced", "interests": [theme],
                                    "theme": theme, "arrival_time": f"{start} 10:30", "departure_time": f"{end} 18:00"})
    assert r.status_code == 201, r.text
    return r.json()


def test_the_weather_endpoint_lists_every_trip_day_and_flags_the_outdoor_places_of_rainy_days(tmp_path):
    client = make_client(tmp_path, FakeMeteo(forecast_prob=[10, 90, 10, 10], forecast_code=[3, 63, 3, 3]))
    plan = new_plan(client)
    r = client.get(f"/plans/{plan['plan_id']}/weather")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and "Open-Meteo" in body["note"]
    assert [d["date"] for d in body["days"]] == ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17"]
    rainy = [d for d in body["days"] if d["rainy"]]
    assert [d["date"] for d in rainy] == ["2026-10-15"]
    itinerary = plan["itinerary"]
    day2 = itinerary["days"][1]
    expected = [i["place_id"] for i in day2["items"] if i.get("place_id") in LIB.places
                and LIB.places[i["place_id"]].setting == Setting.outdoor]
    assert expected, "摄影主题第 2 天应该有室外点，否则这个测试没意义"
    assert [p["place_id"] for p in rainy[0]["outdoor_places"]] == expected
    for p in rainy[0]["outdoor_places"]:
        assert p["title"]


def test_outdoor_places_are_curated_not_guessed(tmp_path):
    client = make_client(tmp_path, FakeMeteo())
    plan = new_plan(client)
    body = client.get(f"/plans/{plan['plan_id']}/weather").json()
    for d in body["days"]:
        for p in d["outdoor_places"]:
            assert LIB.places[p["place_id"]].setting == Setting.outdoor
        for p in d["mixed_places"]:
            assert LIB.places[p["place_id"]].setting == Setting.mixed


def test_when_the_service_is_down_the_endpoint_still_answers_200_with_available_false(tmp_path):
    client = make_client(tmp_path, FakeMeteo(fail=500))
    plan = new_plan(client)
    r = client.get(f"/plans/{plan['plan_id']}/weather")
    assert r.status_code == 200 and r.json() == {"available": False, "days": []}


def test_the_weather_of_an_unknown_plan_is_404(tmp_path):
    client = make_client(tmp_path, FakeMeteo())
    assert client.get("/plans/nope/weather").status_code == 404


# --------------------------------------------------------------------------- #
# 换成室内备选
# --------------------------------------------------------------------------- #
def test_indoor_only_alternatives_contain_nothing_but_indoor_places(tmp_path):
    client = make_client(tmp_path, FakeMeteo())
    plan = new_plan(client)
    itinerary = Itinerary.model_validate(plan["itinerary"])
    request = TravelRequest.model_validate({
        "destination": "Paris", "start_date": "2026-10-14", "end_date": "2026-10-17", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["photography"], "arrival_time": "2026-10-14T10:30:00",
        "departure_time": "2026-10-17T18:00:00"})
    outdoors = [i.place_id for d in itinerary.days for i in d.items
                if i.place_id in LIB.places and LIB.places[i.place_id].setting == Setting.outdoor]
    assert outdoors, "摄影主题的行程里应该有室外点，否则这个测试没意义"
    got = alternatives(itinerary, request, outdoors[0], indoor_only=True, limit=10)
    assert got and all(LIB.places[a.place_id].setting == Setting.indoor for a in got)
    plain = alternatives(itinerary, request, outdoors[0], limit=10)
    assert len(plain) >= len(got)

    r = client.post(f"/plans/{plan['plan_id']}/alternatives",
                    json={"itinerary": plan["itinerary"], "place_id": outdoors[0], "indoor_only": True})
    assert r.status_code == 200
    assert all(LIB.places[a["place_id"]].setting == Setting.indoor for a in r.json())


def test_every_place_in_the_library_has_a_setting_and_the_counts_are_sane():
    settings = [p.setting for p in LIB.places.values()]
    assert len(settings) == len(LIB.places)
    assert settings.count(Setting.indoor) > 20 and settings.count(Setting.outdoor) > 15
    assert LIB.places["eiffel-tower"].setting == Setting.outdoor
    assert LIB.places["louvre"].setting == Setting.indoor
    assert LIB.places["versailles"].setting == Setting.mixed


# --------------------------------------------------------------------------- #
# 分享页和导出也带天气（M10 S1g / S1h）
# --------------------------------------------------------------------------- #
def test_the_share_endpoint_carries_the_weather_without_leaking_the_plan_id(tmp_path):
    client = make_client(tmp_path, FakeMeteo(forecast_prob=[10, 90, 10, 10], forecast_code=[3, 63, 3, 3]))
    plan = new_plan(client)
    token = client.post(f"/plans/{plan['plan_id']}/share").json()["token"]
    r = client.get(f"/share/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["weather"]["available"] is True
    assert [d["date"] for d in body["weather"]["days"]] == ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17"]
    assert [d["rainy"] for d in body["weather"]["days"]] == [False, True, False, False]
    assert plan["plan_id"] not in r.text                                       # 仍然不带行程号


def test_the_share_endpoint_still_works_when_the_weather_is_down(tmp_path):
    client = make_client(tmp_path, FakeMeteo(fail=500))
    plan = new_plan(client)
    token = client.post(f"/plans/{plan['plan_id']}/share").json()["token"]
    r = client.get(f"/share/{token}")
    assert r.status_code == 200 and r.json()["weather"] == {"available": False, "days": []}


def test_markdown_and_html_exports_have_a_weather_line_under_each_day(tmp_path):
    client = make_client(tmp_path, FakeMeteo(forecast_prob=[10, 90, 10, 10], forecast_code=[3, 63, 3, 3]))
    plan = new_plan(client)
    pid = plan["plan_id"]
    md = client.get(f"/plans/{pid}/itinerary.md").text
    assert md.count("天气：") == 4
    assert "天气：阴 · 15° / 8° · 降雨 10%" in md
    assert "天气：中雨 · 15° / 8° · 降雨 90%。预报降雨概率 90%，室外点：" in md and "建议备选室内" in md
    html = client.get(f"/plans/{pid}/itinerary.html").text
    assert html.count('class="weather') == 4 and 'class="weather rainy"' in html
    assert html.count("天气：") == 4


def test_exports_say_last_year_for_far_dates_and_stay_silent_when_the_weather_is_unavailable(tmp_path):
    client = make_client(tmp_path, FakeMeteo())
    plan = new_plan(client, start="2026-11-20", end="2026-11-22")
    md = client.get(f"/plans/{plan['plan_id']}/itinerary.md").text
    assert md.count("去年同期，仅作参考") == 3 and "预报降雨概率" not in md

    down = make_client(tmp_path / "down", FakeMeteo(fail=500))
    plan2 = new_plan(down)
    for ext in ("md", "html"):
        r = down.get(f"/plans/{plan2['plan_id']}/itinerary.{ext}")
        assert r.status_code == 200 and "天气：" not in r.text                   # 取不到就整行省略，导出照常


def test_the_pdf_export_still_succeeds_with_weather_lines(tmp_path):
    client = make_client(tmp_path, FakeMeteo(forecast_prob=90, forecast_code=63))
    plan = new_plan(client)
    r = client.get(f"/plans/{plan['plan_id']}/itinerary.pdf")
    if r.status_code == 503:
        pytest.skip("本机没有可用的 PDF 浏览器")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
