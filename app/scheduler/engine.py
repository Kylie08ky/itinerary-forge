"""排程引擎（规格 M03）。

输入相同则输出相同：不调用 LLM、不访问网络。流程：

1. 算每天的可用窗口（抵达 / 离开缓冲、节奏上限）。
2. 建候选池：主题成员 → 辅助点 → 相邻主题；剔除「避开」和长期闭馆（必去除外）。
3. 先放固定的（pinned）和必去，再放郊区一日游（独占一天），最后按「种子 + 就近」逐天填满。
4. 每天的顺序在所有排列里选通勤 + 惩罚最小的（≤7 个点精确枚举，更多用最近邻 + 2-opt）。
5. 时间线：加通勤、午餐 / 晚餐占位、开放时间；放不下的组合当场判为不可行，由填充步骤换别的点。
"""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from app.cities import City, load_city
from app.daytrips import day_trip_labels
from app.library import (
    WEEKDAY_ZH,
    BestTime,
    Place,
    PlaceLibrary,
    Role,
    ThemeRecipe,
    Ticketing,
    Zone,
    load_library,
)
from app.scheduler.geo import Point, haversine_km, travel
from app.scheduler.models import DayPlan, ScheduleDraft, Stop
from app.scheduler.sun import sunset_minutes
from app.schemas import TravelRequest, normalize_area
from app.validator import ValidatorConfig, edge_buffers, leave_place, leave_title

BIG_VENUE_MIN = 150          # 达到这个时长算「大馆」，受配方 max_big_venues_per_day 限制
HALF_DAY_MIN = 240           # 达到这个时长的点，同一天最多一个
MAX_EXACT = 7                # 不超过这个点数就精确枚举顺序
EVENING_FROM = 16 * 60 + 30     # 晚上的点不早于这个时间开始（规格 07 P20；冬天天黑得早，黄昏就算晚上）
NIGHT = (BestTime.evening, BestTime.golden_hour)      # 属于「晚上」这一段的点：夜场（演出）和日落前后才好看的点
LUNCH_PLACE_BONUS = 2000           # 红孩子市场这类点能当午饭，就一定当（排法的代价减这么多；比「没午饭」的罚分小）
NIGHT_ORDER_PENALTY = 300         # 晚上的点后面还排了白天的点，每个加这么多：晚上的点要排在一天最后
SLOT_PENALTY = 60               # 整天里午饭前的点数不是 1 个，每差一个，这种排法的代价加这么多
EVENING_MAX_MINUTES = 150       # 晚上的点最长这么久
MIN_DAYS_FOR_THEME_DAY_TRIP = 4   # 主题自带的郊区一日游（凡尔赛、迪士尼等）至少要这么多天才默认出现


@dataclass(frozen=True)
class SchedulerConfig:
    day_start: int = 9 * 60
    day_end: int = 21 * 60 + 30
    lunch_at: int = 12 * 60
    lunch_min: int = 75              # 均衡节奏的午饭；轻松 / 紧凑见 lunch_minutes()
    quick_lunch_min: int = 30        # 紧凑节奏的午饭；其它节奏放不下完整午饭时也缩成这个（简餐）
    dinner_at: int = 18 * 60 + 30
    dinner_min: int = 90
    min_window: int = 120            # 窗口短于这个就整天只安排接驳
    min_trip_minutes: int = 300      # 一日游至少要玩这么久才值得去
    nearby_weight: float = 0.7       # 填充时每公里相当于扣多少权重
    filler_below: float = 0.55       # 当天活动量低于上限的这个比例，就补「顺手的小点」
    filler_gap: int = 120            # 当天有超过这么多分钟的空档，也补「顺手的小点」
    filler_max: int = 0              # 每天最多补几个「顺手的小点」；0 = 不补，一天只有你要的那几个行程（规格 07 P20）
    filler_max_minutes: int = 120    # 小点的时长上限

    def lunch_minutes(self, style: str) -> int:
        """午饭时长（规格 07 P16）：轻松比均衡长 15 分钟，紧凑是 30 分钟简餐。"""
        return {"relaxed": self.lunch_min + 15, "intense": self.quick_lunch_min}.get(style, self.lunch_min)


# --------------------------------------------------------------------------- #
# 内部结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Cand:
    place: Place
    weight: int
    tier: int                # 0 主题主要点 / 必去；1 主题辅助点；2 相邻主题里和主题搭的；3 相邻主题里不搭的（最后手段）
    role: str                # anchor | support | must_go
    source: str              # 来自哪个主题（给解释用）
    theme_id: str = ""       # 来自哪个主题的 id（算「每个主题排了几个」用；必去 / 固定 / 手动为空）


@dataclass
class Window:
    day: date
    start: int
    end: int
    arrival: Stop | None = None
    departure: Stop | None = None
    cap_ratio: float = 1.0
    is_full: bool = True
    min_window: int = 120
    leave_place: str = "机场"      # 离开那天去哪里（机场 / 车站）：写进「当天只留出前往……的时间」
    sunset: int | None = None      # 当天日落（当地时间的分钟数）：「黄昏最佳」的点按它定几点去
    car: bool = False              # 选了自驾：城市里的移动按开车算

    @property
    def usable(self) -> bool:
        return self.end - self.start >= self.min_window


@dataclass
class Timeline:
    stops: list[Stop]
    cost: float
    order: tuple[Cand, ...] = field(default_factory=tuple)
    lunch_ok: bool = True        # 这天该有午饭的话，有没有（规格 07 P16）


def _hhmm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:])


def _pt(p: Place) -> Point:
    return (p.lat, p.lng)


def _open_on(p: Place, d: date, *, allow_long_term: bool = False) -> bool:
    return p.closed_reason(d, allow_long_term=allow_long_term) is None


# --------------------------------------------------------------------------- #
# 1. 每天的窗口
# --------------------------------------------------------------------------- #
def _build_windows(req: TravelRequest, cfg: SchedulerConfig, vcfg: ValidatorConfig) -> list[Window]:
    windows: list[Window] = []
    n = req.trip_days
    for i in range(n):
        d = req.start_date + timedelta(days=i)
        w = Window(day=d, start=cfg.day_start, end=cfg.day_end, min_window=cfg.min_window, car=req.self_drive)
        arrive_buf, depart_buf = edge_buffers(req, vcfg)
        trip = day_trip_labels(req)
        if i == 0 and req.arrival_time and req.arrival_time.date() == d:
            arr = req.arrival_time.hour * 60 + req.arrival_time.minute
            w.arrival = Stop(kind="logistics", title=trip[0] if trip else f"抵达{req.city.name_zh}并前往住宿",
                             start=arr, duration=15 if trip else 60, edge="arrive")
            w.start = max(w.start, arr + arrive_buf)
            w.is_full = False
            if arr >= vcfg.late_arrival_after and not trip:
                w.cap_ratio = vcfg.edge_day_load_ratio
        if i == n - 1 and req.departure_time and req.departure_time.date() == d:
            dep = req.departure_time.hour * 60 + req.departure_time.minute
            leave = max(0, dep - depart_buf)
            w.end = min(w.end, leave)
            w.leave_place = leave_place(req)
            w.departure = Stop(kind="logistics", title=trip[1] if trip else leave_title(req), start=leave,
                               duration=min(30 if trip else 150, 24 * 60 - leave), edge="leave")
            w.is_full = False
            if dep <= vcfg.early_departure_before and not trip:
                w.cap_ratio = vcfg.edge_day_load_ratio
        windows.append(w)
    return windows


