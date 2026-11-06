"""随行清单（规格 M10 S2 / S3）：行李清单生成、推荐待办、待办紧急度。

纯函数：不联网、不读数据库，天气 / 预订由调用方传进来，所以好测。算法生成，不用模型。
行李模板在 `data/companion/packing.json`：带事实的条目（护照有效期、插头插座）必须有来源网址和核对日期，其余是建议。
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.cities import DEFAULT_CITY, City, load_city
from app.library import PlaceLibrary, load_library
from app.schemas import Itinerary, ItineraryItem, Residence, Traveler, TravelRequest

_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "companion"
CATEGORY_ORDER = ["证件", "衣物", "电子", "药品", "洗护", "其他", "儿童"]
CLIMATE_ZH = {"cold": "偏冷", "cool": "偏凉", "mild": "温和", "warm": "偏热"}
SOON_DAYS = 3                    # 待办离截止日 ≤ 3 天：红色提醒


# --------------------------------------------------------------------------- #
# 模板数据
# --------------------------------------------------------------------------- #
class Fact(BaseModel):
    note: str
    source_url: str = Field(min_length=8)
    checked_at: date


class Country(BaseModel):
    """一个热门目的地国家的随行数据（scripts/build_countries.py 生成，来源和核对日期见文件头）。"""

    name_zh: str
    plug_types: list[str]
    voltage_v: int
    frequency_hz: str
    schengen: bool = False
    europe: bool = False
    visa_url: str | None = None                 # 该国官方的签证 / 入境页面；没能核对的不写
    visa_name: str | None = None
    visa_url_status: Literal["ok", "bot_protected"] | None = None


class CountriesData(BaseModel):
    checked_at: date
    plug_source_url: str
    china: dict[str, Any]
    countries: dict[str, Country]


@lru_cache(maxsize=1)
def load_countries() -> CountriesData:
    return CountriesData.model_validate(json.loads((_DATA_DIR / "countries.json").read_text(encoding="utf-8")))


_CHINA_PLUGS = {"A", "I"}                       # 国内的插头类型（同一张表）
_HK_QID, _MACAU_QID = "Q8646", "Q14773"         # 香港、澳门：国内游客要办通行证和签注


def country_key(city: City) -> str:
    """查国家数据用的键：国家代码；香港用 HK。"""
    return "HK" if city.wikidata == _HK_QID else city.country


def plug_text(c: Country) -> str:
    """转换插头那条建议：当地插座类型 / 电压，和国内（A、I 型，220 V）比一比；都来自维基百科那张表。"""
    types = " / ".join(c.plug_types)
    same = sorted(_CHINA_PLUGS & set(c.plug_types))
    plug = (f"有和国内相同的 {' / '.join(same)} 型，多数国内插头能直接插，仍建议带一个转换插头备用" if same
            else "和国内常见的 A / I 型不同，要带转换插头")
    volt = ("电压和国内一样（220 V），国内电器不用变压" if c.voltage_v >= 220 else
            f"电压比国内（220 V）低，国内电器要看铭牌是否支持 {c.voltage_v} V（多数手机、电脑充电器支持 100–240 V）")
    return f"转换插头：{c.name_zh}是 {types} 型插座，{c.voltage_v} V / {c.frequency_hz} Hz；{plug}；{volt}"


class PackingTemplate(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    category: str
    text: str = Field(min_length=1, max_length=200)
    when: dict[str, Any] = Field(default_factory=dict)
    qty: dict[str, float] | None = None
    fact: str | None = None
    show_source: bool = False       # 只有涉及官方规定的条目才在页面上给来源链接；事实依据照常留在数据里


class PackingData(BaseModel):
    city: str
    facts: dict[str, Fact]
    monthly_high_c: dict[int, float]
    monthly_high_basis: str
    items: list[PackingTemplate]

    @model_validator(mode="after")
    def _facts_and_keys(self) -> PackingData:
        keys = [i.key for i in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError("行李模板里有重复的 key")
        for i in self.items:
            if i.category not in CATEGORY_ORDER:
                raise ValueError(f"{i.key}: 不认识的分类 {i.category}")
            if i.fact and i.fact not in self.facts:
                raise ValueError(f"{i.key}: 引用了不存在的事实 {i.fact}（事实必须登记来源和核对日期）")
        if set(self.monthly_high_c) != set(range(1, 13)):
            raise ValueError("monthly_high_c 要有 12 个月")
        return self


@lru_cache(maxsize=4)
def load_packing(city: str = DEFAULT_CITY) -> PackingData:
    paris = PackingData.model_validate(json.loads((_DATA_DIR / "packing.json").read_text(encoding="utf-8")))
    return paris if city == DEFAULT_CITY else generic_packing(paris, load_city(city))


# 巴黎模板里写死了巴黎 / 申根 / 法国 / 欧元的条目：别的城市用通用写法（没有来源的事实不写，都是「建议」）
# 这几条在巴黎模板里带着对巴黎的断言（「扒手多」「袋子要另收费」「部分酒店不提供牙刷」），没有来源，不能搬到别的城市
_CITY_SPECIFIC = {"passport", "visa", "plug-adapter", "cards", "walking-shoes", "rain-gear", "anti-theft",
                  "shopping-bag", "toiletries"}


def generic_packing(base: PackingData, city: City) -> PackingData:
    hk_macau = city.wikidata in (_HK_QID, _MACAU_QID)
    domestic = city.country == "CN" and not hk_macau
    country = load_countries().countries.get(country_key(city))
    facts: dict[str, Fact] = {}
    new: list[PackingTemplate] = [
        PackingTemplate(key="walking-shoes", category="衣物", text="舒适的步行鞋（景点之间要走很多路）"),
        PackingTemplate(key="rain-gear", category="衣物", text="折叠伞或轻便雨衣"),
        PackingTemplate(key="shopping-bag", category="其他", text="折叠购物袋（逛街、逛市集时用）"),
        PackingTemplate(key="toiletries", category="洗护", text="牙刷牙膏、洗面奶、护肤品"),
    ]
    if domestic:
        new += [PackingTemplate(key="id-card", category="证件", text="身份证（住宿登记、景点实名预约都要）"),
                PackingTemplate(key="cards", category="证件", text="手机支付（微信 / 支付宝）和银行卡，带少量现金")]
    elif hk_macau:
        new += [PackingTemplate(key="permit", category="证件",
                                text="往来港澳通行证（电子或卡式）和有效签注，以出入境管理部门的规定为准；另带身份证"),
                PackingTemplate(key="cards", category="证件",
                                text="银行卡（开通境外支付）和少量当地现金，手机支付先确认能用")]
    else:
        passport = ("护照（有效期需在离开申根区后 3 个月以上，签发不满 10 年）" if country and country.schengen
                    else "护照（有效期等要求以目的地国家的官方规定为准）")
        if country and country.schengen:
            facts["passport"] = base.facts["passport"]        # 申根共用的一条规则（法国签证官网那条已核对的事实）
        visa_text = "签证 / 入境材料（是否需要、要哪些，以目的地国家的官方渠道为准）"
        if country and country.visa_url:
            facts["visa"] = Fact(note=f"是否需要签证、要办哪些材料，以{country.visa_name}为准；这里不替官方下结论",
                                 source_url=country.visa_url, checked_at=load_countries().checked_at)
            visa_text = f"签证 / 入境材料（是否需要、要哪些，以{country.visa_name}为准）"
        plug = PackingTemplate(key="plug-adapter", category="电子", when={"any_traveler": "not_resident"},
                               text="转换插头：出发前查一下当地的插头类型和电压")
        if country:
            facts["plug"] = Fact(note=plug_text(country), source_url=load_countries().plug_source_url,
                                 checked_at=load_countries().checked_at)
            plug = PackingTemplate(key="plug-adapter", category="电子", when={"any_traveler": "not_resident"},
                                   text=plug_text(country), fact="plug")
        new += [
            PackingTemplate(key="passport", category="证件", when={"any_traveler": "not_resident"}, text=passport,
                            fact="passport" if "passport" in facts else None, show_source="passport" in facts),
            PackingTemplate(key="visa", category="证件", when={"any_traveler": "not_resident"}, text=visa_text,
                            fact="visa" if "visa" in facts else None, show_source="visa" in facts),
            PackingTemplate(key="cards", category="证件",
                            text=f"银行卡（开通境外支付）和少量当地现金（{city.currency}）"),
            plug,
        ]
    if domestic or hk_macau:
        new.append(PackingTemplate(key="id-backup", category="证件", text="身份证等重要证件的电子备份（手机里存一份）"))
    items = [i for i in base.items if i.key not in _CITY_SPECIFIC and not (domestic and i.key == "residence-permit")
             and not ((domestic or hk_macau) and i.key == "id-backup")]
    highs = city.monthly_high_c or dict.fromkeys(range(1, 13), 18.0)
    basis = ("按去年当地每月的平均最高气温估算（没有天气预报时用）；是估算，不是预报" if city.monthly_high_c
             else "没取到当地的常年气温，按温和气候估算；出发前请看天气预报")
    return PackingData(city=city.id, facts=facts, monthly_high_c=highs, monthly_high_basis=basis,
                       items=[*items, *new])


# --------------------------------------------------------------------------- #
# 气候
# --------------------------------------------------------------------------- #
def climate_class(avg_high_c: float) -> str:
    return "cold" if avg_high_c < 10 else "cool" if avg_high_c < 17 else "mild" if avg_high_c < 24 else "warm"


def climate_for(dates: list[date], weather: list[dict[str, Any]] | None, data: PackingData) -> tuple[str, str]:
    """(气候档, 依据说明)。有天气预报用预报的最高温；没有（或只有去年同期）用月份常年气温估算。"""
    highs = [w["t_max"] for w in (weather or []) if w.get("source") == "forecast" and w.get("t_max") is not None]
    if highs:
        avg = sum(highs) / len(highs)
        return climate_class(avg), f"按天气预报（平均最高 {avg:.0f}°C）：{CLIMATE_ZH[climate_class(avg)]}"
    avg = sum(data.monthly_high_c[d.month] for d in dates) / len(dates)
    return climate_class(avg), f"{data.monthly_high_basis}（约 {avg:.0f}°C）：{CLIMATE_ZH[climate_class(avg)]}"


# --------------------------------------------------------------------------- #
# 行李清单
# --------------------------------------------------------------------------- #
def _resident(t: Traveler) -> bool:
    return t.residence != Residence.none


def uses_concessions(party: list[Traveler]) -> bool:
    """有人可能享受年龄 / 居留 / 学生优惠（和预算同一口径）。"""
    return any(t.age < 26 or _resident(t) or t.student for t in party)


def _matches(when: dict[str, Any], *, climate: str, party: list[Traveler], categories: set[str],
             theme: str | None) -> bool:
    for key, want in when.items():
        if key == "climate" and climate not in want:
            return False
        if key == "children_under" and not any(t.age < want for t in party):
            return False
        if key == "any_traveler" and want == "not_resident" and all(_resident(t) for t in party):
            return False
        if key == "all_travelers" and want == "resident" and not all(_resident(t) for t in party):
            return False
        if key == "discount_proof" and bool(want) != uses_concessions(party):
            return False
        if key == "places_category" and not (categories & set(want)):
            return False
        if key == "theme" and theme not in want:
            return False
    return True


def _qty(q: dict[str, float], nights: int) -> int:
    n = math.ceil(nights * q.get("per_night", 1) + q.get("extra", 0))
    return int(max(q.get("min", 1), min(q.get("cap", 99), n)))


_RELIGIOUS = re.compile(
    r"教堂|大教堂|主教座堂|修道院|圣母院|寺|庙|神社|清真寺|礼拜堂|圣殿|圣堂|"
    r"church|cathedral|temple|mosque|shrine|monastery|basilica|abbey", re.I)
_HILLS = re.compile(r"山|长城|瀑布|峡谷|岭|峰|trail|mount|falls|canyon", re.I)


def _names(places: list[Any], limit: int = 3) -> str:
    shown = [p.name_zh for p in places[:limit]]
    return "、".join(shown) + (f" 等 {len(places)} 处" if len(places) > limit else "")


def context_items(it: Itinerary, lib: PlaceLibrary, city: City, months: list[int]) -> list[dict[str, str]]:
    """只属于这趟的几条建议：来自这座城市去年同期的气候（降水、温差、海拔）和这份行程里的地点（教堂寺庙、爬山、户外），
    都是经验性建议，页面上标「建议」；没有数据的项就不出现，不编。"""
    placed = list({i.place_id: lib.places[i.place_id] for d in it.days for i in d.items
                   if i.place_id and i.place_id in lib.places}.values())
    out: list[dict[str, str]] = []

    def add(key: str, category: str, text: str) -> None:
        out.append({"key": f"ctx-{key}", "category": category, "text": text})

    religious = [p for p in placed if _RELIGIOUS.search(f"{p.name_zh} {p.name_en}")]
    if religious:
        add("religious", "衣物", "参观教堂 / 寺庙 / 清真寺要穿得得体：带一件能遮住肩膀和膝盖的衣服，"
                              f"有的地方还要脱鞋或戴头巾，以现场规定为准（行程里有：{_names(religious)}）")
    hills = [p for p in placed if _HILLS.search(f"{p.name_zh} {p.name_en}") or (
        p.setting.value == "outdoor" and p.duration_min >= 180)]
    if hills:
        add("hills", "衣物", f"户外走得多 / 要爬山：穿防滑的鞋、带小背包和防磨脚的创可贴（行程里有：{_names(hills)}）")
    outdoor = [p for p in placed if p.setting.value == "outdoor"]
    if placed and len(outdoor) / len(placed) >= 0.5:
        add("outdoor", "洗护", f"这趟大半时间在户外（{len(outdoor)} / {len(placed)} 个地点）：防晒霜、润唇膏、遮阳帽，"
                             "出门带水")
    if city.monthly_rain_mm:
        rain = sum(city.monthly_rain_mm[m] for m in months) / len(months)
        if rain >= 100:
            add("rain", "衣物", f"这个季节雨水多（当地去年同期月降水约 {rain:.0f} 毫米）：带防水外套和防滑耐水的鞋，"
                              "雨天可以把室外的点换成室内的")
    if city.monthly_high_c and city.monthly_low_c:
        spread = sum(city.monthly_high_c[m] - city.monthly_low_c[m] for m in months) / len(months)
        if spread >= 12:
            add("spread", "衣物", f"早晚温差大（当地去年同期平均约 {spread:.0f}°C）：多穿几层、带一件容易穿脱的外套")
        high = sum(city.monthly_high_c[m] for m in months) / len(months)
        if high >= 25 and abs(city.center[0]) < 28:
            add("bugs", "洗护", "当地偏热偏潮，蚊虫多：带驱蚊液，选轻薄透气的衣服")
    if city.elevation_m and city.elevation_m >= 1500:
        add("altitude", "药品", f"海拔约 {city.elevation_m:.0f} 米：第一天别排太满，多喝水、防晒保湿；"
                               "有心肺疾病或担心高原反应，出发前问医生")
    return out


ISIC_URL = "https://www.isic.org/"
ISIC_CHECKED = date(2026, 10, 31)


def student_items(city: City, party: list[Traveler]) -> list[dict[str, Any]]:
    """有人是学生：国内带学生证（景点学生票凭证件）；国外加 ISIC 国际学生证（官网说在 100 多个国家有学生优惠），
    欧洲尤其值得提前办——很多景点 / 博物馆 / 交通对学生有优惠，具体以各景点规定为准。"""
    if not any(t.student for t in party):
        return []
    domestic = city.country == "CN" and city.wikidata not in (_HK_QID, _MACAU_QID)
    if domestic:
        return [{"key": "student-card", "category": "证件",
                 "text": "学生证（原件）：景点的学生票、部分交通优惠要凭证件，没带可能买不到优惠"}]
    country = load_countries().countries.get(country_key(city))
    europe = bool(country and country.europe)
    where = "欧洲很多景点、博物馆和交通对学生有优惠，" if europe else "不少景点和交通对学生有优惠，"
    return [{"key": "student-card", "category": "证件", "source_url": ISIC_URL, "checked_at": ISIC_CHECKED.isoformat(),
             "source_note": "ISIC 官网：学生身份认证，100 多个国家有学生优惠",
             "text": f"学生证原件和 ISIC 国际学生证：{where}出发前提前办好（办理要几天），"
                     "具体以各景点 / 交通的规定为准"}]


def generate_packing(it: Itinerary, request: TravelRequest, party: list[Traveler],
                     weather: list[dict[str, Any]] | None, *, library: PlaceLibrary | None = None,
                     data: PackingData | None = None) -> dict[str, Any]:
    """按季节 / 天数 / 出行人 / 行程内容生成清单。返回 {items: [...], basis: 说明, climate}。"""
    lib = library or load_library(request.city_id)
    data = data or load_packing(request.city_id)
    days = len(it.days)
    nights = max(days - 1, 1)
    climate, basis = climate_for([d.date for d in it.days], weather, data)
    categories = {lib.places[i.place_id].category.value for d in it.days for i in d.items
                  if i.place_id and i.place_id in lib.places}
    theme = it.generation.theme_id if it.generation else None
    rainy = [i + 1 for i, w in enumerate(weather or []) if w.get("rainy")]
    out = []
    for t in data.items:
        if not _matches(t.when, climate=climate, party=party, categories=categories, theme=theme):
            continue
        text = t.text.format(n=_qty(t.qty, nights), days=days) if t.qty else t.text
        item: dict[str, Any] = {"key": t.key, "category": t.category, "text": text}
        if t.fact and t.show_source:
            f = data.facts[t.fact]
            item.update(source_note=f.note, source_url=f.source_url, checked_at=f.checked_at.isoformat())
        if t.key == "rain-gear" and rainy:
            item["text"] = text + f"（预报第 {'、'.join(map(str, rainy))} 天有雨）"
        out.append(item)
    months = sorted({d.date.month for d in it.days})
    out.extend(student_items(load_city(request.city_id), party))
    out.extend(context_items(it, lib, load_city(request.city_id), months))
    out.sort(key=lambda x: CATEGORY_ORDER.index(x["category"]))
    return {"items": out, "basis": basis, "climate": climate}


def generate_packing_multi(it: Itinerary, parent: TravelRequest, party: list[Traveler],
                           weather: list[dict[str, Any]] | None) -> dict[str, Any]:
    """多城市：整趟旅行按第一座城市的季节 / 天数生成一份，再补上其他城市各自才有的条目（插头、当地提示等）。"""
    from app import multicity  # 延迟导入：multicity 用 trips，不应在导入 companion 时被牵进来
    from app.library import combined_library

    legs = multicity.legs_of(parent)
    base = generate_packing(it, parent.model_copy(update={"legs": None}), party, weather,
                            library=combined_library(tuple(multicity.leg_ids(parent))),
                            data=load_packing(legs[0].request.city_id))
    out = list(base["items"])
    have = {(x["key"], x["text"]) for x in out}
    keys = {x["key"] for x in out if x.get("key")}
    by_date = {w["date"]: w for w in weather or []}
    for req, view in multicity.split(parent, it)[1:]:
        leg_weather = [by_date[d.date.isoformat()] for d in view.days if d.date.isoformat() in by_date] or None
        extra = generate_packing(view, req, party, leg_weather)["items"]
        for item in extra:
            if (item["key"], item["text"]) in have or (item.get("key") and item["key"] in keys):
                continue
            have.add((item["key"], item["text"]))
            out.append(item)
    out.sort(key=lambda x: CATEGORY_ORDER.index(x["category"]))
    return {**base, "items": out}


# --------------------------------------------------------------------------- #
# 待办
# --------------------------------------------------------------------------- #
def note_ref(day: date, item: ItineraryItem) -> str:
    """一条行程安排的备注对象：地点库里的点按地点记（拖到别的一天也跟着），其余按「日期|名称」。"""
    return item.place_id or f"{day.isoformat()}|{item.title}"


def day_note_ref(day: date) -> str:
    """以前的「整天笔记」。"""
    return f"day|{day.isoformat()}"


def valid_note_refs(itinerary) -> set[str]:
    refs = {day_note_ref(d.date) for d in itinerary.days}
    refs |= {note_ref(d.date, i) for d in itinerary.days for i in d.items}
    return refs


def todo_urgency(due: date | None, done: bool, today: date) -> dict[str, Any]:
    """done 已完成 / none 没设截止日 / overdue 已过期 / today 今天截止 / soon ≤ 3 天 / ok 还早。
    days = 距截止日的天数（过期为负）。"""
    if done:
        return {"level": "done", "days": None}
    if due is None:
        return {"level": "none", "days": None}
    left = (due - today).days
    level = "overdue" if left < 0 else "today" if left == 0 else "soon" if left <= SOON_DAYS else "ok"
    return {"level": level, "days": left}


def todo_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    open_ = [i for i in items if i["urgency"]["level"] != "done"]
    hot = [i for i in open_ if i["urgency"]["level"] in ("overdue", "today", "soon")]
    return {"open": len(open_), "done": len(items) - len(open_), "urgent": len(hot),
            "overdue": sum(1 for i in open_ if i["urgency"]["level"] == "overdue")}


# 推荐待办（规格 M10 S3c）：天数是「建议」，不是官方规定
SUGGESTED = [
    ("passport-validity", "核对护照有效期（离开申根区后要有 3 个月以上，且签发不满 10 年）", 60),
    ("visa", "核对入境要求 / 办签证（以法国签证官网 france-visas.gouv.fr 为准，预留办理时间）", 45),
    ("flights", "订机票", 60),
    ("hotel", "订住宿", 45),
    ("insurance", "买旅行保险", 14),
    ("money", "换欧元 / 办外币卡（开通境外支付）", 10),
    ("sim", "开通漫游或买电话卡", 7),
    ("offline", "保存离线行程和地图（下载 PDF、离线地图）", 3),
    ("checkin", "网上值机", 1),
]


def transport_todos(transport: Any) -> list[tuple[str, str, date, str]]:
    """按来回交通生成的提醒（规格 07 P07）：飞机 → 网上值机（起飞前一天截止）；
    高铁 / 火车 → 取票、提前到站（出发当天）；其余方式没有。
    开放时间、提前量都是通用建议，写明「以你的航司 / 车站说明为准」，不编具体规定。"""
    out: list[tuple[str, str, date, str]] = []
    for leg, label, tag in ((transport.outbound, "去程", "out"), (transport.inbound, "返程", "back")):
        if leg.depart_at is None:
            continue
        day = leg.depart_at.date()
        if leg.mode.value == "flight":
            out.append((f"sug-checkin-{tag}", f"{label}网上值机", day - timedelta(days=1),
                        "多数航空公司在起飞前 24–48 小时开放网上值机，以你的航司为准（通用提醒，不是官方规定）"))
        elif leg.mode.value in ("high_speed_rail", "train"):
            out.append((f"sug-ticket-{tag}", f"{label}取票、提前到站（建议提前约 1 小时，带好证件）", day,
                        "通用建议，具体以车站 / 12306 的说明为准"))
    return out


def suggested_for(city: City | None) -> list[tuple[str, str, int]]:
    """推荐待办：巴黎用上面写死的（法国签证、欧元）；别的城市用通用写法，国内城市去掉护照 / 签证 / 换外币。"""
    if city is None or city.id == DEFAULT_CITY:
        return SUGGESTED
    domestic = city.country == "CN"
    out: list[tuple[str, str, int]] = []
    for key, text, offset in SUGGESTED:
        if key in ("passport-validity", "visa", "sim") and domestic:
            continue
        if key == "passport-validity" and (c0 := load_countries().countries.get(country_key(city))) and c0.schengen:
            out.append((key, "核对护照有效期（离开申根区后要有 3 个月以上，且签发不满 10 年）", offset))
            continue
        if key == "passport-validity":
            text = "核对护照有效期（要求以目的地国家的官方规定为准）"
        elif key == "visa":
            c = load_countries().countries.get(country_key(city))
            where = c.visa_name if c and c.visa_url else "目的地国家的官方渠道"
            text = f"核对入境要求 / 办签证（以{where}为准，预留办理时间）"
        elif key == "money":
            text = ("确认手机支付（微信 / 支付宝）可用，带少量现金" if domestic
                    else f"办外币卡（开通境外支付），准备少量当地现金（{city.currency}）")
        out.append((key, text, offset))
    return out


def suggest_todos(start: date, today: date, reservations: list[dict[str, Any]],
                  existing_keys: set[str], transport: Any = None, city: City | None = None) -> list[dict[str, Any]]:
    """推荐待办：日期按出发日倒推；已经加过的不重复；日期已过去的按今天并标「已临近，尽快」。
    购票清单里有明确提前天数的景点再加一条「订『xx』门票」，截止日就是建议最晚订票日。"""
    out: list[dict[str, Any]] = []

    def add(key: str, text: str, due: date, why: str) -> None:
        if key in existing_keys:
            return
        late = due < today
        out.append({"key": key, "text": text, "due": (today if late else due).isoformat(),
                    "why": ("已临近，尽快（" + why + "）") if late else why})

    for key, text, offset in suggested_for(city):
        if key == "checkin" and transport is not None:         # 有交通信息：值机提醒按飞机 / 高铁分别生成（下面）
            continue
        add(f"sug-{key}", text, start - timedelta(days=offset), f"建议在出发前 {offset} 天前完成，不是官方规定")
    if transport is not None:
        for key, text, due, why in transport_todos(transport):
            add(key, text, due, why)
    for r in reservations:
        if r.get("book_by") and r.get("status") != "booked" and r.get("urgency") != "past" and r.get("in_itinerary"):
            add(f"book-{r['place_id']}", f"订「{r['title']}」门票", date.fromisoformat(r["book_by"]),
                f"来自购票清单：建议最晚订票日（提前 {r['lead_days']} 天，经验值，以官网为准）")
    return out
