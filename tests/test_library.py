from datetime import date

import pytest
from pydantic import ValidationError

from app.library import (
    Category,
    Place,
    PlaceLibrary,
    Role,
    ThemeMember,
    ThemePack,
    ThemeRecipe,
    Ticketing,
    load_library,
)
from app.planner import attraction_catalog

TUESDAY = date(2026, 10, 6)
MONDAY = date(2026, 10, 5)


@pytest.fixture(scope="module")
def lib() -> PlaceLibrary:
    return load_library("paris")


def _place(**over) -> Place:
    base = dict(id="x-place", name_zh="测试点", name_en="Test", category="sight", setting="outdoor", area="1区",
                lat=48.86, lng=2.34, duration_min=60)
    base.update(over)
    return Place.model_validate(base)


def _recipe() -> ThemeRecipe:
    return ThemeRecipe(anchors_per_day={"relaxed": 1, "balanced": 2, "intense": 3})


# --------------------------------------------------------------------------- #
# 真实数据
# --------------------------------------------------------------------------- #
def test_library_size_and_themes(lib):
    assert len(lib.places) >= 80
    assert set(lib.themes) == {
        "art", "fashion", "shopping", "museums", "entertainment", "family", "photography", "food",
    }


def test_every_theme_has_enough_anchors(lib):
    """v0.1 要求：每个主题至少 6 个 anchor，否则排不满几天。"""
    for theme_id in lib.themes:
        assert len(lib.theme_members(theme_id, Role.anchor)) >= 6, theme_id


def test_legacy_attractions_all_migrated_without_loss(lib):
    """v1 的 33 个景点必须都在新库里，且官网链接、闭馆日、预约标记没丢。"""
    by_name = {p.name_zh: p for p in lib.places.values()}
    closed_map = {"周一": 0, "周二": 1}
    for old in attraction_catalog():
        new = by_name[old["name_zh"]]
        assert new.official_url == old.get("url"), old["name_zh"]
        if old["booking"]:          # 旧的「需预约」不能丢：新库里至少是有线上购票渠道的
            assert new.ticketing != Ticketing.none, old["name_zh"]
        assert new.long_term_closed == bool(old.get("long_term_closed")), old["name_zh"]
        if old["closed"] in closed_map:
            assert new.closed_weekdays == [closed_map[old["closed"]]], old["name_zh"]


# 自动核对打不开、搜索也无法确认域名的官网：链接置空（页面显示「请自行搜索官网」），待之后核实。
LINK_PENDING: set[str] = set()


def test_places_with_a_ticket_channel_carry_an_official_link(lib):
    """有购票渠道的地点必须带官网链接，否则游客没处买票；核对不了的在 LINK_PENDING 里登记。"""
    missing = {p.id for p in lib.places.values() if p.ticketing != Ticketing.none and not p.official_url}
    assert missing == LINK_PENDING


def test_long_term_closed_never_an_anchor(lib):
    assert lib.get_place("pompidou").long_term_closed
    for theme_id in lib.themes:
        anchors = {p.id for p, _ in lib.theme_members(theme_id, Role.anchor)}
        assert "pompidou" not in anchors


def test_day_trips_are_full_day(lib):
    for pid in ("versailles", "disneyland-paris", "vallee-village"):
        assert lib.get_place(pid).full_day, pid
    assert not lib.get_place("palais-royal").full_day


def test_verified_flags_carry_a_date(lib):
    """标了「已核对」的地点必须有核对日期；没标的就是策展录入、待核实。"""
    for p in lib.places.values():
        if p.hours_verified or p.coords_verified or p.official_url:
            assert p.verified_at is not None, p.id
    assert any(not p.coords_verified for p in lib.places.values())      # 街区 / 步行街这类没法按点核对
    assert sum(p.coords_verified for p in lib.places.values()) >= 60


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def test_closed_days(lib):
    louvre = lib.get_place("louvre")
    assert lib.is_closed_on(louvre, TUESDAY)
    assert not lib.is_closed_on(louvre, MONDAY)
    assert lib.is_closed_on(lib.get_place("orsay"), MONDAY)
    # 长期闭馆不看星期几
    assert lib.is_closed_on(lib.get_place("pompidou"), date(2026, 10, 7))