# --------------------------------------------------------------------------- #
# 2. 候选池
# --------------------------------------------------------------------------- #
def selected_themes(req: TravelRequest, theme_id: str) -> list[tuple[str, int]]:
    """这次生成用到的主题和权重，权重大的在前。没有混选就是「生成时选的那一个」。"""
    sel = [(t.id, t.weight) for t in req.themes]
    if not sel:
        return [(theme_id, 3)]
    return sorted(sel, key=lambda t: -t[1])


BALANCED_NAME = "均衡"


def is_balanced(sel: list[tuple[str, int]], lib: PlaceLibrary) -> bool:
    """「均衡」：没有特定倾向——这座城市的主题全选、权重一样（规格 07 P15）。名字叫「均衡」，不是一长串主题名。"""
    return len(sel) > 1 and {t for t, _w in sel} == set(lib.themes) and len({w for _t, w in sel}) == 1


def _theme_strength(lib: PlaceLibrary, theme_id: str) -> tuple[int, int]:
    """主题的「代表性」：最高的主要点权重、主要点个数。点数分配并列时，代表性高的主题先得。"""
    anchors = [m.weight for _p, m in lib.theme_members(theme_id, Role.anchor)]
    return (max(anchors, default=0), len(anchors))


def theme_targets(sel: list[tuple[str, int]], n_slots: int, lib: PlaceLibrary) -> dict[str, int]:
    """每个所选主题大约要排几个主要点（规格 07 二点六）。

    保底：权重 ≥ 3 的主题 2 个，权重 1–2 的 1 个；保底加起来超过 n_slots 就全部降到 1 个，
    再超就只给最该有的前 n_slots 个主题各 1 个。剩下的点数按权重用最大余数法分；
    并列时代表性高的主题先得。总数恒等于 n_slots。
    """
    if len(sel) == 1:
        return {sel[0][0]: n_slots}
    weights = dict(sel)
    rank = sorted(weights, key=lambda t: (-weights[t], *(-x for x in _theme_strength(lib, t)), t))
    floors = {t: 2 if w >= 3 else 1 for t, w in weights.items()}
    if sum(floors.values()) > n_slots:
        floors = dict.fromkeys(weights, 1)
    if sum(floors.values()) > n_slots:
        floors = {t: 1 if i < n_slots else 0 for i, t in enumerate(rank)}
    rest = n_slots - sum(floors.values())
    total_w = sum(weights.values())
    exact = {t: rest * w / total_w for t, w in weights.items()}
    out = {t: floors[t] + int(exact[t]) for t in weights}
    left = n_slots - sum(out.values())
    for t in sorted(weights, key=lambda t: (-(exact[t] - int(exact[t])), rank.index(t)))[:left]:
        out[t] += 1
    return out


def _mixed_recipe(lib: PlaceLibrary, sel: list[tuple[str, int]]) -> ThemeRecipe:
    """混选时每天的主要点数 / 大馆上限：各主题配方按权重加权平均（单主题就是它自己的配方）。"""
    if len(sel) == 1:
        return lib.get_theme(sel[0][0]).recipe
    total = sum(w for _t, w in sel)
    recipes = [(lib.get_theme(t).recipe, w) for t, w in sel]
    anchors = {style: max(1, round(sum(r.anchors_per_day[style] * w for r, w in recipes) / total))
               for style in ("relaxed", "balanced", "intense")}
    big = max(1, round(sum(r.max_big_venues_per_day * w for r, w in recipes) / total))
    return ThemeRecipe(anchors_per_day=anchors, max_big_venues_per_day=big)


CORE_SHARE = 0.4         # 一个兴趣标签出现在至少这个比例的成员地点里，才算主题的「核心兴趣」


def core_interests(lib: PlaceLibrary, theme_ids: list[str], share: float = CORE_SHARE) -> set[str]:
    """所选主题的「核心兴趣」：在至少 share 比例的成员地点里出现的兴趣标签（摄影主题 = 摄影、地标）。

    借相邻主题的点补空档时，要和它有交集才算搭；「建筑」「街区」这类到处都有的标签不够格（比例低）。
    混选时每个主题各算各的，再合并。
    """
    core: set[str] = set()
    for tid in theme_ids:
        members = [p for p, _m in lib.theme_members(tid)]
        if not members:
            continue
        counts: dict[str, int] = {}
        for place in members:
            for tag in set(place.interests):
                counts[tag] = counts.get(tag, 0) + 1
        core |= {tag for tag, n in counts.items() if n / len(members) >= share}
    return core


FAME_SOURCE = "名气"
FAME_TOP_SHARE = ((0.10, 5), (0.25, 4), (0.50, 3))      # 全城名气前 10% 权重 5、前 25% 权重 4、前 50% 权重 3，其余 2
KIND_PENALTY = 1.2                                      # 同一天里已经有同主题的点，再排一个同主题的扣的分


def fame_weight(rank: int, total: int) -> int:
    for share, weight in FAME_TOP_SHARE:
        if rank <= max(1, round(total * share)):
            return weight
    return 2


def fame_targets(lib: PlaceLibrary, n_slots: int) -> dict[str, int]:
    """「均衡」+ 名气分：每个主题要排几个点（M01 S15b）。主题按它最有名的点排先后，轮流各拿 1 个名额，
    直到分完或点用完——所以名额够的话每个主题（寺庙 / 博物馆 / 山水 / 地标……）都至少有 1 个，名气大的主题多拿。"""
    by_theme: dict[str, list[int]] = {}
    for pid, rank in lib.fame_ranking().items():
        themes = lib.themes_of(pid)
        if themes:
            by_theme.setdefault(themes[0], []).append(rank)
    order = sorted(by_theme, key=lambda t: min(by_theme[t]))
    targets = dict.fromkeys(order, 0)
    left = n_slots
    while left > 0:
        gave = False
        for t in order:
            if left > 0 and targets[t] < len(by_theme[t]):
                targets[t] += 1
                left -= 1
                gave = True
        if not gave:
            break
    return targets


def _fame_pool(pool: dict[str, Cand], lib: PlaceLibrary, avoid: set[str], n_slots: int) -> None:
    """「均衡」+ 有名气分：全城市区的点进候选池，权重按全城名气排名；每个主题内部按名气排，
    名额内（再多一个备选）的是第 0 档，其余第 1 档（M01 S15 / S15b）。郊区的点照旧只来自主题。"""
    ranking = lib.fame_ranking()
    total = len(ranking)
    targets = fame_targets(lib, n_slots)
    seen: dict[str, int] = {}
    for pid, rank in ranking.items():                               # 已经按名气从高到低
        place = lib.places[pid]
        themes = lib.themes_of(pid)
        theme_id = themes[0] if themes else ""
        k = seen.get(theme_id, 0)
        seen[theme_id] = k + 1
        if pid in avoid or place.long_term_closed:
            continue
        tier = 0 if k < targets.get(theme_id, 0) + 1 else 1
        pool[pid] = Cand(place, fame_weight(rank, total), tier, "anchor" if tier == 0 else "support",
                         FAME_SOURCE, theme_id)


