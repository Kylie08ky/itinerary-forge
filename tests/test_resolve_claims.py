"""现建城市的种子取数（app/citybuild/resolve.py seed_for）：维基数据里一个城市常有好几条「国家」「货币」记录，
包括已经结束的历史政权，要取现在有效的那一条（佛罗伦萨第一条是历史政权，曾经读不出国家）。"""

from __future__ import annotations

import httpx

from app.citybuild.resolve import Candidate, Resolver


def _claim(qid: str, *, rank: str = "normal", ended: bool = False) -> dict:
    return {"mainsnak": {"datavalue": {"value": {"id": qid}}}, "rank": rank,
            "qualifiers": {"P582": [{}]} if ended else {}}


def _text(pid: str, value: str) -> dict:
    return {pid: [{"mainsnak": {"datavalue": {"value": value}}, "rank": "normal"}]}


ENTITIES = {
    "Q2044": {"labels": {"en": {"value": "Florence"}, "zh-cn": {"value": "佛罗伦萨"}},
              "claims": {"P17": [_claim("Q172579", ended=True), _claim("Q148540", ended=True),
                                 _claim("Q38", rank="preferred")], **_text("P1082", "360000")}},
    "Q38": {"claims": {"P38": [_claim("Q_LIRA", ended=True), _claim("Q4916")], "P37": [_claim("Q652")],
                       **_text("P297", "IT")}},
    "Q172579": {"claims": {}},                                    # 历史政权：没有 ISO 代码
    "Q4916": {"claims": _text("P498", "EUR")},
    "Q_LIRA": {"claims": _text("P498", "ITL")},
    "Q652": {"claims": _text("P218", "it")},
}


def test_a_city_with_several_countries_uses_the_current_one_not_a_former_state():
    r = Resolver(httpx.Client())
    r.labels = lambda ids: {i: ENTITIES[i] for i in ids if i in ENTITIES}          # type: ignore[method-assign]
    seed = r.seed_for(Candidate(qid="Q2044", label="佛罗伦萨", lat=43.7696, lng=11.2558), "佛罗伦萨")
    assert seed["country"] == "IT" and seed["currency"] == "EUR" and seed["local_language"] == "it"
    assert seed["timezone"] == "Europe/Rome"


def test_a_country_lists_the_curated_cities_first_then_the_most_visited_ones():
    """「意大利」的弹窗：罗马、佛罗伦萨、米兰、威尼斯、那不勒斯、博洛尼亚、比萨、锡耶纳、都灵固定在前面
    （比萨、锡耶纳人口不到 10 万，按人口 / 名气排不进前十），后面再补最常去的其他城市。"""
    from app.citybuild.resolve import PINNED_CITIES, Candidate

    names = {"Q220": "罗马", "Q2044": "佛罗伦萨", "Q490": "米兰", "Q641": "威尼斯", "Q2634": "那不勒斯",
             "Q1891": "博洛尼亚", "Q13375": "比萨", "Q2751": "锡耶纳", "Q495": "都灵", "Q2028": "维罗纳",
             "Q2656": "巴勒莫", "Q1492": "巴塞罗那"}
    r = Resolver(httpx.Client())
    r._get = lambda *_a, **_k: {"results": {"bindings": [                              # type: ignore[method-assign]
        {"item": {"value": f"http://www.wikidata.org/entity/{q}"}, "sitelinks": {"value": str(200 - i)}}
        for i, q in enumerate(["Q220", "Q641", "Q2656", "Q2028", "Q1891"])]}}
    r.describe = lambda ids: [Candidate(qid=q, label=q, lat=43.0, lng=11.0,           # type: ignore[method-assign]
                                        kind="other" if q == "Q1891" else "settlement") for q in ids]
    r.labels = lambda ids: {i: {"labels": {"zh-cn": {"value": names[i]}}} for i in ids}   # type: ignore[method-assign]
    got = r.top_cities(Candidate(qid="Q38", label="意大利", kind="country"))
    pinned = PINNED_CITIES["Q38"]
    assert [c.qid for c in got[: len(pinned)]] == pinned
    assert len({c.qid for c in got}) == len(got)                                       # 不重复
    assert {"Q2656", "Q2028"} & {c.qid for c in got} == {"Q2656", "Q2028"}             # 后面补最常去的


def test_a_mainland_city_gets_chinese_as_its_local_language_not_english():
    """中国的「官方语言」在维基数据里是「标准汉语」，它没有两字母代码（P218）：曾经因此回落成 en。"""
    ents = {
        "Q_QZ": {"labels": {"en": {"value": "Quanzhou"}, "zh-cn": {"value": "泉州"}},
                 "claims": {"P17": [_claim("Q148")], **_text("P1082", "8000000")}},
        "Q148": {"claims": {"P38": [_claim("Q39099")], "P37": [_claim("Q727694")], **_text("P297", "CN")}},
        "Q39099": {"claims": _text("P498", "CNY")},
        "Q727694": {"claims": {}},                                          # 标准汉语：没有 P218
    }
    r = Resolver(httpx.Client())
    r.labels = lambda ids: {i: ents[i] for i in ids if i in ents}                    # type: ignore[method-assign]
    seed = r.seed_for(Candidate(qid="Q_QZ", label="泉州", lat=24.87, lng=118.68), "泉州")
    assert seed["country"] == "CN" and seed["local_language"] == "zh"
