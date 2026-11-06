"""M2：把 TravelRequest 交给 LLM，产出结构合法的 Itinerary。

流程：拼 system / user 提示词 → 调 LLM（structured output）→ 解析成 Itinerary。
解析失败会带着错误信息重试一次。语义质检（密度、必去项、预约）在 M3 的 validator 里做。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from jinja2 import Template
from pydantic import ValidationError

from app.cities import DEFAULT_CITY, itinerary_city_id
from app.llm import LLMClient, LLMError, get_client
from app.llm_trace import trace_context
from app.schemas import BookingItem, Itinerary, TravelRequest

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "data" / "knowledge"

_SYSTEM_PROMPT = (_PROMPT_DIR / "plan_system.txt").read_text(encoding="utf-8")
_USER_TEMPLATE = Template((_PROMPT_DIR / "plan_user.jinja").read_text(encoding="utf-8"))


@dataclass
class PlanResult:
    itinerary: Itinerary
    raw_json: str
    attempts: int
    warnings: list[str] = field(default_factory=list)


def load_knowledge(city_id: str = DEFAULT_CITY) -> str:
    """规划要点简报（从 SOP 提炼的确定性事实：闭馆日、预约、通票、交通、节奏）。每座城市一份。"""
    return (_KNOWLEDGE_DIR / f"{city_id}-planning-brief.md").read_text(encoding="utf-8").strip()


def attraction_catalog(city_id: str = DEFAULT_CITY) -> list[dict]:
    """景点白名单。planner 把景点/博物馆限定在这张表里，避免模型编造。每座城市一份。"""
    path = _KNOWLEDGE_DIR / f"{city_id}-attractions.json"
    return json.loads(path.read_text(encoding="utf-8"))["attractions"]


def _score(entry: dict, interests: set[str]) -> int:
    return len(set(entry.get("interests", [])) & interests)


def format_attractions(request: TravelRequest, catalog: list[dict] | None = None) -> str:
    """把白名单排成一张紧凑表；和用户兴趣匹配度高的排前面。"""
    catalog = catalog if catalog is not None else attraction_catalog(request.city_id)
    interests = {i.strip().lower() for i in request.interests}
    ranked = sorted(catalog, key=lambda e: (-_score(e, interests), e["name_zh"]))
    lines = ["| 名称 | 区 | 类型 | 约需 | 最近地铁 | 闭馆 | 需预约 |", "|---|---|---|---|---|---|---|"]
    for e in ranked:
        lines.append(
            f"| {e['name_zh']} | {e['arr']} | {e['type']} | {e['duration_min']}min "
            f"| {e['metro']} | {e['closed'] or '—'} | {'是' if e['booking'] else '否'} |"
        )
    return "\n".join(lines)


def _iter_dates(request: TravelRequest) -> list[str]:
    days = request.trip_days
    return [
        (request.start_date + timedelta(days=i)).isoformat() for i in range(days)
    ]


def build_prompts(request: TravelRequest, knowledge: str | None = None) -> tuple[str, str]:
    knowledge = knowledge if knowledge is not None else load_knowledge(request.city_id)
    user = _USER_TEMPLATE.render(
        r=request,
        dates=_iter_dates(request),
        knowledge=knowledge,
        attractions=format_attractions(request),
    )
    return _SYSTEM_PROMPT, user


def _reconcile_bookings(itinerary: Itinerary) -> Itinerary:
    """确定性补账：每个 booking_required 的项目在 booking_checklist 里都有条目。

    模型经常标了 booking_required=true 却忘了写清单——这是可以算出来的派生数据，直接补。
    """
    listed = " ".join(b.title for b in itinerary.booking_checklist).lower()
    for day in itinerary.days:
        for item in day.items:
            if not item.booking_required:
                continue
            key = re.split(r"[（(]", item.title.strip())[0].strip()
            if key and key.lower() not in listed:
                itinerary.booking_checklist.append(
                    BookingItem(title=key, reason="行程中标记为需预约，请提前在官网订时段票",
                                deadline_hint="确定日期后尽早预约")
                )
                listed += " " + key.lower()
    return itinerary


def _catalog_lookup(city_id: str = DEFAULT_CITY) -> dict[str, dict]:
    return {e["name_zh"]: e for e in attraction_catalog(city_id)}


def _match_catalog(title: str, lookup: dict[str, dict]) -> dict | None:
    """把 booking_checklist/item 的标题对上白名单条目。

    标题经常带后缀（"门票"「需预约」…）或用别名（"迪士尼乐园" vs 白名单的
    "迪士尼乐园巴黎"），纯前缀/包含匹配够不到，所以复用 validator 的地点别名表。
    """
    from app.validator import PlaceIndex  # 延迟导入，避免和 validator 的模块级循环依赖

    text = title.strip().lower()
    if not text:
        return None
    if title.strip() in lookup:
        return lookup[title.strip()]
    places = PlaceIndex.load()
    for name, entry in lookup.items():
        if any(s in text for s in places.surfaces_for(name)):
            return entry
    return None


def _enrich_booking_urls(itinerary: Itinerary) -> Itinerary:
    """URL 一律不信模型的：官网链接只能来自白名单，白名单没有就置空。

    模型会编——它给「卢浮宫」编出一个听起来很像官网的 /online-tickets 路径，
    这种半真不假的链接比没链接更危险，所以匹配上白名单就整个覆盖，不只是「补空」。
    白名单里没有的项目，宁可留 null 也不用模型自己写的网址。
    """
    lookup = _catalog_lookup(itinerary_city_id(itinerary.trip.destination))
    for booking in itinerary.booking_checklist:
        match = _match_catalog(booking.title, lookup)
        booking.url = match.get("url") if match else None
    return itinerary


def _parse(raw: str) -> Itinerary:
    data = json.loads(raw)
    itinerary = Itinerary.model_validate(data)
    itinerary = _reconcile_bookings(itinerary)
    itinerary = _enrich_booking_urls(itinerary)
    return itinerary


class PlanShapeError(ValueError):
    """结构合法但和需求对不上（天数/日期错位），需要重试。"""


def _semantic_check(itinerary: Itinerary, request: TravelRequest) -> None:
    """只查最基础的对齐：天数、逐日日期。丰富的语义质检留给 M3 validator。"""
    expected = _iter_dates(request)
    got = [d.date.isoformat() for d in itinerary.days]
    if len(got) != len(expected):
        raise PlanShapeError(
            f"days 应该有 {len(expected)} 天，实际 {len(got)} 天"
        )
    if got != expected:
        raise PlanShapeError(
            f"days 的日期必须是 {expected}，实际是 {got}"
        )


def run_attempts(
    client: LLMClient,
    system: str,
    user: str,
    request: TravelRequest,
    *,
    max_retries: int = 1,
) -> PlanResult:
    """调 LLM → 解析 → 基础对齐检查，失败则带着错误信息重试。

    生成（M2）和修订（M5）共用这段循环。
    """
    schema = Itinerary.model_json_schema()
    warnings: list[str] = []
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 2):
        prompt = user
        if last_error is not None:
            prompt = f"{user}\n\n## 上一次输出的问题（请修正后重新输出完整 JSON）\n{last_error}"
        with trace_context(purpose="freeform_generate"):
            raw = client.complete_json(system=system, user=prompt, schema=schema)
        try:
            itinerary = _parse(raw)
            _semantic_check(itinerary, request)
        except (json.JSONDecodeError, ValidationError, PlanShapeError) as exc:
            last_error = exc
            warnings.append(f"第 {attempt} 次输出不合格：{type(exc).__name__}")
            continue
        return PlanResult(itinerary=itinerary, raw_json=raw, attempts=attempt, warnings=warnings)

    raise LLMError(f"连续 {max_retries + 1} 次都没拿到合法 Itinerary；最后错误：{last_error}")


def generate_itinerary(
    request: TravelRequest,
    *,
    client: LLMClient | None = None,
    max_retries: int = 1,
) -> PlanResult:
    client = client or get_client()
    system, user = build_prompts(request)
    return run_attempts(client, system, user, request, max_retries=max_retries)
