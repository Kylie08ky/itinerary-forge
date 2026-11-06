"""v2 地点库与主题包（roadmap B1 / B2）。

- Place：一个可被排进行程的真实地点（策展数据，不信模型）。
- ThemePack：一个主题 = 一份「配方」+ 一组成员（Place 的 id + 角色 + 权重）。
- PlaceLibrary：把两者装载并做完整性校验，给后续的候选池 / 排程器用。

「一个地点属于哪些主题」只存在 ThemePack.members 里，Place 自己不存 themes，避免两处写不一致。
数据文件：data/places/<city>.json、data/themes/<city>.json。
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.cities import DEFAULT_CITY, Bbox, load_city
from app.citybuild.store import generated_path
from app.notes.text import fold
from app.user_places import load_user_places

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# 市区坐标的合理范围（来自城市配置 data/cities/<id>.json）；超出的必须标 zone=day_trip（凡尔赛、迪士尼这类一日游）。
# 校验 Place 时用「当前正在装载的城市」的范围；单独构造 Place（测试）时默认是巴黎。
_CITY_BBOX: ContextVar[tuple[Bbox, str]] = ContextVar("city_bbox", default=(load_city(DEFAULT_CITY).bbox, "巴黎"))

WEEKDAY_ZH = "一二三四五六日"


class Category(StrEnum):
    museum = "museum"
    sight = "sight"
    park = "park"
    shopping = "shopping"
    food = "food"
    entertainment = "entertainment"
    activity = "activity"


class Setting(StrEnum):
    indoor = "indoor"          # 基本都在室内：博物馆、百货、剧院、有顶的拱廊
    outdoor = "outdoor"        # 基本在室外：广场、桥、花园、街区、观景台
    mixed = "mixed"            # 室内外各占一部分：宫殿 + 花园、游船、游乐园


class BestTime(StrEnum):
    morning = "morning"
    afternoon = "afternoon"
    evening = "evening"
    golden_hour = "golden_hour"
    any = "any"


class Zone(StrEnum):
    core = "core"              # 城市主城区（原来写作 paris；老数据文件里的 "paris" 加载时读作 core）
    day_trip = "day_trip"      # 要单独占大半天或一整天的郊区


class Role(StrEnum):
    anchor = "anchor"          # 可以当一天的主要节点
    support = "support"        # 填充 / 缓冲


class UrlStatus(StrEnum):
    ok = "ok"                          # 自动请求能打开官网
    bot_protected = "bot_protected"    # 官网域名正确，但有反爬（Cloudflare 等），自动请求被拦


class Ticketing(StrEnum):
    """购票方式（规格 M06）。规则：有线上购票渠道就推荐线上购票，除非游客说想收集纸质票根。"""

    online_required = "online_required"      # 必须提前线上购票（现场基本买不到 / 只卖时段票）
    online_or_walkin = "online_or_walkin"    # 可线上购票，现场也可排队购买（可能排长队、热门日售罄）
    none = "none"                            # 无需购票 / 免费 / 没有购票渠道（公园、街区、商场、咖啡馆等）


class Booking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = False
    lead_time_hint: str | None = None
    # 明确的「建议提前几天订」（规格 M06 S3）。只对说法明确的地点填；没有明确天数就留空，页面显示「建议尽早」，不编数字
    lead_days: int | None = Field(default=None, ge=1, le=120)


class OpeningHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: str
    close: str

    @field_validator("open", "close")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"时间必须是 HH:MM，收到 {v!r}")
        return v


Who = Literal["any", "resident_eu", "resident_fr"]    # 优惠适用的人：所有人 / 长居欧盟（欧洲经济区）/ 长居法国


class AgeBand(BaseModel):
    """一个年龄段的价格（含免费 = 0）。who 限定身份：官网常写「26 岁以下欧盟居民免费」。"""

    min_age: int = Field(default=0, ge=0, le=120)
    max_age: int = Field(ge=0, le=120)
    eur: float = Field(ge=0)
    who: Who = "any"
    note: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> AgeBand:
        if self.max_age < self.min_age:
            raise ValueError("年龄段的 max_age 不能小于 min_age")
        return self


class PricePeriod(BaseModel):
    """分月份价格（淡旺季）。months 是 1–12；eea_eur 是这个时段的欧盟居民价。"""

    months: list[int] = Field(min_length=1)
    eur: float = Field(ge=0)
    eea_eur: float | None = Field(default=None, ge=0)

    @field_validator("months")
    @classmethod
    def _months_ok(cls, v: list[int]) -> list[int]:
        if any(m < 1 or m > 12 for m in v) or len(set(v)) != len(v):
            raise ValueError("months 要是不重复的 1–12")
        return v


class Concessions(BaseModel):
    """优惠规则：必须带官方来源和核对日期。没有这个对象 = 优惠规则没核对过，预算一律按成人全价。"""

    source_url: str = Field(min_length=8)
    checked_at: date
    eea_eur: float | None = Field(default=None, ge=0)        # 长居欧盟 / 欧洲经济区居民的成人价
    bands: list[AgeBand] = Field(default_factory=list)
    student_eur: float | None = Field(default=None, ge=0)
    student_who: Who = "any"
    complete: bool = True            # False = 只查到一部分（比如没查到 18 岁以下的规则）
    note: str | None = None


class NameLocalCheck(BaseModel):
    """当地语言名的核对记录（规格 07 P18）：名字来自哪、是否两个来源一致、哪天查的。名字不由模型生成。"""

    model_config = ConfigDict(extra="forbid")

    source: Literal["osm", "wikidata", "osm+wikidata", "manual"]
    status: Literal["verified", "draft"]      # verified = 两个来源一致（或人工核对）；draft = 单一来源或不一致
    checked_at: date
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")


_MMDD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


class DraftSource(BaseModel):
    """开放数据草稿的来源（规格 08 3.4）：OSM 对象、维基数据条目、哪天建的。"""

    model_config = ConfigDict(extra="forbid")

    osm: str | None = None                      # 「node/123」「way/456」「relation/789」
    # 高德的地点编号（用户从高德里搜来加的点，S7d）；没有就不输出，别的城市的数据形状不变
    amap: str | None = Field(default=None, exclude_if=lambda v: v is None)
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")
    built_at: date


class Fame(BaseModel):
    """名气分（M02 S9）：维基数据里这个条目有多少种语言版本的维基百科。公开、可核对，不是模型打的分。"""

    model_config = ConfigDict(extra="forbid")

    sitelinks: int = Field(ge=0)
    source: Literal["wikidata"] = "wikidata"
    checked_at: date
    # 热度（M02 S9）：近 12 个月中文维基百科的浏览量（只算真人）；没有中文条目的点没有
    views: int | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)
    views_period: str | None = Field(default=None, max_length=20, exclude_if=lambda v: v is None)


class VoyageMark(BaseModel):
    """维基导游（Wikivoyage，CC BY-SA 4.0）的城市页里列了这个地点：只存标记和链接，不存它的文字（M02 S9）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["see", "do", "eat", "drink", "buy"]
    url: str = Field(max_length=300)
    revid: int = Field(ge=0)
    checked_at: date