def test_themes_of(lib):
    assert {"art", "museums"} <= set(lib.themes_of("louvre"))
    assert "family" in lib.themes_of("disneyland-paris")


def test_theme_members_sorted_by_weight_then_name(lib):
    pairs = lib.theme_members("art", Role.anchor)
    weights = [m.weight for _, m in pairs]
    assert weights == sorted(weights, reverse=True)
    assert lib.theme_members("art") == lib.theme_members("art")   # 结果稳定


def test_find_prefers_longest_name(lib):
    assert lib.find("老佛爷百货购物").id == "galeries-lafayette-shopping"
    assert lib.find("老佛爷天台观景").id == "lafayette-rooftop"
    assert lib.find("Eiffel Tower").id == "eiffel-tower"
    assert lib.find("卢浮宫（需预约）").id == "louvre"
    assert lib.find("a") is None
    assert lib.find("一个不存在的地方 xyz") is None


def test_unknown_ids_raise_helpful_errors(lib):
    with pytest.raises(KeyError, match="没有这个主题"):
        lib.get_theme("nope")
    with pytest.raises(KeyError, match="地点库里没有"):
        lib.get_place("nope")


# --------------------------------------------------------------------------- #
# 模型校验
# --------------------------------------------------------------------------- #
def test_place_rejects_bad_weekdays_and_urls():
    with pytest.raises(ValidationError, match="0–6"):
        _place(closed_weekdays=[7])
    with pytest.raises(ValidationError, match="重复"):
        _place(closed_weekdays=[1, 1])
    with pytest.raises(ValidationError, match="https://"):
        _place(official_url="http://insecure.example")


def test_place_zone_must_match_coordinates():
    with pytest.raises(ValidationError, match="day_trip"):
        _place(lat=48.80, lng=2.12)                       # 凡尔赛坐标却标 paris
    with pytest.raises(ValidationError, match="市区内"):
        _place(zone="day_trip")                           # 市区坐标却标 day_trip
    assert _place(lat=48.80, lng=2.12, zone="day_trip").full_day


def test_every_place_declares_whether_it_is_indoor_outdoor_or_mixed():
    with pytest.raises(ValidationError, match="setting"):
        Place.model_validate(dict(id="x", name_zh="测试", name_en="T", category="sight", area="1区",
                                  lat=48.86, lng=2.34, duration_min=60))        # 不写就报错：必须逐个策展
    with pytest.raises(ValidationError):
        _place(setting="underwater")


def test_place_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        _place(themes=["art"])    # 主题只存在 ThemePack 里，Place 不能自己带


def test_recipe_requires_all_three_styles():
    with pytest.raises(ValidationError, match="relaxed"):
        ThemeRecipe(anchors_per_day={"relaxed": 1, "balanced": 2})


def test_library_rejects_dangling_member():
    theme = ThemePack(id="t", name_zh="t", tagline="t", description="t", recipe=_recipe(),
                      members=[ThemeMember(place_id="ghost", role=Role.anchor, weight=3)])
    with pytest.raises(ValidationError, match="不存在的地点"):
        PlaceLibrary(city="x", places={"x-place": _place()}, themes={"t": theme})


def test_library_rejects_long_term_closed_anchor():
    closed = _place(long_term_closed=True)
    theme = ThemePack(id="t", name_zh="t", tagline="t", description="t", recipe=_recipe(),
                      members=[ThemeMember(place_id="x-place", role=Role.anchor, weight=3)])
    with pytest.raises(ValidationError, match="长期闭馆"):
        PlaceLibrary(city="x", places={"x-place": closed}, themes={"t": theme})


def test_categories_are_the_documented_set(lib):
    assert {p.category for p in lib.places.values()} <= set(Category)


# --------------------------------------------------------------------------- #
# 官网链接的核对记录（规格 M02 S? / F10：页面要写「已于 X 日自动核对」）
# --------------------------------------------------------------------------- #
def test_official_url_requires_check_record():
    with pytest.raises(ValidationError, match="核对"):
        _place(official_url="https://example.com/")
    with pytest.raises(ValidationError, match="核对"):
        _place(official_url="https://example.com/", url_status="ok")           # 缺核对日期
    ok = _place(official_url="https://example.com/", url_status="ok", verified_at="2026-10-08")
    assert ok.verified_at == date(2026, 10, 8)


