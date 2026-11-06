"""意大利圣诞实测（规格 12，场景 I1–I6）：罗马 · 佛罗伦萨 · 米兰 · 那不勒斯，12 月 20–26 日。

用的是 `scripts/rebuild_generated_city.py` 建在 data/generated/ 里的城市（不进仓库）：没建好就跳过。
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import pytest

from app import multicity
from app.cities import reload_cities
from app.library import load_library
from app.trips import TripSpec

CITIES = ("rome", "florence", "milan", "naples")
REAL = Path(__file__).resolve().parents[1] / "data" / "generated"        # 你机器上现建的数据；conftest 默认把它隔离了


def _built() -> bool:
    return all((REAL / "places" / f"{c}.json").exists() for c in CITIES)


pytestmark = pytest.mark.skipif(not _built(), reason="意大利四座城市还没建（scripts/rebuild_generated_city.py）")


@pytest.fixture(autouse=True)
def _real_cities(monkeypatch):
    monkeypatch.setenv("CITY_GENERATED_DIR", str(REAL))
    reload_cities()
    yield
    reload_cities()


def spec(legs: list[tuple[str, str, str]], **common) -> TripSpec:
    return TripSpec.model_validate({
        "legs": [{"destination": d, "start_date": a, "end_date": b} for d, a, b in legs],
        "outbound": {"mode": "flight", "depart_at": f"{legs[0][1]}T06:00", "arrive_at": f"{legs[0][1]}T10:00"},
        "inbound": {"mode": "flight", "depart_at": f"{legs[-1][2]}T20:00"},
        "transfers": [{"mode": "train"} for _ in legs[1:]],
        "common": {"budget_level": "medium", "travel_style": "balanced", "interests": ["均衡"], **common}})


def build(trip: TripSpec):
    parent, _ = multicity.build_request(trip)
    return parent, multicity.generate(parent, client=None)


@lru_cache(maxsize=1)
def _rfm():
    return build(spec([("罗马", "2026-12-20", "2026-12-22"), ("佛罗伦萨", "2026-12-22", "2026-12-24"),
                       ("米兰", "2026-12-24", "2026-12-26")]))


@pytest.fixture
def rome_florence_milan():
    return _rfm()


def libs():
    return {c: load_library(c) for c in CITIES}


def test_i1_nothing_is_scheduled_on_a_day_the_place_is_closed(rome_florence_milan):
    _parent, it = rome_florence_milan
    lib = libs()
    placed = 0
    for day in it.days:
        for item in day.items:
            if item.place_id:
                placed += 1
                p = lib[item.city_id].get_place(item.place_id)
                assert p.closed_reason(day.date) is None, f"{day.date} 排了闭馆的 {p.name_zh}"
    assert placed >= 10


def test_i1_the_officially_closed_dates_are_in_the_data():
    lib = libs()
    by_name = {p.name_zh: p for c in lib.values() for p in c.places.values()}
    for name, city in (("梵蒂冈博物馆", "rome"), ("乌菲兹美术馆", "florence"), ("彼提宫", "florence"),
                       ("布雷拉画廊", "milan")):
        p = by_name[name]
        assert p.closed_reason(date(2026, 12, 25)) is not None, f"{city} 的 {name} 12 月 25 日应该是闭馆"
    assert by_name["梵蒂冈博物馆"].closed_reason(date(2026, 12, 26)) is not None
    assert by_name["乌菲兹美术馆"].closed_reason(date(2026, 12, 21)) is not None            # 周一


def test_i2_the_last_supper_must_be_booked_online_with_a_source():
    lib = libs()["milan"]
    p = next(p for p in lib.places.values() if "最后的晚餐" in p.name_zh)
    assert p.ticketing.value == "online_required" and p.booking.required
    assert p.public_url and p.public_url.startswith("https://")
    assert p.ticket_note and "官网" in p.ticket_note


def test_i5_the_booking_list_has_what_the_official_site_says_to_book():
    lib = libs()
    must = {"milan": "最后的晚餐", "naples": "圣塞维诺"}
    for city, word in must.items():
        trip = spec([("罗马", "2026-12-20", "2026-12-21"), ("米兰" if city == "milan" else "那不勒斯",
                                                              "2026-12-21", "2026-12-24")],
                    must_go=[next(p.name_zh for p in lib[city].places.values() if word in p.name_zh)])
        _parent, it = build(trip)
        assert any(word in b.title and b.must_online and b.url for b in it.booking_checklist), city


def test_i3_a_train_day_talks_about_the_station_not_the_airport(rome_florence_milan):
    _parent, it = rome_florence_milan
    for day in it.days:
        if day.date.isoformat() not in ("2026-12-22", "2026-12-24"):
            continue
        for item in day.items:
            if item.type.value == "logistics":
                text = item.transport + item.notes + item.title
                assert "机场" not in text and "航班" not in text and "RER" not in text, (day.date, text)


def test_i4_florence_never_says_metro(rome_florence_milan):
    _parent, it = rome_florence_milan
    florence = [i for d in it.days for i in d.items if i.city_id == "florence" and i.type.value != "logistics"]
    assert florence and all("地铁" not in i.transport for i in florence)


def test_i6_a_place_the_official_site_does_not_cover_is_not_called_open_or_closed():
    lib = libs()["rome"]
    colosseum = next(p for p in lib.places.values() if "斗兽场" in p.name_zh)
    assert not colosseum.closed_dates and not colosseum.hours_verified
    assert colosseum.closed_reason(date(2026, 12, 25)) is None


# ---- 12 月 21 日–1 月 4 日其余热门城市（威尼斯 / 比萨 / 锡耶纳 / 博洛尼亚 / 都灵 / 维罗纳） ----
MORE = ("venice", "pisa", "siena", "bologna", "turin", "verona")
ALL_ITALY = (*CITIES, *MORE)


def _more_built() -> bool:
    return all((REAL / "places" / f"{c}.json").exists() for c in MORE)


more = pytest.mark.skipif(not _more_built(), reason="威尼斯等六座城市还没建（scripts/rebuild_generated_city.py）")


def _named(city: str, word: str):
    lib = load_library(city)
    return next(p for p in lib.places.values() if word in p.name_zh)


@more
def test_i7_the_weekly_and_christmas_closures_from_the_official_sites_are_in_the_data():
    assert _named("turin", "夫人宫").closed_reason(date(2026, 12, 22)) is not None            # 周二
    assert _named("turin", "夫人宫").closed_reason(date(2026, 12, 25)) is not None
    assert _named("turin", "萨包达").closed_reason(date(2026, 12, 23)) is not None            # 周三（王室博物馆群）
    assert _named("turin", "维纳利亚").closed_reason(date(2026, 12, 21)) is not None          # 周一
    assert _named("venice", "古根汉").closed_reason(date(2026, 12, 22)) is not None
    assert _named("venice", "古根汉").closed_reason(date(2026, 12, 25)) is not None
    assert _named("venice", "雷佐尼科").closed_reason(date(2026, 12, 29)) is not None
    assert _named("venice", "佩萨罗").closed_reason(date(2026, 12, 28)) is not None
    assert _named("verona", "老城堡博物馆").closed_reason(date(2026, 12, 28)) is not None
    assert _named("verona", "朗贝尔蒂").closed_reason(date(2026, 12, 25)) is not None
    assert _named("bologna", "阿奇吉纳西欧").closed_reason(date(2026, 12, 27)) is not None


@more
def test_i8_only_an_official_christmas_date_counts_as_verified_hours():
    assert _named("turin", "夫人宫").hours_verified                          # 官网列了 12 月 24 / 25 / 31 日
    assert not _named("verona", "老城堡博物馆").hours_verified               # 只有每周闭馆日，圣诞怎么开没核对
    assert not _named("pisa", "斜塔").hours_verified


@more
def test_i9_every_place_has_a_chinese_name_and_no_place_is_listed_twice():
    for city in ALL_ITALY:
        names = [p.name_zh for p in load_library(city).places.values()]
        ascii_only = [n for n in names if not any("一" <= ch <= "鿿" for ch in n)]
        assert not ascii_only, (city, ascii_only)
        assert len(set(names)) == len(names), city
    turin = [p.name_zh for p in load_library("turin").places.values()]
    assert "国家电影博物馆" not in turin                                      # 就在安托内利尖塔里
    verona = [p.name_zh for p in load_library("verona").places.values()]
    assert "老城堡" not in verona and "老城堡博物馆" in verona


@more
def test_i10_the_famous_ones_are_there():
    must = {"venice": ["总督宫", "穆拉诺", "布拉诺", "圣洛克"], "pisa": ["比萨斜塔", "比萨主教座堂"],
            "siena": ["田野广场", "曼吉亚"], "bologna": ["双塔", "圣白托略"],
            "turin": ["埃及博物馆", "安托内利"], "verona": ["圆形竞技场", "朱丽叶"]}
    for city, words in must.items():
        names = " ".join(p.name_zh for p in load_library(city).places.values())
        for w in words:
            assert w in names, f"{city} 缺 {w}"
