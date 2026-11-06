"""M6：把纯 Python 链路包成 HTTP 接口。

对应 specs/modules 各板块的接口（/intake /plan /validate /render /revision 等），做成 RESTful 形状：

    GET  /health
    GET  /attractions                     景点白名单（旧，给前端画地图/贴士用）
    GET  /themes                          主题包列表（首页主题选择）
    GET  /places?theme=                   地点库（含官网链接的核对记录）
    POST /intake                          需求 dict -> 追问 or 通过
    POST /plans?repair=bool               创建 plan；body 带 theme 走「排程器 + 地点库」，不带走旧的纯 LLM 流程
    GET  /plans                           plan_id 列表
    GET  /plans/{pid}?version=n           某版本行程 + 质检 + 历史
    POST /plans/{pid}/revisions           {feedback} -> 新版本
    GET  /plans/{pid}/itinerary.md|html|pdf?version=n

启动：uvicorn app.api:app --reload
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote as urlquote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, ValidationError

from app import attachments as attach
from app import booking, driveest, multicity, multiedit
from app.budget import BudgetSettings, estimate_budget, estimate_budget_multi, fit_to_budget, has_budget_data
from app.changelog import load_changelog
from app.cities import (
    DEFAULT_CITY,
    City,
    city_by_wikidata,
    city_id_of,
    city_of,
    load_city,
    needs_rebuild,
    supported_cities,
)
from app.citybuild.jobs import BuildJobs
from app.citybuild.resolve import Candidate, Resolver
from app.companion import (
    CATEGORY_ORDER,
    ISIC_CHECKED,
    ISIC_URL,
    generate_packing,
    generate_packing_multi,
    load_packing,
    suggest_todos,
    todo_summary,
    todo_urgency,
    valid_note_refs,
)
from app.culture import culture_overview, recap, stories_by_place, stories_for_place
from app.daytrips import (
    DAYTRIP_THEME,
    DayTripError,
    build_day_request,
    day_trip_info,
    describe_destination,
    destinations_for,
    home_of,
    load_pairs,
)
from app.daytrips_live import (
    build_live_request,
    list_destinations,
    make_origin,
    offline_destinations,
    zh_city_names,
    zh_name_of,
)
from app.editing import (
    Alternative,
    Edit,
    EditError,
    EditResult,
    RemovePlace,
    Reschedule,
    SetEntryTime,
    SetLock,
    alternatives,
    apply_edit,
    entry_target,
    normalize,
)
from app.exports import render_ics, render_kml
from app.food import food_overview
from app.fx import FxService
from app.generator import generate_from_theme
from app.geo import build_route
from app.geocode import Geocoder, GeoHit, nominatim
from app.intake import IntakeResult, run_intake
from app.intake_nl import MAX_TEXT as NL_MAX_TEXT
from app.intake_nl import Intent, parse_intent
from app.library import _CITY_BBOX, Category, Place, PlaceLibrary, Setting, library_for_request, load_library
from app.llm import NOTE_MODEL_DEFAULT, LLMClient, LLMConfig, LLMError, get_client
from app.llm_trace import LlmCallStore, TracedClient
from app.maps import config as map_config_keys
from app.maps import quota as map_quota
from app.maps.amap import AmapClient
from app.media import hero_path, media_payload, photo_path, pick_hero
from app.names import local_language_of, localize_itinerary
from app.nl_edit import ModelUnavailable, NlEditError, Translation, apply_ops, describe, parse_ops, translate
from app.notes.fetch import FetchError, fetch_page
from app.notes.guard import SiteGuard
from app.notes.models import NoteSource, ParseResult, PendingPlace
from app.notes.service import NoteTooLong, parse_text, parse_url
from app.notes.sites import xhs_cookie
from app.planner import attraction_catalog, generate_itinerary
from app.renderer import html_to_pdf, html_to_png, render_html, render_markdown
from app.revision import auto_repair, diff_itineraries, revise_itinerary
from app.route_generator import deviation as route_deviation
from app.route_generator import refresh_route_display
from app.routes import RouteError, get_route, load_routes, summarize
from app.schemas import Currency, Itinerary, Stay, TransportMode, Traveler, TravelRequest
from app.shifting import ShiftError, new_conflicts, shift_plan, shift_summary
from app.stay import CAVEAT as STAY_CAVEAT
from app.stay import recommend_areas
from app.tasks import Task, TaskCard, TaskNotFound, TaskNotRemovable, TaskRunner, TaskStore
from app.transit import TransitError, Transitous
from app.trips import TripError, TripSpec
from app.user_places import place_dict, save_user_place
from app.validator import ValidationReport, validate_itinerary
from app.versions import PlanNotFound, PlanStore, VersionRecord
from app.weather import WeatherService

ClientFactory = Callable[[], LLMClient]


# --------------------------------------------------------------------------- #
# 响应模型
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    status: str = "ok"
    llm_backend: str


class RevisionChange(BaseModel):
    summary: str
    changed_days: list[str]
    added: list[str]
    removed: list[str]
    applied: list[str] = []         # 自然语言修改：做成了的改动（人话）
    skipped: list[str] = []         # 没做成的操作和原因


class PlanResponse(BaseModel):
    plan_id: str
    version: int
    itinerary: Itinerary
    report: ValidationReport
    repair_rounds: int = 0
    change: RevisionChange | None = None
    conflicts: list[dict[str, Any]] = []        # 平移日期后新出现的闭馆日冲突
    stays: list[Stay] = []
    notes: list[str] = []                       # 保存住宿时的提示（地址没定位到、按区中心估算……）


class PlanDetail(BaseModel):
    plan_id: str
    latest_version: int
    version: int
    itinerary: Itinerary
    report: ValidationReport
    history: list[VersionRecord]
    archived: bool = False
    copied_from: str | None = None
    stays: list[Stay] = []
    note_sources: list[NoteSource] = []         # 笔记导入：哪些地点来自笔记（M12 S3）
    note_pending: list[PendingPlace] = []       # 笔记里提到、地点库里没有的地点（M12 S2）
    city_id: str = DEFAULT_CITY                 # 这份行程在哪座城市：前端按它取地点库（周边一日游的目的地不是巴黎）
    day_trip_from: str | None = Field(          # 周边一日游的出发地；有值时前端不显示住宿卡、预算卡换成说明
        default=None, exclude_if=lambda v: v is None)
    route_status: dict[str, Any] | None = Field(       # 深度路线：{id, name_zh, deviated}
        default=None, exclude_if=lambda v: v is None)
    day_trip_info: dict[str, str] | None = Field(      # {city, origin}：「海德堡 Heidelberg」「斯图加特 Stuttgart」
        default=None, exclude_if=lambda v: v is None)
    stories: dict[str, list[dict[str, Any]]] = Field(  # 地点 → 它出现在哪些作品里（规格 09 十五）；读取时现算
        default_factory=dict, exclude_if=lambda v: not v)
    culture: list[dict[str, Any]] = Field(             # 这趟旅行走过的作品
        default_factory=list, exclude_if=lambda v: not v)
    cities: list[dict[str, Any]] | None = Field(       # 多城市行程：每座城市的 id / 名字 / 起止日期
        default=None, exclude_if=lambda v: v is None)
    transfers: list[dict[str, Any]] | None = Field(    # 多城市行程：城市之间的交通（第 i 项 = 第 i 座 → 第 i+1 座）
        default=None, exclude_if=lambda v: v is None)


class NlIntakeBody(BaseModel):
    """自然语言填表（M01 S10–S13）。today 只给测试用，默认用后端的今天。"""

    text: str | None = None
    today: str | None = None
    city: str = DEFAULT_CITY            # 这句话是给哪座城市的（M17）；默认巴黎


class AttachmentPatch(BaseModel):
    """改附件：改挂靠（target_kind + target_ref），或从回收站恢复（restore）。"""

    target_kind: str | None = None
    target_ref: str | None = None
    restore: bool = False


class ParseBody(BaseModel):
    """笔记导入（M12）：粘贴正文。"""

    text: str | None = None
    url: str | None = None                 # 和 text 二选一
    destination: str = DEFAULT_CITY


class TaskCreated(BaseModel):
    task_id: str
    status: str


class PlanCard(BaseModel):
    """「我的行程」里的一张卡片。"""

    plan_id: str
    title: str
    theme_id: str | None = None
    theme_name: str | None = None
    start_date: date
    end_date: date
    days: int
    latest_version: int
    created_at: str
    errors: int
    warnings: int
    narration: str | None = None        # "template" | "llm"；旧流程生成的行程没有
    archived: bool = False
    copied_from: str | None = None      # 复制自哪份行程
    city_id: str = DEFAULT_CITY
    hero: dict[str, Any] | None = None  # 封面大图（这趟行程里标志性地点的大图 + 署名）；没有就是 null
    # 多城市行程：每座城市 {city_id, name_zh, hero}；封面轮流切换各城市的图。单城市行程没有
    cities: list[dict[str, Any]] | None = Field(default=None, exclude_if=lambda v: v is None)




class AddressBody(BaseModel):
    q: str = Field(min_length=3, max_length=120)


class LiveOriginBody(BaseModel):
    """周边一日游的出发地：城市（显示用）+ 住址坐标（只用来查路线，不保存、不写日志——所以这些接口都用 POST）。"""

    city: str = Field(min_length=1, max_length=60)
    city_zh: str | None = Field(default=None, max_length=30)
    country: str = Field(min_length=2, max_length=2)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class LiveSearchBody(LiveOriginBody):
    day: date = Field(alias="date")
    depart: str = "09:00"
    dticket: bool = False
    origin: str | None = None                 # 预设出发地（斯图加特 / 亚琛）：时刻表服务连不上时回退到存好的时刻表


class LiveRequestBody(LiveOriginBody):
    destination: str
    day: date = Field(alias="date")
    depart: str = "09:00"
    back: str | None = None
    kind: Literal["fastest", "regional"] = "fastest"


class ResolveBody(BaseModel):
    text: str = Field(min_length=1, max_length=80)


class BuildBody(BaseModel):
    qid: str = Field(pattern=r"^Q\d+$")
    query: str = Field(default="", max_length=80)


class EditBody(BaseModel):
    itinerary: Itinerary          # 前端当前正在编辑的行程（还没存成版本）
    edit: Edit


class EditResponse(BaseModel):
    itinerary: Itinerary
    report: ValidationReport
    touched: list[date]           # 内容被重算过的日期
    notes: list[str]


class BudgetSettingsBody(BaseModel):
    """预算设置（规格 M09 S1h / S2a）：传了哪个字段就改哪个；cap_amount / meal_eur_per_day 给 null 表示清掉。"""

    travelers: list[Traveler] | None = Field(None, min_length=1, max_length=20)
    currency: Currency | None = None
    cap_amount: float | None = Field(None, ge=0, le=10_000_000)
    cap_currency: Currency | None = None
    meal_eur_per_day: float | None = Field(None, ge=0, le=1000)


class PackingAddBody(BaseModel):
    category: str = Field(min_length=1, max_length=20)
    text: str = Field(min_length=1, max_length=120)


class PackingPatchBody(BaseModel):
    packed: bool | None = None
    text: str | None = Field(None, min_length=1, max_length=120)
    category: str | None = Field(None, min_length=1, max_length=20)


class TodoAddBody(BaseModel):
    text: str = Field(min_length=1, max_length=120)
    due: date | None = None
    key: str | None = Field(None, max_length=60)


class TodoPatchBody(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=120)
    due: date | None = None               # 传 null = 清掉截止日（用 model_fields_set 区分「没传」和「传了 null」）
    done: bool | None = None


class NoteBody(BaseModel):
    ref: str = Field(min_length=1, max_length=120)     # 备注对象：place_id / 日期|名称 / day|日期（companion.note_ref）
    text: str = Field(max_length=2000)


class ShareOptionsBody(BaseModel):
    packing: bool = False
    todos: bool = False
    notes: bool = False


class AlternativesBody(BaseModel):
    itinerary: Itinerary
    place_id: str
    indoor_only: bool = False      # 雨天换备选：只找室内地点（M10 S1d）


class SaveVersionBody(BaseModel):
    itinerary: Itinerary
    name: str = Field("", max_length=60)
    note: str = Field("", max_length=500)


BULK_REMOVE = 3          # 一句话要删这么多处及以上，先让用户确认


class ShiftBody(BaseModel):
    start_date: date


class CustomPlaceBody(BaseModel):
    """从高德搜到的一个点（规格 M05 S7d）；名字、地址、坐标都来自高德，类别按高德的分类换算。"""

    city: str
    name: str = Field(min_length=1, max_length=60)
    address: str = Field(default="", max_length=120)
    area: str = Field(default="", max_length=30)
    category: Category = Category.sight
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class CommuteLeg(BaseModel):
    from_lat: float = Field(ge=-90, le=90)
    from_lng: float = Field(ge=-180, le=180)
    to_lat: float = Field(ge=-90, le=90)
    to_lng: float = Field(ge=-180, le=180)


class CommuteBody(BaseModel):
    """一天里相邻两点的坐标（规格 M05 S7b）；高德只拿到坐标。"""

    city: str
    legs: list[CommuteLeg] = Field(min_length=1, max_length=12)
    drive: bool = False                         # 这趟是自驾：远一点的两点查驾车路线，不查公交


class StaysBody(BaseModel):
    stays: list[dict[str, Any]]               # 整体替换住宿列表；校验交给 Stay / TravelRequest，错误说人话
    reschedule: bool = False                    # 保存后按新住宿重新排程（锁定的点不动）


class TransferBody(BaseModel):
    """旅行页改换城市的交通：出发日期 = 前一座城市的离开日，到达日期 = 后一座城市的开始日。"""

    mode: TransportMode
    depart_at: datetime
    arrive_at: datetime
    number: str | None = Field(default=None, max_length=20)


class ArchiveBody(BaseModel):
    archived: bool


_OPT_HHMM = r"^(([01]\d|2[0-3]):[0-5]\d)?$"


class ReservationBody(BaseModel):
    """None = 不改这一项；空字符串 = 清掉。"""

    status: Literal["todo", "booked"]
    confirmation_code: str | None = Field(default=None, max_length=60)
    slot: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=300)
    # 入馆时间 / 时段开始，时段结束（选填）；"" = 清掉
    entry_from: str | None = Field(default=None, pattern=_OPT_HHMM)
    entry_to: str | None = Field(default=None, pattern=_OPT_HHMM)


class RevisionBody(BaseModel):
    feedback: str = Field(min_length=1, max_length=2000)
    ops: list[dict[str, Any]] | None = None     # 用户确认过的操作（上一次返回 409 时带回来的）；有它就不再问模型


# --------------------------------------------------------------------------- #
def _public_place(p, city: str = DEFAULT_CITY) -> dict[str, Any]:
    """地点对外的样子：官网链接只给需要订票的地点（M02 S2b），其余只有地址。"""
    d = p.model_dump(mode="json")
    d["official_url"] = p.public_url
    d["local_language"] = local_language_of(city)          # 官方语言是中文（国内）的地点，页面不再补英文名
    if p.public_url is None:
        d["url_status"] = None          # 没有对外的链接，也就没有链接的核对状态可说
    d["media"] = media_payload(p.id, city)    # 图片 + 署名、简介 + 来源（M02 S7b）；没有就是 null
    return d


def create_app(
    *,
    store: PlanStore | None = None,
    client_factory: ClientFactory | None = None,
    geocoder: Geocoder | None = None,
    weather: WeatherService | None = None,
    fx: FxService | None = None,
    transit: Transitous | None = None,
    today: Callable[[], date] | None = None,
    resolver: Resolver | None = None,
    build_jobs: BuildJobs | None = None,
) -> FastAPI:
    store = store or PlanStore()
    today = today or (lambda: booking.today())          # 晚绑定：ATP_TODAY / 测试里 monkeypatch booking.today 都生效
    weather_service = weather or WeatherService()
    fx_service = fx or FxService(store.database)
    transit_service = transit or Transitous()

    def _fit(request: TravelRequest, itinerary: Itinerary) -> Itinerary:
        """生成之后按预算调整（规格 M01 S4b）：填了上限才做；每一换写进「生成说明」里留痕。"""
        if request.budget_cap is None or request.day_trip_from or not has_budget_data(request.city_id):
            # 一日游 / 现建的城市没有核对过价格的预算数据：不按预算调整
            return itinerary
        try:
            fitted, notes = fit_to_budget(itinerary, request, fx_service.get("EUR", "CNY"))
        except Exception:  # noqa: BLE001 - 预算调整失败不能让生成失败：照原行程交付
            return itinerary
        if notes and fitted.generation is not None:
            fitted.generation.notes = [*fitted.generation.notes, *notes]
        return fitted

    cfg = LLMConfig.from_env()
    llm_calls = LlmCallStore(store.database, price_in_per_mtok=cfg.price_in_per_mtok,
                             price_out_per_mtok=cfg.price_out_per_mtok)
    base_client: ClientFactory = client_factory or get_client

    def make_client() -> LLMClient:
        """所有模型调用都过追踪包装：记耗时、token、成败（不存提示词原文）。"""
        return TracedClient(base_client(), llm_calls)

    tasks = TaskStore(store.database)
    runner = TaskRunner(store, tasks, client_factory=make_client, post_generate=_fit)

    app = FastAPI(title="AI Travel Planner Agent", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
        expose_headers=["Content-Disposition"],        # 网页下载时用后端给的文件名
    )

    def _single_only(request: TravelRequest) -> None:
        """一句话修改还不支持多城市（话里的「第二天」「那个博物馆」分不清是哪座城市）：明说，不硬套。"""
        if request.legs:
            raise HTTPException(409, "多城市行程的一句话修改还没做；可以直接在页面上换点、拖拽、锁定，或改住宿和交通。")

    def _apply(it: Itinerary, request: TravelRequest, edit: Any) -> EditResult:
        """编辑：多城市行程拆给被改的那座城市，单城市原样。"""
        if request.legs:
            return multiedit.apply_edit_multi(it, request, edit)
        return apply_edit(it, request, edit)

    def _load(pid: str, version: int | None):
        try:
            itinerary = store.load(pid, version)
            request = store.request(pid, version)      # 每个版本带着自己的需求（平移日期后日期不同）
        except PlanNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        def localized(req: TravelRequest, it: Itinerary) -> tuple[TravelRequest, Itinerary]:
            try:
                it = localize_itinerary(it, load_library(req.city_id))      # 老行程换成当地名（P18）
                it = refresh_route_display(it, req.city_id)                 # 老的路线行程补每站的理由
            except FileNotFoundError:
                pass
            return req, it

        if request.legs:                                    # 多城市：每座城市各换，再合回一份
            request, itinerary = multicity.map_legs(request, itinerary, localized)
        else:
            _, itinerary = localized(request, itinerary)
        return itinerary, request

    def _cities_of(request: TravelRequest) -> list[dict[str, Any]] | None:
        if not request.legs:
            return None
        return [{"city_id": leg.request.city_id, "name_zh": multicity.city_name(leg.request.city_id),
                 "start_date": leg.request.start_date.isoformat(), "end_date": leg.request.end_date.isoformat()}
                for leg in request.legs]

    def _transfers_of(request: TravelRequest) -> list[dict[str, Any]] | None:
        """城市之间的交通：存在每座城市自己那份需求的返程里（下一座的去程是同一段）。"""
        if not request.legs:
            return None
        out = []
        for i, (a, b) in enumerate(zip(request.legs, request.legs[1:], strict=False)):
            leg = a.request.transport.inbound
            if leg is None or leg.depart_at is None or leg.arrive_at is None:
                continue
            out.append({"position": i, "from_city": a.request.city_id, "to_city": b.request.city_id,
                        "from_name": multicity.city_name(a.request.city_id),
                        "to_name": multicity.city_name(b.request.city_id),
                        "mode": leg.mode.value, "number": leg.number, "estimated": leg.estimated,
                        "depart_at": leg.depart_at.isoformat(timespec="minutes"),
                        "arrive_at": leg.arrive_at.isoformat(timespec="minutes")})
        return out

    def _place_ids(itinerary: Itinerary) -> list[str]:
        return [i.place_id for d in itinerary.days for i in d.items if i.place_id]

    def _stories(itinerary: Itinerary, request: TravelRequest) -> dict[str, list[dict[str, Any]]]:
        if request.legs:
            out: dict[str, list[dict[str, Any]]] = {}
            for req, it in multicity.split(request, itinerary):
                out.update(stories_by_place(req.city_id, _place_ids(it)))
            return out
        return stories_by_place(request.city_id, _place_ids(itinerary))

    def _culture(itinerary: Itinerary, request: TravelRequest) -> list[dict[str, Any]]:
        if request.legs:
            return [row for req, it in multicity.split(request, itinerary)
                    for row in recap(req.city_id, _place_ids(it))]
        return recap(request.city_id, _place_ids(itinerary))

    # ---- health ------------------------------------------------------- #
    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(llm_backend=LLMConfig.from_env().backend)

    map_usage = map_quota.Usage(store.database)
    amap_client = AmapClient(map_config_keys.web_key, map_usage)

    def _library(city: str) -> PlaceLibrary:
        """按城市 id 取地点库；不认识 / 没有数据 → 404，并说清楚现在支持哪些。"""
        known = {c.id for c in supported_cities()}
        if city not in known:
            raise HTTPException(404, f"没有这座城市的数据：{city}（现在支持：{', '.join(sorted(known))}）")
        return load_library(city)

    @app.get("/cities", response_model=list[dict[str, Any]])
    def cities() -> list[dict[str, Any]]:
        """已支持的城市（规格 M17）：首页选城市、前端按币种 / 时区 / 身份选项显示都靠它。"""
        return [{"id": c.id, "name_zh": c.name_zh, "name_en": c.name_en, "country": c.country, "currency": c.currency,
                 "timezone": c.timezone, "center": list(c.center), "map_provider": c.map_provider,
                 "residency_options": c.residency_options, "hero": pick_hero(c.id), "tier": c.tier,
                 "suggested_days": c.suggested_days.model_dump()} for c in supported_cities()]

    # ---- 任意目的地（规格 10）：认城市 → 现建 → 轮询进度 ----
    jobs = build_jobs or BuildJobs()

    def _resolver_call(fn: Callable[[Resolver], Any]) -> Any:
        if resolver is not None:
            return fn(resolver)
        from app.citybuild.fetch import make_client

        with make_client() as client:
            return fn(Resolver(client))

    def _choice(c: Candidate) -> dict[str, Any]:
        return {"qid": c.qid, "label": c.label, "country": c.country_label, "description": c.description}

    @app.post("/destinations/resolve", response_model=dict[str, Any])
    def resolve_destination(body: ResolveBody) -> dict[str, Any]:
        """任何目的地：库里有 → ready；认得出没建 → build；国家 / 同名 → choices；认不出 → unknown。"""
        text = body.text.strip()
        known = city_of(text)
        if known is not None and needs_rebuild(known) and known.wikidata:       # 旧版现建的：按新流程重建
            return {"status": "build", "choice": {"qid": known.wikidata, "label": known.name_zh, "country": "",
                                                  "description": "更新这座城市的数据"}}
        if known is not None:
            return {"status": "ready", "city_id": known.id, "name_zh": known.name_zh}
        try:
            res = _resolver_call(lambda r: r.resolve(text))
        except RuntimeError as exc:
            raise HTTPException(502, f"没能查询目的地：{exc}。请稍后再试，或直接写城市名。") from exc
        if res.kind == "unknown":
            return {"status": "unknown", "reason": res.reason or "没找到叫这个名字的目的地"}
        if res.kind == "choices":
            return {"status": "choices", "reason": res.reason, "many": res.many,
                    "choices": [_choice(c) for c in res.choices]}
        assert res.city is not None
        built = city_by_wikidata(res.city.qid)
        if built is not None and not needs_rebuild(built):
            return {"status": "ready", "city_id": built.id, "name_zh": built.name_zh}
        return {"status": "build", "choice": _choice(res.city)}

    @app.post("/destinations/build", status_code=202, response_model=dict[str, Any])
    def build_destination(body: BuildBody) -> dict[str, Any]:
        """开始现建一座城市（后台，几分钟）；已经建好就直接 ready。"""
        built = city_by_wikidata(body.qid)
        if built is not None and not needs_rebuild(built):
            return {"status": "ready", "city_id": built.id, "name_zh": built.name_zh}

        def make(r: Resolver) -> dict:
            cands = r.describe([body.qid])
            if not cands or cands[0].lat is None:
                raise RuntimeError("维基数据里没有这个地方的坐标")
            names = r.labels([body.qid])
            lab = {k: v["value"] for k, v in ((names.get(body.qid) or {}).get("labels") or {}).items()}
            cands[0].label = lab.get("zh-cn") or lab.get("zh-hans") or lab.get("zh") or lab.get("en") or body.qid
            return r.seed_for(cands[0], body.query)

        try:
            seed = _resolver_call(make)
        except RuntimeError as exc:
            raise HTTPException(502, f"没能读取目的地信息：{exc}。请稍后再试。") from exc
        if built is not None:
            seed["id"] = built.id                                      # 旧版建的同一座城市：原地重建，行程里的城市不变
        else:
            try:
                load_city(seed["id"])
                seed["id"] = f"{seed['id']}-{body.qid.lower()}"        # 同名但不是同一座城市：换个 id
            except KeyError:
                pass
        seed["supported"] = True
        job = jobs.start(seed)
        return {"status": "building", "job_id": job.id, "city_id": job.city_id, "name_zh": job.name_zh}

    @app.get("/destinations/jobs/{job_id}", response_model=dict[str, Any])
    def destination_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务（服务重启过的话请重新输入目的地）")
        out = job.public()
        if job.status == "done":
            built = load_city(job.city_id)
            out["name_zh"] = built.name_zh
        return out

    @app.get("/attractions", response_model=list[dict[str, Any]])
    def attractions(city: str = Query(DEFAULT_CITY)) -> list[dict[str, Any]]:
        _library(city)
        return attraction_catalog(city)

    @app.get("/themes", response_model=list[dict[str, Any]])
    def themes(city: str = Query(DEFAULT_CITY)) -> list[dict[str, Any]]:
        lib = _library(city)
        return [
            {"id": t.id, "name_zh": t.name_zh, "tagline": t.tagline, "description": t.description,
             "tips": t.tips, "anchors_per_day": t.recipe.anchors_per_day, "place_count": len(t.members)}
            for t in lib.themes.values()
        ]

    @app.get("/themes/{theme_id}", response_model=dict[str, Any])
    def theme_detail(theme_id: str, city: str = Query(DEFAULT_CITY)) -> dict[str, Any]:
        """一个主题和它的成员（地点 id、角色、默认权重），给候选池面板用。"""
        lib = _library(city)
        try:
            t = lib.get_theme(theme_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from exc
        return {
            "id": t.id, "name_zh": t.name_zh, "tagline": t.tagline, "description": t.description,
            "tips": t.tips, "anchors_per_day": t.recipe.anchors_per_day,
            "members": [{"place_id": p.id, "role": m.role.value, "weight": m.weight}
                        for p, m in lib.theme_members(theme_id)],
        }

    # ---- 周边一日游（规格 08 3.4）------------------------------------- #
    @app.get("/daytrips/origins", response_model=list[dict[str, Any]])
    def daytrip_origins() -> list[dict[str, Any]]:
        t = load_pairs()
        return [{"id": oid, "name_local": o.name_local, "name_zh": o.name_zh, "country": o.country,
                 "station": o.station.model_dump()} for oid, o in sorted(t.origins.items())]

    @app.get("/daytrips", response_model=dict[str, Any])
    def daytrip_list(origin: str = Query(...),
                     day: date | None = Query(None, alias="date")) -> dict[str, Any]:  # noqa: B008
        """从 origin 出发能一日游的目的地：单程用时（带来源）、亮点、当季活动（日期在不在期内）、数据是不是草稿。"""
        try:
            dests = destinations_for(origin)
        except DayTripError as exc:
            raise HTTPException(404, str(exc)) from exc
        home = home_of(origin)
        return {"origin": {"id": origin, "name_local": home.name_local, "name_zh": home.name_zh,
                           "station": home.station.model_dump()},
                "date": day.isoformat() if day else None, "destinations": [describe_destination(d, day) for d in dests]}

    @app.post("/daytrips/geocode", response_model=list[dict[str, Any]])
    def daytrip_geocode(body: AddressBody) -> list[dict[str, Any]]:
        """住址 → 最多 4 个候选地址（城市、国家、坐标）。地址只发给时刻表服务查坐标，不保存。"""
        try:
            found = transit_service.geocode(body.q, zh_city_names())
        except TransitError as exc:
            raise HTTPException(503, str(exc)) from exc
        return [{"label": a.label, "city": a.city, "city_zh": zh_name_of(a.city), "country": a.country,
                 "lat": a.lat, "lng": a.lng, "kind": a.kind} for a in found if a.city and a.country]

    @app.post("/daytrips/search", response_model=dict[str, Any])
    def daytrip_search(body: LiveSearchBody) -> dict[str, Any]:
        """从住址出发的目的地：每座城市给「最快」和「只坐区间车」两个方案（有就给）。连不上时预设出发地回退到存好的。"""
        try:
            origin = make_origin(body.city, body.country, body.lat, body.lng, body.city_zh)
            return list_destinations(origin, body.day, transit_service, depart=body.depart, dticket=body.dticket)
        except DayTripError as exc:
            raise HTTPException(422, detail={"message": str(exc)}) from exc
        except TransitError as exc:
            if body.origin in load_pairs().origins:
                return offline_destinations(body.origin, body.day, body.depart)
            raise HTTPException(503, f"{exc}：稍后再试") from exc

    @app.post("/daytrips/plan-request", response_model=dict[str, Any])
    def daytrip_plan_request(body: LiveRequestBody) -> dict[str, Any]:
        """「排一天」（住址出发）：现查去程和当晚最晚回程，生成可交给 POST /tasks 的需求。需求里没有街道和门牌。"""
        try:
            origin = make_origin(body.city, body.country, body.lat, body.lng, body.city_zh)
            req, info = build_live_request(origin, body.destination, body.day, transit_service, depart=body.depart,
                                           back=body.back, kind=body.kind)
        except DayTripError as exc:
            raise HTTPException(422, detail={"message": str(exc)}) from exc
        except TransitError as exc:
            raise HTTPException(503, f"{exc}：稍后再试") from exc
        return {"payload": {**req.model_dump(mode="json"), "theme": DAYTRIP_THEME}, **info}

    @app.get("/daytrips/request", response_model=dict[str, Any])
    def daytrip_request(origin: str = Query(...), destination: str = Query(...),
                        day: date = Query(..., alias="date"), depart: str = Query("09:00"),  # noqa: B008
                        back: str | None = Query(None)) -> dict[str, Any]:
        """「排一天」：出发地 + 目的地 + 日期 + 时间 → 一份可以交给 POST /tasks 的需求（到达 / 返程时间按时刻表算）。"""
        try:
            req = build_day_request(origin, destination, day, depart=depart, back=back)
        except DayTripError as exc:
            raise HTTPException(422, detail={"message": str(exc)}) from exc
        pair = next(d.pair for d in destinations_for(origin) if d.city.id == req.city_id)
        return {"payload": {**req.model_dump(mode="json"), "theme": DAYTRIP_THEME},
                "arrive": f"{req.arrival_time:%H:%M}", "leave": f"{req.departure_time:%H:%M}",
                "last_return": pair.last_return, "minutes": pair.minutes}

    @app.get("/places", response_model=list[dict[str, Any]])
    def places(theme: str | None = Query(None), city: str = Query(DEFAULT_CITY)) -> list[dict[str, Any]]:
        lib = _library(city)
        if theme is None:
            chosen = list(lib.places.values())
        else:
            try:
                chosen = [p for p, _ in lib.theme_members(theme)]
            except KeyError as exc:
                raise HTTPException(404, str(exc.args[0])) from exc
        return [_public_place(p, city) for p in chosen]

    @app.get("/places/search", response_model=list[dict[str, Any]])
    def search_places(q: str = Query("", max_length=60), limit: int = Query(8, ge=1, le=30),
                      city: str = Query(DEFAULT_CITY)) -> list[dict[str, Any]]:
        """地点库内搜索（名字 / 英文名 / 别名 / 区域）。库外的地点没有开放时间和闭馆日，暂不支持。"""
        return [_public_place(p, city) for p in _library(city).search(q, limit)]

    @app.get("/places/lookup", response_model=dict[str, Any])
    def lookup_place(q: str = Query(..., min_length=1, max_length=60),
                     city: str = Query(DEFAULT_CITY)) -> dict[str, Any]:
        """库外地点（规格 M05 S7d）：用户点了「在高德里搜」才调用；只给名字、地址、类型、坐标，不含任何经营信息。"""
        found = {c.id: c for c in supported_cities()}.get(city)
        if found is None:
            raise HTTPException(404, f"没有这座城市的数据：{city}")
        if found.map_provider != "amap":
            return {"hits": [], "reason": "not_amap_city"}
        hits = amap_client.search(q, found.name_zh)
        if hits is None:
            return {"hits": [], "reason": amap_client.reason_unavailable("search") or "unreachable"}
        return {"hits": [{"id": h.id, "name": h.name, "address": h.address, "area": h.area, "type": h.type,
                          "category": h.category, "lat": h.lat, "lng": h.lng} for h in hits]}

    @app.post("/places/custom", response_model=dict[str, Any], status_code=201)
    def add_custom_place(body: CustomPlaceBody) -> dict[str, Any]:
        """高德里搜到的点加进这座城市的地点库（规格 M05 S7d）：草稿档，没有营业时间和价格，行程里标「没核对」。"""
        found = {c.id: c for c in supported_cities()}.get(body.city)
        if found is None or found.map_provider != "amap":
            raise HTTPException(422, "只有国内城市可以从高德里加地点")
        raw = place_dict(name=body.name, address=body.address, area=body.area,
                         category=body.category, lat=body.lat, lng=body.lng, today=booking.today())
        token = _CITY_BBOX.set((found.bbox, found.name_zh))
        try:
            place = Place.model_validate(raw)
        except ValidationError as exc:
            if "范围" in str(exc):
                raise HTTPException(422, f"这个点不在{found.name_zh}的市区范围内") from exc
            raise HTTPException(422, "这个点的信息不完整，没法加入") from exc
        finally:
            _CITY_BBOX.reset(token)
        save_user_place(body.city, raw)
        load_library.cache_clear()
        return _public_place(_library(body.city).get_place(place.id), body.city)

    @app.post("/commute", response_model=dict[str, Any])
    def commute(body: CommuteBody) -> dict[str, Any]:
        """一天里相邻两点的真实通勤时间（规格 M05 S7b）：用户点了才调；不缓存、不保存，查不到的那段是 null。"""
        found = {c.id: c for c in supported_cities()}.get(body.city)
        if found is None:
            raise HTTPException(404, f"没有这座城市的数据：{body.city}")
        if found.map_provider != "amap":
            return {"legs": [None] * len(body.legs), "reason": "not_amap_city"}
        legs: list[dict[str, Any] | None] = []
        reason: str | None = None
        for leg in body.legs:
            reason = reason or amap_client.reason_unavailable("lbs")
            got = None if reason else amap_client.route((leg.from_lat, leg.from_lng), (leg.to_lat, leg.to_lng),
                                                        found.name_zh, drive=body.drive)
            legs.append(None if got is None else {"minutes": got.minutes, "mode": got.mode,
                                                  "distance_m": got.distance_m, "steps": got.steps})
        if reason is None and all(x is None for x in legs):
            reason = "unreachable"
        return {"legs": legs, **({"reason": reason} if reason else {})}

    @app.get("/media/places/{filename}")
    def place_photo(filename: str) -> FileResponse:
        """地点照片（缩略图，来自 Wikimedia Commons，署名在 /places 的 media 字段里）；只认 photos/ 下的文件。"""
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*\.(jpg|png)", filename):
            raise HTTPException(404, "没有这张图片")
        path = photo_path(filename)
        if not path.is_file():
            raise HTTPException(404, "没有这张图片")
        return FileResponse(path, media_type="image/png" if filename.endswith(".png") else "image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/media/hero/{filename}")
    def hero_photo(filename: str) -> FileResponse:
        """封面大图（≥ 1600 宽，来自 Wikimedia Commons，署名在 media.hero 里）；只认 hero/ 下的 .jpg。"""
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*\.jpg", filename):
            raise HTTPException(404, "没有这张图片")
        path = hero_path(filename)
        if not path.is_file():
            raise HTTPException(404, "没有这张图片")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/places/{place_id}", response_model=dict[str, Any])
    def place_detail(place_id: str, city: str = Query(DEFAULT_CITY)) -> dict[str, Any]:
        """地点详情卡：策展字段 + 它属于哪些主题 + 各项事实的核对状态。"""
        lib = _library(city)
        try:
            p = lib.get_place(place_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from exc
        d = _public_place(p, city)
        rank = lib.fame_ranking().get(p.id)
        if p.fame and rank:
            d["fame"] = {**d["fame"], "rank": rank, "of": len(lib.fame_ranking())}      # 地点卡：名气 · 全城第 N
        d["themes"] = [
            {"id": t.id, "name_zh": t.name_zh, "role": m.role.value, "weight": m.weight}
            for t in lib.themes.values() for m in t.members if m.place_id == p.id
        ]
        d["stories"] = stories_for_place(lib.city, p.id)
        d["verification"] = {
            "coords_verified": p.coords_verified, "hours_verified": p.hours_verified,
            "verified_at": p.verified_at.isoformat() if p.verified_at else None, "url_status": d["url_status"],
            "ticketing_checked_at": p.ticketing_checked_at.isoformat() if p.ticketing_checked_at else None,
        }
        return d

    # ---- intake ----------------------------------------------------- #
    @app.post("/intake", response_model=IntakeResult)
    def intake(payload: dict[str, Any]) -> IntakeResult:
        return run_intake(payload)

    # ---- 文件附件（M10 S5）：本机文件夹 + 数据库记录；不进分享 / 导出 ------------------ #
    files = attach.Attachments(store.database, store.root)
    files.purge_expired()                                       # 启动时清一次回收站里超过 30 天的

    def _attach_error(exc: attach.AttachmentError) -> HTTPException:
        return HTTPException(413 if exc.code == "too_big" else 422, detail={"code": exc.code, "message": exc.message})

    def _check_attach_target(pid: str, kind: str | None, ref: str | None) -> None:
        """挂靠的对象要真的存在：地点 / 预订是地点库里的地点，某一天是这份行程里的日期。"""
        if kind in (None, "plan"):
            return
        itinerary, request = _load(pid, None)
        ok = False
        if kind in ("place", "reservation"):
            ok = ref in library_for_request(request).places
        elif kind == "day":
            ok = ref in {d.date.isoformat() for d in itinerary.days}
        if not ok:
            raise HTTPException(422, detail={
                "code": "target", "message": "挂靠的对象不存在（这个地点或日期不在这份行程里）"})

    def _attachment_json(rec: dict) -> dict[str, Any]:
        return {k: rec[k] for k in ("id", "name", "kind", "size", "target_kind", "target_ref", "uploaded_at",
                                    "deleted_at")}

    @app.get("/plans/{pid}/attachments", response_model=dict[str, Any])
    def list_attachments(pid: str) -> dict[str, Any]:
        _plan_or_404(pid)
        files.purge_expired()
        return {"attachments": [_attachment_json(r) for r in files.list(pid)],
                "trash": [_attachment_json(r) for r in files.list(pid, trash=True)],
                "limits": {"max_bytes": attach.MAX_BYTES, "max_files": attach.MAX_FILES,
                           "trash_days": attach.TRASH_DAYS, "kinds": list(attach.KINDS)}}

    @app.post("/plans/{pid}/attachments", response_model=dict[str, Any], status_code=201)
    async def upload_attachment(
        pid: str, request: Request, name: str = Query("", max_length=400), target_kind: str = Query("plan"),
        target_ref: str | None = Query(None, max_length=100),
    ) -> dict[str, Any]:
        """请求体就是文件的字节（不用 multipart，不新增依赖）。

        边读边数，超过 10MB 立刻停；声明的长度超限时连读都不读。
        """
        _plan_or_404(pid)
        too_big = HTTPException(413, detail={"code": "too_big", "message": "文件超过 10MB"})
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > attach.MAX_BYTES:
            raise HTTPException(413, detail={
                "code": "too_big", "message": f"文件超过 10MB（当前 {declared / 1024 / 1024:.1f} MB）"})
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > attach.MAX_BYTES:
                raise too_big
            chunks.append(chunk)
        _check_attach_target(pid, target_kind, target_ref)
        try:
            return _attachment_json(files.add(pid, name, b"".join(chunks), target_kind=target_kind,
                                              target_ref=target_ref))
        except attach.AttachmentError as exc:
            raise _attach_error(exc) from exc

    @app.get("/plans/{pid}/attachments/{aid}/file")
    def attachment_file(pid: str, aid: str, download: bool = Query(False)) -> FileResponse:
        """预览（inline）或下载。响应头保证文件里的脚本无论如何不会被浏览器当页面执行。"""
        rec = files.get(pid, aid)
        path = files.path(pid, rec) if rec else None
        if rec is None or path is None or not path.exists():
            raise HTTPException(404, "没有这个文件")
        quoted = urlquote(rec["name"], safe="")
        ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", rec["name"]) or "file"
        disposition = f"{'attachment' if download else 'inline'}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"
        return FileResponse(path, media_type=attach.MEDIA_TYPES[rec["kind"]], headers={
            "Content-Disposition": disposition, "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox", "Cache-Control": "private, no-store"})

    @app.patch("/plans/{pid}/attachments/{aid}", response_model=dict[str, Any])
    def patch_attachment(pid: str, aid: str, body: AttachmentPatch) -> dict[str, Any]:
        _plan_or_404(pid)
        try:
            if body.restore:
                rec = files.restore(pid, aid)
            else:
                _check_attach_target(pid, body.target_kind or "plan", body.target_ref)
                rec = files.retarget(pid, aid, body.target_kind or "plan", body.target_ref)
        except attach.AttachmentError as exc:
            raise _attach_error(exc) from exc
        if rec is None:
            raise HTTPException(404, "没有这个文件")
        return _attachment_json(rec)

    @app.delete("/plans/{pid}/attachments/{aid}", status_code=204)
    def delete_attachment(pid: str, aid: str, permanent: bool = Query(False)) -> Response:
        """默认进回收站（30 天内可恢复）；?permanent=1 永久删除。"""
        _plan_or_404(pid)
        done = files.purge(pid, aid) if permanent else files.trash(pid, aid)
        if not done:
            raise HTTPException(404, "没有这个文件")
        return Response(status_code=204)

    # ---- 自然语言填表（M01 S10–S13）：无状态，只返回读到的字段，不存正文、不生成行程 ---------- #
    @app.post("/intake/nl", response_model=Intent)
    def intake_nl(body: NlIntakeBody) -> Intent:
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(422, detail={"code": "empty", "message": "请先写一句话，说说你想怎么玩"})
        if len(text) > NL_MAX_TEXT:
            raise HTTPException(422, detail={
                "code": "too_long", "message": f"最多 {NL_MAX_TEXT} 字（现在 {len(text)} 字），请精简一下"})
        try:
            today = date.fromisoformat(body.today) if body.today else booking.today()
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "today", "message": "today 要写成 YYYY-MM-DD"}) from exc
        try:
            client: LLMClient | None = make_client()
        except LLMError:
            client = None                # 连客户端都建不起来：只按规则读
        return parse_intent(text, _library(body.city), client, today=today)

    # ---- 笔记导入（M12）：无状态，只返回解析结果，不存正文 -------------------- #
    def make_note_client() -> LLMClient:
        """笔记导入单独用更大的模型（默认 14B，`.env` 的 NOTE_OLLAMA_MODEL 可改）：读乐园 / 商场类笔记时更干净；
        别的功能仍用 OLLAMA_MODEL（7B）。注入了自定义客户端（测试）时照常用它。"""
        if client_factory is not None:
            return make_client()
        base = LLMConfig.from_env()
        model = os.getenv("NOTE_OLLAMA_MODEL", "").strip() or NOTE_MODEL_DEFAULT
        return TracedClient(get_client(replace(base, ollama_model=model)), llm_calls)

    link_guard = SiteGuard()          # 小红书：同一篇缓存、限速、连续失败就停（保护用户的账号）

    def read_link(url: str):
        return link_guard.fetch(url, lambda u: fetch_page(u))

    @app.get("/materials/sources", response_model=dict[str, bool])
    def material_sources() -> dict[str, bool]:
        """导入弹窗用：哪些来源配好了。只返回有没有，绝不返回 Cookie 本身。"""
        return {"xhs_cookie": xhs_cookie() is not None}

    @app.post("/materials/parse", response_model=ParseResult)
    def parse_material(body: ParseBody) -> ParseResult:
        text, url = (body.text or "").strip(), (body.url or "").strip()
        if text and url:
            raise HTTPException(422, detail={"code": "both", "message": "正文和链接请只给一个"})
        if not text and not url:
            raise HTTPException(422, detail={"code": "empty", "message": "请先粘贴笔记正文或链接"})
        try:
            lib = load_library(city_id_of(body.destination))
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(
                422, detail={"code": "destination", "message": f"还不支持这个目的地：{body.destination}"}) from exc
        try:
            client: LLMClient | None = make_note_client()
        except LLMError:
            client = None                # 连客户端都建不起来：只识别地点库里已有的名字
        try:
            return parse_url(url, lib, client, fetcher=read_link) if url else parse_text(text, lib, client)
        except NoteTooLong as exc:
            raise HTTPException(422, detail={"code": "too_long", "message": str(exc)}) from exc
        except FetchError as exc:
            raise HTTPException(422, detail={
                "code": "fetch_failed", "reason": str(exc),
                "message": f"无法读取这个链接：{exc}。请直接把笔记正文粘贴到「粘贴文本」里。"}) from exc

    # ---- create plan --------------------------------------------------- #
    @app.post("/plans", response_model=PlanResponse, status_code=201)
    def create_plan(
        payload: dict[str, Any],
        repair: bool = Query(False),
        repair_rounds: int = Query(2, ge=1, le=4),
    ) -> PlanResponse:
        theme, result = _parse_payload(payload)

        if theme:
            return _create_by_theme(result.request, str(theme))

        client = make_client()
        try:
            plan = generate_itinerary(result.request, client=client, max_retries=2)
        except LLMError as exc:
            raise HTTPException(502, f"生成失败：{exc}") from exc

        itinerary = plan.itinerary
        rounds = 0
        if repair:
            rep = auto_repair(itinerary, result.request, client=client, max_rounds=repair_rounds)
            itinerary, rounds = rep.itinerary, len(rep.rounds)

        report = validate_itinerary(itinerary, result.request)
        plan_id = store.create(result.request, itinerary, note="via POST /plans")
        return PlanResponse(
            plan_id=plan_id, version=1, itinerary=store.load(plan_id, 1),
            report=report, repair_rounds=rounds,
        )

    def _origin_geocode(text: str, where: City) -> GeoHit | None:
        """自驾出发地 → 坐标：国内目的地先问高德，其余（和问不到的）用 OpenStreetMap，不限国家。"""
        if geocoder:
            return geocoder(text)
        hit = amap_client.geocode(text, "") if where.map_provider == "amap" else None
        return hit or nominatim(text, city=where, anywhere=True)

    def _drive_estimates(payload: dict[str, Any]) -> dict[str, Any]:
        """选了自驾又写了出发地 / 回到哪里：补上大致路程（公里、时间）。单城市和多城市两种需求格式都认。"""
        out = copy.deepcopy(payload)
        if out.get("legs"):
            first, last = (city_of(str(out["legs"][i].get("destination", ""))) if isinstance(out["legs"][i], dict)
                           else None for i in (0, -1))
            driveest.annotate({"outbound": out.get("outbound"), "inbound": out.get("inbound")}, first, last,
                              _origin_geocode)
        else:
            dest = city_of(str(out.get("destination", "")))
            driveest.annotate(out.get("transport"), dest, dest, _origin_geocode)
        return out

    def _parse_payload(payload: dict[str, Any]):
        """需求 dict -> (主题, 通过校验的 intake 结果)；缺信息 / 不合法统一回 422，带追问。"""
        payload = _drive_estimates(payload)
        theme = payload.pop("theme", None)
        result = run_intake(payload)
        if not result.ok:
            raise HTTPException(422, detail={
                "message": "需求不完整",
                "questions": result.questions,
                "missing_fields": result.missing_fields,
                "errors": result.errors,
            })
        req = result.request
        if req.themes:                                       # 混选：校验每个主题存在，权重最大的当主主题
            lib = load_library(req.city_id)
            unknown = [t.id for t in req.themes if t.id not in lib.themes]
            if unknown:
                raise HTTPException(422, detail={
                    "message": f"没有这个主题：{'、'.join(unknown)}", "themes": sorted(lib.themes)})
            theme = max(req.themes, key=lambda t: t.weight).id
        return theme, result

    # ---- 后台生成任务（真实进度） ------------------------------------- #
    @app.post("/tasks", response_model=TaskCreated, status_code=202)
    def create_task(payload: dict[str, Any]) -> TaskCreated:
        if payload.get("legs") is not None:                  # 多城市：一份行程里有几座城市
            return _create_multi_task(payload)
        theme, result = _parse_payload(payload)
        lib = load_library(result.request.city_id)
        if not theme:
            raise HTTPException(422, detail={"message": "请先选一个主题", "themes": sorted(lib.themes)})
        if theme not in lib.themes:
            raise HTTPException(422, detail={"message": f"没有这个主题：{theme}", "themes": sorted(lib.themes)})
        tid = runner.submit(result.request, str(theme))
        return TaskCreated(task_id=tid, status=tasks.get(tid).status)

    def _create_multi_task(payload: dict[str, Any]) -> TaskCreated:
        try:
            spec = TripSpec.model_validate(_drive_estimates(payload))
        except ValidationError as exc:
            raise HTTPException(422, detail={"message": "旅行设置不完整或不合法", "errors": [
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]}) from exc
        try:
            parent, _ = multicity.build_request(spec)
        except TripError as exc:
            raise HTTPException(422, detail={"message": str(exc)}) from exc
        except ValidationError as exc:
            msg = "；".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
            raise HTTPException(422, detail={"message": msg}) from exc
        tid = runner.submit(parent, "multi")
        return TaskCreated(task_id=tid, status=tasks.get(tid).status)

    @app.get("/tasks", response_model=list[TaskCard])
    def list_unfinished_tasks(unfinished: bool = Query(...)) -> list[TaskCard]:
        """「我的行程」里的「正在生成」区（规格 07 P19）。只提供这一种列法：不把全部历史任务倒出来。"""
        if not unfinished:
            raise HTTPException(422, "目前只支持 unfinished=true")
        return tasks.unfinished_cards()

    @app.delete("/tasks/{tid}", status_code=204)
    def remove_failed_task(tid: str) -> Response:
        try:
            tasks.remove_failed(tid)
        except TaskNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except TaskNotRemovable as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(status_code=204)

    @app.get("/tasks/{tid}", response_model=Task)
    def get_task(tid: str) -> Task:
        try:
            return tasks.get(tid)
        except TaskNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    # ---- 我的行程 ----------------------------------------------------- #
    def _card_cities(request: TravelRequest, itinerary: Itinerary) -> list[dict[str, Any]] | None:
        if not request.legs:
            return None
        out = []
        for req, it in multicity.split(request, itinerary):
            out.append({"city_id": req.city_id, "name_zh": multicity.city_name(req.city_id),
                        "hero": pick_hero(req.city_id, _place_ids(it))})
        return out

    @app.get("/plan-cards", response_model=list[PlanCard])
    def plan_cards(archived: bool = Query(False)) -> list[PlanCard]:
        cards: list[PlanCard] = []
        for s, itinerary in store.summaries_with_itineraries(archived=archived):
            report = validate_itinerary(itinerary, s.request)
            gen = itinerary.generation
            theme_name = gen.theme_name if gen else None
            city = c.name_zh if (c := city_of(s.request.destination)) else s.request.destination
            if s.request.legs:
                city = multicity.title_of(s.request)
            cards.append(PlanCard(
                plan_id=s.plan_id, title=f"{city} · {theme_name or f'{s.request.trip_days} 天行程'}",
                theme_id=gen.theme_id if gen else None, theme_name=theme_name,
                start_date=s.request.start_date, end_date=s.request.end_date, days=s.request.trip_days,
                latest_version=s.latest_version, created_at=s.created_at,
                errors=len(report.errors), warnings=len(report.warnings),
                narration=gen.narration if gen else None,
                archived=s.archived, copied_from=s.copied_from, city_id=s.request.city_id,
                hero=pick_hero(s.request.city_id,
                               [i.place_id for d in itinerary.days for i in d.items if i.place_id]),
                cities=_card_cities(s.request, itinerary),
            ))
        return cards

    @app.get("/routes", response_model=list[dict[str, Any]])
    def routes(city: str = Query(DEFAULT_CITY)) -> list[dict[str, Any]]:
        """一座城市的深度游学路线（规格 09）：路线卡片用。"""
        lib = _library(city)
        return [summarize(r, lib) for r in load_routes(lib.city).values()]

    @app.get("/culture", response_model=list[dict[str, Any]])
    def culture(city: str = Query(DEFAULT_CITY)) -> list[dict[str, Any]]:
        """文化入门（规格 09 十三）：一座城市的电影 / 纪录片 / 书 / 播客，每一部带你看哪些地方、经过哪些路线。"""
        return culture_overview(_library(city).city)

    @app.get("/changelog", response_model=list[dict[str, Any]])
    def changelog() -> list[dict[str, Any]]:
        """版本足迹：每次更新了什么（新→旧，第一条是当前版本）。"""
        return load_changelog()

    @app.get("/food", response_model=list[dict[str, Any]])
    def food(city: str = Query(DEFAULT_CITY)) -> list[dict[str, Any]]:
        """美食（规格 09 十七）：名菜名点，有招牌的带场所；每条都有来源。"""
        return food_overview(_library(city).city)

    @app.get("/routes/{route_id}", response_model=dict[str, Any])
    def route_detail(route_id: str, city: str = Query(DEFAULT_CITY)) -> dict[str, Any]:
        """一条路线的详情：主线、每章讲什么、每章有哪几站、延伸阅读、策展说明。"""
        lib = _library(city)
        try:
            return summarize(get_route(lib.city, route_id), lib, detail=True)
        except RouteError as exc:
            raise HTTPException(404, str(exc)) from exc

    def _create_by_theme(request, theme: str) -> PlanResponse:
        try:
            client: LLMClient | None = make_client()
        except LLMError:
            client = None                # 连客户端都建不起来：直接用模板文案
        try:
            trip = generate_from_theme(request, theme, client=client)
        except KeyError as exc:
            lib = load_library(request.city_id)
            raise HTTPException(422, detail={
                "message": f"没有这个主题：{theme}", "themes": sorted(lib.themes),
            }) from exc
        itinerary = _fit(request, trip.itinerary)
        report = trip.report if itinerary is trip.itinerary else validate_itinerary(itinerary, request)
        plan_id = store.create(request, itinerary, note=f"via POST /plans theme={theme}")
        return PlanResponse(plan_id=plan_id, version=1, itinerary=store.load(plan_id, 1), report=report)

    # ---- list / get -------------------------------------------------- #
    @app.get("/plans", response_model=list[str])
    def list_plans() -> list[str]:
        return store.list_plans()

    def _route_status(itinerary: Itinerary) -> dict[str, Any] | None:
        gen = itinerary.generation
        if gen is None or gen.route is None:
            return None
        return {"id": gen.route.id, "name_zh": gen.route.name_zh, "deviated": route_deviation(itinerary)}

    @app.get("/plans/{pid}", response_model=PlanDetail)
    def get_plan(pid: str, version: int | None = Query(None, ge=1)) -> PlanDetail:
        itinerary, request = _load(pid, version)
        return PlanDetail(
            plan_id=pid,
            latest_version=store.latest_version(pid),
            version=version or store.latest_version(pid),
            itinerary=itinerary,
            report=validate_itinerary(itinerary, request),
            history=store.history(pid),
            archived=store.is_archived(pid),
            copied_from=store.copied_from(pid),
            stays=request.stays,
            note_sources=request.note_sources,
            note_pending=request.note_pending,
            city_id=request.city_id,
            day_trip_from=request.day_trip_from,
            day_trip_info=day_trip_info(request),
            route_status=_route_status(itinerary),
            stories=_stories(itinerary, request),
            culture=_culture(itinerary, request),
            cities=_cities_of(request),
            transfers=_transfers_of(request),
        )

    # ---- revise ---------------------------------------------------- #
    @app.post("/plans/{pid}/revisions", response_model=PlanResponse, status_code=201)
    def create_revision(pid: str, body: RevisionBody) -> PlanResponse:
        current, request = _load(pid, None)
        _single_only(request)
        errors_before = len(validate_itinerary(current, request).errors)
        if current.generation is not None:
            return _revise_by_ops(pid, current, request, body.feedback, errors_before, body.ops)
        try:
            rev = revise_itinerary(current, request, instructions=body.feedback, client=make_client())
        except LLMError as exc:
            raise HTTPException(502, f"修订失败：{exc}") from exc

        report = validate_itinerary(rev.itinerary, request)
        version = store.add_version(
            pid, rev.itinerary, trigger="user-feedback", note=body.feedback,
            change_summary=rev.change.as_text(),
            errors_before=errors_before, errors_after=len(report.errors),
        )
        return PlanResponse(
            plan_id=pid, version=version, itinerary=store.load(pid, version), report=report,
            change=RevisionChange(
                summary=rev.change.as_text(), changed_days=rev.change.changed_days,
                added=rev.change.added, removed=rev.change.removed,
            ),
        )

    # ---- 手动编辑（无状态：前端带着当前行程来，后端算出新行程返回） --------- #
    @app.post("/plans/{pid}/edit", response_model=EditResponse)
    def edit_plan(pid: str, body: EditBody) -> EditResponse:
        _, request = _load(pid, None)
        try:
            res = _apply(body.itinerary, request, body.edit)
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        return EditResponse(itinerary=res.itinerary, report=validate_itinerary(res.itinerary, request),
                            touched=res.touched, notes=res.notes)

    @app.post("/plans/{pid}/alternatives", response_model=list[Alternative])
    def plan_alternatives(pid: str, body: AlternativesBody) -> list[Alternative]:
        _, request = _load(pid, None)
        try:
            if request.legs:
                return multiedit.alternatives_multi(body.itinerary, request, body.place_id,
                                                    indoor_only=body.indoor_only)
            return alternatives(body.itinerary, request, body.place_id, indoor_only=body.indoor_only)
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/plans/{pid}/versions", response_model=PlanResponse, status_code=201)
    def save_version(pid: str, body: SaveVersionBody) -> PlanResponse:
        current, request = _load(pid, None)
        errors_before = len(validate_itinerary(current, request).errors)
        try:
            # 事实按地点库重算，不采信前端传来的
            itinerary = (multiedit.normalize_multi(body.itinerary, request) if request.legs
                         else normalize(body.itinerary, request))
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        report = validate_itinerary(itinerary, request)
        version = store.add_version(
            pid, itinerary, trigger="manual-save", name=body.name.strip(), note=body.note.strip(),
            change_summary=diff_itineraries(current, itinerary).as_text(),
            errors_before=errors_before, errors_after=len(report.errors),
        )
        return PlanResponse(plan_id=pid, version=version, itinerary=store.load(pid, version), report=report)

    @app.post("/plans/{pid}/versions/{n}/restore", response_model=PlanResponse, status_code=201)
    def restore_version(pid: str, n: int) -> PlanResponse:
        """回滚 = 以旧版本的内容新建一个版本；旧版本一个都不删。"""
        old, request = _load(pid, n)
        latest = store.latest_version(pid)
        report = validate_itinerary(old, request)
        version = store.add_version(
            pid, old, trigger="rollback", note=f"回滚到 v{n}",
            change_summary=f"与 v{latest} 相比：{diff_itineraries(store.load(pid, latest), old).as_text()}",
            errors_after=len(report.errors), request=request,       # 回滚到平移前的版本，日期也回到那一版的
        )
        return PlanResponse(plan_id=pid, version=version, itinerary=store.load(pid, version), report=report)

    # ---- 行程管理：平移日期 / 复制 / 归档（规格 M04 S8 / S12 / S13）--------------- #
    @app.post("/plans/{pid}/shift", response_model=PlanResponse, status_code=201)
    def shift_dates(pid: str, body: ShiftBody) -> PlanResponse:
        """整体平移日期：生成新版本；返回平移后新出现的闭馆日冲突。"""
        current, request = _load(pid, None)
        lib = load_library(request.city_id)
        try:
            if request.legs:
                shifted, new_req = multiedit.shift_multi(current, request, body.start_date)
            else:
                shifted, new_req = shift_plan(current, request, body.start_date, lib)
        except ShiftError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValidationError as exc:                      # 平移后的请求对不上：说清楚，别 500（页面会误报「离线」）
            msg = "；".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
            raise HTTPException(422, msg) from exc
        before = validate_itinerary(current, request)
        after = validate_itinerary(shifted, new_req)
        conflicts = new_conflicts(current, before, shifted, after)
        version = store.add_version(
            pid, shifted, trigger="date-shift", note=f"开始日期 {request.start_date} → {new_req.start_date}",
            change_summary=shift_summary(request.start_date, new_req.start_date),
            errors_before=len(before.errors), errors_after=len(after.errors), request=new_req,
        )
        store.notes_shift(pid, (new_req.start_date - request.start_date).days)        # 笔记跟着「那一天」走
        files.shift(pid, (new_req.start_date - request.start_date).days)              # 挂在某一天的文件也是
        return PlanResponse(plan_id=pid, version=version, itinerary=store.load(pid, version), report=after,
                            conflicts=conflicts)

    @app.post("/plans/{pid}/duplicate", response_model=PlanResponse, status_code=201)
    def duplicate_plan(pid: str) -> PlanResponse:
        _plan_or_404(pid)
        new_id = store.duplicate(pid)
        itinerary, request = _load(new_id, None)
        return PlanResponse(plan_id=new_id, version=1, itinerary=itinerary,
                            report=validate_itinerary(itinerary, request))

    # ---- 预订记录与最晚订票日（规格 M06 S3 / S4）--------------------------------- #
    def _reservation_rows(pid: str, itinerary: Itinerary, request: TravelRequest) -> list[dict[str, Any]]:
        lib = library_for_request(request)
        return booking.build_view(itinerary, lib, store.get_reservations(pid))

    def _res_map(pid: str, itinerary: Itinerary, request: TravelRequest) -> dict[str, dict]:
        return {r["place_id"]: r for r in _reservation_rows(pid, itinerary, request)}

    @app.get("/plans/{pid}/reservations", response_model=dict[str, Any])
    def list_reservations(pid: str, version: int | None = Query(None, ge=1)) -> dict[str, Any]:
        """购票清单 + 我的预订状态 + 建议最晚订票日（只有地点库里有明确提前天数的地点才给日期）。"""
        itinerary, request = _load(pid, version)
        return {"today": booking.today().isoformat(), "items": _reservation_rows(pid, itinerary, request)}

    @app.put("/plans/{pid}/reservations/{place_id}", response_model=dict[str, Any])
    def save_reservation(pid: str, place_id: str, body: ReservationBody) -> dict[str, Any]:
        itinerary, request = _load(pid, None)
        lib = library_for_request(request)
        place = lib.places.get(place_id)
        if place is None:
            raise HTTPException(422, f"地点库里没有 {place_id!r}")
        if place.ticketing.value == "none":
            raise HTTPException(422, f"「{place.name_zh}」无需订票，不用记预订")
        visit = next((d.date.isoformat() for d in itinerary.days for i in d.items if i.place_id == place_id), None)
        old = store.get_reservations(pid).get(place_id) or {}
        final_from = (body.entry_from if body.entry_from is not None else old.get("entry_from")) or None
        final_to = (body.entry_to if body.entry_to is not None else old.get("entry_to")) or None
        if final_to and not final_from:
            raise HTTPException(422, "时段结束要和入馆时间（时段开始）一起填")
        if final_from and final_to and final_to <= final_from:
            raise HTTPException(422, "时段结束必须晚于入馆时间")
        store.save_reservation(pid, place_id, status=body.status, confirmation_code=body.confirmation_code,
                               slot=body.slot, note=body.note, visit_date=visit,
                               entry_from=body.entry_from, entry_to=body.entry_to)
        rec = store.get_reservations(pid)[place_id]
        version, notes = _apply_booking_lock(pid, place_id, rec, itinerary, request)
        if version is not None:
            itinerary = store.load(pid, version)
        row = next(r for r in _reservation_rows(pid, itinerary, request) if r["place_id"] == place_id)
        return {**row, "version": version, "notes": notes}

    def _apply_booking_lock(pid: str, place_id: str, rec: dict, itinerary: Itinerary,
                            request: TravelRequest) -> tuple[int | None, list[str]]:
        """已订 + 有入馆时间 -> 把景点锁在这个时间（新版本）；取消勾选 / 清空时间 -> 只解开预订自己加的锁。"""
        item = next((i for d in itinerary.days for i in d.items if i.place_id == place_id), None)
        if item is None:
            return None, []
        errors_before = len(validate_itinerary(itinerary, request).errors)
        want = rec["status"] == "booked" and rec["entry_from"]
        if want:
            def mins(t: str) -> int:
                return int(t[:2]) * 60 + int(t[3:])

            target = entry_target(current=mins(item.time), start=mins(rec["entry_from"]),
                                  end=mins(rec["entry_to"]) if rec["entry_to"] else None)
            if item.locked and item.time == f"{target // 60:02d}:{target % 60:02d}" and rec["locked"]:
                return None, []                                  # 已经是这样了：不另起版本
            try:
                res = _apply(itinerary, request, SetEntryTime(place_id=place_id, start=rec["entry_from"],
                                                              end=rec["entry_to"] or None))
            except EditError as exc:
                return None, [f"预订已记下，但没能锁定行程：{exc}"]
            when = f"{rec['entry_from']}–{rec['entry_to']} 时段" if rec["entry_to"] else f"{rec['entry_from']} "
            got = next(i for d in res.itinerary.days for i in d.items if i.place_id == place_id)
            summary = f"预订：{item.title} {when}入场" + (f"，排在 {got.time}" if rec["entry_to"] else "") + "，已锁定"
            trigger, locked = "booking-lock", True
        elif rec["locked"]:
            try:
                res = _apply(itinerary, request, SetLock(place_id=place_id, locked=False))
            except EditError as exc:
                return None, [str(exc)]
            summary, trigger, locked = f"取消预订：{item.title} 已解锁（时间保持不动）", "booking-unlock", False
        else:
            return None, []
        after = validate_itinerary(res.itinerary, request)
        version = store.add_version(pid, res.itinerary, trigger=trigger, note=summary, change_summary=summary,
                                    errors_before=errors_before, errors_after=len(after.errors))
        store.set_reservation_locked(pid, place_id, locked)
        return version, list(res.notes)

    @app.delete("/plans/{pid}/reservations/{place_id}", status_code=204)
    def reset_reservation(pid: str, place_id: str) -> Response:
        _plan_or_404(pid)
        store.delete_reservation(pid, place_id)
        return Response(status_code=204)

    # ---- 住宿（规格 M06 S5 / S5b）------------------------------------------- #
    def _stay_summary(old: TravelRequest, new: TravelRequest) -> str:
        key = lambda s: (s.name, s.check_in, s.check_out)  # noqa: E731
        before, after = {key(s): s for s in old.stays}, {key(s): s for s in new.stays}
        bits = [f"新增「{k[0]}」{k[1]:%m-%d}→{k[2]:%m-%d}" for k in after if k not in before]
        bits += [f"删除「{k[0]}」" for k in before if k not in after]
        return "住宿：" + ("；".join(bits) if bits else "信息有调整")

    @app.put("/plans/{pid}/stays", response_model=PlanResponse, status_code=201)
    def save_stays(pid: str, body: StaysBody) -> PlanResponse:
        """整体替换住宿列表：生成一个「住宿更新」版本；行程内容不变，除非勾选了按新住宿重新排程。"""
        current, request = _load(pid, None)
        notes: list[str] = []
        raw = [dict(s) for s in body.stays]
        for s in raw:                                   # 有地址、没坐标：尝试定位（OSM），定位不到按所在区的中心
            addr = (s.get("address") or "").strip()
            if addr and s.get("lat") is None and s.get("lng") is None:
                where = request.city
                if request.legs:                        # 多城市：按这处住宿所在的城市定位
                    try:
                        check_in = s["check_in"] if isinstance(s["check_in"], date) else date.fromisoformat(
                            str(s["check_in"]))
                        where = request.legs[multiedit.leg_index_for_stay(request, check_in)].request.city
                    except (EditError, ValueError, KeyError):
                        pass
                if geocoder:
                    hit = geocoder(addr)
                else:                                   # 国内城市先问高德（S7d），问不到 / 不能问再退回 OpenStreetMap
                    hit = amap_client.geocode(addr, where.name_zh) if where.map_provider == "amap" else None
                    hit = hit or nominatim(addr, city=where)
                if hit is not None:
                    s["lat"], s["lng"] = hit.lat, hit.lng
                elif s.get("area"):
                    notes.append(f"「{s.get('name', '')}」的地址没能定位，路线按 {s['area']} 的中心估算。")
                else:
                    notes.append(f"「{s.get('name', '')}」的地址没能定位，"
                                 f"路线暂按{request.city.name_zh}市中心估算；填上所在区会更准。")
        itinerary = current
        try:
            if request.legs:
                itinerary, new_req, multi_notes = multiedit.set_stays_multi(current, request, raw,
                                                                            reschedule=body.reschedule)
                notes.extend(multi_notes)
            else:
                new_req = TravelRequest.model_validate({**request.model_dump(), "stays": raw})
        except ValidationError as exc:
            msg = "；".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
            raise HTTPException(422, msg) from exc
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        summary = _stay_summary(request, new_req)
        if body.reschedule and not request.legs:
            try:
                itinerary = apply_edit(current, new_req, Reschedule()).itinerary
            except EditError as exc:
                raise HTTPException(422, str(exc)) from exc
        if body.reschedule:
            summary += "；按新住宿重新排程"
        before, after = validate_itinerary(current, request), validate_itinerary(itinerary, new_req)
        version = store.add_version(pid, itinerary, trigger="stay-update", note="更新住宿", change_summary=summary,
                                    errors_before=len(before.errors), errors_after=len(after.errors), request=new_req)
        return PlanResponse(plan_id=pid, version=version, itinerary=store.load(pid, version), report=after,
                            stays=new_req.stays, notes=notes)

    @app.put("/plans/{pid}/transfers/{position}", response_model=PlanResponse, status_code=201)
    def save_plan_transfer(pid: str, position: int, body: TransferBody) -> PlanResponse:
        """改换城市的交通：前一座城市的返程和后一座的去程一起换，这两座城市按新时间重新排程（锁定的点不动）。"""
        current, request = _load(pid, None)
        if not request.legs:
            raise HTTPException(404, "这份行程没有换城市的交通")
        try:
            itinerary, new_req = multiedit.set_transfer_multi(
                current, request, position, mode=body.mode.value, depart_at=body.depart_at,
                arrive_at=body.arrive_at, number=body.number)
        except EditError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValidationError as exc:
            msg = "；".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
            raise HTTPException(422, msg) from exc
        before, after = validate_itinerary(current, request), validate_itinerary(itinerary, new_req)
        version = store.add_version(pid, itinerary, trigger="transport-update", note="换城市的交通",
                                    change_summary="换城市的交通改了，按新时间重新排程",
                                    errors_before=len(before.errors), errors_after=len(after.errors), request=new_req)
        return PlanResponse(plan_id=pid, version=version, itinerary=store.load(pid, version), report=after)

    @app.get("/plans/{pid}/stay-recommendation", response_model=dict[str, Any])
    def stay_recommendation(pid: str) -> dict[str, Any]:
        """住哪个区：按行程里各景点的位置给 2–3 个备选区（不含价格、治安等，不推荐具体酒店）。"""
        itinerary, request = _load(pid, None)
        recs = recommend_areas(itinerary, library_for_request(request))
        return {"recommendations": [r.__dict__ for r in recs], "caveat": STAY_CAVEAT}

    @app.delete("/plans/{pid}", status_code=204)
    def delete_plan(pid: str) -> Response:
        """永久删除一份行程（所有版本、分享链接一起）。前端点之前会弹确认框。"""
        _plan_or_404(pid)
        store.delete(pid)
        return Response(status_code=204)

    @app.put("/plans/{pid}/archive", response_model=dict[str, Any])
    def archive_plan(pid: str, body: ArchiveBody) -> dict[str, Any]:
        _plan_or_404(pid)
        store.set_archived(pid, body.archived)
        return {"archived": body.archived}

    def _revise_by_ops(pid, current, request, feedback: str, errors_before: int, ops=None) -> PlanResponse:
        """主题生成的行程：模型只把一句话翻译成操作，操作走和手动编辑相同的函数（规格 M04 S2）。"""
        try:
            if ops is not None:                              # 用户确认过的操作：照做，不再问模型
                t = parse_ops(ops, Translation())
            else:
                t = translate(current, request, feedback, make_client())
        except (LLMError, ModelUnavailable) as exc:
            raise HTTPException(502, f"修订失败：{exc}") from exc
        except NlEditError as exc:
            raise HTTPException(422, str(exc)) from exc
        removals = [e for e in t.edits if isinstance(e, RemovePlace)]
        if ops is None and len(removals) >= BULK_REMOVE:
            lib = load_library(request.city_id)
            raise HTTPException(409, detail={
                "needs_confirm": True,
                "message": f"这句话会删除 {len(removals)} 处安排，请确认后再执行",
                "preview": [describe(e, current, lib) for e in t.edits],
                "ops": [e.model_dump(mode="json") for e in t.edits],
            })
        rev = apply_ops(current, request, t)
        if not rev.applied:
            raise HTTPException(422, detail={
                "message": "没能根据这句话做出任何修改", "skipped": rev.skipped, "explanation": rev.explanation})
        report = validate_itinerary(rev.itinerary, request)
        change = diff_itineraries(current, rev.itinerary)
        version = store.add_version(
            pid, rev.itinerary, trigger="user-feedback", note=feedback,
            change_summary="；".join(rev.applied), errors_before=errors_before, errors_after=len(report.errors),
        )
        return PlanResponse(
            plan_id=pid, version=version, itinerary=store.load(pid, version), report=report,
            change=RevisionChange(summary="；".join(rev.applied), changed_days=change.changed_days,
                                  added=change.added, removed=change.removed,
                                  applied=rev.applied, skipped=rev.skipped),
        )

    # ---- render -------------------------------------------------- #
    @app.get("/plans/{pid}/itinerary.md", response_class=PlainTextResponse)
    def render_md(pid: str, version: int | None = Query(None, ge=1)) -> PlainTextResponse:
        itinerary, request = _load(pid, version)
        report = validate_itinerary(itinerary, request)
        return PlainTextResponse(render_markdown(itinerary, request, report,
                                                  reservations=_res_map(pid, itinerary, request),
                                                  weather=_weather_map(itinerary, request),
                                                  budget=_budget_or_none(pid, itinerary, request),
                                                  notes=store.notes_get(pid),
                                                  stories=_stories(itinerary, request)),
                                 media_type="text/markdown; charset=utf-8")

    @app.get("/plans/{pid}/itinerary.html", response_class=HTMLResponse)
    def render_html_ep(pid: str, version: int | None = Query(None, ge=1)) -> HTMLResponse:
        itinerary, request = _load(pid, version)
        report = validate_itinerary(itinerary, request)
        return HTMLResponse(render_html(itinerary, request, report, reservations=_res_map(pid, itinerary, request),
                                        weather=_weather_map(itinerary, request),
                                        budget=_budget_or_none(pid, itinerary, request), notes=store.notes_get(pid),
                                        stories=_stories(itinerary, request)))

    def _export_stem(pid: str, itinerary: Itinerary) -> str:
        """导出文件名（不含版本和扩展名）：单城市就是行程号；多城市按行程里的顺序写上所有目的地，
        如「上海市-重庆市-20261101-9cd41c」（行程号里本来只有第一座城市）。"""
        names = [re.sub(r'[\\/:*?"<>|\s]+', "", b.trip.destination) for b in itinerary.legs or []]
        if len(names) < 2:
            return pid
        rest = pid[len(names[0]) + 1:] if pid.startswith(names[0] + "-") else pid
        return "-".join([*names, rest])

    export_cache = store.database.path.parent / "pdf_cache"

    def _prune_export_cache(keep: int = 30) -> None:
        for pattern in ("*.pdf", "*.png"):
            files = sorted(export_cache.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[keep:]:
                old.unlink(missing_ok=True)

    def _cached_export(html: str, ext: str, make: Callable[[Path, Path], Path], what: str) -> Path:
        """导出文件按「渲染出来的 HTML 的哈希」缓存：内容没变就直接给上次的文件。"""
        key = hashlib.sha256(html.encode("utf-8")).hexdigest()[:32]
        cached = export_cache / f"{key}.{ext}"
        if cached.exists():
            cached.touch()
            return cached
        tmp = Path(tempfile.mkdtemp(prefix="atp_api_"))
        try:
            src = tmp / "itinerary.html"
            src.write_text(html, encoding="utf-8")
            made = make(src, tmp / f"itinerary.{ext}")
            export_cache.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(made, cached.with_suffix(".tmp"))
            cached.with_suffix(".tmp").replace(cached)
            _prune_export_cache()
        except Exception as exc:
            raise HTTPException(503, f"{what}生成失败（本机缺 Chrome/Edge 或 Playwright？）：{exc}") from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return cached

    @app.get("/plans/{pid}/itinerary.pdf")
    def render_pdf_ep(pid: str, version: int | None = Query(None, ge=1),
                      page_per_day: bool = Query(True)) -> FileResponse:
        """PDF 按内容缓存（版本、天气、预订、预算、备注任何一项变了才重新生成）。"""
        itinerary, request = _load(pid, version)
        report = validate_itinerary(itinerary, request)
        html = render_html(itinerary, request, report, page_per_day=page_per_day,
                           reservations=_res_map(pid, itinerary, request),
                           weather=_weather_map(itinerary, request),
                           budget=_budget_or_none(pid, itinerary, request), notes=store.notes_get(pid),
                           stories=_stories(itinerary, request), inline_images=True)
        path = _cached_export(html, "pdf", lambda src, dst: html_to_pdf(src, dst), "PDF ")
        v = version or store.latest_version(pid)
        return FileResponse(path, media_type="application/pdf", filename=f"{_export_stem(pid, itinerary)}-v{v}.pdf")

    @app.get("/plans/{pid}/itinerary.png")
    def render_png_ep(pid: str, version: int | None = Query(None, ge=1)) -> FileResponse:
        """长图（规格 M08 S6）：拿去发给别人看的，所以不含预算 / 出行人年龄 / 预订确认码 / 备注。"""
        itinerary, request = _load(pid, version)
        report = validate_itinerary(itinerary, request)
        html = render_html(itinerary, request, report, page_per_day=False,
                           weather=_weather_map(itinerary, request), image=True, stories=_stories(itinerary, request),
                           inline_images=True)
        path = _cached_export(html, "png", lambda src, dst: html_to_png(src, dst), "长图")
        v = version or store.latest_version(pid)
        return FileResponse(path, media_type="image/png", filename=f"{_export_stem(pid, itinerary)}-v{v}.png")

    # ---- 地图（规格 M05 S7）：高德 JS 的 Key 运行时下发；配额保护见 S7c ---- #
    @app.get("/map/config", response_model=dict[str, Any])
    def map_config() -> dict[str, Any]:
        """前端画国内地图前问一次：能用高德就给 JS Key（算一次地图加载），否则说明为什么退回 OpenStreetMap。"""
        keys = map_config_keys.js_keys()
        if keys is None:
            return {"provider": "osm", "reason": "no_key"}
        if not map_usage.allow("map_init"):
            return {"provider": "osm", "reason": "quota"}
        map_usage.record("map_init")
        return {"provider": "amap", "js_key": keys[0], "security_code": keys[1]}

    @app.get("/map/usage", response_model=dict[str, Any])
    def map_usage_summary() -> dict[str, Any]:
        """高德本月用量：每类服务已用 / 自留上限 / 免费额度（只统计我们代码里的调用；官方数字看高德控制台）。"""
        return {"services": map_usage.snapshot()}

    # ---- 模型调用追踪 ---------------------------------------------------- #
    @app.get("/llm/usage", response_model=dict[str, Any])
    def llm_usage(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        """模型用量总览：调用次数、失败数、平均耗时、token、费用，按用途分组，外加最近的调用。"""
        return {**llm_calls.summary(), "recent": llm_calls.recent(limit)}

    @app.delete("/llm/usage", status_code=204)
    def reset_llm_usage() -> Response:
        """清零模型用量统计（只删调用记录，不动行程）。"""
        llm_calls.reset()
        return Response(status_code=204)

    def _trace(task: Task) -> dict[str, Any]:
        def seconds(s) -> float | None:
            if not (s.started_at and s.finished_at):
                return None
            took = datetime.fromisoformat(s.finished_at) - datetime.fromisoformat(s.started_at)
            return round(took.total_seconds(), 2)

        degraded, reason = False, None
        if task.plan_id:
            it = store.load(task.plan_id)
            if it.generation and it.generation.narration == "template":
                degraded, reason = True, it.generation.narration_note or "说明文字由模板生成"
        return {
            "task_id": task.id, "status": task.status, "plan_id": task.plan_id,
            "steps": [{"name": s.name, "state": s.state, "seconds": seconds(s)} for s in task.steps],
            "llm": llm_calls.summary(task_id=task.id), "calls": list(reversed(llm_calls.recent(100, task_id=task.id))),
            "degraded": degraded, "degraded_reason": reason,
        }

    @app.get("/tasks/{tid}/trace", response_model=dict[str, Any])
    def task_trace(tid: str) -> dict[str, Any]:
        """一次生成的轨迹：每一步耗时、模型调用（次数 / 耗时 / token / 成败）、是否降级。"""
        try:
            return _trace(tasks.get(tid))
        except TaskNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/plans/{pid}/trace", response_model=dict[str, Any])
    def plan_trace(pid: str) -> dict[str, Any]:
        _plan_or_404(pid)
        try:
            return _trace(tasks.for_plan(pid))
        except TaskNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    # ---- 导出：日历 / 地图 / 路线 ----------------------------------------- #
    def _attachment(filename: str) -> str:
        """下载文件名：行程号里有中文城市名（里昂-20261212-…）时，HTTP 头里不能直接放，要按 RFC 5987 编码。"""
        return f"attachment; filename*=UTF-8''{urlquote(filename)}"

    @app.get("/plans/{pid}/itinerary.ics")
    def render_ics_ep(pid: str, version: int | None = Query(None, ge=1)) -> Response:
        itinerary, _request = _load(pid, version)
        v = version or store.latest_version(pid)
        return Response(render_ics(itinerary, uid_prefix=pid), media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": _attachment(f"{_export_stem(pid, itinerary)}-v{v}.ics")})

    @app.get("/plans/{pid}/itinerary.kml")
    def render_kml_ep(pid: str, version: int | None = Query(None, ge=1)) -> Response:
        itinerary, _request = _load(pid, version)
        v = version or store.latest_version(pid)
        return Response(render_kml(itinerary), media_type="application/vnd.google-earth.kml+xml; charset=utf-8",
                        headers={"Content-Disposition": _attachment(f"{_export_stem(pid, itinerary)}-v{v}.kml")})

    @app.get("/plans/{pid}/route", response_model=dict[str, Any])
    def plan_route(pid: str, version: int | None = Query(None, ge=1)) -> dict[str, Any]:
        """每天的路线：按顺序经过的点（核对过的坐标）、颜色、直线距离、Google 地图导航链接。"""
        itinerary, request = _load(pid, version)
        out = build_route(itinerary).as_dict()
        lib = library_for_request(request)
        ids = {i.place_id for d in itinerary.days for i in d.items if i.place_id in lib.places}
        prices = [lib.places[i].price_eur for i in ids if lib.places[i].price_eur is not None]
        out["ticket_eur"] = sum(prices) if prices else None     # 门票合计（仅库里有票价的点）；一个都没有 → None
        out["ticket_currency"] = "EUR"                          # 地点库里的票价现在都是欧元（人民币票价另做）
        return out

    WEATHER_NOTE = "天气来自 Open-Meteo；15 天以外的日期显示去年同一天的实际天气，仅作参考。"

    def _weather_days(itinerary: Itinerary, request: TravelRequest) -> list[dict[str, Any]] | None:
        """每天的天气 + 行程里的室外 / 半室外点；取不到（接口失败 / 目的地没登记）返回 None。"""
        if request.legs:                       # 多城市：每座城市查自己的天气；换城市那天用后一座城市的
            found = {}
            for leg in request.legs:
                dates = [d.date for d in itinerary.days if leg.request.start_date <= d.date <= leg.request.end_date]
                for w in weather_service.get(leg.request.destination, dates, today()) or []:
                    found[w.date] = w
            days = [found[d.date] for d in itinerary.days if d.date in found]
        else:
            days = weather_service.get(request.destination, [d.date for d in itinerary.days], today())
        if not days:
            return None
        lib = library_for_request(request)
        by_date = {d.date: d for d in itinerary.days}
        out = []
        for w in days:
            places = [i for i in by_date[w.date].items if i.place_id in lib.places]
            out.append({
                **w.model_dump(mode="json"),
                "outdoor_places": [{"place_id": i.place_id, "title": i.title} for i in places
                                   if lib.places[i.place_id].setting == Setting.outdoor],
                "mixed_places": [{"place_id": i.place_id, "title": i.title} for i in places
                                 if lib.places[i.place_id].setting == Setting.mixed],
            })
        return out

    def _weather_payload(itinerary: Itinerary, request: TravelRequest) -> dict[str, Any]:
        days = _weather_days(itinerary, request)
        return {"available": True, "days": days, "note": WEATHER_NOTE} if days else {"available": False, "days": []}

    def _weather_map(itinerary: Itinerary, request: TravelRequest) -> dict[str, dict[str, Any]] | None:
        """导出（PDF / HTML / Markdown）用：日期 -> 当天天气；取不到就是 None，导出照常。"""
        days = _weather_days(itinerary, request)
        return {d["date"]: d for d in days} if days else None

    def _settings(pid: str, request: TravelRequest) -> dict[str, Any]:
        """当前预算设置：用户改过的；没改过就用行程需求里的初始值。"""
        row = store.get_budget_settings(pid)
        if row is not None:
            return row
        return {"travelers": [t.model_dump(mode="json") for t in request.travelers],
                "currency": request.currency.value, "cap_amount": request.budget_cap,
                "cap_currency": request.currency.value, "meal_eur_per_day": None}

    DAYTRIP_NO_BUDGET = ("一日游暂不估算预算：目的地的门票价格还是没核对的开放数据草稿，"
                         "火车票请以 DB Navigator 等官方渠道的价格为准。")

    CITY_NO_BUDGET = ("这座城市的数据是自动建的，门票和交通价格没有核对，暂不估算预算；"
                      "出发前请按各景点官网的价格自己估算。")

    def _has_budget(request: TravelRequest) -> bool:
        """有核对过价格的城市才估算；多城市里只要有一座有就估（没有的在备注里点名）。"""
        if request.legs:
            return any(has_budget_data(leg.request.city_id) for leg in request.legs)
        return has_budget_data(request.city_id)

    def _budget_or_none(pid: str, itinerary: Itinerary, request: TravelRequest) -> dict[str, Any] | None:
        """导出用：一日游 / 现建的城市没有预算数据（没有核对过的价格），不写费用估算一节，其余照常。"""
        if request.day_trip_from or not _has_budget(request):
            return None
        return _budget(pid, itinerary, request)

    def _budget(pid: str, itinerary: Itinerary, request: TravelRequest) -> dict[str, Any]:
        row = _settings(pid, request)
        settings = BudgetSettings(
            travelers=tuple(Traveler.model_validate(t) for t in row["travelers"]), currency=row["currency"],
            cap_amount=row["cap_amount"], cap_currency=row["cap_currency"], meal_eur_per_day=row["meal_eur_per_day"])
        fx = fx_service.get("EUR", "CNY")
        if request.legs:
            return estimate_budget_multi(itinerary, request, settings, fx)
        return estimate_budget(itinerary, request, settings, fx)

    @app.get("/plans/{pid}/budget", response_model=dict[str, Any])
    def plan_budget(pid: str, version: int | None = Query(None, ge=1)) -> dict[str, Any]:
        """预算估算：门票 / 餐饮 / 交通 / 住宿，逐人计价，结算货币 + 并列货币，超预算时带替换建议（规格 M09）。"""
        itinerary, request = _load(pid, version)
        if request.day_trip_from:
            raise HTTPException(404, DAYTRIP_NO_BUDGET)
        if not _has_budget(request):
            raise HTTPException(404, CITY_NO_BUDGET)
        return _budget(pid, itinerary, request)

    @app.get("/plans/{pid}/budget-settings", response_model=dict[str, Any])
    def get_budget_settings(pid: str) -> dict[str, Any]:
        _, request = _load(pid, None)
        return _settings(pid, request)

    @app.put("/plans/{pid}/budget-settings", response_model=dict[str, Any])
    def save_budget_settings(pid: str, body: BudgetSettingsBody) -> dict[str, Any]:
        _, request = _load(pid, None)
        cur = _settings(pid, request)
        for key in body.model_fields_set:
            value = getattr(body, key)
            if key in ("travelers", "currency", "cap_currency") and value is None:
                raise HTTPException(422, f"{key} 不能清空")
            if key == "travelers":
                cur["travelers"] = [t.model_dump(mode="json") for t in value]
            elif key in ("currency", "cap_currency"):
                cur[key] = value.value
            else:
                cur[key] = value
        store.put_budget_settings(pid, **cur)
        return cur

    @app.get("/plans/{pid}/weather", response_model=dict[str, Any])
    def plan_weather(pid: str, version: int | None = Query(None, ge=1)) -> dict[str, Any]:
        """每天的天气（预报或去年同期）+ 行程里的室外点。接口失败 / 目的地没登记：available=false，页面不显示天气。"""
        itinerary, request = _load(pid, version)
        return _weather_payload(itinerary, request)

    # ---- 随行清单：行李 / 待办 / 每日笔记（规格 M10 S2–S4）----------------------- #
    def _packing_payload(pid: str, request: TravelRequest) -> dict[str, Any]:
        facts = {}
        for cid in multicity.leg_ids(request) if request.legs else [request.city_id]:
            try:
                data = load_packing(cid)
                facts.update({t.key: data.facts[t.fact] for t in data.items if t.fact and t.show_source})
            except FileNotFoundError:
                pass
        items = store.packing_list(pid)
        for it in items:
            f = facts.get(it["key"] or "")
            if f:
                it.update(source_note=f.note, source_url=f.source_url, checked_at=f.checked_at.isoformat())
            elif it["key"] == "student-card" and "ISIC" in it["text"]:          # ISIC 官网：学生优惠的依据
                it.update(source_note="ISIC 官网：学生身份认证，100 多个国家有学生优惠", source_url=ISIC_URL,
                          checked_at=ISIC_CHECKED.isoformat())
        extra = sorted({i["category"] for i in items} - set(CATEGORY_ORDER))
        return {"items": items, "packed": sum(1 for i in items if i["packed"]), "total": len(items),
                "categories": [c for c in [*CATEGORY_ORDER, *extra] if any(i["category"] == c for i in items)],
                "generated": store.packing_has_generated(pid), "hidden": store.packing_hidden_count(pid)}

    @app.get("/plans/{pid}/packing", response_model=dict[str, Any])
    def get_packing(pid: str) -> dict[str, Any]:
        _, request = _load(pid, None)
        return _packing_payload(pid, request)

    @app.post("/plans/{pid}/packing/generate", response_model=dict[str, Any])
    def generate_packing_list(pid: str) -> dict[str, Any]:
        """按季节 / 天数 / 出行人 / 行程内容补上推荐项；已有的（勾过的、改过的、自己加的、删掉的）都不动。"""
        itinerary, request = _load(pid, None)
        row = _settings(pid, request)
        party = [Traveler.model_validate(t) for t in row["travelers"]]
        make = generate_packing_multi if request.legs else generate_packing
        gen = make(itinerary, request, party, _weather_days(itinerary, request))
        added = store.packing_merge(pid, gen["items"])
        return {**_packing_payload(pid, request), "added": added, "basis": gen["basis"], "climate": gen["climate"]}

    @app.post("/plans/{pid}/packing/restore", response_model=dict[str, Any])
    def restore_packing(pid: str) -> dict[str, Any]:
        _, request = _load(pid, None)
        restored = store.packing_restore(pid)
        return {**_packing_payload(pid, request), "restored": restored}

    @app.post("/plans/{pid}/packing", response_model=dict[str, Any], status_code=201)
    def add_packing(pid: str, body: PackingAddBody) -> dict[str, Any]:
        _, request = _load(pid, None)
        store.packing_add(pid, body.category.strip(), body.text.strip())
        return _packing_payload(pid, request)

    @app.patch("/plans/{pid}/packing/{item_id}", response_model=dict[str, Any])
    def patch_packing(pid: str, item_id: int, body: PackingPatchBody) -> dict[str, Any]:
        _, request = _load(pid, None)
        fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.model_dump(exclude_none=True).items()}
        if not store.packing_update(pid, item_id, **fields):
            raise HTTPException(404, "没有这一项")
        return _packing_payload(pid, request)

    @app.delete("/plans/{pid}/packing/{item_id}", response_model=dict[str, Any])
    def delete_packing(pid: str, item_id: int) -> dict[str, Any]:
        _, request = _load(pid, None)
        if not store.packing_delete(pid, item_id):
            raise HTTPException(404, "没有这一项")
        return _packing_payload(pid, request)

    def _todos_payload(pid: str, itinerary: Itinerary, request: TravelRequest) -> dict[str, Any]:
        now = booking.today()
        items = store.todos_list(pid)
        for t in items:
            t["urgency"] = todo_urgency(date.fromisoformat(t["due"]) if t["due"] else None, t["done"], now)
        keys = {t["key"] for t in items if t["key"]}
        suggestions = suggest_todos(itinerary.days[0].date, now, _reservation_rows(pid, itinerary, request), keys,
                                    request.transport, request.city)
        return {"today": now.isoformat(), "items": items, "summary": todo_summary(items), "suggestions": suggestions}

    @app.get("/plans/{pid}/todos", response_model=dict[str, Any])
    def get_todos(pid: str) -> dict[str, Any]:
        itinerary, request = _load(pid, None)
        return _todos_payload(pid, itinerary, request)

    @app.post("/plans/{pid}/todos", response_model=dict[str, Any], status_code=201)
    def add_todo(pid: str, body: TodoAddBody) -> dict[str, Any]:
        itinerary, request = _load(pid, None)
        if body.key and any(t["key"] == body.key for t in store.todos_list(pid)):
            return _todos_payload(pid, itinerary, request)                      # 推荐待办已经加过了：不重复
        store.todo_add(pid, body.text.strip(), body.due.isoformat() if body.due else None, body.key)
        return _todos_payload(pid, itinerary, request)

    @app.patch("/plans/{pid}/todos/{todo_id}", response_model=dict[str, Any])
    def patch_todo(pid: str, todo_id: int, body: TodoPatchBody) -> dict[str, Any]:
        itinerary, request = _load(pid, None)
        fields: dict[str, Any] = {}
        for key in body.model_fields_set:
            value = getattr(body, key)
            if key == "due":
                fields["due"] = value.isoformat() if value else None
            elif value is not None:
                fields[key] = value.strip() if isinstance(value, str) else value
        if not store.todo_update(pid, todo_id, **fields):
            raise HTTPException(404, "没有这一项待办")
        return _todos_payload(pid, itinerary, request)

    @app.delete("/plans/{pid}/todos/{todo_id}", response_model=dict[str, Any])
    def delete_todo(pid: str, todo_id: int) -> dict[str, Any]:
        itinerary, request = _load(pid, None)
        if not store.todo_delete(pid, todo_id):
            raise HTTPException(404, "没有这一项待办")
        return _todos_payload(pid, itinerary, request)

    @app.get("/plans/{pid}/notes", response_model=dict[str, Any])
    def get_notes(pid: str) -> dict[str, Any]:
        _plan_or_404(pid)
        return {"notes": store.notes_get(pid)}

    @app.put("/plans/{pid}/notes", response_model=dict[str, Any])
    def put_note(pid: str, body: NoteBody) -> dict[str, Any]:
        """给一条行程安排（或以前的整天笔记）写备注；空文本 = 删除。备注对象必须在这份行程里。"""
        itinerary, _request = _load(pid, None)
        if body.ref not in valid_note_refs(itinerary):
            raise HTTPException(422, "这份行程里没有这一条安排，不能写备注")
        store.note_put(pid, body.ref, body.text.strip())
        return {"notes": store.notes_get(pid)}

    # ---- 公开只读分享 ---------------------------------------------------- #
    def _share_path(token: str | None, pid: str | None = None) -> dict[str, Any]:
        options = store.share_options(pid) if (token and pid) else {k: False for k in store.SHARE_OPTIONS}
        return {"active": token is not None, "token": token, "path": f"/s/{token}" if token else None,
                "options": options}

    def _plan_or_404(pid: str) -> None:
        try:
            store.latest_version(pid)
        except PlanNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/plans/{pid}/share", response_model=dict[str, Any], status_code=201)
    def create_share(pid: str) -> dict[str, Any]:
        _plan_or_404(pid)
        return _share_path(store.create_share(pid), pid)

    @app.get("/plans/{pid}/share", response_model=dict[str, Any])
    def share_status(pid: str) -> dict[str, Any]:
        _plan_or_404(pid)
        return _share_path(store.active_share(pid), pid)

    @app.put("/plans/{pid}/share/options", response_model=dict[str, Any])
    def set_share_options(pid: str, body: ShareOptionsBody) -> dict[str, Any]:
        """分享时一起分享什么（行李清单 / 待办 / 行程备注）；链接不变，立刻生效。"""
        _plan_or_404(pid)
        if not store.set_share_options(pid, body.model_dump()):
            raise HTTPException(409, "还没有有效的分享链接，先生成链接")
        return _share_path(store.active_share(pid), pid)

    @app.delete("/plans/{pid}/share", status_code=204)
    def revoke_share(pid: str) -> Response:
        _plan_or_404(pid)
        store.revoke_share(pid)
        return Response(status_code=204)

    @app.get("/share/{token}", response_model=dict[str, Any])
    def read_share(token: str) -> dict[str, Any]:
        """无账号的朋友打开的只读内容：最新保存的版本 + 路线。不含行程号和需求原文。"""
        hit = store.resolve_share(token)
        if hit is None:
            raise HTTPException(404, "没有这个分享链接")
        pid, revoked = hit
        if revoked:
            raise HTTPException(410, "链接已失效")
        itinerary, request = _load(pid, None)
        out: dict[str, Any] = {"itinerary": itinerary.model_dump(mode="json"), "version": store.latest_version(pid),
                               "city_id": request.city_id, "stories": _stories(itinerary, request),
                               "route": build_route(itinerary).as_dict(),
                               "weather": _weather_payload(itinerary, request)}
        # 只有分享的人勾了的才放进响应（没勾的字段根本不在里面），且只带朋友看得懂的字段
        opts = store.token_options(token)
        out["shared"] = [k for k in store.SHARE_OPTIONS if opts[k]]
        if opts["packing"]:
            pk = _packing_payload(pid, request)
            out["packing"] = {"items": [{"category": i["category"], "text": i["text"], "packed": i["packed"]}
                                        for i in pk["items"]],
                              "categories": pk["categories"], "packed": pk["packed"], "total": pk["total"]}
        if opts["todos"]:
            out["todos"] = [{"text": t["text"], "due": t["due"], "done": t["done"]} for t in store.todos_list(pid)]
        if opts["notes"]:
            out["notes"] = store.notes_get(pid)
        return out

    return app


app = create_app()