def test_every_official_url_in_library_has_check_record(lib):
    missing = [p.id for p in lib.places.values() if p.official_url and not (p.url_status and p.verified_at)]
    assert missing == []


def test_public_url_only_for_places_with_a_ticket_channel(lib):
    """规格 M02 S2b / M06：有线上购票渠道（必须线上，或线上+现场）才对外给官网链接，无需购票的只给地址。"""
    for p in lib.places.values():
        assert p.public_url == (p.official_url if p.ticketing != Ticketing.none else None), p.id
    assert lib.get_place("louvre").public_url.startswith("https://")
    assert lib.get_place("rodin").public_url.startswith("https://")          # 线上 + 现场，也给链接
    assert lib.get_place("tuileries-garden").public_url is None
    no_ticket = _place(official_url="https://example.com/", url_status="ok", verified_at="2026-10-08")
    assert no_ticket.ticketing == Ticketing.none and no_ticket.public_url is None


def test_must_book_online_is_exactly_the_online_required_tier(lib):
    for p in lib.places.values():
        assert p.booking.required == (p.ticketing == Ticketing.online_required), p.id
    with pytest.raises(ValidationError, match="必须线上"):
        _place(booking={"required": True}, ticketing="online_or_walkin")


# 2026-10-09 逐个查过的地点（资料多为第三方，官网为准）
RESEARCHED_MUST = {"louvre", "orsay", "palais-garnier", "disneyland-paris", "moulin-rouge"}
RESEARCHED_EITHER = {"eiffel-tower", "sainte-chapelle", "versailles", "rodin", "fondation-louis-vuitton",
                     "orangerie", "picasso", "pantheon", "arc-de-triomphe"}
NO_TICKET = {"notre-dame", "tuileries-garden", "montmartre-sacre-coeur", "champs-elysees", "citypharma",
             "cafe-de-flore", "printemps", "galeries-lafayette-shopping", "vallee-village", "pere-lachaise"}


def test_ticketing_tiers_for_researched_places(lib):
    for pid in RESEARCHED_MUST:
        assert lib.get_place(pid).ticketing == Ticketing.online_required, pid
    for pid in RESEARCHED_EITHER:
        assert lib.get_place(pid).ticketing == Ticketing.online_or_walkin, pid
    for pid in NO_TICKET:
        assert lib.get_place(pid).ticketing == Ticketing.none, pid
    for pid in RESEARCHED_MUST | RESEARCHED_EITHER:
        p = lib.get_place(pid)
        assert p.ticket_note, pid                       # 带一句经验 / 规则说明
        assert p.ticketing_checked_at == date(2026, 10, 9), pid


def test_unsure_cases_lean_to_must_book_online(lib):
    """拿不准时按「必须线上」：之前标过需预约、这次没查的，不能降级成「现场也可」。"""
    for pid in ("lido", "crazy-horse", "seine-cruise", "ysl-museum", "galerie-dior", "philharmonie"):
        assert lib.get_place(pid).ticketing == Ticketing.online_required, pid


def test_every_place_has_a_street_address(lib):
    """链接不给的地点只剩地址可用，所以每个地点都必须有，且带邮编。"""
    import re
    missing = [p.id for p in lib.places.values() if not p.address or not re.search(r"\b\d{5}\b", p.address)]
    assert missing == []


def test_retail_shop_uses_address_instead_of_website(lib):
    """Citypharma 是零售药妆店，线上商城链接对游客没用：不放官网，给门店地址。"""
    p = lib.get_place("citypharma")
    assert p.official_url is None
    assert p.address == "26 rue du Four, 75006 Paris"
    assert _place().address is None          # 地址是选填


def test_known_bad_urls_were_fixed(lib):
    assert lib.get_place("versailles").official_url == "https://www.chateauversailles.fr/"
    assert lib.get_place("angelina").official_url == "https://www.angelina-paris.fr/"
    assert lib.get_place("lido").official_url == "https://theatredulido.com/en/"
    # 本机连不上、搜索也没法确认域名：置空，不放未核实的链接
    assert lib.get_place("citypharma").official_url is None