def _build_pool(req: TravelRequest, sel: list[tuple[str, int]], lib: PlaceLibrary, notes: list[str],
                n_slots: int) -> tuple[dict[str, Cand], list[str]]:
    """候选池：所选主题的成员 → 辅助点 → 相邻主题；再叠上用户的必去 / 不去 / 权重。

    混选时按权重给每个主题分「主要点名额」：名额内的 anchor 是第 0 档，名额外的退到第 1 档（只在第 0 档排不下时才用），
    所以权重 3:1 的两个主题，主要点大约 3:1 地出现在同一份行程里。
    """
    avoid = {p.id for t in req.avoid if (p := lib.find(t))} | set(req.avoid_ids)

    must: list[str] = []
    for text in req.must_go:
        p = lib.find(text)
        if p is None:
            notes.append(f"必去「{text}」不在地点库里，没能排入，请出发前自行安排。")
        elif p.id not in must:
            must.append(p.id)
    for pid in req.must_go_ids:
        if pid not in lib.places:
            notes.append(f"必去的地点「{pid}」不在地点库里，没能排入。")
        elif pid not in must:
            must.append(pid)

    pool: dict[str, Cand] = {}

    def add(place: Place, weight: int, tier: int, role: str, source: str, theme_id: str = "") -> None:
        if place.id in avoid or place.long_term_closed:
            return
        if place.zone == Zone.day_trip and weight < 4:       # 郊区只收权重 ≥4 的，不拿它凑数
            return
        old = pool.get(place.id)
        if old is None or tier < old.tier or (tier == old.tier and weight > old.weight):
            pool[place.id] = Cand(place, weight, tier, role, source, theme_id)

    chosen = {t for t, _w in sel}
    targets = theme_targets(sel, n_slots, lib) if len(sel) > 1 else {}
    for theme_id, _w in sel:
        theme = lib.get_theme(theme_id)
        quota = targets.get(theme_id)
        seen = 0
        for place, m in lib.theme_members(theme_id):
            if m.role == Role.anchor:
                tier = 0 if quota is None or seen < quota + 1 else 1      # +1 留一个备选，应对闭馆日
                seen += 1
                add(place, m.weight, tier, "anchor" if tier == 0 else "support", theme.name_zh, theme_id)
            else:
                add(place, m.weight, 1, "support", theme.name_zh, theme_id)
    core = core_interests(lib, [t for t, _w in sel])
    for other_id, other in lib.themes.items():
        if other_id in chosen:
            continue
        for place, m in lib.theme_members(other_id, Role.anchor):
            if m.weight >= 3:
                # 和所选主题有共同的核心兴趣才算「搭」；不搭的退到最后一档，只在空档实在太大时才用
                add(place, m.weight - 1, 2 if set(place.interests) & core else 3, "support", other.name_zh, other_id)

    if is_balanced(sel, lib) and lib.fame_ready():
        _fame_pool(pool, lib, avoid, n_slots)

    # 候选池里用户给的权重：4–5 提到第 0 档（池外的也加进来）、1–2 退到最后一档、3 只在池外时才加
    for pid, w in req.place_weights.items():
        place = lib.places.get(pid)
        if place is None or pid in avoid:
            continue
        old = pool.get(pid)
        if w >= 4:
            tier, role = 0, "anchor"
        elif w <= 2:
            tier, role = 1, "support"
        elif old is not None:
            continue
        else:
            tier, role = 0, "anchor"
        if place.zone == Zone.day_trip and w < 4:
            continue
        pool[pid] = Cand(place, w, tier, role, "你选的")

    for pid in must:
        p = lib.get_place(pid)
        pool[pid] = Cand(p, 5, 0, "must_go", "必去")
        if p.long_term_closed:
            notes.append(f"「{p.name_zh}」目前长期闭馆（整修）。你把它标成了必去，已排入行程；出发前请务必确认是否开放。")
    return pool, must


# --------------------------------------------------------------------------- #
# 3. 日内顺序与时间线
# --------------------------------------------------------------------------- #
GOLDEN_HARD = 150           # 「黄昏最佳」的点，最早在日落前这么多分钟到（再早就硬性不排）
GOLDEN_SOFT = 90            # 最好在日落前这么多分钟到；更早的按提前量扣分：能排晚就排晚，为了不留长空档也允许早一点
GOLDEN_FLOOR = 15 * 60      # 冬天日落早，也不早于下午 3 点
GOLDEN_CEIL = 19 * 60 + 30  # 夏天日落晚（22 点），也不晚于 19:30 开始，不然排不进当天


def golden_start(sunset: int | None, lead: int = GOLDEN_HARD) -> int:
    """「黄昏最佳」的点的开始时间：日落前 lead 分钟，随季节变化（规格 M03 S12）。没有日落信息退回 16:30。"""
    if sunset is None:
        return 16 * 60 + 30
    return max(GOLDEN_FLOOR, min(sunset - lead, GOLDEN_CEIL))


FEW_PLACES = 20            # 自动建的城市景点少于这个数，行程说明里提醒一句
STANDARD_CITY_NOTE = ("这座城市的数据是自动建的：景点按名气自动挑选，价格、预约渠道、开放时间和部分闭馆日没有逐条核对，"
                      "出发前请以官网为准。")


def theme_windows(windows: list[Window], recipe: ThemeRecipe, city: City | None = None) -> list[Window]:
    """摄影这类围着光线排的主题（规格 M03 S12）：「黄昏最佳」的点按当天日落定几点去；早上 8 点就出门。"""
    if BestTime.golden_hour in recipe.best_time_bias:
        for w in windows:
            if city is None:
                w.sunset = sunset_minutes(w.day)
            else:
                w.sunset = sunset_minutes(w.day, *city.center, tz=city.timezone)
            if w.arrival is None:
                w.start = min(w.start, 8 * 60)
    return windows


def _earliest(p: Place, w: Window | None = None) -> int:
    if p.meal == "tea":
        return 14 * 60 + 30                          # 下午茶不会排到上午
    if p.best_time == BestTime.evening:
        return 18 * 60 + 30
    if p.best_time == BestTime.golden_hour:
        return golden_start(w.sunset if w else None)
    return 0


