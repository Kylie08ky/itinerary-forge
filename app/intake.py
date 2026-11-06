"""需求收集与缺失字段追问。

对应 specs/modules/M01-trip-generation.md（需求收集与追问）。
职责：拿到原始 dict → 判断能否进入规划阶段；不能则给出人类可读的追问。
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from app.cities import supported_cities
from app.schemas import TravelRequest

# 必填字段（M01 里标「必填」的字段）。
REQUIRED_FIELDS = (
    "destination",
    "start_date",
    "end_date",
    "budget_level",
    "travel_style",
    "interests",
)

# 缺失时给用户的追问话术（zh-CN）。
FOLLOW_UP_QUESTIONS: dict[str, str] = {
    "destination": f"你想去哪个城市？（完整支持{'、'.join(c.name_zh for c in supported_cities() if c.tier == 'full')}；"
                   "周边一日游请用「周边一日游」入口）",
    "start_date": "行程从哪天开始？请用 YYYY-MM-DD 格式。",
    "end_date": "行程到哪天结束？请用 YYYY-MM-DD 格式。",
    "budget_level": "预算大概什么档？低 / 中 / 高（low / medium / high）。",
    "travel_style": "你偏好的节奏？轻松 / 均衡 / 紧凑（relaxed / balanced / intense）。",
    "interests": "你这趟最想体验什么？比如博物馆、美食、摄影、地标、公园，至少写一个。",
    # 下面是选填但强烈建议补充的字段
    "hotel_area": "你住在哪个区，或最近的地铁站是哪一站？用来安排每天的动线。",
    "arrival_time": "抵达当天大概几点到市区？晚到的话首日会安排得轻一些。",
    "departure_time": "离开当天大概几点走？早走的话末日会预留出撤离时间。",
}

# 选填但建议补充的字段（用于「软追问」）。
SOFT_FIELDS = ("hotel_area", "arrival_time", "departure_time")


class IntakeResult(BaseModel):
    """intake 阶段的结论。"""

    ok: bool
    request: TravelRequest | None = None
    missing_fields: list[str] = []
    questions: list[str] = []          # 阻塞性追问：不回答无法继续
    soft_questions: list[str] = []     # 非阻塞建议：可跳过，但会降低质量
    errors: list[str] = []             # 字段值不合法的说明

    model_config = {"arbitrary_types_allowed": True}


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, dict, tuple)) and len(value) == 0:
        return True
    return False


def run_intake(raw: dict) -> IntakeResult:
    """把原始需求 dict 变成 IntakeResult。

    - 缺必填字段 → ok=False，返回阻塞性追问。
    - 字段值不合法 → ok=False，返回 errors。
    - 通过 → ok=True，返回 request；并附上选填字段的软追问 + schema 层的 notes。
    """
    raw = dict(raw or {})
    raw.pop("_meta", None)  # persona 文件里的说明字段，忽略。

    missing = [f for f in REQUIRED_FIELDS if _is_empty(raw.get(f))]
    if missing:
        return IntakeResult(
            ok=False,
            missing_fields=missing,
            questions=[FOLLOW_UP_QUESTIONS[f] for f in missing],
        )

    try:
        request = TravelRequest.model_validate(raw)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()
        ]
        return IntakeResult(ok=False, errors=errors)

    soft_questions = [
        FOLLOW_UP_QUESTIONS[f] for f in SOFT_FIELDS if _is_empty(raw.get(f))
    ]

    return IntakeResult(
        ok=True,
        request=request,
        soft_questions=soft_questions,
        errors=list(request.notes),  # schema 校验里累积的软提示
    )
