"""M01 S10–S13：自然语言填表。用户写一句话，读出日期 / 天数 / 人数 / 预算 / 主题 / 节奏 / 想去的地点，填进首页表单。

    一句话 ─► 去掉像指令的句子 ─► 规则读（日期、天数、人数、预算、主题、节奏、地点库里的地点）
                              └► 模型只补三样：主题映射、节奏、限制（输出严格校验，不信任）

确定性的事交给代码：**日期永远由代码算**（模型不碰日期 / 金额 / 人数 / 地点）；读不出来的项不填，列进 `missing`。
这里只做「读懂意图」，不生成行程，也不替用户做决定——填好的表单由用户确认后再生成。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.library import PlaceLibrary
from app.llm import LLMClient, LLMError
from app.llm_trace import trace_context
from app.notes.extract import find_spans
from app.notes.match import match_mention
from app.notes.models import Confidence
from app.notes.text import Folded, drop_instructions
from app.schemas import TRANSPORT_ZH
from app.trips import MAX_LEGS

MAX_TEXT = 500

_SYSTEM = (Path(__file__).resolve().parent / "prompts" / "intake_nl_system.txt").read_text(encoding="utf-8")
_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"


class Intent(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)       # 只含读到的项
    understood: list[str] = Field(default_factory=list)        # 给人看的中文句子
    missing: list[str] = Field(default_factory=list)           # 没读出来的项
    notes: list[str] = Field(default_factory=list)             # 提醒（天数不在建议范围等）
    model_used: bool = False                                   # False = 模型没开 / 没读出来，只按规则


def cn_int(text: str) -> int | None:
    """「十」「十一」「二十」「二十八」「三十一」或阿拉伯数字 → int；认不出返回 None。"""
    if text.isdigit():
        return int(text)
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN.get(head, 1) if head else 1
        ones = _CN.get(tail, 0) if tail else 0
        if (head and head not in _CN) or (tail and tail not in _CN):
            return None
        return tens * 10 + ones
    return _CN.get(text) if len(text) == 1 else None


def _mask(text: str, span: tuple[int, int]) -> str:
    return text[: span[0]] + " " * (span[1] - span[0]) + text[span[1]:]


def _next_date(month: int, day: int, today: date, year: int | None = None) -> date | None:
    """没写年份就取「今天（含）之后最近的一次」（今天 10 月 22 日，说 10 月 20 日是明年）；2 月 29 日往后找闰年。"""
    if year is not None:
        try:
            return date(year, month, day)
        except ValueError:
            return None
    start = today.year if (month, day) >= (today.month, today.day) else today.year + 1
    for y in range(start, start + 9):
        try:
            return date(y, month, day)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# 日期与天数
# --------------------------------------------------------------------------- #
_VAGUE_DATE = re.compile(
    r"五一|十一(?!\s*月)|国庆|春节|元旦|圣诞|暑假|寒假"
    r"|下个月(?!\s*" + _NUM + r"\s*[号日])")
_RANGE_TAIL = r"\s*(?:到|至|-|~|—|－|–)\s*(?:" + _NUM + r"\s*月\s*)?" + _NUM + r"\s*[日号]?"
_FULL_DATE = re.compile(r"(?:(\d{4})\s*年\s*)?" + _NUM + r"\s*月\s*" + _NUM + r"\s*[日号]?(?:" + _RANGE_TAIL + r")?")
_SLASH_DATE = re.compile(r"(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/])")
_NEXT_MONTH = re.compile(r"下个月\s*" + _NUM + r"\s*[号日]")
_BARE_DAY = re.compile(r"(?<![\d月日号年/\-])" + _NUM + r"\s*(?:号|日(?=\s*(?:出发|走|去|到|抵达|开始)))")
_MONTH_ONLY = re.compile(_NUM + r"\s*月")


# 相对日期（场景 M37）：「明天」「后天」「周六」「这周六」「下周三」「周末」「下周末」「下周」「这周」；
# 前面是数字的「一周」「2周」是天数，不是日期
_REL_DAY = re.compile(r"大后天|后天|明天|今天")
_REL_WEEK = re.compile(
    r"(?<![\d一二两三四五六七八九十])(下下|下|这|本)?\s*个?\s*(?:周|星期|礼拜)\s*([一二三四五六日天末])?")
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "末": 5}
_WEEK_ZH = "一二三四五六日"


def _week_day(prefix: str | None, wd: str, today: date) -> date:
    """「下周三」「这周六」「周六」→ 日期；没写这周 / 下周的，取今天（含）之后最近的那一天。"""
    weeks = {"下下": 2, "下": 1}.get(prefix or "", 0)
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=weeks)
    day = monday + timedelta(days=_WEEKDAY[wd])
    if not prefix and day < today:
        day += timedelta(weeks=1)                                          # 这周一已经过了：取下一个
    return day


def _relative_date(text: str, today: date, out: Intent) -> tuple[date, tuple[int, int]] | None:
    """读「明天 / 周六 / 下周三 / 周末 / 下周」这类说法；读不出（或只是「一周」这种天数）返回 None。日期由代码算。"""
    if m := _REL_DAY.search(text):
        return today + timedelta(days={"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[m.group(0)]), m.span()
    for m in _REL_WEEK.finditer(text):
        prefix, wd = m.group(1), m.group(2)
        if not prefix and not wd:
            continue                                                       # 单独一个「周」：不是日期
        if wd:
            return _week_day(prefix, wd, today), m.span()
        weeks = {"下下": 2, "下": 1}.get(prefix or "", 0)
        day = today - timedelta(days=today.weekday()) + timedelta(weeks=weeks) if weeks else today
        out.notes.append(f"「{m.group(0).strip()}」按 {day.month}月{day.day}日（周{_WEEK_ZH[day.weekday()]}）算，"
                         "不对请在下面改")                                  # 只说「下周」→ 下周一；「这周」→ 今天
        return day, m.span()
    return None


_REL_TAIL = re.compile(r"\s*(?:到|至|-|~|—|－|–|回)\s*")
_REL_DAY_AT = re.compile(_REL_DAY.pattern)
_REL_WEEK_AT = re.compile(
    r"(下下|下|这|本)?\s*个?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])")


def _relative_end(text: str, start: date, after: int, today: date) -> tuple[date, tuple[int, int]] | None:
    """开始日期是相对说法时，紧跟着的「到下周二」「至周日」「到后天」→ 结束日期。没写这周 / 下周的「周二」，
    取开始日期之后最近的那个周二。"""
    tail = _REL_TAIL.match(text, after)
    if not tail:
        return None
    if m := _REL_DAY_AT.match(text, tail.end()):
        days = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[m.group(0)]
        return today + timedelta(days=days), (tail.start(), m.end())
    if m := _REL_WEEK_AT.match(text, tail.end()):
        prefix, wd = m.group(1), m.group(2)
        if prefix:
            return _week_day(prefix, wd, today), (tail.start(), m.end())
        ahead = (_WEEKDAY[wd] - start.weekday()) % 7 or 7
        return start + timedelta(days=ahead), (tail.start(), m.end())
    return None


def _read_dates(text: str, today: date, out: Intent) -> tuple[date | None, date | None, str]:
    """返回 (开始, 结束, 去掉日期后的原文)；读不出来 / 说不清的日期返回 None 并写进 missing。"""
    start = end = None
    work = text
    if m := _FULL_DATE.search(work):
        year = int(m.group(1)) if m.group(1) else None
        mo, d1 = cn_int(m.group(2)), cn_int(m.group(3))
        if mo and d1 and 1 <= mo <= 12:
            start = _next_date(mo, d1, today, year)
            if start and m.group(5):                               # 区间的结束一半
                mo2 = cn_int(m.group(4)) if m.group(4) else mo
                d2 = cn_int(m.group(5))
                if mo2 and d2 and 1 <= mo2 <= 12:
                    y2 = start.year + (1 if mo2 < start.month else 0)
                    cand = _next_date(mo2, d2, today, y2)
                    if cand and cand > start:
                        end = cand
                    else:
                        out.missing.append("结束日期（比出发日期还早，没法读）")
            work = _mask(work, m.span())
    if start is None and (m := _SLASH_DATE.search(work)):
        mo, d1 = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d1 <= 31:
            start = _next_date(mo, d1, today)
            work = _mask(work, m.span())
    if start is None and (m := _NEXT_MONTH.search(work)):
        d1 = cn_int(m.group(1))
        nm = today.month % 12 + 1
        if d1:
            start = _next_date(nm, d1, today, today.year + (1 if today.month == 12 else 0))
            work = _mask(work, m.span())
    if start is None and (m := _BARE_DAY.search(work)):
        d1 = cn_int(m.group(1))
        if d1 and 1 <= d1 <= 31:
            for add in range(0, 14):                               # 取今天（含）之后最近的、有这一号的日子
                y, mo = today.year + (today.month - 1 + add) // 12, (today.month - 1 + add) % 12 + 1
                try:
                    cand = date(y, mo, d1)
                except ValueError:
                    continue
                if cand >= today:
                    start = cand
                    break
            work = _mask(work, m.span())
    if start is None and (rel := _relative_date(work, today, out)):
        start = rel[0]
        if (tail := _relative_end(work, start, rel[1][1], today)) and tail[0] > start:
            end = tail[0]                                                  # 「这周六到下周二」
            work = _mask(work, (rel[1][0], tail[1][1]))
        else:
            work = _mask(work, rel[1])
            if re.search(r"周末", text[rel[1][0]: rel[1][1]]) and not _read_days(work):
                end = start + timedelta(days=1)                            # 只说「周末」：周六到周日
                out.notes.append("「周末」按周六、周日两天算，不对请在下面改")
    if start is None:
        vague = _VAGUE_DATE.search(text)
        month_only = _MONTH_ONLY.search(work)
        if vague:
            out.missing.append(f"出发日期（「{vague.group(0)}」这类说法没法读，请在下面选）")
        elif month_only and cn_int(month_only.group(1)) and 1 <= (cn_int(month_only.group(1)) or 0) <= 12:
            out.missing.append(f"出发日期（只说了月份：{cn_int(month_only.group(1))} 月，没说几号）")
        else:
            out.missing.append("出发日期")
    return start, end, work


def _read_days(work: str) -> int | None:
    if m := re.search(_NUM + r"\s*(?:天|日游)", work):
        n = cn_int(m.group(1))
        if n:
            return n
    if re.search(r"(?:一|1)\s*(?:周|个星期|星期)", work):
        return 7
    if m := re.search(_NUM + r"\s*晚", work):
        n = cn_int(m.group(1))
        if n:
            return n + 1
    return None


# --------------------------------------------------------------------------- #
# 人数
# --------------------------------------------------------------------------- #
_KID_AFTER = re.compile(r"(?:孩子|小孩|儿子|女儿|宝宝|娃|小朋友)\s*(?:今年|大概|约)?\s*" + _NUM + r"\s*岁")
_KID_BEFORE = re.compile(_NUM + r"\s*岁\s*(?:的)?\s*(?:孩子|小孩|儿子|女儿|宝宝|娃|小朋友)")
_COUNT = re.compile(r"(?:我们|咱们|咱)?\s*" + _NUM + r"\s*(?:个|位|口)?\s*(?:人|位|口)(?!均)|我们\s*" + _NUM + r"\s*个")
_SOLO = re.compile(r"一个人|独自|自己一个人|solo", re.IGNORECASE)
_COUPLE = re.compile(
    r"情侣|夫妻|两口子|我们俩|咱俩|俩人|夫妇"
    r"|我和(?:女朋友|男朋友|老婆|老公|爱人|闺蜜|搭档|朋友|同学|同事|妈妈|爸爸|老妈|老爸)")
_STUDENT = re.compile(r"学生|留学生|大学生")


def _read_party(text: str, out: Intent) -> list[dict[str, Any]] | None:
    kids = [cn_int(m.group(1)) for rx in (_KID_AFTER, _KID_BEFORE) for m in rx.finditer(text)]
    kids = [k for k in kids if k is not None and 0 <= k <= 17][:6]
    count = None
    if _SOLO.search(text):
        count = 1
    elif _COUPLE.search(text):
        count = 2
    else:
        for m in _COUNT.finditer(text):
            n = cn_int(m.group(1) or m.group(2) or "")
            if n and 1 <= n <= 20:
                count = n
                break
    student = bool(_STUDENT.search(text))
    if count is None and not kids and not student:
        return None
    if count is None:
        adults = 1
        if kids:
            out.notes.append("没写大人几位，先按 1 位成人 + 孩子算，不对请在下面改")
    else:
        adults = max(1, count - len(kids))
    if student:
        out.notes.append("学生优惠要看年龄，请在下面确认每个人的年龄")
    people = [{"age": 30, "residence": "none", "student": student} for _ in range(adults)]
    people += [{"age": k, "residence": "none", "student": False} for k in kids]
    return people


# --------------------------------------------------------------------------- #
# 预算
# --------------------------------------------------------------------------- #
_AMOUNT_AFTER = re.compile(r"(\d+(?:\.\d+)?)\s*(万)?\s*(欧元|欧|EUR|eur|€|元|块钱|块|人民币|RMB|rmb)")
_AMOUNT_BEFORE = re.compile(r"(人民币|RMB|rmb|¥|￥|€)\s*(\d+(?:\.\d+)?)\s*(万)?")
_BUDGET_CUE = re.compile(r"预算|总共|一共|共计|控制在|不超过|最多|花费")
_NOT_TOTAL = re.compile(r"人均|门票|票价|每人|一晚|一杯|一顿|住宿|酒店")
_BARE_NUMBER = re.compile(
    r"预算\s*(?:大概|大约|约|在|是|为)?\s*(\d+(?:\.\d+)?)\s*(万)?(?!\s*(?:天|人|岁|月|日|号|晚|欧|元|块|€|¥))")


def _currency(unit: str) -> str:
    return "EUR" if unit in ("欧元", "欧", "EUR", "eur", "€") else "CNY"


def _read_budget(text: str, out: Intent) -> tuple[float, str] | None:
    if re.search(r"预算\s*人均|人均\s*预算", text) or (_BUDGET_CUE.search(text) and re.search(r"人均\s*\d", text)):
        out.missing.append("预算（人均说法第一版不支持，请直接填总预算上限）")
        return None
    found: list[tuple[int, float, str]] = []
    for m in _AMOUNT_AFTER.finditer(text):
        found.append((m.start(), float(m.group(1)) * (10_000 if m.group(2) else 1), _currency(m.group(3))))
    for m in _AMOUNT_BEFORE.finditer(text):
        found.append((m.start(), float(m.group(2)) * (10_000 if m.group(3) else 1), _currency(m.group(1))))
    for pos, amount, cur in sorted(found):
        window = text[max(0, pos - 8):pos]
        if _NOT_TOTAL.search(window):
            continue
        cued = bool(_BUDGET_CUE.search(text[max(0, pos - 8):pos + 1]))
        if not cued and amount < (100 if cur == "EUR" else 500):      # 光有金额又很小：多半是一杯咖啡、一张票
            continue
        return amount, cur
    if m := _BARE_NUMBER.search(text):
        out.missing.append(f"预算币种（{m.group(1)} 是欧元还是人民币？）")
    return None


# --------------------------------------------------------------------------- #
# 主题 / 节奏 / 限制 / 地点
# --------------------------------------------------------------------------- #
THEME_WORDS: dict[str, tuple[str, ...]] = {
    "art": ("艺术", "美术馆", "画展", "看画", "艺术馆"),
    "museums": ("博物馆",),
    "food": ("美食", "好吃", "吃货", "吃饭", "咖啡", "甜品", "甜点", "餐厅", "小吃", "面包", "探店", "下午茶"),
    "shopping": ("购物", "逛街", "买买买", "血拼", "奥特莱斯", "折扣"),
    "fashion": ("时尚", "买手店", "时装", "设计师"),
    "entertainment": ("演出", "歌剧", "剧院", "音乐会", "看秀", "红磨坊", "夜生活"),
    "family": ("亲子", "带娃", "家庭", "带孩子", "孩子", "小孩", "宝宝", "迪士尼"),
    "photography": ("摄影", "拍照", "出片", "打卡拍"),
}
_RELAXED = ("轻松", "悠闲", "慢慢", "不赶", "别太累", "休闲", "放松", "不想太累")
_INTENSE = ("紧凑", "特种兵", "高强度", "多去几个", "赶一赶", "满满")
# 「去 X 玩」「到 X」里的 X（目的地）：只当候选，真假由 POST /destinations/resolve 去维基数据认（规格 10）
_DEST_ZH = re.compile(r"(?=(?:去|到|飞|前往|飞往)([一-鿿·]{2,8}?)(?:玩|旅游|旅行|度假|逛|待|走走|看看|转转|自由行|出差|"
                      r"\d|[一二三四五六七八九十两]+[天日晚]|[，,。、；;\s]|$))")
_DEST_EN = re.compile(r"\b(?:to|in|visit|visiting)\s+([A-Z][A-Za-z.'-]+(?:\s[A-Z][A-Za-z.'-]+){0,2})")
_DEST_BAD = re.compile(r"[年月号周的了在和与跟也还要想去到]|[0-9一二三四五六七八九十两][日天晚点]")


def _read_destination(text: str, without_places: str) -> str | None:
    """中文在「去掉地点名之后的文字」里找（不把「去故宫」当目的地）；英文在原文里找（要看大小写）。"""
    for m in _DEST_ZH.finditer(without_places):
        cand = m.group(1)
        if not _DEST_BAD.search(cand):
            return cand
    m = _DEST_EN.search(text)
    return m.group(1) if m else None
# --------------------------------------------------------------------------- #
# 出发地与来回交通（场景 M41）：「从上海宝山出发，高铁来回」；只认明说的，不猜
# --------------------------------------------------------------------------- #
_ORIGIN = re.compile(
    r"(?:从|由|自)([一-鿿]{2,12}?)(?:出发|启程|走|坐|乘|搭|开车|自驾|飞)"
    r"|出发地(?:是|在|：|:)?\s*([一-鿿]{2,12})")
_ORIGIN_BAD = re.compile(r"[年月号周的了在和与跟也还要想去到天日晚点]")
_MODE_WORDS = (
    ("high_speed_rail", re.compile(r"高铁|动车|高速铁路")),
    ("flight", re.compile(r"飞机|航班|机票")),
    ("train", re.compile(r"火车|绿皮|硬卧|卧铺")),
    ("coach", re.compile(r"大巴|长途汽车|客车")),
    ("drive", re.compile(r"自驾|开车|驾车|租车")),
)
_ROUND_TRIP = re.compile(r"来回|往返|双程|去回")


def _read_origin(text: str, destination: str | None) -> str | None:
    for m in _ORIGIN.finditer(text):
        cand = m.group(1) or m.group(2)
        if cand and not _ORIGIN_BAD.search(cand) and cand != destination:
            return cand
    return None


def _read_transport(text: str) -> tuple[str | None, str | None]:
    """→ (去程方式, 返程方式)。一种方式（或「来回 / 往返」）两头都用；两种按先后分去 / 回；
    前面写了「去程 / 返程」的按它分。"""
    found = sorted((m.start(), mode) for mode, rx in _MODE_WORDS for m in rx.finditer(text))
    if not found:
        return None, None
    modes = list(dict.fromkeys(mode for _, mode in found))
    if len(modes) == 1:
        before = text[max(0, found[0][0] - 6): found[0][0]]
        if _ROUND_TRIP.search(text) or not re.search(r"去程|返程|回程|去|回", before):
            return modes[0], modes[0]
        return (None, modes[0]) if re.search(r"返程|回程|回", before) else (modes[0], None)
    out = back = None
    for pos, mode in found:
        before = text[max(0, pos - 6): pos]
        if re.search(r"返程|回程|回", before) and back is None:
            back = mode
        elif out is None:
            out = mode
        elif back is None:
            back = mode
    return out, back


# --------------------------------------------------------------------------- #
# 多城市（规格 M01 S14）：「北京3天然后西安3天」按顺序读出每座城市和天数
# --------------------------------------------------------------------------- #
MAX_STOPS = MAX_LEGS                                  # 和多城市旅行的上限一致
_STOP_SPLIT = re.compile(r"(然后|接着|之后|再去|再到|再飞|转去|转到|最后去|最后到|→|->|＞|>|再|[，,；;、])")
_STOP_STRONG = re.compile(r"然后|接着|之后|再|转|最后|→|->|＞|>")
_STOP_LEAD = re.compile(r"^(?:\s|先|再|最后|然后|接着|之后|我们|咱们|我|想要|想|要|打算|计划|准备|一起|出发)+")
_GO = r"(?:出发去|出发到|去|到|飞|前往|飞往)"
_STOP_NAME = r"([一-鿿·]{2,6}?)"
_STOP_VERB = r"(?:玩|待|逛|住|游玩|旅游|旅行|度假|走走|看看|转转|自由行)?"
_STOP_WITH_DAYS = re.compile(r"^" + _GO + r"?" + _STOP_NAME + _STOP_VERB + r"\s*" + _NUM + r"\s*(?:天|日游)")
_STOP_WITH_DAYS_ANYWHERE = re.compile(_GO + _STOP_NAME + _STOP_VERB + r"\s*" + _NUM + r"\s*(?:天|日游)")
_STOP_GO_ONLY = re.compile(r"^" + _GO + _STOP_NAME + _STOP_VERB + r"\s*$")
_STOP_BARE = re.compile(r"^" + _STOP_NAME + _STOP_VERB + r"\s*$")
_STOP_NAME_BAD = re.compile(
    r"[年月号周的了在和与跟也还要想去到人位口个岁带吃玩逛住喜欢预算共]"
    r"|^[回返出离]|[站场店馆园街路楼堂寺山湖桥塔院厅]$"
    r"|^(?:大概|大约|差不多|总计|一起)$|[0-9零〇一二两三四五六七八九十][日天晚点]")
_TOTAL_DAYS = re.compile(r"(?:一共|总共|共计|共|一起|总计)\s*" + _NUM + r"\s*天")


def _stop_in(seg: str, *, after_break: bool) -> tuple[str, int | None] | None:
    """一个小句里的「城市 + 几天」；after_break = 它前面有「然后 / 再 / →」这类明确的衔接词。"""
    seg = _STOP_LEAD.sub("", seg)
    found: tuple[str, int | None] | None = None
    if m := _STOP_WITH_DAYS.search(seg):
        found = (m.group(1), cn_int(m.group(2)))
    elif m := _STOP_WITH_DAYS_ANYWHERE.search(seg):
        found = (m.group(1), cn_int(m.group(2)))
    elif m := _STOP_GO_ONLY.search(seg):
        found = (m.group(1), None)
    elif after_break and (m := _STOP_BARE.search(seg)):
        found = (m.group(1), None)
    if found is None or _STOP_NAME_BAD.search(found[0]) or (found[1] is not None and found[1] < 1):
        return None
    return found


def _read_stops(work: str) -> list[tuple[str, int | None]]:
    """按顺序读出 ≥2 座城市；不足 2 座就当没有（走原来的单城市读法）。"""
    parts = _STOP_SPLIT.split(work)
    out: list[tuple[str, int | None]] = []
    for i in range(0, len(parts), 2):
        stop = _stop_in(parts[i], after_break=i > 0 and bool(_STOP_STRONG.match(parts[i - 1])))
        if stop and (not out or stop[0] != out[-1][0]):
            out.append(stop)
    return out if len(out) >= 2 else []


def _split_evenly(total: int, n: int) -> list[int]:
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def _apply_stops(stops: list[tuple[str, int | None]], start: date | None, end: date | None, work: str,
                 out: Intent) -> int | None:
    """算出每座城市几天、哪天到哪天（换城市默认在第二天），写进 fields；返回整趟天数。"""
    f = out.fields
    if len(stops) > MAX_STOPS:
        stops = stops[:MAX_STOPS]
        out.notes.append(f"最多 {MAX_STOPS} 座城市，后面的没填")
    n = len(stops)
    given = [d for _name, d in stops]
    real = (end - start).days + 1 if start and end else None
    explicit = None
    if real is None and (m := _TOTAL_DAYS.search(work)):
        explicit = cn_int(m.group(1))
    total = real or explicit
    days: list[int | None] = list(given)
    if all(d is None for d in given):
        if total is None and (m := re.search(_NUM + r"\s*(?:天|日游)", work)):
            total = cn_int(m.group(1))
        if total is not None and total >= n:
            days = list(_split_evenly(total, n))
            out.notes.append("每座城市几天没写，先平均分，请在下面改")
        else:
            out.missing.append("各城市各玩几天" if total is None else "各城市各玩几天（总天数不够分）")
    elif any(d is None for d in given):
        known = sum(d for d in given if d)
        blanks = [i for i, d in enumerate(given) if d is None]
        left = (total - known) if total is not None else None
        if left is not None and left >= len(blanks):
            for i, share in zip(blanks, _split_evenly(left, len(blanks)), strict=True):
                days[i] = share
            out.notes.append("有的城市没写几天，按总天数分了，请在下面确认")
        else:
            out.missing.append("有的城市没写几天")
    elif total is not None and sum(d or 0 for d in days) != total:
        before = sum(d or 0 for d in days[:-1])
        if total - before >= 1:
            days[-1] = total - before
            basis = "日期和各城市的天数对不上，按日期算" if real else "各城市的天数加起来和「共 N 天」对不上"
            out.notes.append(f"{basis}（共 {total} 天），最后一座城市调整为 {days[-1]} 天")
        else:
            out.missing.append(f"各城市天数（日期只有 {total} 天，装不下每座城市写的天数）")
            days = [None] * n
    result: list[dict[str, Any]] = []
    cursor = start
    for (name, _g), d in zip(stops, days, strict=True):
        row: dict[str, Any] = {"destination": name}
        if d is not None:
            row["days"] = d
            if cursor is not None:
                row["start_date"] = cursor.isoformat()
                row["end_date"] = (cursor + timedelta(days=d - 1)).isoformat()
                cursor = cursor + timedelta(days=d)               # 下一座城市从第二天开始
        result.append(row)
    f["stops"] = result
    f["destination"] = result[0]["destination"]
    if all(d is not None for d in days):
        sum_days = sum(d for d in days if d)
        if start:
            out.notes.append(f"换城市默认在第二天（{result[0]['destination']}最后一晚出发，交通按夜车估算），"
                             f"要当天换请在下面改{result[1]['destination']}的开始日期")
        return sum_days
    return total

_AVOID_BEFORE = re.compile(
    r"(?:不想去|不想逛|不想看|不要去|不用去|不去|避开|避免|别去|跳过)\s*(?:了)?[^，。,.；;！!？?]{0,6}$")
_AVOID_AFTER = re.compile(r"^\s*(?:就)?\s*(?:不去|不想去|不看|不逛|算了|跳过)")
_CLAUSE_END = re.compile(r"[，。,.；;！!？?\n]")


def _read_themes(text: str, lib: PlaceLibrary) -> list[str]:
    hits: list[tuple[int, str]] = []
    for tid, words in THEME_WORDS.items():
        if tid not in lib.themes:
            continue
        pos = [text.find(w) for w in words if w in text]
        if pos:
            hits.append((min(pos), tid))
    return [tid for _p, tid in sorted(hits)]


def _read_pace(text: str, out: Intent) -> str | None:
    relaxed, intense = any(w in text for w in _RELAXED), any(w in text for w in _INTENSE)
    if relaxed and intense:
        out.notes.append("节奏的说法有冲突（又轻松又紧凑），没填，请在下面选")
        return None
    return "relaxed" if relaxed else "intense" if intense else None


def _read_places(text: str, lib: PlaceLibrary, out: Intent) -> tuple[list[str], list[str], str]:
    """返回 (必去, 不去, 把地点名遮掉的原文)。遮掉是为了让「奥赛博物馆」里的「博物馆」不被当成主题。"""
    must: list[str] = []
    avoid: list[str] = []
    masked = text
    folded = Folded(text)
    for span in find_spans(folded, lib, []):
        surface = text[span.start:span.end]
        match = match_mention(surface, lib, after=text[span.end:span.end + 40])
        if match.kind != "matched":
            continue
        masked = masked[:span.start] + "　" * (span.end - span.start) + masked[span.end:]
        if match.confidence == Confidence.low:
            out.missing.append(f"「{surface}」对得上好几个地点，请在下面的候选池里选")
            continue
        pid = match.place_ids[0]
        before = text[max(0, span.start - 14):span.start]
        before = _CLAUSE_END.split(before)[-1]
        after = _CLAUSE_END.split(text[span.end:span.end + 10])[0]
        bucket = avoid if (_AVOID_BEFORE.search(before) or _AVOID_AFTER.search(after)) else must
        if pid not in must and pid not in avoid:
            bucket.append(pid)
    return must, avoid, masked


# --------------------------------------------------------------------------- #
# 模型：只补主题映射 / 节奏 / 限制
# --------------------------------------------------------------------------- #
class _ModelFailed(RuntimeError):
    pass


def _ask_model(text: str, lib: PlaceLibrary, client: LLMClient) -> dict[str, Any]:
    body = re.sub(r"</?\s*user_text\s*>", "", text, flags=re.IGNORECASE)
    menu = "\n".join(f"- {t.id}：{t.name_zh}" for t in lib.themes.values())
    user = f"主题清单：\n{menu}\n\n<user_text>\n{body}\n</user_text>"
    schema = {"type": "object", "properties": {"themes": {"type": "array", "items": {"type": "string"}},
                                                "pace": {"type": ["string", "null"]},
                                                "restrictions": {"type": "array", "items": {"type": "string"}}}}
    try:
        with trace_context(purpose="intake_nl"):
            raw = client.complete_json(system=_SYSTEM, user=user, schema=schema)
        data = json.loads(raw)
    except (LLMError, ValueError) as exc:
        raise _ModelFailed(str(exc)) from exc
    if not isinstance(data, dict) or ("themes" in data and not isinstance(data["themes"], list)):
        raise _ModelFailed("模型返回的不是约定的 JSON")
    return data


def _merge_model(data: dict[str, Any], text: str, lib: PlaceLibrary, fields: dict[str, Any]) -> None:
    themes = [t for t in fields.get("theme_ids", [])]
    for t in data.get("themes", []):
        if isinstance(t, str) and t in lib.themes and t not in themes:
            themes.append(t)
    if themes:
        fields["theme_ids"] = themes
    pace = data.get("pace")
    if "travel_style" not in fields and pace in ("relaxed", "intense"):
        fields["travel_style"] = pace
    folded = Folded(text)
    restrictions: list[str] = []
    for r in data.get("restrictions", []) if isinstance(data.get("restrictions"), list) else []:
        if isinstance(r, str) and 2 <= len(r.strip()) <= 20 and folded.find_all(r) and r.strip() not in restrictions:
            restrictions.append(r.strip())
    if restrictions:
        fields["restrictions"] = restrictions[:3]


# --------------------------------------------------------------------------- #
def _fmt_day(d: date) -> str:
    return f"{d.month}月{d.day}日"


def parse_intent(text: str, lib: PlaceLibrary, client: LLMClient | None, *, today: date) -> Intent:
    out = Intent()
    text = text.strip()
    cleaned, _dropped = drop_instructions(text)
    cleaned = cleaned.strip()
    if not cleaned:
        out.missing.append("出发日期和天数")
        return out

    start, end, work = _read_dates(cleaned, today, out)
    stops = _read_stops(work)
    days = _apply_stops(stops, start, end, work, out) if stops else _read_days(work)
    f = out.fields
    if stops:                                 # 多城市：每座城市的日期已由 _apply_stops 算好，这里只补整趟的结束日
        if start and days and not end:
            end = start + timedelta(days=days - 1)
    elif start and end:
        real = (end - start).days + 1
        if days and days != real:
            out.notes.append(f"日期和天数对不上，按日期算（{real} 天）")
        days = real
    elif start and days:
        end = start + timedelta(days=days - 1)
    if start:
        f["start_date"] = start.isoformat()
    if end:
        f["end_date"] = end.isoformat()
    if days:
        f["days"] = days                  # 天数合不合适由表单里按目的地给的时长提示说（每座城市的建议不一样）
    if start and not days:
        out.missing.append("天数或返程日期")
    if not start and not days and not any(m.startswith("出发日期") for m in out.missing):
        out.missing.append("天数")

    if (travelers := _read_party(cleaned, out)) is not None:
        f["travelers"] = travelers
    if (budget := _read_budget(cleaned, out)) is not None:
        f["budget_cap"], f["currency"] = (int(budget[0]) if budget[0] == int(budget[0]) else budget[0]), budget[1]
    must, avoid, without_places = _read_places(cleaned, lib, out)
    if themes := _read_themes(without_places, lib):
        f["theme_ids"] = themes
    if pace := _read_pace(cleaned, out):
        f["travel_style"] = pace
    if must:
        f["must_go_ids"] = must
        f["must_go"] = [lib.get_place(p).name_zh for p in must]       # 给首页「偏好」里的文字框用
    if avoid:
        f["avoid_ids"] = avoid
        f["avoid"] = [lib.get_place(p).name_zh for p in avoid]
    if dest := _read_destination(cleaned, without_places):
        f["destination"] = dest
    if origin := _read_origin(cleaned, f.get("destination")):
        f["origin"] = origin
    t_out, t_back = _read_transport(cleaned)
    if t_out:
        f["transport_out"] = t_out
    if t_back:
        f["transport_back"] = t_back

    if client is not None:
        try:
            _merge_model(_ask_model(cleaned, lib, client), cleaned, lib, f)
            out.model_used = True
        except _ModelFailed:
            out.model_used = False

    out.understood = _describe(f, lib)
    return out


def _describe(f: dict[str, Any], lib: PlaceLibrary) -> list[str]:
    said: list[str] = []
    start = date.fromisoformat(f["start_date"]) if "start_date" in f else None
    end = date.fromisoformat(f["end_date"]) if "end_date" in f else None
    if start and end:
        said.append(f"出发 {_fmt_day(start)}，共 {f['days']} 天（到 {_fmt_day(end)}）")
    elif start:
        said.append(f"出发 {_fmt_day(start)}")
    elif "days" in f:
        said.append(f"共 {f['days']} 天")
    if "origin" in f:
        said.append(f"出发地：{f['origin']}")
    if "transport_out" in f or "transport_back" in f:
        zh = TRANSPORT_ZH
        o, b = f.get("transport_out"), f.get("transport_back")
        said.append(f"交通：{zh[o]}来回" if o == b else "交通：" + "，".join(
            x for x in (f"去程 {zh[o]}" if o else "", f"返程 {zh[b]}" if b else "") if x))
    if "stops" in f:
        said.append(" → ".join(f"{s['destination']} {s['days']} 天" if "days" in s else s["destination"]
                               for s in f["stops"]))
    if "theme_ids" in f:
        said.append("主题：" + "、".join(lib.themes[t].name_zh for t in f["theme_ids"]))
    if "travelers" in f:
        adults = [t for t in f["travelers"] if t["age"] >= 18]
        kids = [t for t in f["travelers"] if t["age"] < 18]
        who = f"{len(adults)} 位成人" + ("（学生）" if adults and all(t["student"] for t in adults) else "")
        if kids:
            who += f" + {len(kids)} 位孩子（{'、'.join(str(k['age']) for k in kids)} 岁）"
        said.append("出行人：" + who)
    if "budget_cap" in f:
        said.append(f"预算上限：{f['budget_cap']:g} {'欧元' if f['currency'] == 'EUR' else '人民币'}")
    if "travel_style" in f:
        said.append("节奏：" + {"relaxed": "轻松", "intense": "紧凑"}[f["travel_style"]])
    if "must_go_ids" in f:
        said.append("必去：" + "、".join(lib.get_place(p).name_zh for p in f["must_go_ids"]))
    if "avoid_ids" in f:
        said.append("不去：" + "、".join(lib.get_place(p).name_zh for p in f["avoid_ids"]))
    if "restrictions" in f:
        said.append("限制：" + "、".join(f["restrictions"]))
    return said


__all__ = ["Intent", "MAX_TEXT", "cn_int", "parse_intent"]
