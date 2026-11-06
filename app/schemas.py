"""数据契约：用户输入 (TravelRequest) 与行程输出 (Itinerary)。

对应 specs/03-data-model.md 第 4 节（已落地的实体，以这里的 Pydantic 模型为准）。
所有跨字段校验放在这里，让 intake / planner / validator 拿到的对象一定是结构合法的。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.cities import City, city_id_of, city_of, load_city, supported_cities
from app.notes.models import MAX_NOTE_ITEMS, NoteSource, PendingPlace

# 建议的行程天数来自城市配置（data/cities/<id>.json 的 suggested_days）；超出只提示、不拒绝。


class BudgetLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class Currency(StrEnum):
    CNY = "CNY"
    EUR = "EUR"


class Residence(StrEnum):
    none = "none"      # 不长居欧洲（普通游客）
    fr = "fr"          # 长居法国（留学生、工作居留等）
    eu = "eu"          # 长居其他欧盟 / 欧洲经济区国家


class Traveler(BaseModel):
    """一位出行人（规格 M09 S1b、07 P09）：姓名（选填，随行程保存）、年龄、长居身份、是否学生，用来逐人算优惠票。
    姓名和年龄一样不进分享页 / 长图 / PDF / 导出。"""

    name: str | None = Field(default=None, max_length=20, exclude_if=lambda v: v is None)
    age: int = Field(default=30, ge=0, le=120)
    residence: Residence = Residence.none
    student: bool = False

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, v: object) -> object:
        return (v.strip() or None) if isinstance(v, str) else v


class TravelStyle(StrEnum):
    relaxed = "relaxed"
    balanced = "balanced"
    intense = "intense"


class Language(StrEnum):
    zh_cn = "zh-CN"
    en = "en"


MAX_MIXED_THEMES = 12      # 主题不设「最多 3 个」的限制（规格 07 P15）；这里只是防乱传的上限


class ThemeWeight(BaseModel):
    """混选时的一个主题和它的权重（1–5，权重大的主题在行程里占比更大）。"""

    id: str
    weight: int = Field(3, ge=1, le=5)


MAX_STAYS = 10


class TransportMode(StrEnum):
    flight = "flight"                      # 飞机
    high_speed_rail = "high_speed_rail"    # 高铁
    train = "train"                        # 火车
    coach = "coach"                        # 大巴
    drive = "drive"                        # 自驾
    other = "other"


TRANSPORT_ZH = {"flight": "飞机", "high_speed_rail": "高铁", "train": "火车", "coach": "大巴", "drive": "自驾",
                "other": "其他"}


class TransportLeg(BaseModel):
    """来回交通的一段（规格 07 P07 / M06 S6）：方式 + 出发 / 到达时间（当地时间），
    选填出发地、目的地、航班号 / 车次。"""

    mode: TransportMode
    depart_at: datetime | None = None
    arrive_at: datetime | None = None
    from_place: str | None = Field(default=None, max_length=40)
    to_place: str | None = Field(default=None, max_length=40)
    number: str | None = Field(default=None, max_length=20)
    # 多城市行程里换城市的交通：用户没填、系统按距离估的（页面标「估算」）
    estimated: bool = Field(default=False, exclude_if=lambda v: not v)
    # 自驾且写了出发地 / 回到哪里：系统按直线距离估的大致路程（公里）和时间（分钟）；只是估算，不查路线
    drive_km: int | None = Field(default=None, ge=0, le=20000, exclude_if=lambda v: v is None)
    drive_min: int | None = Field(default=None, ge=0, le=24 * 60 * 14, exclude_if=lambda v: v is None)

    @field_validator("from_place", "to_place", "number", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return (v.strip() or None) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _order(self) -> TransportLeg:
        if self.depart_at and self.arrive_at and self.arrive_at <= self.depart_at:
            raise ValueError("到达时间要晚于出发时间")
        return self


class DayTripOrigin(BaseModel):
    """周边一日游的出发地（规格 08 3.7）：用户填的住址在前端和后端查询时才用，**行程里不存街道和门牌**，
    只存城市名、国家、最近的车站（公共信息）和这次乘车的说明（乘什么车、要不要另买票）。"""

    name_local: str = Field(min_length=1, max_length=60)        # 城市的当地写法：Stuttgart
    name_zh: str | None = Field(default=None, max_length=30)    # 中文名；不知道就没有，页面只写当地名
    country: str = Field(min_length=2, max_length=2)
    station: str = Field(min_length=1, max_length=40)           # 上车的车站：Stuttgart Hauptbahnhof
    station_lat: float = Field(ge=-90, le=90)
    station_lng: float = Field(ge=-180, le=180)
    ride_note: str | None = Field(default=None, max_length=160)

    @field_validator("name_zh", "ride_note", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return (v.strip() or None) if isinstance(v, str) else v


class Transport(BaseModel):
    """来回交通：去程必须有出发 + 到达时间，返程必须有出发时间（规格 07 P07：交通是必填项）。"""

    outbound: TransportLeg
    inbound: TransportLeg

    @model_validator(mode="after")
    def _required(self) -> Transport:
        if self.outbound.depart_at is None or self.outbound.arrive_at is None:
            raise ValueError("去程要填出发时间和到达时间")
        if self.inbound.depart_at is None:
            raise ValueError("返程要填出发时间")
        return self


def normalize_area(text: str) -> str:
    """「9区」「9th arrondissement」「9e arr.」「第9区」都归一成地点库里的写法「9区」；认不出的原样返回。"""
    m = re.search(r"(\d{1,2})\s*(?:区|arr|er\b|e\b|ème|eme|st\b|nd\b|rd\b|th\b)", text.strip(), re.I)
    return f"{int(m.group(1))}区" if m else text.strip()


class Stay(BaseModel):
    """一家住宿（规格 M06 S5）：用户自己填的，不是策展数据。入住晚、退房早上；每晚价格给预算用。"""

    name: str = Field(min_length=1, max_length=80)
    check_in: date
    check_out: date
    area: str | None = Field(default=None, max_length=40)        # 「9区」；没有精确坐标时用它的中心
    address: str | None = Field(default=None, max_length=200)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    nightly_eur: float | None = Field(default=None, ge=0, le=100_000)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("area", "address", "note", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return (v.strip() or None) if isinstance(v, str) else v

    @field_validator("area")
    @classmethod
    def _area(cls, v: str | None) -> str | None:
        return normalize_area(v) if v else v

    @model_validator(mode="after")
    def _check(self) -> Stay:
        if self.check_out <= self.check_in:
            raise ValueError("退房日期必须晚于入住日期")
        if (self.lat is None) != (self.lng is None):
            raise ValueError("经纬度要么都填、要么都不填")
        if not (self.area or self.address or self.lat is not None):
            raise ValueError("请填所在区或地址（至少一个），否则没法算每天的路线")
        return self

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #
class TravelRequest(BaseModel):
    """用户提交的旅行需求（已通过结构校验）。"""

    destination: str
    start_date: date
    end_date: date
    budget_level: BudgetLevel
    travel_style: TravelStyle
    interests: list[str] = Field(min_length=1)

    arrival_time: datetime | None = None
    departure_time: datetime | None = None
    # 来回交通（规格 07 P07）：新建页必填；给了就由它决定到达 / 离开时间。老行程和脚本可以不给。
    transport: Transport | None = Field(default=None, exclude_if=lambda v: v is None)
    # 周边一日游（规格 08 3.4）：从哪个出发地当天往返。有它就是「一天去一座城市再回来」：到达 / 离开按火车站算，
    # 没有机场那种几小时缓冲，也不当成晚到 / 早走日。老行程没有这个字段。
    day_trip_from: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # 出发地是用户填的住址时（规格 08 3.7）：day_trip_from 写 "address"，出发地的城市 / 车站 / 乘车说明放这里；
    # 预设的斯图加特 / 亚琛（离线时刻表）没有这个字段
    day_trip_origin: DayTripOrigin | None = Field(default=None, exclude_if=lambda v: v is None)
    # 深度游学路线（规格 09）：选了路线就按路线的章节排（theme 仍写路线对应的主题）。老行程没有这个字段。
    route: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # 路线里你勾选要去的站；None = 全部
    route_stops: list[str] | None = Field(default=None, exclude_if=lambda v: v is None)
    # 多城市行程里整趟自驾（去程或返程是自驾）：每一段城市里的移动都按开车算，即使这一段的交通是别的
    car_in_city: bool = Field(default=False, exclude_if=lambda v: not v)
    hotel_area: str | None = None
    restrictions: list[str] = Field(default_factory=list)
    must_go: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    language: Language = Language.zh_cn
    # True: 游客想收集纸质票根 → 能现场买的地点建议现场窗口购票，其余仍推荐线上购票（规格 M06 S1c）
    paper_tickets: bool = False

    # 候选池（规格 M02 S5）：按地点 id 精确指定，和上面按名字写的 must_go / avoid 并存
    themes: list[ThemeWeight] = Field(default_factory=list)       # 混选；空 = 只用生成时选的那一个主题
    must_go_ids: list[str] = Field(default_factory=list)
    avoid_ids: list[str] = Field(default_factory=list)
    place_weights: dict[str, int] = Field(default_factory=dict)   # 地点 id -> 权重 1–5，覆盖主题里的默认权重

    # 笔记导入（规格 M12 S2 / S3）：地点从哪段笔记来的；库外、没核对过的地点（只显示，不进排程）。随版本保存
    note_sources: list[NoteSource] = Field(default_factory=list, max_length=MAX_NOTE_ITEMS)
    note_pending: list[PendingPlace] = Field(default_factory=list, max_length=MAX_NOTE_ITEMS)

    # 住宿（规格 M06 S5）：每个版本的需求自带，所以平移日期、回滚、复制都会一起带上
    stays: list[Stay] = Field(default_factory=list)

    # 多城市旅行（规格 11 第八节）：一份行程里的几座城市，每座城市一份自己的单城市需求 + 主题。
    # 外层这份需求只记整趟的总信息（destination = 第一座城市，起止 = 整趟，交通 = 第一段去程 + 最后一段返程）。
    # 单城市行程没有这个字段，老行程完全不变。
    legs: list[LegPlan] | None = Field(default=None, exclude_if=lambda v: v is None)

    # 出行人与预算（规格 M01 S4b）：生成时用来算门票等费用；填了预算上限就按预算调整行程
    travelers: list[Traveler] = Field(default_factory=lambda: [Traveler()], min_length=1, max_length=20)
    budget_cap: float | None = Field(default=None, ge=0, le=10_000_000)      # 单位 = currency
    currency: Currency = Currency.CNY                                        # 结算货币：预算上限和费用以它为主

    # 非用户输入，只是把提示信息挂在对象上，方便后续步骤读取。
    notes: list[str] = Field(default_factory=list, exclude=True)

    @field_validator("themes")
    @classmethod
    def _themes_ok(cls, v: list[ThemeWeight]) -> list[ThemeWeight]:
        if len(v) > MAX_MIXED_THEMES:
            raise ValueError(f"最多混选 {MAX_MIXED_THEMES} 个主题")
        if len({t.id for t in v}) != len(v):
            raise ValueError("混选的主题不能重复")
        return v

    @field_validator("place_weights")
    @classmethod
    def _weights_ok(cls, v: dict[str, int]) -> dict[str, int]:
        if any(not 1 <= w <= 5 for w in v.values()):
            raise ValueError("地点权重必须在 1–5 之间")
        return v

    @field_validator("must_go_ids", "avoid_ids", mode="before")
    @classmethod
    def _dedupe_ids(cls, v: object) -> object:
        if v is None:
            return []
        return list(dict.fromkeys(v)) if isinstance(v, list) else v

    @field_validator("destination")
    @classmethod
    def _known_destination(cls, v: str) -> str:
        if city_of(v) is None:
            raise ValueError(f"目前只支持：{', '.join(sorted(c.id for c in supported_cities()))}（收到 {v!r}）")
        return v.strip()

    @property
    def city_id(self) -> str:
        """这份需求的城市 id（按 destination 认：「Paris」「巴黎」→ paris）；数据、时区、币种都按它取。"""
        return city_id_of(self.destination)

    @property
    def city(self) -> City:
        """这份需求的城市配置（时区、中心点、币种……）。destination 在校验时已经确认认得出。"""
        return load_city(self.city_id)

    @field_validator("interests", "restrictions", "must_go", "avoid", mode="before")
    @classmethod
    def _clean_list(cls, v: object) -> object:
        """去空白、去空项、去重（保序）。允许传单个字符串。"""
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        seen: list[str] = []
        for item in v:
            s = str(item).strip()
            if s and s not in seen:
                seen.append(s)
        return seen

    @model_validator(mode="after")
    def _check_dates(self) -> TravelRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        if self.day_trip_from is not None:
            from app.daytrips import origin_ids  # 延迟导入：daytrips 也要用本模块

            if self.day_trip_origin is None and self.day_trip_from not in origin_ids():
                raise ValueError(f"没有这个出发地：{self.day_trip_from}（现在支持：{'、'.join(origin_ids())}）")
            if self.end_date != self.start_date:
                raise ValueError("周边一日游只能是当天往返（开始和离开是同一天）")

        if self.route is not None:
            from app.routes import load_routes  # 延迟导入：routes 要读地点库

            known = load_routes(self.city_id)
            if self.route not in known:
                raise ValueError(f"没有这条路线：{self.route}（可选：{'、'.join(known) or '无'}）")
            if self.route_stops is not None:
                valid = set(known[self.route].place_ids())
                if not self.route_stops or any(p not in valid for p in self.route_stops):
                    raise ValueError("route_stops 要至少有一站，且都是这条路线上的站")
        elif self.route_stops is not None:
            raise ValueError("route_stops 只能和 route 一起用")

        if self.legs is not None:                      # 多城市：每一段自己的需求已经校验过（天数建议、路线等）
            self._check_legs()
            self._apply_transport()
            self._check_stays()
            return self
        days = self.trip_days
        advice = self.city.suggested_days
        if not advice.min <= days <= advice.max:
            tail = ""
            if advice.fill_max is not None and days > advice.fill_max:
                tail = f"{self.city.name_zh}的点最多排满约 {advice.fill_max} 天，多出来的天会留成自由日。"
            elif days > advice.quality_max:
                tail = f"超过 {advice.quality_max} 天主题里的点不够，后面几天会排其他主题的代表性景点。"
            self.notes.append(f"行程天数 {days}，{self.city.name_zh}建议 {advice.min}-{advice.max} 天。{tail}")
        self._apply_transport()

        if self.arrival_time and self.arrival_time.date() != self.start_date:
            self.notes.append(
                "arrival_time 的日期和 start_date 不一致，已按 arrival_time 为准处理抵达日。"
            )
        if self.departure_time and self.departure_time.date() != self.end_date:
            self.notes.append(
                "departure_time 的日期和 end_date 不一致，已按 departure_time 为准处理离开日。"
            )
        self._check_stays()
        return self

    def _check_legs(self) -> None:
        legs = self.legs or []
        if len(legs) < 2:
            raise ValueError("多城市行程至少要 2 座城市")
        if legs[0].request.start_date != self.start_date or legs[-1].request.end_date != self.end_date:
            raise ValueError("整趟的起止日期要等于第一座城市的开始日期和最后一座城市的离开日期")
        for a, b in zip(legs, legs[1:], strict=False):
            if (b.request.start_date - a.request.end_date).days not in (0, 1):
                raise ValueError("下一座城市要在上一座城市离开的当天或第二天开始")

    @property
    def is_multi(self) -> bool:
        return self.legs is not None

    @property
    def self_drive(self) -> bool:
        """去程或返程选了自驾：城市里的移动也按开车算。"""
        tr = self.transport
        return self.car_in_city or tr is not None and TransportMode.drive in (tr.outbound.mode, tr.inbound.mode)

    def _apply_transport(self) -> None:
        """有来回交通就用它定到达 / 离开时间：去程到达要在开始日期、返程出发要在离开日期。"""
        tr = self.transport
        if tr is None:
            return
        arrive, leave = tr.outbound.arrive_at, tr.inbound.depart_at
        if arrive.date() != self.start_date:
            raise ValueError(f"去程到达时间（{arrive:%Y-%m-%d %H:%M}）要在开始日期 {self.start_date} 当天")
        if leave.date() != self.end_date:
            raise ValueError(f"返程出发时间（{leave:%Y-%m-%d %H:%M}）要在离开日期 {self.end_date} 当天")
        self.arrival_time, self.departure_time = arrive, leave

    def _check_stays(self) -> None:
        if len(self.stays) > MAX_STAYS:
            raise ValueError(f"住宿最多 {MAX_STAYS} 家")
        self.stays = sorted(self.stays, key=lambda s: s.check_in)
        for s in self.stays:
            if s.check_in < self.start_date or s.check_out > self.end_date:
                raise ValueError(
                    f"住宿「{s.name}」的日期（{s.check_in} 到 {s.check_out}）不在行程日期范围内"
                    f"（{self.start_date} 到 {self.end_date}）")
        for a, b in zip(self.stays, self.stays[1:], strict=False):
            if b.check_in < a.check_out:
                raise ValueError(f"住宿「{a.name}」和「{b.name}」的日期重叠")

    def hotel_for(self, day: date) -> Stay | None:
        """这一天路线的起终点用哪家住宿：当晚住的那家；退房当天（没有当晚）用刚退的那家；没住宿的日子返回 None。"""
        for s in self.stays:
            if s.check_in <= day < s.check_out:
                return s
        for s in self.stays:
            if s.check_out == day:
                return s
        return None

    @property
    def trip_days(self) -> int:
        """含首尾两天的自然日数量。"""
        return (self.end_date - self.start_date).days + 1


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
class DateRange(BaseModel):
    start: date
    end: date


class TravelerProfile(BaseModel):
    style: TravelStyle
    budget: BudgetLevel
    interests: list[str]


class Trip(BaseModel):
    destination: str
    date_range: DateRange
    traveler_profile: TravelerProfile


class ItemType(StrEnum):
    sight = "sight"
    museum = "museum"
    food = "food"
    meal = "meal"              # 排程器放的午餐 / 晚餐占位（具体餐厅用户自己选），不计入当日活动强度
    logistics = "logistics"
    activity = "activity"
    rest = "rest"


# LLM 经常自造类型词（cafe / landmark / gallery…）。这里做一层归一，
# 认不出来的一律归为 activity，避免因为一个枚举值让整份行程作废。
_TYPE_SYNONYMS: dict[str, ItemType] = {
    "cafe": ItemType.food, "coffee": ItemType.food, "restaurant": ItemType.food,
    "meal": ItemType.food, "lunch": ItemType.food, "dinner": ItemType.food,
    "breakfast": ItemType.food, "brunch": ItemType.food, "bar": ItemType.food,
    "landmark": ItemType.sight, "viewpoint": ItemType.sight, "attraction": ItemType.sight,
    "photo": ItemType.sight, "photography": ItemType.sight, "park": ItemType.sight,
    "church": ItemType.sight, "monument": ItemType.sight, "street": ItemType.sight,
    "gallery": ItemType.museum, "exhibition": ItemType.museum, "exhibit": ItemType.museum,
    "transport": ItemType.logistics, "transfer": ItemType.logistics, "checkin": ItemType.logistics,
    "check-in": ItemType.logistics, "hotel": ItemType.logistics, "airport": ItemType.logistics,
    "walk": ItemType.activity, "tour": ItemType.activity, "shopping": ItemType.activity,
    "cruise": ItemType.activity, "show": ItemType.activity,
    "break": ItemType.rest, "leisure": ItemType.rest, "free": ItemType.rest,
}


class ItineraryItem(BaseModel):
    time: str  # "HH:MM"
    title: str
    type: ItemType
    location: str
    duration_minutes: int = Field(ge=0)
    transport: str | None = None
    booking_required: bool = False
    notes: str | None = None
    place_id: str | None = None     # 来自地点库的点才有；质检、地图、官网链接都靠它对上地点库
    locked: bool = False            # 用户锁定：重新排程时位置和开始时间不变（规格 M04 S5）
    # 深度路线：这一站看什么（策展数据，带来源；不是模型写的）。普通行程没有。
    look_for: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    # 多城市行程里这一项在哪座城市；单城市行程没有
    city_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # 深度路线：这一站为什么在这条线上（策展数据，一句话；显示在这一站自己下面，不再堆在一天的导览里）。
    why_here: str = Field(default="", exclude_if=lambda v: not v)

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        key = v.strip().lower()
        if key in {t.value for t in ItemType}:
            return key
        return _TYPE_SYNONYMS.get(key, ItemType.activity)


class Day(BaseModel):
    date: date
    theme: str
    items: list[ItineraryItem] = Field(min_length=1)
    # 深度路线：这一天讲的章节（「第 1 章 · 王室收藏与私人藏家」）和一段导览；普通行程没有
    chapter: str | None = Field(default=None, exclude_if=lambda v: v is None)
    guide: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # 多城市行程：这一天主要在哪座城市（换城市那天 = 出发的城市）；换城市那天上午 / 下午各一座城市，
    # 各自的主题记在 theme_by_city（拆回每座城市时用）。单城市行程没有这两项。
    city_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    theme_by_city: dict[str, str] = Field(default_factory=dict, exclude_if=lambda v: not v)


class BookingItem(BaseModel):
    title: str
    reason: str
    deadline_hint: str | None = None
    url: str | None = None
    place_id: str | None = None
    must_online: bool = True                                    # True: 必须提前线上购票；False: 线上购票，现场也可排队
    note: str | None = None                                     # 该地点的购票经验说明（来自地点库）
    url_status: Literal["ok", "bot_protected"] | None = None    # 官网链接的自动核对结果
    verified_at: date | None = None                             # 最近一次自动核对的日期


class RouteReading(BaseModel):
    title: str
    kind: Literal["book", "film"]
    creator: str
    year: int
    note: str
    source_url: str


class RouteInfo(BaseModel):
    """这份行程跟的是哪条深度路线（规格 09）：页面据此显示路线名、延伸阅读，并判断你改过之后有没有偏离路线。"""

    id: str
    name_zh: str
    thesis: str
    reading: list[RouteReading] = Field(default_factory=list)
    baseline: dict[str, list[str]] = Field(default_factory=dict)    # 生成时每天排的路线站（日期 -> 地点 id，按顺序）
    skipped: list[str] = Field(default_factory=list)                # 没排进来的路线站（地点 id）
    notes: list[str] = Field(default_factory=list)                  # 策展说明（哪些站为什么没收）


class GenerationInfo(BaseModel):
    """这份行程是怎么生成的：页面据此显示「说明由模板生成」「为什么这样排」等。"""

    theme_id: str                                       # 主题（混选时是权重最大的那个）
    theme_name: str                                     # 混选时是「艺术 + 美食」
    themes: list[str] = Field(default_factory=list)     # 所有主题 id，按权重从大到小
    narration: Literal["template", "llm"]
    narration_note: str | None = None                   # 降级为模板的原因
    notes: list[str] = Field(default_factory=list)      # 跨天说明：补充了哪些点、必去排不进……
    day_reasons: dict[str, list[str]] = Field(default_factory=dict)    # 日期 -> 为什么这样排
    travel_estimated: bool = True                       # 通勤时间是否为估算
    route: RouteInfo | None = Field(default=None, exclude_if=lambda v: v is None)    # 深度路线（规格 09）


class LegBlock(BaseModel):
    """多城市行程里每座城市自己的那一份元信息（拆回单城市行程时原样取回）。"""

    city_id: str
    trip: Trip
    generation: GenerationInfo | None = None
    booking_checklist: list[BookingItem] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class Itinerary(BaseModel):
    trip: Trip
    days: list[Day] = Field(min_length=1)
    booking_checklist: list[BookingItem] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    version: str = "v1"
    generation: GenerationInfo | None = None
    legs: list[LegBlock] | None = Field(default=None, exclude_if=lambda v: v is None)   # 多城市行程才有


class LegPlan(BaseModel):
    """多城市行程里的一座城市：它自己的单城市需求（legs 为空）+ 生成它用的主题。"""

    request: TravelRequest
    theme: str


TravelRequest.model_rebuild()
