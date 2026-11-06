"""LLM Judge：主题契合度（规格 M07 S5）——开发者评测工具，不在产品界面里、不存库。

只评一件事——这份行程的景点和节奏是否符合所选主题的定位，给 1–5 分和一句理由。
路线、价格、开放时间这些有确定性规则，不交给模型评。结果是「参考级」，不产生质检 error / warning，不影响「可交付」结论；
由 scripts/eval_judge.py 评测时调用，用来发现「排程算法生成的行程是否真的围绕所选主题」，再去改选点规则。

模型只拿到：主题名 + 一句话定位 + 描述，以及每天每个安排的开始时间、名称、类别、室内 / 室外和最佳时段
（不给价格、开放时间）。返回不合规（不是 JSON / 分数不在 1–5）时重试一次。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.cities import itinerary_city_id
from app.library import PlaceLibrary, load_library
from app.llm import LLMClient, LLMError
from app.llm_trace import trace_context
from app.schemas import Itinerary

PURPOSE = "judge"
THRESHOLD = 3            # 低于这个分数页面显示为「提醒」
MAX_REASON_CHARS = 60
MAX_ATTEMPTS = 2         # 返回不合规时重试 1 次

_CATEGORY_ZH = {"museum": "博物馆", "sight": "景点", "park": "公园", "shopping": "购物", "food": "餐饮",
                "entertainment": "演出娱乐", "activity": "活动"}
_BEST_TIME_ZH = {"morning": "上午最佳", "afternoon": "下午最佳", "evening": "晚上最佳", "golden_hour": "黄昏最佳"}
_SETTING_ZH = {"indoor": "室内", "outdoor": "室外", "mixed": "半室外"}
_TYPE_ZH = {"meal": "用餐", "logistics": "交通 / 住宿", "rest": "休息", "food": "餐饮", "sight": "景点",
            "museum": "博物馆", "activity": "活动"}


class JudgeError(RuntimeError):
    """评估没法给出可用的结果（行程不是按主题生成的，或模型返回的内容不合规）。"""


class JudgeUnavailable(JudgeError):
    """模型没开 / 超时 / 调用失败。"""


@dataclass(frozen=True)
class JudgeResult:
    score: int
    reason: str
    theme_name: str


SYSTEM = (
    "你是旅行行程的评审。你只评一件事：这份行程里的景点和节奏，是否符合所选主题的定位。"
    "不要评路线顺不顺、价格、开放时间——那些由别的程序检查。\n"
    "评分 1–5 的整数：5 = 非常契合（景点基本都围绕这个主题），4 = 比较契合，3 = 一般（有一半沾边），"
    "2 = 不太契合，1 = 基本无关。\n"
    '只输出 JSON：{"score": 整数, "reason": "一句中文理由，不超过 60 字，要点名行程里具体的景点或类别"}。'
)


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 5}, "reason": {"type": "string"}},
        "required": ["score", "reason"],
    }


def _prompt(it: Itinerary, lib: PlaceLibrary) -> str:
    gen = it.generation
    assert gen is not None
    lines = ["【所选主题】"]
    for tid in gen.themes or [gen.theme_id]:
        t = lib.themes.get(tid)
        if t:
            lines.append(f"- {t.name_zh}：{t.tagline}。{t.description}")
    lines.append("\n【行程】")
    for n, day in enumerate(it.days, start=1):
        parts = []
        for item in day.items:
            place = lib.places.get(item.place_id) if item.place_id else None
            kind = _CATEGORY_ZH.get(place.category.value, "") if place else _TYPE_ZH.get(item.type.value, "")
            # 时间和「室内 / 室外、最佳时段」让评委能判断和光线、时段有关的主题（摄影：日出、黄昏）；不给价格和开放时间
            tags = [t for t in (kind, _SETTING_ZH.get(place.setting.value, "") if place else "",
                                _BEST_TIME_ZH.get(place.best_time.value, "") if place else "") if t]
            parts.append(f"{item.time} {item.title}（{'·'.join(tags)}）" if tags else f"{item.time} {item.title}")
        lines.append(f"第 {n} 天：{'；'.join(parts)}")
    return "\n".join(lines)


def _parse(raw: str) -> tuple[int, str]:
    try:
        data = json.loads(raw)
        score = data["score"]
        if isinstance(score, str) and re.fullmatch(r"\s*[1-5]\s*", score):
            score = int(score)
        if isinstance(score, float) and score.is_integer():
            score = int(score)
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise ValueError("分数不在 1–5")
        reason = re.sub(r"\s+", " ", str(data.get("reason", ""))).strip()
        if not reason:
            raise ValueError("没有理由")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise JudgeError(f"模型返回的内容不符合要求：{exc}") from exc
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[: MAX_REASON_CHARS - 1] + "…"
    return score, reason


def evaluate(it: Itinerary, client: LLMClient, library: PlaceLibrary | None = None) -> JudgeResult:
    """评一份行程的主题契合度。模型调用失败抛 JudgeUnavailable；返回不合规重试后仍不行抛 JudgeError。"""
    if it.generation is None:
        raise JudgeError("这份行程不是按主题生成的，没有「所选主题」可以对照")
    lib = library or load_library(itinerary_city_id(it.trip.destination))
    user = _prompt(it, lib)
    last: JudgeError | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            with trace_context(purpose=PURPOSE):
                raw = client.complete_json(system=SYSTEM, user=user, schema=_schema())
        except LLMError as exc:
            raise JudgeUnavailable(f"模型没有响应：{exc}") from exc
        try:
            score, reason = _parse(raw)
        except JudgeError as exc:
            last = exc
            continue
        return JudgeResult(score=score, reason=reason, theme_name=it.generation.theme_name)
    raise last or JudgeError("评估失败")
