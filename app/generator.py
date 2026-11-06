"""M01：主题 → 行程。

    request + theme ──► 排程器（确定性）──► 行程骨架（模板文案）──► 质检 ──► 模型补文案（可选）

事实（地点、顺序、时间、预约、官网链接、闭馆）只来自排程器和地点库。模型只允许写两样东西：每天的标题、
每个地点一句话的看点；而且文案里一旦出现网址、价格、开放时间一律丢弃。模型不可用或返回不合格时，
整份行程照样给出，文案用模板，并在 generation.narration_note 里写明原因。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jinja2 import Template
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.cities import City
from app.daytrips import crosses_border
from app.geo import haversine_km
from app.library import Category, Place, PlaceLibrary, Ticketing, load_library
from app.llm import LLMClient, LLMError
from app.llm_trace import trace_context
from app.names import bilingual, item_location
from app.scheduler import DayPlan, ScheduleDraft, Stop, schedule
from app.schemas import (
    BookingItem,
    DateRange,
    Day,
    GenerationInfo,
    ItemType,
    Itinerary,
    ItineraryItem,
    TransportMode,
    TravelerProfile,
    TravelRequest,
    Trip,
)
from app.user_places import UNCHECKED_NOTE
from app.validator import GROUND_MODES, ValidationReport, validate_itinerary

# 进度步骤名固定，前端按它显示文案。
STEPS = ("library", "schedule", "validate", "narrate")

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_SYSTEM = (_PROMPT_DIR / "narrate_system.txt").read_text(encoding="utf-8")
_USER = Template((_PROMPT_DIR / "narrate_user.jinja").read_text(encoding="utf-8"))

_MAX_THEME_CHARS = 40
_MAX_BLURB_CHARS = 60
# 文案里不许出现的事实类内容：网址、价格、具体时间、开放 / 闭馆表述。
_FORBIDDEN = re.compile(r"(https?://|www\.|\.com|\.fr|€|欧元|\d{1,2}[:：]\d{2}|闭馆|休馆|营业|开放时间|门票)", re.I)

_TYPE_BY_CATEGORY = {
    Category.museum: ItemType.museum,
    Category.sight: ItemType.sight,
    Category.park: ItemType.sight,
    Category.shopping: ItemType.activity,
    Category.entertainment: ItemType.activity,
    Category.activity: ItemType.activity,
    Category.food: ItemType.food,
}


class DayNarration(BaseModel):
    model_config = ConfigDict(extra="forbid")      # 格式对不上（比如旧样例行程）就当作不合格，降级为模板

    date: date
    theme: str = ""
    notes: dict[str, str] = Field(default_factory=dict)


class Narration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: list[DayNarration]


@dataclass
class GeneratedTrip:
    itinerary: Itinerary
    draft: ScheduleDraft
    report: ValidationReport


# --------------------------------------------------------------------------- #
# 骨架
# --------------------------------------------------------------------------- #
def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _edge_mode(stop: Stop, request: TravelRequest) -> TransportMode:
    """抵达 / 离开这一段用的交通方式；没写交通（老行程、脚本）按飞机。"""
    t = request.transport
    if t is None:
        return TransportMode.flight
    return (t.inbound if stop.edge == "leave" else t.outbound).mode


def _edge_hub(mode: TransportMode) -> str:
    """接驳连接的是机场还是车站；自驾 / 其他不点名。"""
    return "机场" if mode == TransportMode.flight else "车站" if mode in GROUND_MODES else ""


def _transport(stop: Stop, first_of_day: bool, request: TravelRequest, city: City | None = None) -> str:
    if stop.kind == "meal":
        return "就近步行"
    city = city or request.city
    if stop.kind == "logistics":
        if request.day_trip_from:
            return "车站与景点之间：步行 / 当地公交，自行选择"
        hub = _edge_hub(_edge_mode(stop, request))
        if _edge_mode(stop, request) == TransportMode.drive:
            return "与住宿之间：自驾，到住宿附近停车"
        if not hub:
            return "与住宿之间：公共交通 / 打车，自行选择"
        how = "步行 / 公交 / 地铁 / 打车"
        if hub == "机场":                   # 巴黎的机场线是 RER；别的城市不借用它的线路名
            how = "RER / 地铁 / 打车" if city.tier == "full" else "公共交通 / 打车"
        return f"{hub}与住宿之间：{how}，自行选择"
    where = "从住宿出发" if first_of_day else "从上一站出发"
    mode = city.transit_word(stop.travel_mode or "")
    return f"{where}，{mode}约 {stop.travel_min} 分钟（估算）"


def _place_notes(p: Place, stop: Stop, day: date | None = None) -> str:
    """条目备注只放「这次行程特有」的提醒；地点自身的说明和看点页面 / 导出里从地点库单独展示，不在这里重复。"""
    parts: list[str] = []
    if stop.meal:
        parts.append("这一站就是" + {"lunch": "午餐", "tea": "下午茶"}[stop.meal])
    if p.booking.required:
        parts.append("需提前订票" + (f"：{p.booking.lead_time_hint}" if p.booking.lead_time_hint else ""))
    if stop.warning:
        parts.append(stop.warning)
    if day and (when := p.note_for(day)):
        parts.append(when)                           # 官网写明的那天提前闭馆 / 改时间
    elif day and not p.hours_verified and day.month == 12 and day.day in (24, 25, 26):
        parts.append("圣诞假期开不开没有核对，出发前请到官网确认")        # 不替官网下断言，也不假装它开着
    if p.draft_source and p.draft_source.amap:
        parts.append(UNCHECKED_NOTE)                 # 高德搜来的点：营业时间没有任何来源，别让人当成核对过的
    return "；".join(parts)


def _drive_note(stop: Stop, request: TravelRequest) -> str:
    """自驾且写了出发地 / 回到哪里：写大致路程和时间（按直线距离估算，不是导航）。"""
    t = request.transport
    if t is None or stop.edge not in ("arrive", "leave"):
        return ""
    leg = t.inbound if stop.edge == "leave" else t.outbound
    if leg.mode != TransportMode.drive or not leg.drive_km:
        return ""
    far = leg.to_place if stop.edge == "leave" else leg.from_place
    if not far:
        return ""
    hours, mins = divmod(leg.drive_min or 0, 60)
    took = f"{hours} 小时 {mins} 分钟" if hours and mins else f"{hours} 小时" if hours else f"{mins} 分钟"
    route = f"{request.city.name_zh}到{far}" if stop.edge == "leave" else f"{far}到{request.city.name_zh}"
    return f"自驾{route}：约 {leg.drive_km} 公里、约 {took}（按直线距离估算，实际以导航为准）"


def _item(stop: Stop, lib: PlaceLibrary, first_of_day: bool, request: TravelRequest,
          day: date | None = None) -> ItineraryItem:
    base = dict(time=_hhmm(stop.start), title=stop.title, duration_minutes=stop.duration,
                transport=_transport(stop, first_of_day, request), booking_required=False)
    if stop.kind == "meal":
        return ItineraryItem(**base, type=ItemType.meal, location="就近",
                             notes=LUNCH_PLACEHOLDER)
    if stop.kind == "logistics":
        if request.day_trip_from:
            notes = "时间按时刻表推算，请以实际车次为准；末班返程请提前在官方渠道确认"
            if request.day_trip_origin and request.day_trip_origin.ride_note:
                notes = f"{request.day_trip_origin.ride_note}。{notes}"
            if crosses_border(request):
                notes = "出境：带好护照（或居留卡）原件。" + notes
            return ItineraryItem(**base, type=ItemType.logistics, location="火车站", notes=notes)
        where = f"住宿（{request.hotel_area}）" if request.hotel_area else "住宿"
        hub = _edge_hub(_edge_mode(stop, request))
        notes = {"车站": "时间为按火车 / 汽车时间推算的缓冲，请以实际车次为准",
                 "机场": "时间为按航班时间推算的缓冲，请以实际航班为准"}.get(hub, "时间为推算的缓冲，请以实际时间为准")
        if (drive := _drive_note(stop, request)):
            notes = f"{drive}。{notes}"
        return ItineraryItem(**base, type=ItemType.logistics, location=where, notes=notes)
    p = lib.get_place(stop.place_id or "")
    return ItineraryItem(
        **{**base, "booking_required": p.booking.required},
        type=_TYPE_BY_CATEGORY[p.category], location=item_location(p, request.city.local_language),
        notes=_place_notes(p, stop, day), place_id=p.id,
    )


LUNCH_PLACEHOLDER = "排程占位：具体餐厅请自行选择，热门餐厅记得提前订位"
LUNCH_HINT_KM = 1.2           # 午餐点离前后两个景点最近的一个不超过这个距离才推荐（约 15 分钟步行）
LUNCH_HINT_MAX = 2


def _hours_text(p: Place) -> str:
    h = p.typical_hours
    if not (p.hours_verified and h):
        return "开放时间未核对"
    return f"{h.open}–{'24:00' if h.close == '23:59' else h.close}"


def add_lunch_hints(days: list[Day], lib: PlaceLibrary) -> list[Day]:
    """「午餐（自行选餐厅）」那一行的备注：前后景点附近有核对过的午餐店就写出来（规格 09 十七之五）。

    只改还是占位说明（或上次写的建议）的行，可重复调用；简餐不推荐；当天闭馆、行程里已经排了的店不推荐。
    """
    taken = {i.place_id for d in days for i in d.items if i.place_id}
    cands = [p for p in lib.places.values() if p.meal == "lunch" and not p.long_term_closed]
    for day in days:
        for k, item in enumerate(day.items):
            if item.type != ItemType.meal or not item.title.startswith("午餐（自行"):
                continue
            if not (item.notes == LUNCH_PLACEHOLDER or item.notes.startswith("附近可以去：")):
                continue
            around = day.items[max(k - 1, 0):k] + day.items[k + 1:k + 2]       # 紧挨着的前一项和后一项
            near = [lib.places[i.place_id] for i in around if i.place_id in lib.places]
            found: list[tuple[float, Place]] = []
            for p in cands:
                if p.id in taken or p.closed_reason(day.date) is not None:
                    continue
                dist = min((haversine_km((q.lat, q.lng), (p.lat, p.lng)) for q in near), default=None)
                if dist is not None and dist <= LUNCH_HINT_KM:
                    found.append((dist, p))
            found.sort(key=lambda t: t[0])
            if not found:
                item.notes = LUNCH_PLACEHOLDER
                continue
            shown = "；".join(
                f"{bilingual(p)}（约 {round(d * 1000, -1):.0f} 米，{_hours_text(p)}"
                + (f"，{p.booking.lead_time_hint}" if p.booking.lead_time_hint else "") + "）"
                for d, p in found[:LUNCH_HINT_MAX])
            item.notes = f"附近可以去：{shown}。也可以自己选餐厅，热门餐厅记得提前订位"
    return days


def free_day_item() -> ItineraryItem:
    """库里的点用完了、这天一个都排不进时的占位（生成和手动编辑共用）。"""
    return ItineraryItem(time="09:00", title="自由安排", type=ItemType.rest, location="自由安排", duration_minutes=60,
                         transport=None, notes="这一天目前没有安排，想去哪可以再加")


def _template_theme(day: DayPlan, theme_name: str) -> str:
    names = [s.title for s in day.stops if s.kind == "place"]
    kinds = {s.kind for s in day.stops}
    if not names:
        if any("抵达" in s.title for s in day.stops):
            return "抵达日 · 入住与休整"
        if any("机场" in s.title for s in day.stops):
            return "返程日"
        return "自由安排"
    shown = " + ".join(names[:2]) + ("等" if len(names) > 2 else "")
    return f"{theme_name} · {shown}" if "logistics" not in kinds else f"{shown}（含接驳）"


def _ticket_advice(p: Place, paper_tickets: bool) -> str:
    """购票建议（规格 M06）：有线上渠道就推荐线上购票，除非游客想收集纸质票根。"""
    if p.ticketing == Ticketing.online_required:
        return "只能线上购票，纸质票根不一定能拿到" if paper_tickets else "必须提前线上购票"
    if paper_tickets:
        return "想收集纸质票根：到现场窗口排队购买即可；想省排队时间也可线上购票"
    return "建议线上购票；现场也可排队购买（旺季可能排长队或售罄）"


def build_checklist(places: list[Place], paper_tickets: bool = False) -> list[BookingItem]:
    """行程里所有有线上购票渠道的地点（按出现顺序给，同一个地点只列一次）；「必须线上」排在前面。

    排程生成和手动编辑共用：购票信息永远从地点库重算，不信前端传来的内容。
    """
    out: list[BookingItem] = []
    seen: set[str] = set()
    for p in places:
        if p.id in seen or p.ticketing == Ticketing.none:
            continue
        seen.add(p.id)
        out.append(BookingItem(
            title=p.name_zh, reason=_ticket_advice(p, paper_tickets),
            deadline_hint=p.booking.lead_time_hint, url=p.public_url, place_id=p.id,
            must_online=p.ticketing == Ticketing.online_required, note=p.ticket_note,
            url_status=p.url_status.value if p.url_status else None, verified_at=p.verified_at,
        ))
    return sorted(out, key=lambda b: not b.must_online)      # sorted 是稳定排序


def _checklist(draft: ScheduleDraft, lib: PlaceLibrary, paper_tickets: bool = False) -> list[BookingItem]:
    places = [lib.get_place(s.place_id or "") for day in draft.days for s in day.stops if s.kind == "place"]
    return build_checklist(places, paper_tickets)


def build_risk_notes(placed: list[Place], extra: list[str] | None = None) -> list[str]:
    notes = ["各点之间的通勤时间是按直线距离估算的，实际以地图导航为准。",
             "购票方式与规则会变化，出发前请以各景点官网为准；现场排队多久、热门景点现场有没有票，这里还没有数据。"]
    if any(not p.hours_verified for p in placed):
        notes.append("部分地点的闭馆日 / 开放时间尚未自动核对，出发前请以官网为准。")
    if any(p.zone.value == "day_trip" for p in placed):
        notes.append("郊区一日游往返耗时长，建议提前确认交通方式和末班车时间。")
    notes.extend(extra or [])
    return notes


CHRISTMAS_NOTE = ("行程包含 12 月 24–26 日圣诞假期：很多餐厅休息或需要提前订位，景点也可能提前关门或临时调整开放时间，"
                  "出发前请逐个到官网确认。")


def _risk_notes(draft: ScheduleDraft, lib: PlaceLibrary) -> list[str]:
    placed = [lib.get_place(s.place_id) for d in draft.days for s in d.stops if s.kind == "place"]
    extra = list(draft.notes)
    if any(d.date.month == 12 and d.date.day in (24, 25, 26) for d in draft.days):
        extra.append(CHRISTMAS_NOTE)
    return build_risk_notes(placed, extra)


def _skeleton(request: TravelRequest, draft: ScheduleDraft, lib: PlaceLibrary, theme_name: str) -> Itinerary:
    days = []
    for plan in draft.days:
        items = [_item(s, lib, idx == 0, request, plan.date) for idx, s in enumerate(plan.stops)] or [free_day_item()]
        days.append(Day(date=plan.date, theme=_template_theme(plan, theme_name), items=items))
    add_lunch_hints(days, lib)
    return Itinerary(
        trip=Trip(
            destination=request.destination,
            date_range=DateRange(start=request.start_date, end=request.end_date),
            traveler_profile=TravelerProfile(style=request.travel_style, budget=request.budget_level,
                                             interests=list(request.interests)),
        ),
        days=days,
        booking_checklist=_checklist(draft, lib, request.paper_tickets),
        risk_notes=_risk_notes(draft, lib),
        generation=GenerationInfo(
            theme_id=draft.theme_id, theme_name=theme_name, themes=draft.theme_ids or [draft.theme_id],
            narration="template",
            narration_note="未启用模型，说明文字由模板生成", notes=list(draft.notes),
            day_reasons={d.date.isoformat(): d.reasons for d in draft.days if d.reasons},
        ),
    )


# --------------------------------------------------------------------------- #
# 模型补文案
# --------------------------------------------------------------------------- #
def _clean(text: str, limit: int) -> str | None:
    text = " ".join(text.split())
    if not text or len(text) > limit or _FORBIDDEN.search(text):
        return None
    return text


def _narrate(itinerary: Itinerary, lib: PlaceLibrary, client: LLMClient) -> tuple[Itinerary, str | None]:
    """返回 (可能被补了文案的行程, 降级原因)。降级原因为 None 表示用上了模型文案。"""
    system, user = _SYSTEM, _USER.render(
        theme=itinerary.generation.theme_name,
        days=[{
            "date": d.date.isoformat(), "theme": d.theme,
            "places": [{"id": i.place_id, "name": i.title, "highlights": lib.get_place(i.place_id).highlights[:3]}
                       for i in d.items if i.place_id],
        } for d in itinerary.days],
    )
    try:
        with trace_context(purpose="narration"):
            raw = client.complete_json(system=system, user=user, schema=Narration.model_json_schema())
        narration = Narration.model_validate_json(raw)
    except LLMError:
        return itinerary, "模型不可用，说明文字由模板生成"
    except (ValidationError, json.JSONDecodeError):
        return itinerary, "模型返回的内容无法解析，说明文字由模板生成"

    by_date = {n.date: n for n in narration.days}
    used = 0
    out = itinerary.model_copy(deep=True)
    for day in out.days:
        n = by_date.get(day.date)
        if n is None:
            continue
        if (theme := _clean(n.theme, _MAX_THEME_CHARS)):
            day.theme = theme
            used += 1
        for item in day.items:
            blurb = _clean(n.notes.get(item.place_id or "", ""), _MAX_BLURB_CHARS)
            if blurb:
                item.notes = f"{item.notes}；{blurb}" if item.notes else blurb
                used += 1
    if used == 0:
        return itinerary, "模型没有返回可用的文案，说明文字由模板生成"
    out.generation.narration, out.generation.narration_note = "llm", None
    return out, None


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def generate_from_theme(
    request: TravelRequest,
    theme_id: str,
    *,
    client: LLMClient | None,
    library: PlaceLibrary | None = None,
    on_step: Callable[[str], None] | None = None,
) -> GeneratedTrip:
    if request.route:                          # 深度游学路线（规格 09）：按路线的章节排，不按主题挑点
        from app.route_generator import generate_route_trip

        return generate_route_trip(request, client=client, library=library, on_step=on_step)
    step = on_step or (lambda _name: None)

    step("library")
    lib = library or load_library(request.city_id)
    theme = lib.get_theme(theme_id)            # 主题不存在 -> KeyError，由调用方转成 4xx

    step("schedule")
    draft = schedule(request, theme_id, lib)
    itinerary = _skeleton(request, draft, lib, draft.theme_name or theme.name_zh)

    step("validate")
    report = validate_itinerary(itinerary, request)

    step("narrate")
    if client is not None:
        itinerary, why = _narrate(itinerary, lib, client)
        if why:
            itinerary.generation.narration_note = why
    return GeneratedTrip(itinerary=itinerary, draft=draft, report=report)