class Season(BaseModel):
    """当季活动的举办期（圣诞市场等）：日期是事实，必须带来源网址和核对日期。只在这段日期里排得进行程。"""

    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    source_url: str
    checked_at: date
    confirmed_by: Literal["official", "listing"] = "listing"    # official = 官方网站写明；listing = 只在聚合网站上看到
    note: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Season:
        if self.end < self.start:
            raise ValueError("season 的结束日期早于开始日期")
        if not self.source_url.startswith("https://"):
            raise ValueError("season.source_url 必须以 https:// 开头")
        return self


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name_zh: str
    name_en: str
    name_local: str | None = None               # 当地语言名（法语 / 意大利语…），带核对记录；老数据可以没有
    name_local_check: NameLocalCheck | None = None
    category: Category
    setting: Setting                            # 室内 / 室外 / 半室外：策展数据，雨天联动用，不由 LLM 判断
    zone: Zone = Zone.core
    area: str                                   # 「7区」「凡尔赛」等，给人看的
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    duration_min: int = Field(gt=0)
    price_eur: float | None = Field(default=None, ge=0)
    # 门票价格是事实，必须带来源网址和核对日期（硬规则）；季节 / 欧盟居民 / 线上线下的差异写进 price_note
    price_source_url: str | None = None
    price_checked_at: date | None = None
    price_note: str | None = None
    price_periods: list[PricePeriod] = Field(default_factory=list)   # 分月份价格；月份没覆盖到时回退到 price_eur
    concessions: Concessions | None = None
    open_access: bool = False                   # 公共开放空间（广场、桥、街区、免费公园）：不收门票，预算里不算「待补」
    best_time: BestTime = BestTime.any
    closed_weekdays: list[int] = Field(default_factory=list)   # 0=周一 … 6=周日
    closed_dates: list[str] = Field(default_factory=list)      # 每年固定闭馆的「MM-DD」，如 12-25、01-01
    date_notes: dict[str, str] = Field(default_factory=dict)   # 「MM-DD」→ 那天的提醒（提前闭馆、改时间）
    season: Season | None = None                               # 当季活动的举办期；不在期内当作闭馆
    data_status: Literal["verified", "draft"] = "verified"     # 页面上区分「已核对」和「开放数据草稿」
    draft_source: DraftSource | None = None
    fame: Fame | None = Field(default=None, exclude_if=lambda v: v is None)      # 现建城市的地点才有
    voyage: VoyageMark | None = Field(default=None, exclude_if=lambda v: v is None)
    typical_hours: OpeningHours | None = None
    long_term_closed: bool = False
    booking: Booking = Field(default_factory=Booking)
    official_url: str | None = None
    ticketing: Ticketing = Ticketing.none
    meal: Literal["lunch", "tea"] | None = None   # 这个点本身就是一顿午饭 / 一次下午茶（只给说明里写明的点打标）
    ticket_note: str | None = None              # 购票经验 / 规则说明（资料来源多为第三方，以官网为准）
    ticketing_checked_at: date | None = None    # 上面这条购票说明是哪天查的
    address: str | None = None                  # 门店地址：零售店等没有官网 / 官网只是线上商城的地点，给地址就够
    url_status: UrlStatus | None = None         # 官网链接的自动核对结果；有链接就必须有
    verified_at: date | None = None             # 最近一次自动核对（官网 / 坐标 / 闭馆日）的日期
    metro: str | None = None
    highlights: list[str] = Field(default_factory=list)
    note: str = ""
    interests: list[str] = Field(default_factory=list)          # v1 遗留的兴趣标签
    # 数据可信度标记：为 False 的字段要在 B4（Google Places）阶段核实
    hours_verified: bool = False
    coords_verified: bool = False

    @field_validator("closed_dates")
    @classmethod
    def _closed_dates_ok(cls, v: list[str]) -> list[str]:
        if any(not _MMDD.match(d) for d in v):
            raise ValueError("closed_dates 要写成「MM-DD」，如 12-25")
        if len(set(v)) != len(v):
            raise ValueError("closed_dates 有重复")
        return sorted(v)

    @field_validator("date_notes")
    @classmethod
    def _date_notes_ok(cls, v: dict[str, str]) -> dict[str, str]:
        if any(not _MMDD.match(d) for d in v):
            raise ValueError("date_notes 的键要写成「MM-DD」")
        return v

    @model_validator(mode="after")
    def _draft_has_source(self) -> Place:
        if self.data_status == "draft" and self.draft_source is None:
            raise ValueError(f"{self.id}: 草稿必须带 draft_source（OSM / 维基数据来源和建立日期）")
        return self

    @field_validator("closed_weekdays")
    @classmethod
    def _weekdays(cls, v: list[int]) -> list[int]:
        if any(not 0 <= d <= 6 for d in v):
            raise ValueError("closed_weekdays 取值必须在 0–6")
        if len(set(v)) != len(v):
            raise ValueError("closed_weekdays 有重复")
        return sorted(v)

    @field_validator("official_url")
    @classmethod
    def _https(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("https://"):
            raise ValueError("official_url 必须以 https:// 开头")
        return v

    @model_validator(mode="after")
    def _local_name_has_check_record(self) -> Place:
        if bool(self.name_local) != bool(self.name_local_check):
            raise ValueError(f"{self.id}: name_local 和 name_local_check（来源和核对日期）要同时有")
        return self

    @model_validator(mode="after")
    def _url_has_check_record(self) -> Place:
        if self.official_url and not (self.url_status and self.verified_at):
            raise ValueError(f"{self.id}: 有官网链接就必须带 url_status 和 verified_at（核对记录）")
        return self

    @model_validator(mode="after")
    def _price_has_check_record(self) -> Place:
        if self.price_eur is not None and not (self.price_source_url and self.price_checked_at):
            raise ValueError(f"{self.id}: 有门票价格就必须带 price_source_url 和 price_checked_at（来源和核对日期）")
        months = [m for p in self.price_periods for m in p.months]
        if len(set(months)) != len(months):
            raise ValueError(f"{self.id}: price_periods 的月份不能重叠")
        if self.price_periods and self.price_eur is None:
            raise ValueError(f"{self.id}: 有 price_periods 就必须有 price_eur（月份没覆盖到时的回退价）")
        if self.concessions and self.price_eur is None:
            raise ValueError(f"{self.id}: 有优惠规则就必须有 price_eur")
        if self.open_access and self.price_eur is not None:
            raise ValueError(f"{self.id}: open_access（公共开放空间，不收门票）不该再有 price_eur")
        return self

    @model_validator(mode="after")
    def _ticketing_matches_booking_flag(self) -> Place:
        if self.booking.required != (self.ticketing == Ticketing.online_required):
            raise ValueError(f"{self.id}: booking.required 必须线上购票 ⇔ ticketing=online_required，两者不一致")
        return self

    @model_validator(mode="after")
    def _lead_days_has_a_stated_basis(self) -> Place:
        if self.booking.lead_days is None:
            return self
        if self.ticketing == Ticketing.none:
            raise ValueError(f"{self.id}: 无需订票的地点不该有 lead_days")
        if not (self.booking.lead_time_hint or self.ticket_note):
            raise ValueError(f"{self.id}: lead_days 必须有订票说明（lead_time_hint / ticket_note）作依据，不能凭空填")
        return self

    @field_validator("zone", mode="before")
    @classmethod
    def _legacy_zone_name(cls, v: object) -> object:
        return "core" if v == "paris" else v

    @model_validator(mode="after")
    def _zone_matches_coords(self) -> Place:
        bbox, name = _CITY_BBOX.get()
        inside = bbox.contains(self.lat, self.lng)
        if self.zone == Zone.core and not inside:
            raise ValueError(f"{self.id}: 坐标不在{name}市区范围内，应标 zone=day_trip")
        if self.zone == Zone.day_trip and inside:
            raise ValueError(f"{self.id}: 坐标在市区内，却标了 day_trip")
        return self

    def closed_reason(self, day: date, *, allow_long_term: bool = False) -> str | None:
        """这天为什么不能去：长期闭馆 / 每周闭馆日 / 每年固定闭馆日 / 当季活动不在期内；能去 → None。"""
        if self.long_term_closed and not allow_long_term:
            return "长期闭馆 / 整修中"
        if day.weekday() in self.closed_weekdays:
            return f"每周{WEEKDAY_ZH[day.weekday()]}闭馆"
        key = f"{day.month:02d}-{day.day:02d}"
        if key in self.closed_dates:
            return f"{day.month} 月 {day.day} 日闭馆"
        if self.season and not (self.season.start <= day <= self.season.end):
            return f"当季活动，{self.season.start} 到 {self.season.end} 举办，不在期内"
        return None

    def note_for(self, day: date) -> str | None:
        """这天特别要提醒的事（12/24 提前闭馆这类）；没有 → None。"""
        return self.date_notes.get(f"{day.month:02d}-{day.day:02d}")

    @property
    def public_url(self) -> str | None:
        """对外（页面、导出、接口）展示的官网链接：有线上购票渠道的地点才给，其余只给地址。

        official_url 仍保留在数据里（带核对记录，将来可能用），但任何出口都必须走这个属性。
        """
        return self.official_url if self.ticketing != Ticketing.none else None

    @property
    def full_day(self) -> bool:
        """需要整天（≥4 小时，或郊区一日游）。排程器据此不和别的大点同一天。"""
        return self.duration_min >= 240 or self.zone == Zone.day_trip


class ThemeRecipe(BaseModel):
    """排程时怎么「按这个主题安排一天」。"""

    model_config = ConfigDict(extra="forbid")

    anchors_per_day: dict[str, int]             # relaxed / balanced / intense -> 每天主要节点数
    best_time_bias: list[BestTime] = Field(default_factory=list)
    max_big_venues_per_day: int = Field(default=2, ge=1)
    notes: str = ""

    @field_validator("anchors_per_day")
    @classmethod
    def _styles(cls, v: dict[str, int]) -> dict[str, int]:
        if set(v) != {"relaxed", "balanced", "intense"}:
            raise ValueError("anchors_per_day 必须恰好包含 relaxed / balanced / intense")
        if any(n < 1 for n in v.values()):
            raise ValueError("anchors_per_day 的值必须 ≥ 1")
        return v


class ThemeMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place_id: str
    role: Role
    weight: int = Field(ge=1, le=5)


class ThemePack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name_zh: str
    tagline: str
    description: str
    recipe: ThemeRecipe
    members: list[ThemeMember]
    tips: list[str] = Field(default_factory=list)


class PlaceLibrary(BaseModel):
    city: str
    places: dict[str, Place]
    themes: dict[str, ThemePack]

    @model_validator(mode="after")
    def _integrity(self) -> PlaceLibrary:
        for theme in self.themes.values():
            seen: set[str] = set()
            for m in theme.members:
                if m.place_id not in self.places:
                    raise ValueError(f"主题 {theme.id} 引用了不存在的地点 {m.place_id}")
                if m.place_id in seen:
                    raise ValueError(f"主题 {theme.id} 里 {m.place_id} 重复")
                seen.add(m.place_id)
                if m.role == Role.anchor and self.places[m.place_id].long_term_closed:
                    raise ValueError(f"主题 {theme.id} 把长期闭馆的 {m.place_id} 当成了 anchor")
        return self

    # ---- 查询 ---------------------------------------------------------- #
    def get_place(self, place_id: str) -> Place:
        try:
            return self.places[place_id]
        except KeyError:
            raise KeyError(f"地点库里没有 {place_id!r}") from None

    def fame_ranking(self) -> dict[str, int]:
        """市区里有名气分的点：地点号 → 全城第几名（1 = 名气最大；语言版本数相同的并列，按名字稳定排序）。"""
        ranked = sorted((p for p in self.places.values() if p.fame and p.zone == Zone.core),
                        key=lambda p: (-p.fame.sitelinks, -(p.fame.views or 0), p.name_zh))      # type: ignore[union-attr]
        return {p.id: n for n, p in enumerate(ranked, start=1)}

    def fame_ready(self) -> bool:
        """这座城市的名气分够不够用来排序：市区的点八成以上有（人工策展的老城市没有，照旧按主题）。"""
        core = [p for p in self.places.values() if p.zone == Zone.core]
        return bool(core) and sum(1 for p in core if p.fame) >= 0.8 * len(core)

    def get_theme(self, theme_id: str) -> ThemePack:
        try:
            return self.themes[theme_id]
        except KeyError:
            raise KeyError(f"没有这个主题：{theme_id!r}（可选：{', '.join(self.themes)}）") from None

    def theme_members(self, theme_id: str, role: Role | None = None) -> list[tuple[Place, ThemeMember]]:
        """某主题的成员，按权重从高到低（同权重按名称，保证结果稳定）。"""
        pairs = [
            (self.places[m.place_id], m)
            for m in self.get_theme(theme_id).members
            if role is None or m.role == role
        ]
        return sorted(pairs, key=lambda pm: (-pm[1].weight, pm[0].name_zh))

    def themes_of(self, place_id: str) -> list[str]:
        self.get_place(place_id)
        return [t.id for t in self.themes.values() if any(m.place_id == place_id for m in t.members)]

    def is_closed_on(self, place: Place, day: date) -> bool:
        return place.closed_reason(day) is not None

    def search(self, query: str, limit: int = 8) -> list[Place]:
        """按名字（中 / 英 / 别名）、区域模糊搜地点库，最相关的在前。空查询返回空。"""
        q = fold(query)
        if not q:
            return []
        aliases = _alias_groups()
        scored: list[tuple[int, str, Place]] = []
        for p in self.places.values():
            names = {fold(p.name_zh), fold(p.name_en)}
            if p.name_local:
                names.add(fold(p.name_local))
            for group in aliases:
                if names & group:
                    names |= group
            best = 0
            for n in names:
                if n == q:
                    best = max(best, 100)
                elif n.startswith(q):
                    best = max(best, 80)
                elif q in n:
                    best = max(best, 60)
            area = fold(p.area)
            if area == q:
                best = max(best, 70)
            elif q in area:
                best = max(best, 50)
            if best == 0 and any(q in fold(h) for h in p.highlights):
                best = 20
            if best:
                scored.append((best, p.name_zh, p))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [p for _s, _n, p in scored[:limit]]

    def find(self, text: str) -> Place | None:
        """按名字（中 / 英 / 当地名）在一段文本里找地点；多个命中取名字最长的，避免「老佛爷」吞掉「老佛爷百货购物」。"""
        low = fold(text)
        if len(low) < 2:        # 一个字符会命中一大片名字，没有意义
            return None
        best: tuple[int, Place] | None = None
        for p in self.places.values():
            for name in (p.name_zh, p.name_en, p.name_local or ""):
                n = fold(name)
                if n and (n in low or low in n) and (best is None or len(n) > best[0]):
                    best = (len(n), p)
        return best[1] if best else None


@lru_cache(maxsize=1)
def _alias_groups() -> list[set[str]]:
    """别名组（小写）：「奥赛博物馆 / Orsay / Musée d'Orsay」是同一个地方。搜索时用它把英文和别名对上。"""
    path = _DATA_DIR / "knowledge" / "place-aliases.json"
    if not path.exists():
        return []
    groups = json.loads(path.read_text(encoding="utf-8")).get("groups", [])
    return [{fold(s) for s in g if not s.startswith("_")} for g in groups]


def _data_file(kind: str, city: str) -> Path:
    """精校的 `data/<kind>/` 里先找，再找现建的 `data/generated/<kind>/`（规格 10）。"""
    path = _DATA_DIR / kind / f"{city}.json"
    return path if path.exists() else generated_path(kind, city)


@lru_cache(maxsize=16)
def load_library(city: str = DEFAULT_CITY) -> PlaceLibrary:
    places_raw = json.loads(_data_file("places", city).read_text(encoding="utf-8"))["places"]
    themes_raw = json.loads(_data_file("themes", city).read_text(encoding="utf-8"))["themes"]

    try:
        cfg = load_city(city)
    except KeyError as exc:
        raise FileNotFoundError(f"没有这座城市的配置：{city}") from exc
    token = _CITY_BBOX.set((cfg.bbox, cfg.name_zh))           # 这座城市的市区范围，校验每个点的坐标用
    try:
        places = [Place.model_validate(p) for p in places_raw]
        for raw in load_user_places(city):                    # 用户从高德里搜来加的点；坏的跳过，不拖垮整个库
            try:
                extra = Place.model_validate(raw)
            except ValueError:
                continue
            if all(extra.id != p.id for p in places):
                places.append(extra)
        ids = [p.id for p in places]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"地点 id 重复：{dup}")
        themes = [ThemePack.model_validate(t) for t in themes_raw]
        tids = [t.id for t in themes]
        if len(set(tids)) != len(tids):
            raise ValueError("主题 id 重复")
        # PlaceLibrary 装配时会再校验一遍地点，范围检查也要用这座城市的，所以放在同一个范围里
        return PlaceLibrary(
            city=city,
            places={p.id: p for p in places},
            themes={t.id: t for t in themes},
        )
    finally:
        _CITY_BBOX.reset(token)


