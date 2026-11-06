"""排程器的输出。纯数据，不含任何 I/O，可直接 model_dump()。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Stop(BaseModel):
    kind: Literal["place", "meal", "logistics"]
    title: str
    place_id: str | None = None
    start: int = Field(ge=0)                  # 当天 0:00 起的分钟数
    duration: int = Field(gt=0)
    travel_min: int = 0                       # 从上一个地点过来的通勤
    travel_mode: str | None = None
    travel_estimated: bool = False
    role: Literal["anchor", "support", "must_go", "filler"] | None = None   # filler = 顺手补的小点，不算主要点
    warning: str | None = None                # 需要在页面上醒目提示的内容
    edge: Literal["arrive", "leave"] | None = None   # 接驳项是「抵达」还是「离开」：文字按当时用的交通方式写
    meal: Literal["lunch", "tea"] | None = None   # 这一站本身就是午餐 / 下午茶（排程认出来的，见 engine._build）


class DayPlan(BaseModel):
    date: date
    stops: list[Stop]
    reasons: list[str] = Field(default_factory=list)      # 「为什么这样排」
    chapters: list[str] = Field(default_factory=list)     # 深度路线：这一天讲的章 id（按顺序）；普通行程为空
    label: str | None = None                              # 深度路线里不属于任何章的天（「自由日 · 艺术主题补充」）


class ScheduleDraft(BaseModel):
    theme_id: str
    theme_ids: list[str] = Field(default_factory=list)     # 混选时所有主题，按权重从大到小
    theme_name: str = ""
    hotel: tuple[float, float]
    days: list[DayPlan]
    notes: list[str] = Field(default_factory=list)        # 跨天的说明：补充了哪些点、必去排不进等
    route_id: str | None = None                           # 深度路线（规格 09）：按哪条路线排的
    route_skipped: list[str] = Field(default_factory=list)  # 路线里没排进来的站（地点 id）
