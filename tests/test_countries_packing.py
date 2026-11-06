"""热门国家的随行数据（build_countries.py → countries.json）和按国家 / 国内 / 学生定制的行李清单。"""

from __future__ import annotations

import pytest

from app.cities import City, load_city
from app.companion import (
    ISIC_URL,
    generic_packing,
    load_countries,
    load_packing,
    plug_text,
    student_items,
    suggested_for,
)
from app.schemas import Traveler


def city(country: str, **over) -> City:
    base = load_city("paris").model_dump()
    return City.model_validate({**base, "id": "somewhere", "country": country, "currency": "JPY", **over})


def keys(data) -> dict:
    return {i.key: i for i in data.items}


def test_the_country_data_is_complete_and_every_link_is_https():
    data = load_countries()
    assert {"JP", "KR", "TH", "SG", "US", "GB", "DE", "IT", "ES", "FR", "AU"} <= set(data.countries)
    assert data.china["plug_types"] == ["A", "I"] and data.china["voltage_v"] == 220
    for iso, c in data.countries.items():
        assert c.plug_types and c.voltage_v in (100, 120, 220, 230), iso
        assert (c.visa_url is None) == (c.visa_name is None) == (c.visa_url_status is None), iso
        assert c.visa_url is None or c.visa_url.startswith("https://"), iso
        assert not c.schengen or c.europe, iso


@pytest.mark.parametrize("iso, has, voltage", [("JP", "有和国内相同的 A 型", "电压比国内（220 V）低"),
                                                ("AU", "有和国内相同的 I 型", "电压和国内一样"),
                                                ("DE", "和国内常见的 A / I 型不同，要带转换插头", "电压和国内一样")])
def test_the_plug_advice_compares_the_local_socket_and_voltage_with_china(iso, has, voltage):
    text = plug_text(load_countries().countries[iso])
    assert has in text and voltage in text


def test_a_trip_abroad_gets_that_countrys_plug_facts_and_its_official_visa_page():
    base = load_packing("paris")
    jp = generic_packing(base, city("JP", wikidata="Q35765"))
    k = keys(jp)
    assert "100 V" in k["plug-adapter"].text and k["plug-adapter"].fact == "plug"
    assert jp.facts["plug"].source_url.startswith("https://en.wikipedia.org/")
    assert "日本外务省" in k["visa"].text and jp.facts["visa"].source_url.startswith("https://www.mofa.go.jp/")
    assert k["visa"].show_source and "passport" not in jp.facts                 # 日本不是申根：没有「3 个月」那条
    de = generic_packing(base, city("DE"))
    assert "申根区后 3 个月以上" in keys(de)["passport"].text and "passport" in de.facts      # 申根国家共用
    nowhere = generic_packing(base, city("BR"))                                      # 没有整理过的国家：通用写法，不编
    assert "官方" in keys(nowhere)["visa"].text and "visa" not in nowhere.facts and "plug" not in nowhere.facts


def test_a_domestic_trip_takes_an_id_card_and_hong_kong_takes_the_travel_permit():
    base = load_packing("paris")
    home = keys(generic_packing(base, city("CN", wikidata="Q956", currency="CNY")))
    assert "id-card" in home and not {"passport", "visa", "plug-adapter"} & set(home)
    hk = keys(generic_packing(base, city("CN", wikidata="Q8646", currency="HKD")))
    assert "permit" in hk and "id-card" not in hk and "passport" not in hk
    assert "G / D / M" in hk["plug-adapter"].text if "plug-adapter" in hk else True


def test_students_are_told_to_bring_a_student_card_and_abroad_an_isic_card():
    students = [Traveler(age=21, student=True)]
    dom = student_items(city("CN", wikidata="Q956", currency="CNY"), students)
    assert len(dom) == 1 and "学生证" in dom[0]["text"] and "ISIC" not in dom[0]["text"]
    eu = student_items(load_city("paris"), students)[0]
    assert "ISIC" in eu["text"] and "欧洲" in eu["text"] and eu["source_url"] == ISIC_URL
    jp = student_items(city("JP"), students)[0]
    assert "ISIC" in jp["text"] and "欧洲" not in jp["text"]
    assert student_items(load_city("paris"), [Traveler(age=30)]) == []                # 没人是学生：不出现


def test_the_todo_for_the_visa_names_the_official_site_and_schengen_keeps_its_passport_rule():
    jp = {k: t for k, t, _ in suggested_for(city("JP"))}
    assert "日本外务省" in jp["visa"] and "申根" not in jp["passport-validity"]
    de = {k: t for k, t, _ in suggested_for(city("DE"))}
    assert "申根区后要有 3 个月以上" in de["passport-validity"]


def test_a_domestic_list_has_no_passport_or_visa_backup():
    data = generic_packing(load_packing("paris"), city("CN", wikidata="Q956", currency="CNY"))
    text = " ".join(i.text for i in data.items)
    assert "护照" not in text and "签证" not in text and "身份证等重要证件的电子备份" in text


def test_other_cities_do_not_inherit_the_pickpocket_or_bag_fee_claims_that_were_written_for_paris():
    base = load_packing("paris")
    assert any("扒手" in i.text for i in base.items)                                    # 巴黎的模板保持原样
    for c in (city("JP"), city("DE"), city("CN", wikidata="Q956", currency="CNY"), city("BR")):
        text = " ".join(i.text for i in generic_packing(base, c).items)
        assert "扒手" not in text and "另收费" not in text and "不提供一次性牙刷" not in text
        assert "anti-theft" not in keys(generic_packing(base, c))