@lru_cache(maxsize=16)
def combined_library(city_ids: tuple[str, ...]) -> PlaceLibrary:
    """多城市行程用：把几座城市的地点并成一份只读的库（不含主题）。地点 id 在城市之间不能重复，重复就拒绝。"""
    places: dict[str, Place] = {}
    for cid in city_ids:
        for pid, place in load_library(cid).places.items():
            if pid in places:
                raise ValueError(f"地点 id {pid!r} 在多座城市的库里重复，没法并成一份")
            places[pid] = place
    # 每座城市的地点装载时已经按各自的市区范围校验过，这里并起来不再校验（会拿默认城市的范围重检，误报）
    return PlaceLibrary.model_construct(city="+".join(city_ids), places=places, themes={})


def library_for_itinerary(itinerary) -> PlaceLibrary:
    """一份已有行程对应的地点库：多城市的并起来，单城市按目的地认。"""
    from app.cities import itinerary_city_id

    if itinerary.legs:
        return combined_library(tuple(b.city_id for b in itinerary.legs))
    return load_library(itinerary_city_id(itinerary.trip.destination))


def library_for_request(request) -> PlaceLibrary:
    """一份需求对应的地点库：多城市的并起来，单城市就是它自己的。"""
    if request.legs:
        return combined_library(tuple(leg.request.city_id for leg in request.legs))
    return load_library(request.city_id)