def _best_time_penalty(p: Place, start: int, w: Window | None = None) -> int:
    bt = p.best_time
    if bt == BestTime.golden_hour and w is not None and w.sunset is not None:
        # 比理想时间早到多少就扣多少（每 2 分钟 1 分）
        return max(0, (golden_start(w.sunset, GOLDEN_SOFT) - start) // 2)
    if bt == BestTime.morning and start >= 12 * 60:
        return 40
    if bt == BestTime.afternoon and (start < 12 * 60 or start >= 18 * 60):
        return 25
    return 0


def _meal(title: str, start: int, minutes: int) -> Stop:
    return Stop(kind="meal", title=title, start=start, duration=minutes)


def _morning_ok(p: Place) -> bool:
    """能排在上午：不是下午才开始的点（下午茶、午后才好的点）、也不是晚上的点。"""
    return p.meal != "tea" and p.best_time not in (BestTime.afternoon, *NIGHT)


def is_lunch(s: Stop) -> bool:
    """这一项是这天的午饭：午餐占位，或本身就是一顿午饭的点（红孩子市场这类，规格 09 十七之三）。"""
    return (s.kind == "meal" and s.title.startswith("午")) or s.meal == "lunch"


def _close_of(p: Place) -> int | None:
    return _hhmm(p.typical_hours.close) if p.typical_hours else None


def idle_gaps(stops: list[Stop]) -> list[int]:
    """相邻两项之间、扣掉通勤后还空着的分钟数（午 / 晚餐算已安排的内容；接驳不参与）。"""
    out: list[int] = []
    prev: Stop | None = None
    for s in stops:
        if s.kind == "logistics":
            prev = None
            continue
        if prev is not None:
            out.append(max(0, s.start - (prev.start + prev.duration) - (s.travel_min if s.kind == "place" else 0)))
        prev = s
    return out


def _spread(w: Window, stops: list[Stop], by_id: dict[str, Place], cfg: SchedulerConfig) -> None:
    """把「上午块」往午餐靠、「下午块」居中到午餐和晚餐之间，让空档平摊而不是堆在一处。

    只往后挪（景点的最早时间、黄昏 / 夜场限制都不会被破坏），并且不越过关门时间和当天窗口。
    """
    lunch = next((i for i, s in enumerate(stops) if is_lunch(s)), None)
    dinner = next((i for i, s in enumerate(stops) if s.kind == "meal" and s.title.startswith("晚")), None)
    if lunch is None:
        return

    def room(block: list[Stop], limit: int) -> int:
        """这一块最多还能往后挪多少分钟。"""
        end = max(s.start + s.duration for s in block)
        room_ = limit - end
        for s in block:
            close = _close_of(by_id[s.place_id or ""])
            if close is not None:
                room_ = min(room_, close - (s.start + s.duration))
        return max(0, room_)

    morning = [s for s in stops[:lunch] if s.kind == "place"]
    if morning:
        gap = stops[lunch].start - max(s.start + s.duration for s in morning)
        shift = min(gap, 120, room(morning, stops[lunch].start))
        if shift >= 20:
            for i, s in enumerate(stops[:lunch]):
                if s.kind == "place":
                    stops[i] = s.model_copy(update={"start": s.start + shift})

    if dinner is None:
        return
    after = [s for s in stops[lunch + 1:dinner] if s.kind == "place"]
    if not after:
        return
    lunch_end = stops[lunch].start + stops[lunch].duration
    gap_before = after[0].start - after[0].travel_min - lunch_end
    gap_after = stops[dinner].start - max(s.start + s.duration for s in after)
    if gap_after - gap_before < 40:
        return
    shift = min((gap_after - gap_before) // 2, room(after, stops[dinner].start))
    if shift >= 20:
        for i in range(lunch + 1, dinner):
            if stops[i].kind == "place":
                stops[i] = stops[i].model_copy(update={"start": stops[i].start + shift})


def _balance_gaps(w: Window, stops: list[Stop], by_id: dict[str, Place]) -> None:
    """后面的空档太大（> 90 分钟）时，把这一项往后挪，让它后面的空档缩到 90 分钟（景点不能晚于关门时间和当天结束）；
    空档不会被劈成两个都超过 1 小时的。

    从后往前处理，所以前面的项挪动时后面的位置已经定了。只往后挪，景点的最早时间、黄昏 / 夜场限制不会被破坏。
    """
    for i in range(len(stops) - 2, -1, -1):
        s, nxt = stops[i], stops[i + 1]
        lunch = is_lunch(s)                                              # 午饭也能往后挪，但不晚于 14:00 开始
        if (s.kind != "place" and not lunch) or nxt.kind == "logistics":
            continue
        gap = nxt.start - (s.start + s.duration) - (nxt.travel_min if nxt.kind == "place" else 0)
        if gap <= 90:
            continue
        room = min(gap - 90, nxt.start - (nxt.travel_min if nxt.kind == "place" else 0) - (s.start + s.duration))
        close = _close_of(by_id[s.place_id]) if s.place_id else None
        if close is not None:
            room = min(room, close - (s.start + s.duration))
        if lunch:
            room = min(room, 14 * 60 - s.start)
        if room >= 20:
            stops[i] = s.model_copy(update={"start": s.start + room})


def _cost(w: Window, stops: list[Stop], by_id: dict[str, Place]) -> float:
    """通勤分钟 + 时段偏好惩罚 + 空档惩罚（空档越长、越不平均，代价越高）。"""
    places = [s for s in stops if s.kind == "place"]
    cost = float(sum(s.travel_min for s in places))
    cost += sum(_best_time_penalty(by_id[s.place_id or ""], s.start, w) for s in places)
    gaps = idle_gaps(stops)
    cost += 0.3 * sum(gaps) + 3.0 * max(0, max(gaps, default=0) - 90)
    if places:                                       # 白天一整个上午都空着（比如先吃午饭再玩）
        cost += 0.3 * max(0, min(s.start for s in places) - w.start - 90)
    daytime = [s for s in places if by_id[s.place_id or ""].best_time not in NIGHT and not is_lunch(s)]
    if any(s.meal == "lunch" for s in places):
        cost -= LUNCH_PLACE_BONUS                    # 午饭就在一个点里：比「点 + 另排午餐占位」更好
    first_night = next((i for i, s in enumerate(places) if by_id[s.place_id or ""].best_time in NIGHT), None)
    if first_night is not None:
        cost += NIGHT_ORDER_PENALTY * sum(1 for s in places[first_night + 1:] if s in daytime)
    if w.arrival is None and w.departure is None and w.end - w.start >= 360 and len(daytime) >= 2:
        cost += SLOT_PENALTY * abs(morning_count(stops) - 1)     # 整天：上午正好 1 个，其余在下午（P20）
    return cost


LUNCH_PENALTY = 5000         # 该有午饭却没有的排法，代价加这么多（比最大的空档代价还大）：只要有带午饭的排法就一定选它


def morning_count(stops: list[Stop]) -> int:
    """午饭前排了几个点；这天没有午饭就按 13:00 前算。"""
    lunch = next((s.start for s in stops if is_lunch(s)), 13 * 60)
    return sum(1 for s in stops if s.kind == "place" and s.start < lunch)


def lunch_expected(w: Window) -> bool:
    """这天窗口覆盖午饭时段（13:30 前能开始、13:00 后才结束）就该有午饭；下午才到 / 中午前就走的不要求。"""
    return w.start <= 13 * 60 + 30 and w.end >= 13 * 60


def _lunch_ok(w: Window, stops: list[Stop]) -> bool:
    if not any(s.kind == "place" for s in stops) or not lunch_expected(w):
        return True
    return any(is_lunch(s) for s in stops)


def _build(w: Window, order: tuple[Cand, ...], hotel: Point, cfg: SchedulerConfig, cap: int,
           lunch_after: int | None, fixed: dict[str, int] | None = None,
           lunch_min: int | None = None) -> list[Stop] | None:
    """lunch_after = 午餐前排几个景点；None 表示一日游（不排餐）。放不下就返回 None。

    fixed: {place_id: 开始分钟数}，锁定的点必须正好这个时间开始；赶不到就返回 None。
    """
    t, pos = w.start, hotel
    lunch_min = cfg.lunch_min if lunch_min is None else lunch_min
    lunch_title = "午餐（简餐）" if lunch_min <= cfg.quick_lunch_min else "午餐（自行选餐厅）"
    stops: list[Stop] = []
    lunch = dinner = False
    trip = lunch_after is None
    active = 0

    def add_lunch() -> bool:
        nonlocal t, lunch
        ls = max(t, cfg.lunch_at)
        if ls > 14 * 60 or ls + lunch_min > w.end:
            return False
        stops.append(_meal(lunch_title, ls, lunch_min))
        t, lunch = ls + lunch_min, True
        return True

    for i, c in enumerate(order):
        if not trip and i == lunch_after and not lunch and not add_lunch():
            return None                              # 这个方案要求午餐排在这里，但放不下
        p = c.place
        pt = _pt(p)
        tm, mode = travel(pos, pt, car=w.car)
        s = max(t + tm, _earliest(p, w))
        dur = p.duration_min

        if not trip:
            night = p.best_time == BestTime.evening                     # 演出这类夜场：晚餐排在它前面，可以早到 17:30
            if not dinner and (s >= 19 * 60 or night) and t <= 19 * 60 + 30:      # 晚场的点：晚餐排在它前面
                ds = max(t, 17 * 60 + 30 if night else 18 * 60)
                ns = max(ds + cfg.dinner_min + tm, _earliest(p, w))
                if ns + dur <= w.end:
                    stops.append(_meal("晚餐（自行选餐厅）", ds, cfg.dinner_min))
                    t, s, dinner = ds + cfg.dinner_min, ns, True
        else:
            back, _ = travel(pt, hotel, car=w.car)
            dur = min(dur, w.end - s - back)
            if dur < min(cfg.min_trip_minutes, p.duration_min):      # 4 小时的凡尔赛：放得下它自己的游览时长就够了
                return None

        serves = None                                # 这一站本身就是午饭 / 下午茶（规格 09 十七之三）
        if not trip and p.meal == "lunch" and not lunch and s <= 14 * 60:
            s, serves = max(s, 11 * 60 + 30), "lunch"        # 午饭点最早 11:30 开始

        if fixed and p.id in fixed:
            if s > fixed[p.id]:
                return None                          # 前面的安排挤占了它，赶不上锁定的时间
            s = fixed[p.id]

        close = _close_of(p)
        if p.typical_hours:
            s = max(s, _hhmm(p.typical_hours.open))
            if close is not None and s + dur > close:
                return None
        if s + dur > w.end:
            return None

        if serves == "lunch" and s > 14 * 60:
            serves = None                            # 被开门时间或锁定挤到 14:00 以后：只是个普通的点，午饭另排
        if p.meal == "tea" and not trip and 14 * 60 + 30 <= s <= 17 * 60 + 30:
            serves = "tea"
        warn = "该馆长期闭馆（整修），请出发前确认" if p.long_term_closed else None
        stops.append(Stop(kind="place", title=p.name_zh, place_id=p.id, start=s, duration=dur,
                          travel_min=tm, travel_mode=mode, travel_estimated=True, role=c.role,  # type: ignore[arg-type]
                          warning=warn, meal=serves))  # type: ignore[arg-type]
        if serves == "lunch":
            lunch = True
        active += dur
        t, pos = s + dur, pt

    if len(order) > 1 and active > cap:
        return None

    if not trip:
        if not lunch:
            add_lunch()                              # 放不下（太晚了）就不排午餐
        if not dinner and w.end >= 20 * 60:
            ds = max(t, cfg.dinner_at)
            if ds + cfg.dinner_min <= 22 * 60:
                stops.append(_meal("晚餐（自行选餐厅）", ds, cfg.dinner_min))
    stops.sort(key=lambda x: x.start)
    return stops


def _timeline(w: Window, order: tuple[Cand, ...], hotel: Point, cfg: SchedulerConfig,
              cap: int, fixed: dict[str, int] | None = None, style: str = "balanced") -> Timeline | None:
    """按给定顺序排出一整天：试每一种「午餐前排几个景点」，挑代价最小的；任何一点放不下就返回 None。

    午饭不能跳过（规格 07 P16）：先按节奏的午饭时长排；排出来该有午饭却没有，再试 30 分钟简餐；
    还是没有的排法代价加 LUNCH_PENALTY，只要有带午饭的排法就选它。
    """
    by_id = {c.place.id: c.place for c in order}
    if any(c.place.zone == Zone.day_trip for c in order):
        stops = _build(w, order, hotel, cfg, cap, None, fixed)
        return None if stops is None else Timeline(stops=stops, cost=_cost(w, stops, by_id), order=order)

    best: Timeline | None = None
    lunch_options = [cfg.lunch_minutes(style)]
    if cfg.quick_lunch_min < lunch_options[0]:
        lunch_options.append(cfg.quick_lunch_min)
    for lunch_min in lunch_options:
        for k in range(len(order) + 1):
            stops = _build(w, order, hotel, cfg, cap, k, fixed, lunch_min)
            if stops is None:
                continue
            if not fixed:                            # 有锁定的点时不再平移，免得动了它的时间
                _spread(w, stops, by_id, cfg)
                _balance_gaps(w, stops, by_id)
            ok = _lunch_ok(w, stops)
            cost = _cost(w, stops, by_id) + (0 if ok else LUNCH_PENALTY)
            if best is None or cost < best.cost:
                best = Timeline(stops=stops, cost=cost, order=order, lunch_ok=ok)
        if best is not None and best.lunch_ok:
            break                                    # 完整午饭就排得下，不用缩成简餐
    return best


def _two_opt(cands: list[Cand], hotel: Point) -> list[Cand]:
    """点数多到不能枚举时的退路：最近邻起步，再用 2-opt 消除折返。"""
    left = sorted(cands, key=lambda c: c.place.id)
    cur, route = hotel, []
    while left:
        nxt = min(left, key=lambda c: (haversine_km(cur, _pt(c.place)), c.place.id))
        route.append(nxt)
        left.remove(nxt)
        cur = _pt(nxt.place)

    def length(r: list[Cand]) -> float:
        pts = [hotel, *(_pt(c.place) for c in r)]
        return sum(haversine_km(a, b) for a, b in itertools.pairwise(pts))

    improved = True
    while improved:
        improved = False
        for i in range(len(route) - 1):
            for j in range(i + 1, len(route)):
                cand = route[:i] + route[i:j + 1][::-1] + route[j + 1:]
                if length(cand) + 1e-9 < length(route):
                    route, improved = cand, True
    return route


def _best_plan(w: Window, cands: list[Cand], hotel: Point, cfg: SchedulerConfig, vcfg: ValidatorConfig,
               style: str, pinned_pos: dict[str, int], fixed: dict[str, int] | None = None) -> Timeline | None:
    cap = int(vcfg.active_cap(style) * w.cap_ratio)
    base = sorted(cands, key=lambda c: c.place.id)
    if len(base) <= MAX_EXACT:
        orders = itertools.permutations(base)
    else:
        orders = iter([tuple(_two_opt(base, hotel))])
    best: Timeline | None = None
    best_key: tuple | None = None
    for order in orders:
        if any(pos >= len(order) or order[pos].place.id != pid for pid, pos in pinned_pos.items()):
            continue
        tl = _timeline(w, order, hotel, cfg, cap, fixed, style)
        if tl is None:
            continue
        key = (round(tl.cost, 6), tuple(c.place.id for c in order))
        if best_key is None or key < best_key:
            best, best_key = tl, key
    return best


# --------------------------------------------------------------------------- #
# 4. 分天
# --------------------------------------------------------------------------- #
def _compatible(current: list[Cand], c: Cand, recipe: ThemeRecipe) -> bool:
    big = sum(x.place.duration_min >= BIG_VENUE_MIN for x in current)
    half = sum(x.place.duration_min >= HALF_DAY_MIN for x in current)
    if c.place.duration_min >= BIG_VENUE_MIN and big >= recipe.max_big_venues_per_day:
        return False
    return not (c.place.duration_min >= HALF_DAY_MIN and half >= 1)


def _centroid(cands: list[Cand], fallback: Point) -> Point:
    if not cands:
        return fallback
    return (statistics.fmean(c.place.lat for c in cands), statistics.fmean(c.place.lng for c in cands))


LANDMARK_BONUS = 3.0         # 均衡模式下，城市的标志性地点在挑选时加的分（规格 07 P15）


def _fill_key(c: Cand, here: Point, current: list[Cand], cfg: SchedulerConfig,
              bonus: float = 0.0) -> tuple[float, str]:
    """填充时的排序：第一个点只看权重，之后用「权重 − 距离已选点的公里数 × 系数」，越大越优先。"""
    score = c.weight + bonus - (cfg.nearby_weight * haversine_km(here, _pt(c.place)) if current else 0.0)
    return (-score, c.place.name_zh)


def _area_centre(lib: PlaceLibrary, text: str) -> Point | None:
    area = normalize_area(text)
    pts = [_pt(p) for p in lib.places.values() if p.area == area and p.zone == Zone.core]
    if not pts:
        return None
    return (statistics.fmean(p[0] for p in pts), statistics.fmean(p[1] for p in pts))


def _hotel_point(req: TravelRequest, lib: PlaceLibrary, override: Point | None, day: date | None = None) -> Point:
    """某一天路线的起终点：显式指定 > 当天住的那家（精确坐标优先，其次所在区的中心）> hotel_area > 城市中心。"""
    if override:
        return override
    stay = req.hotel_for(day) if day else None
    if stay is not None:
        if stay.lat is not None and stay.lng is not None:
            return (stay.lat, stay.lng)
        if stay.area and (c := _area_centre(lib, stay.area)):
            return c
    if req.hotel_area and (c := _area_centre(lib, req.hotel_area)):
        return c
    return req.city.center


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def schedule(
    request: TravelRequest,
    theme_id: str,
    library: PlaceLibrary | None = None,
    *,
    pinned: dict[str, tuple] | None = None,
    hotel: Point | None = None,
    config: SchedulerConfig | None = None,
) -> ScheduleDraft:
    """把「主题 + 需求」排成逐天的时间线。

    pinned: {place_id: (日期, 当天第几个地点)} 或 (日期, 位置或 None, 开始分钟数或 None)。
            位置不为 None 时，这个点在当天的顺序位置不会被重排动；开始分钟数不为 None 时，开始时间也不会变（锁定）。
    hotel: 住宿坐标；不给就用 request.hotel_area 里地点的中心，再不行用城市中心。
    """
    lib = library or load_library(request.city_id)
    cfg = config or SchedulerConfig()
    vcfg = ValidatorConfig()
    lib.get_theme(theme_id)                    # 主题不存在 -> KeyError，由调用方转成 4xx
    sel = selected_themes(request, theme_id)
    balanced = is_balanced(sel, lib)
    theme_name = BALANCED_NAME if balanced else " + ".join(lib.get_theme(t).name_zh for t, _w in sel)
    style = request.travel_style.value
    recipe = _mixed_recipe(lib, sel)
    n_target = recipe.anchors_per_day[style]
    pinned = pinned or {}

    notes: list[str] = []
    windows = _build_windows(request, cfg, vcfg)
    theme_windows(windows, recipe, request.city)
    n_slots = max(1, sum(1 for w in windows if w.usable)) * n_target
    pool, must_ids = _build_pool(request, sel, lib, notes, n_slots)
    fame_mode = balanced and lib.fame_ready()          # 均衡 + 有名气分：全城按名气排，不分主题名额（M01 S15）
    if fame_mode:
        notes.append("选点标准：「均衡」模式让每个主题（寺庙、博物馆、山水、地标、美食……）都有点，每个主题里按名气从高到低选"
                     "（名气 = 维基百科的语言版本数，地点卡里有具体数字和全城排名）；"
                     "它只代表知名度，不代表一定适合你。")

    def targets_for(n: int) -> dict[str, int]:
        if fame_mode:
            return fame_targets(lib, n)
        return theme_targets(sel, n, lib) if len(sel) > 1 else {}

    targets = targets_for(n_slots)
    placed: dict[str, int] = {}
    landmarks = set(request.city.landmarks) if balanced and not fame_mode else set()
    avoided = {p.id for x in request.avoid if (p := lib.find(x))} | set(request.avoid_ids)
    # 均衡：城市的地标都当第 0 档候选（郊区的仍按原规则）
    for pid in request.city.landmarks if balanced and not fame_mode else []:
        lm = lib.places.get(pid)
        if lm is None or lm.zone != Zone.core or lm.long_term_closed or pid in avoided:
            continue
        old = pool.get(pid)
        pool[pid] = Cand(lm, max(4, old.weight if old else 0), 0, "anchor", "地标", old.theme_id if old else "")

    def need(c: Cand) -> int:
        return targets.get(c.theme_id, 0) - placed.get(c.theme_id, 0)

    theme_weight = dict(sel)

    def filler_need(c: Cand) -> float:
        """顺手的小点也要尊重权重：混选时，已排的点里这个主题比它「该占的份额」少多少（越少越优先，别的主题的点排最后）。"""
        if len(sel) == 1:
            return 0.0
        if c.theme_id not in theme_weight:
            return -99.0
        share = theme_weight[c.theme_id] / sum(theme_weight.values())
        return round(share * (sum(placed.values()) + 1) - placed.get(c.theme_id, 0), 2)

    def rank_key(c: Cand, here: Point, cur: list[Cand]) -> tuple:
        """先挑「点数还没达到目标」的主题（缺口大的先），再按权重 − 距离（均衡时地标加分）。"""
        n = need(c)
        bonus = LANDMARK_BONUS if c.place.id in landmarks else 0.0
        if fame_mode and c.theme_id:                    # 同一天同类别的第 3 个起让给别的类别
            bonus -= KIND_PENALTY * sum(1 for x in cur if x.theme_id == c.theme_id)
        return (0 if n > 0 else 1, -max(n, 0), *_fill_key(c, here, cur, cfg, bonus))
    hotel_pt = _hotel_point(request, lib, hotel)
    hotel_on = {w.day: _hotel_point(request, lib, hotel, w.day) for w in windows}

    assigned: dict[date, list[Cand]] = {w.day: [] for w in windows}
    plans: dict[date, Timeline | None] = {w.day: None for w in windows}
    trip_days: set[date] = set()
    used: set[str] = set()

    def pinned_for(day: date) -> dict[str, int]:
        return {pid: pin[1] for pid, pin in pinned.items() if pin[0] == day and pin[1] is not None}

    def fixed_for(day: date) -> dict[str, int]:
        return {pid: pin[2] for pid, pin in pinned.items() if pin[0] == day and len(pin) > 2 and pin[2] is not None}

    def try_add(w: Window, c: Cand, *, limit: int, accept: Callable[[Timeline], bool] | None = None) -> bool:
        cur = assigned[w.day]
        if len(cur) >= limit or not _open_on(c.place, w.day, allow_long_term=c.role == "must_go"):
            return False
        if not _compatible(cur, c, recipe):
            return False
        tl = _best_plan(w, [*cur, c], hotel_on[w.day], cfg, vcfg, style, pinned_for(w.day), fixed_for(w.day))
        if tl is None or (accept is not None and not accept(tl)):
            return False
        if not tl.lunch_ok and c.role != "must_go" and not pinned_for(w.day):
            return False                             # 这样排会吃不上午饭：换别的点（规格 07 P16）
        if (c.place.best_time in NIGHT and c.role != "must_go" and not pinned_for(w.day)
                and not any(s.kind == "meal" and s.title.startswith("晚") for s in tl.stops)):
            return False                             # 晚上的点也要吃得上晚饭（P20）
        assigned[w.day].append(c)
        plans[w.day] = tl
        used.add(c.place.id)
        if c.theme_id:
            placed[c.theme_id] = placed.get(c.theme_id, 0) + 1
        return True

    usable = [w for w in windows if w.usable]
    by_day = {w.day: w for w in windows}

    # 3a. 固定点（pinned）先落位
    for pid, pin in sorted(pinned.items()):
        d = pin[0]
        w = by_day.get(d)
        c = pool.get(pid) or Cand(lib.get_place(pid), 5, 0, "must_go", "固定")
        if w is None or not try_add(w, c, limit=n_target + 2):
            notes.append(f"固定的「{c.place.name_zh}」没能放进 {d}，已忽略。")

    # 3b. 必去
    for pid in must_ids:
        c = pool[pid]
        if pid in used or c.place.zone == Zone.day_trip:
            continue
        order = sorted(usable, key=lambda w: (len(assigned[w.day]), w.day))
        if not any(try_add(w, c, limit=n_target + 1) for w in order):
            notes.append(f"必去「{c.place.name_zh}」在这几天里闭馆或排不下，没能排入，请出发前自行安排。")

    # 3c. 郊区一日游：独占一天，放在中间的整天上
    full_days = [w for w in usable if w.is_full]
    # 主题自带的郊区一日游只给 4 天以上的行程；短行程把时间留给市区（必去不受限制）
    allowed = max(1, len(full_days) // 3) if len(full_days) >= 2 and len(usable) >= MIN_DAYS_FOR_THEME_DAY_TRIP else 0
    # 郊区一日游只来自本主题（tier 0）或必去，不从相邻主题里借
    trips = sorted((c for c in pool.values() if c.place.zone == Zone.day_trip and c.tier == 0),
                   key=lambda c: (c.role != "must_go", -c.weight, c.place.name_zh))
    done_trips = 0
    for c in trips:
        forced = c.role == "must_go"
        if not forced and done_trips >= allowed:
            break
        candidates = full_days if full_days else [w for w in usable if w.end - w.start >= 540]
        mid = (len(windows) - 1) / 2
        for w in sorted(candidates, key=lambda w: (abs((w.day - windows[0].day).days - mid), w.day)):
            if assigned[w.day] or w.day in trip_days:
                continue
            if try_add(w, c, limit=1):
                trip_days.add(w.day)
                done_trips += 1
                break
        else:
            if forced:
                notes.append(f"必去「{c.place.name_zh}」需要整天，但找不到合适的空天，没能排入。")

    # 3d. 其余天：种子 + 就近，约束最紧的天先填
    regular = [w for w in usable if w.day not in trip_days]
    if targets and trip_days:                       # 一日游那天只有 1 个点，各主题的点数目标按剩下的天重新分
        targets = targets_for(len(regular) * n_target + len(trip_days))

    def open_count(w: Window) -> int:
        return sum(1 for c in pool.values() if c.place.zone == Zone.core and _open_on(c.place, w.day))

    def day_len(w: Window) -> int:
        """白天排了几个点：夜场的点（必去 / 固定的）占的是晚上，不占白天的名额。"""
        return sum(1 for c in assigned[w.day] if c.place.best_time not in NIGHT and c.place.meal != "lunch")

    for w in sorted(regular, key=lambda w: (open_count(w), w.day)):
        while day_len(w) < n_target:
            cur = assigned[w.day]
            options = [c for c in pool.values()
                       if c.place.id not in used and c.place.zone == Zone.core
                       and not (c.place.best_time in NIGHT                        # 晚上的点一天只排 1 个
                                and any(x.place.best_time in NIGHT for x in cur))
                       and not (c.place.meal == "lunch" and any(x.place.meal == "lunch" for x in cur))
                       and not (c.place.meal == "tea" and any(x.place.meal == "tea" for x in cur))
                       and not (c.place.best_time == BestTime.evening and c.tier > 1)
                       and _open_on(c.place, w.day) and _compatible(cur, c, recipe)]
            need_morning = day_len(w) == n_target - 1 and not any(
                _morning_ok(x.place) and x.place.meal != "lunch" for x in cur)
            if not options:
                break
            here = _centroid(cur, hotel_on[w.day])
            # 先试最靠前的一层；这一层在这天排不下，就试下一层（最多到第 2 档，第 3 档只在别的都没有时才用）
            tiers = sorted({c.tier for c in options if c.tier <= 2}) or [min(c.tier for c in options)]
            added = False
            for strict in (True, False) if need_morning else (False,):     # 先只试上午能排的
                for tier in tiers:
                    tier_options = [c for c in options if c.tier == tier and (not strict or _morning_ok(c.place))]
                    ranked = sorted(tier_options, key=lambda c, here=here, cur=cur: rank_key(c, here, cur))
                    if any(try_add(w, c, limit=n_target + len(cur) - day_len(w)) for c in ranked):
                        added = True
                        break
                if added:
                    break
            if not added:
                break

    # 3e. 顺手的小点：当天太空，或有超过 2 小时的空档时，补几个短的点（花园、步行街、咖啡馆），
    #     它们不算「主要点」。先用本主题的辅助点，不够再借相邻主题的。
    #     「太空」时有就加；只是「空档大」时，必须真的让最长空档变短才加。
    for w in sorted(regular, key=lambda w: w.day):
        cap = int(vcfg.active_cap(style) * w.cap_ratio)
        for _ in range(cfg.filler_max):
            cur = assigned[w.day]
            if sum(c.role == "filler" for c in cur) >= cfg.filler_max:
                break                                   # 上午补的小点也算在内
            tl_now = plans[w.day]
            gap_now = max(idle_gaps(tl_now.stops), default=0) if tl_now else 0
            light = sum(c.place.duration_min for c in cur) < cap * cfg.filler_below
            if not light and gap_now <= cfg.filler_gap:
                break
            here = _centroid(cur, hotel_on[w.day])
            options = [c for c in pool.values()
                       if c.place.id not in used and c.place.zone == Zone.core
                       and (c.tier <= 2 or gap_now > cfg.filler_gap)           # 第 3 档（不搭的）只在空档太大时用
                       and c.place.ticketing != Ticketing.online_required      # 顺手的点不添订票负担
                       and c.place.duration_min <= (cfg.filler_max_minutes if c.tier <= 1 else 90)
                       and _open_on(c.place, w.day)]

            def shorter(tl: Timeline, gap_now: int = gap_now, light: bool = light) -> bool:
                return light or max(idle_gaps(tl.stops), default=0) < gap_now

            ranked = sorted(options, key=lambda c, here=here, cur=cur: (
                c.tier if (len(sel) == 1 or c.tier >= 2) else 0, -filler_need(c), *_fill_key(c, here, cur, cfg)))
            for c in ranked:
                filler = Cand(c.place, c.weight, c.tier, "filler", c.source, c.theme_id)
                if try_add(w, filler, limit=len(cur) + 1, accept=shorter):
                    break
            else:
                break

    # 3f. 每天至少 2 个点（规格 07 P15）：能排景点的整天至少 2 个，到达 / 离开的半天至少 1 个。
    #     主要点不够就从任何一档里借（小点标 filler），借不到才作罢并写进说明。
    unfilled: list[date] = []                    # 库里的点用完了、排不满的天：合成一条说明
    for w in sorted(regular, key=lambda w: w.day):
        floor = 2 if (w.end - w.start >= 360 and w.arrival is None and w.departure is None) else 1
        while len(assigned[w.day]) < floor:
            cur = assigned[w.day]
            here = _centroid(cur, hotel_on[w.day])
            options = [c for c in pool.values()
                       if c.place.id not in used and c.place.zone == Zone.core
                       and c.place.ticketing != Ticketing.online_required      # 和小点一样：不添订票负担、时间短
                       and c.place.best_time not in NIGHT
                       and c.place.duration_min <= cfg.filler_max_minutes
                       and _open_on(c.place, w.day) and _compatible(cur, c, recipe)]
            ranked = sorted(options, key=lambda c, here=here, cur=cur: (c.tier, *rank_key(c, here, cur)))
            for c in ranked:
                cand = Cand(c.place, c.weight, c.tier, "filler", c.source, c.theme_id)      # 凑数的算小点，不占节奏上限
                if try_add(w, cand, limit=floor):
                    break
            else:
                unfilled.append(w.day)
                break

    if unfilled:
        shown = "、".join(f"{d.month}-{d.day:02d}" for d in unfilled)
        notes.append(f"{len(unfilled)} 天排不满（{shown}）：地点库里没有更多这些天开放、又排得进时间的点，"
                     "这几天排得比较少，空出来的时间留作自由安排，想去哪可以再加。")

    # 3g. 晚上（规格 07 P20）：均衡 / 紧凑每晚最多 1 个夜景 / 游船 / 演出，挑不到合适的就让这晚自由，不凑数。
    #     轻松的晚上自由，除非主题本身就是夜场的（比如娱乐）。必去 / 固定的晚上的点已经在了就不再加。
    nights = [p for p in lib.places.values()
              if p.zone == Zone.core and not p.long_term_closed and p.id not in avoided
              and p.best_time in NIGHT and p.duration_min <= EVENING_MAX_MINUTES]

    def themed(p: Place) -> bool:
        c = pool.get(p.id)
        return c is not None and c.tier <= 1

    for w in sorted(regular, key=lambda w: w.day):
        tl_now = plans[w.day]
        if tl_now is None or w.end < 20 * 60:
            continue
        if any(s.kind == "place" and lib.places[s.place_id or ""].best_time in NIGHT for s in tl_now.stops):
            continue
        here = _centroid(assigned[w.day], hotel_on[w.day])
        # 演出（夜场）只给主题里有它的（比如娱乐）；别的主题的晚上是黄昏夜景、游船这类，不给人排歌舞秀
        options = [p for p in nights if p.id not in used and _open_on(p, w.day)
                   and ((themed(p) and (style != "relaxed" or p.best_time == BestTime.evening))
                        or (style != "relaxed" and p.best_time == BestTime.golden_hour))]
        def night_cand(p: Place) -> Cand:
            base = pool.get(p.id)
            return Cand(p, base.weight if base else 2, base.tier if base else 2, "support", "夜间",
                        base.theme_id if base else "")

        for p in sorted(options, key=lambda p, here=here: (
                0 if themed(p) else 1, -filler_need(night_cand(p)),            # 混选：晚上的点也尊重各主题该占的份额
                p.ticketing == Ticketing.online_required,
                -(pool[p.id].weight if p.id in pool else 2), haversine_km(here, _pt(p)), p.id)):
            cand = night_cand(p)

            def at_night(tl: Timeline, pid: str = p.id) -> bool:
                dinner = any(s.kind == "meal" and s.title.startswith("晚") for s in tl.stops)      # 也要吃得上晚饭
                return tl.lunch_ok and dinner and any(s.place_id == pid and s.start >= EVENING_FROM for s in tl.stops)

            if try_add(w, cand, limit=len(assigned[w.day]) + 1, accept=at_night):
                break

    # 5. 解释、说明
    supplement = [c.place.name_zh for cs in assigned.values() for c in cs
                  if c.tier > 0 and c.role != "filler" and c.source != "夜间"]
    if supplement:
        notes.append(f"「{theme_name}」主题的主要地点不够排满 {request.trip_days} 天，"
                     f"已补充相邻的点：{'、'.join(supplement)}。")

    days: list[DayPlan] = []
    for w in windows:
        tl = plans[w.day]
        stops = list(tl.stops) if tl else []
        if w.arrival:
            stops.insert(0, w.arrival)
        if w.departure:
            stops.append(w.departure)
        days.append(DayPlan(date=w.day, stops=stops, reasons=_reasons(w, tl, pool, lib, theme_name, trip_days)))
    if load_city(request.city_id).tier == "standard":
        notes.insert(0, STANDARD_CITY_NOTE)
        if len(lib.places) < FEW_PLACES:
            notes.insert(1, f"开放数据里这座城市只有 {len(lib.places)} 个景点，天数多的话会有不少空白的天；"
                            "可以自己在行程里加地点。")
    return ScheduleDraft(theme_id=sel[0][0], theme_ids=[t for t, _w in sel], theme_name=theme_name,
                         hotel=hotel_on[windows[0].day] if windows else hotel_pt, days=days, notes=notes)


def _reasons(w: Window, tl: Timeline | None, pool: dict[str, Cand], lib: PlaceLibrary,
             theme_name: str, trip_days: set[date]) -> list[str]:
    out: list[str] = []
    if tl is None:
        if w.arrival and not w.usable:
            out.append("抵达较晚，当天只安排抵达和前往住宿，不排景点。")
        elif w.departure and not w.usable:
            out.append(f"离开较早，当天只留出前往{w.leave_place or '出发地点'}的时间，不排景点。")
        return out
    placed = [s for s in tl.stops if s.kind == "place"]
    for s in placed:
        c = pool.get(s.place_id or "")
        if c is None:
            continue
        if c.role == "filler":
            out.append(f"顺手加了「{s.title}」：当天活动偏少，它离前后的点近、时间短。")
            continue
        label = "必去" if c.role == "must_go" else f"{c.source}主题 · 权重 {c.weight}"
        if c.source == FAME_SOURCE and (place := lib.places.get(c.place.id)) and place.fame:
            label = (f"名气——维基百科 {place.fame.sitelinks} 种语言版本，全城第 "
                     f"{lib.fame_ranking().get(place.id, '?')}")
        out.append(f"选了「{s.title}」：{label}。")
    if w.day in trip_days and placed:
        out.append(f"「{placed[0].title}」是郊区一日游，往返和游玩要一整天，所以这天只排它；午餐请在景区内或附近解决。")
    elif len(placed) >= 2:
        legs = [f"{b.title}（{b.travel_mode}约 {b.travel_min} 分钟）" for b in placed[1:]]
        out.append("顺序：" + placed[0].title + " → " + " → ".join(legs) + "。路线按通勤最短排，避免折返。")
    wd = w.day.weekday()
    skipped = [c for c in pool.values() if c.weight >= 4 and c.tier == 0 and wd in c.place.closed_weekdays]
    for c in sorted(skipped, key=lambda c: c.place.name_zh)[:3]:
        out.append(f"周{WEEKDAY_ZH[w.day.weekday()]}「{c.place.name_zh}」闭馆，已避开。")
    return out


# --------------------------------------------------------------------------- #
# 编辑用：只重排某一天（规格 M04）
# --------------------------------------------------------------------------- #
@dataclass
class DayResult:
    plan: DayPlan
    broken_locks: list[str]          # 想保持开始时间、但在新安排里赶不上的锁定点


def plan_day(
    request: TravelRequest,
    day: date,
    place_ids: list[str],
    library: PlaceLibrary | None = None,
    *,
    optimize: bool = True,
    fixed: dict[str, int] | None = None,
    hotel: Point | None = None,
    config: SchedulerConfig | None = None,
) -> DayResult:
    """把「用户指定的这几个地点」排进某一天，算出顺序（optimize）或按给定顺序算出时间。

    和 schedule() 的区别：不挑地点、不检查闭馆日、不限制活动量和当天结束时间——用户手动放进来的就照放，
    有问题（闭馆、超时、赶不上航班）交给质检去提示，而不是在这里拒绝。
    fixed: {place_id: 开始分钟数}，锁定的点保持原来的开始时间；赶不上时放弃锁定并记在 broken_locks 里。
    """
    lib = library or load_library(request.city_id)
    cfg = config or SchedulerConfig()
    vcfg = ValidatorConfig()
    window = next((w for w in _build_windows(request, cfg, vcfg) if w.day == day), None)
    if window is None:
        raise ValueError(f"{day} 不在这次行程的日期范围内")
    relaxed = replace(window, end=24 * 60 - 1, cap_ratio=100.0)
    hotel_pt = _hotel_point(request, lib, hotel, day)
    style = request.travel_style.value
    cands = tuple(Cand(lib.get_place(pid), 3, 0, "anchor", "手动") for pid in place_ids)
    cap = int(vcfg.active_cap(style) * relaxed.cap_ratio)

    def run(fx: dict[str, int] | None) -> Timeline | None:
        if optimize:
            return _best_plan(relaxed, list(cands), hotel_pt, cfg, vcfg, style, {}, fx)
        return _timeline(relaxed, cands, hotel_pt, cfg, cap, fx, style)

    tl = run(fixed or None)
    broken: list[str] = []
    if tl is None and fixed:
        tl, broken = run(None), sorted(fixed)
    if tl is None:
        raise ValueError("这一天排不下这些地点")
    stops = list(tl.stops)
    if window.arrival:
        stops.insert(0, window.arrival)
    if window.departure:
        stops.append(window.departure)
    return DayResult(plan=DayPlan(date=day, stops=stops), broken_locks=broken)
