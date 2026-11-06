"""笔记提纯（F71）：从一个地点的原文片段里，用规则抽出预约 / 排队 / 时长 / 闭馆 / 现金 / 防扒 / 价格。

全是词表 + 数字抽取，不用模型（7B 判断「推荐 / 避雷」不稳，见 M12 的取舍）。提示只展示，不会自动改行程。
"""

from __future__ import annotations

import re

from app.library import Place
from app.notes.models import NoteTip, TipKind

_NUM = r"(\d+(?:\.\d+)?|[一二两三四五六七八九十半]+)"
_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}
_WEEKDAYS = "一二三四五六日"

_NO_BOOKING = re.compile(r"(不用|无需|不需要|免|不必)(提前)?(预约|预订|订票|订位|订)")
_BOOKING = re.compile(
    r"(提前|需要?|必须|一定要|建议|最好|记得)(提前)?(预约|预订|预定|订票|订位|订座|订时段票|订)|订时段票|预约制|要预约"
    r"|(?:官网|线上|网上)预约|预约(?:好|的时间|时间)"
)
_QUEUE = re.compile(rf"排队(?:约|大约|要|等|需要)?\s*{_NUM}\s*(分钟|分|小时|个小时|h)(\+)?")
_DURATION = re.compile(
    rf"(?:逛|玩|看|待|参观|游览|停留|花|留|预留|预计|建议)\s*(?:约|大概|大约|差不多|至少)?\s*{_NUM}\s*(?:个)?(分钟|小时)"
    rf"(?:就)?(?:够了|足够|左右|即可|就好)?|{_NUM}\s*(?:个)?(分钟|小时)(?:就)?(?:够了|足够)"
)
_CLOSED = re.compile(r"(?:周|星期|礼拜)([一二三四五六日天])\s*(?:都)?(闭馆|休息|不开|关门|闭门|休馆|不营业)")
_CLOSED_REV = re.compile(r"(闭馆|休息|不开|关门|休馆)\s*(?:日)?(?:是|在)?\s*(?:周|星期)([一二三四五六日天])")
_CASH = re.compile(r"只收现金|仅收现金|只接受现金|只能付现金|不收卡|不能刷卡|只能现金")
_SAFETY = re.compile(r"小心扒手|注意扒手|有扒手|扒手多|小心小偷|注意财物|看好(?:随身)?(?:物品|财物|包)|防盗|防扒|被偷")
# 价格只认带「是什么价」说明的（人均 / 门票 / 一人…）：光有「2.55€」可能是地铁票、可乐，标成价格会误导
_PRICE = re.compile(rf"(人均|门票|票价|票是|一人|每人|一张|单人|成人票|学生票|票)\s*{_NUM}\s*(欧元|欧|€)")

# 原文像在吐槽：默认不替用户决定去不去。「避坑」是好事（教你怎么避开坑），不算
COMPLAINT_CUES = (
    "避雷", "别去", "不值得", "踩雷", "劝退", "拔草", "纯打卡", "其实一般", "人多东西贵", "不推荐", "纯游客", "智商税",
    "失望", "不好吃", "难吃", "别买", "千万别", "不建议去", "没必要去", "排雷",
)


def _num(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return float(sum(_CN[c] for c in s)) if all(c in _CN for c in s) else 0.0


def _fmt(x: float) -> str:
    return str(int(x)) if x == int(x) else str(x)


def _minutes(value: float, unit: str) -> float:
    return value * 60 if unit.startswith(("小时", "个小时", "h")) else value


def _human(minutes: float) -> str:
    if minutes >= 60 and minutes % 30 == 0:
        return f"{_fmt(minutes / 60)} 小时"
    return f"{_fmt(minutes)} 分钟"


def is_complaint(snippet: str) -> bool:
    return any(c in snippet for c in COMPLAINT_CUES)


def extract_tips(snippet: str, place: Place | None = None) -> list[NoteTip]:
    """`place` 给了就把闭馆日和地点库对一下。同一类提示只留第一条，排队先于时长处理（「排队 2 小时」不是游玩时长）。"""
    tips: list[NoteTip] = []
    masked = snippet

    def mask(m: re.Match[str]) -> None:
        nonlocal masked
        masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]

    if m := _QUEUE.search(snippet):
        mins = _minutes(_num(m.group(1)), m.group(2))
        label = f"排队约 {_human(mins)}" + ("以上" if m.group(3) else "")
        tips.append(NoteTip(kind=TipKind.queue, label=label, text=m.group(0)))
        mask(m)

    if m := _NO_BOOKING.search(snippet):
        tips.append(NoteTip(kind=TipKind.booking, label="无需预约", text=m.group(0)))
    elif m := _BOOKING.search(snippet):
        tips.append(NoteTip(kind=TipKind.booking, label="需要提前订票 / 预约", text=m.group(0)))

    if m := _DURATION.search(masked):
        value = m.group(1) or m.group(3)
        unit = m.group(2) or m.group(4)
        label = f"建议约 {_human(_minutes(_num(value), unit))}"
        tips.append(NoteTip(kind=TipKind.duration, label=label, text=m.group(0)))

    closed = _CLOSED.search(snippet)
    if closed:
        wd = _WEEKDAYS.index("日" if closed.group(1) == "天" else closed.group(1))
        tips.append(_closed_tip(wd, closed.group(0), place))
    elif rev := _CLOSED_REV.search(snippet):
        wd = _WEEKDAYS.index("日" if rev.group(2) == "天" else rev.group(2))
        tips.append(_closed_tip(wd, rev.group(0), place))

    if m := _CASH.search(snippet):
        tips.append(NoteTip(kind=TipKind.cash, label="只收现金", text=m.group(0)))
    if m := _SAFETY.search(snippet):
        tips.append(NoteTip(kind=TipKind.safety, label="小心扒手 / 看好财物", text=m.group(0)))
    if m := _PRICE.search(snippet):
        prefix = m.group(1)
        kind = prefix if prefix in ("人均", "门票", "票价", "学生票", "成人票") else "约"
        label = f"{kind} {_fmt(_num(m.group(2)))} 欧"
        tips.append(NoteTip(kind=TipKind.price, label=label, text=m.group(0).strip()))
    return tips


def _closed_tip(weekday: int, text: str, place: Place | None) -> NoteTip:
    name = f"周{_WEEKDAYS[weekday]}"
    conflict = None
    if place is not None:
        if place.closed_weekdays and weekday not in place.closed_weekdays:
            lib = "、".join(f"周{_WEEKDAYS[d]}" for d in sorted(place.closed_weekdays))
            conflict = f"笔记说{name}，地点库记的是{lib}闭馆，以地点库为准"
        elif not place.closed_weekdays and not place.long_term_closed:
            conflict = f"笔记说{name}闭馆，地点库没有记闭馆日，生成时按地点库排；出发前请看官网"
    return NoteTip(kind=TipKind.closed, label=f"{name}闭馆", text=text, conflict=conflict)
