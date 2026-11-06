"""排程的「深度路线」模式（规格 09 第三节）。

路线给出**章节和每章的站（有顺序）**，这里负责把它们放进日期：

1. 站按路线顺序一天一天往下填，每天先排满再换下一天；章与章的先后不动。
2. 某一站那天闭馆 → 先放到后面的日子（下一天最前面），写进说明；所有日子都闭馆就不排。
3. 一天的顺序先按路线顺序排；开放时间 / 当天容量排不下才允许在同一天里换顺序，并写明。
4. 天数不够：先砍 optional 站，再砍 core 站里权重低的（同权重砍排在后面的），写进说明。
5. 闭馆日、开放时间、午饭、到达 / 离开缓冲、活动量上限都沿用现有排程器（`_timeline`），路线绕不过它们。

输入相同则输出相同：不调用 LLM、不访问网络。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.library import WEEKDAY_ZH, PlaceLibrary, load_library
from app.routes import Chapter, Route, Stop
from app.scheduler.engine import (
    Cand,
    SchedulerConfig,
    Window,
    _best_plan,
    _build_windows,
    _hotel_point,
    _open_on,
    _timeline,
)
from app.scheduler.geo import Point
from app.scheduler.models import DayPlan, ScheduleDraft
from app.schemas import TravelRequest
from app.validator import ValidatorConfig

DAY_STOPS = {"relaxed": 3, "balanced": 4, "intense": 5}     # 每天最多排几个路线站（再受活动量上限约束）


@dataclass(frozen=True)
class Item:
    chapter: Chapter
    stop: Stop
    index: int                       # 在整条路线里的位置（顺序）


def _drop_key(it: Item) -> tuple[int, int, int]:
    """先砍谁：optional 先于 core；同一优先级里权重低的先砍；同权重砍排在后面的。"""
    return (1 if it.stop.priority == "core" else 0, it.stop.weight, -it.index)


def _chapter_label(route: Route, ids: list[str]) -> str:
    if route.layout == "pick-one":                    # 每天每章挑一个：一天顶部不写章标题，每一站自己带说明
        return ""
    idx = [i + 1 for i, c in enumerate(route.chapters) if c.id in ids]
    titles = [c.title for c in route.chapters if c.id in ids]
    if not idx:
        return ""
    head = f"第 {idx[0]} 章" if len(idx) == 1 else f"第 {idx[0]}–{idx[-1]} 章"
    return f"{head} · {' / '.join(titles)}"


def chapter_label(route: Route, ids: list[str]) -> str:
    """一天顶部的章节标题，如「第 2 章 · 印象派与象征主义的转折」。"""
    return _chapter_label(route, ids)


def schedule_route(
    request: TravelRequest,
    route: Route,
    library: PlaceLibrary | None = None,
    *,
    hotel: Point | None = None,
    config: SchedulerConfig | None = None,
) -> ScheduleDraft:
    lib = library or load_library(request.city_id)
    cfg = config or SchedulerConfig()
    vcfg = ValidatorConfig()
    style = request.travel_style.value
    day_cap = DAY_STOPS[style]
    windows = _build_windows(request, cfg, vcfg)
    all_usable = [w for w in windows if w.usable]
    notes: list[str] = []

    avoided = {p.id for x in request.avoid if (p := lib.find(x))} | set(request.avoid_ids)
    chosen = set(request.route_stops) if request.route_stops is not None else None     # 你勾选要去的站；None = 全部
    items: list[Item] = []
    for n, (chapter, stop) in enumerate(route.stops()):
        if chosen is not None and stop.place_id not in chosen:
            continue
        if stop.place_id in avoided:
            notes.append(f"「{lib.places[stop.place_id].name_zh}」在你的「避开」清单里，路线里没有排它。")
            continue
        items.append(Item(chapter, stop, n))

    # 郊区一日游（凡尔赛）：独占一天；「至少玩够多久才值得去」按路线里最短的一日游算，免得 4 小时的凡尔赛永远排不进去
    trip_minutes = [lib.places[it.stop.place_id].duration_min for it in items
                    if lib.places[it.stop.place_id].zone.value == "day_trip"]
    if trip_minutes:
        cfg = replace(cfg, min_trip_minutes=min(cfg.min_trip_minutes, *trip_minutes))

    # 天数只是推荐，不限制（规格 09 十一之二）：路线内容排在前面的几天（最多 route.days.max 天，站少就更少，
    # 平均每天至少两站），多出来的天用路线对应的主题补充
    route_days = min(len(all_usable), route.days.max, max(1, -(-len(items) // 2))) if items else 0
    usable = all_usable[:route_days]
    free_windows = all_usable[route_days:]
    n_days = len(all_usable)
    if n_days < route.days.min:
        notes.append(f"你选了 {n_days} 天，这条路线推荐至少 {route.days.min} 天：已按优先级取舍，没排进来的站见下面。")
    if free_windows:
        theme_name = lib.get_theme(route.base_theme).name_zh
        extra = len(free_windows)
        notes.append(f"这条路线的内容大约够 {route.days.max} 天，多出来的 {extra} 天用「{theme_name}」主题补充。")

    cands = {it.stop.place_id: Cand(lib.places[it.stop.place_id], it.stop.weight, 0,
                                    "anchor" if it.stop.priority == "core" else "support",
                                    route.name_zh, route.base_theme) for it in items}
    hot = {w.day: _hotel_point(request, lib, hotel, w.day) for w in windows}
    reordered: set = set()          # 为了开放时间 / 容量，当天顺序被换过的日子

    def timeline(w: Window, group: list[Cand]):
        cap = int(vcfg.active_cap(style) * w.cap_ratio)
        tl = _timeline(w, tuple(group), hot[w.day], cfg, cap, None, style)
        if (tl is None or not tl.lunch_ok) and len(group) > 1:
            alt = _best_plan(w, group, hot[w.day], cfg, vcfg, style, {})
            if alt is not None and (tl is None or alt.lunch_ok):
                tl = alt
                reordered.add(w.day)
        return tl if tl is not None and tl.lunch_ok else None          # 午饭不能跳过（P16）：排不出午饭的不要

    def fill(kept: list[Item], slack: int = 0) -> tuple[list[list[Item]], list[Item], list[tuple[Item, Window]]]:
        groups: list[list[Item]] = []
        deferred_log: list[tuple[Item, Window]] = []
        pending = list(kept)
        for n, w in enumerate(usable):
            group: list[Item] = []
            skipped: list[Item] = []
            queue = pending
            # 平均着排：剩下的站除以剩下的天数（向上取整），不超过节奏上限，免得前几天塞满、最后几天空着
            # 一日游独占一天：先从「剩下的天」和「剩下的站」里各扣掉它，免得它的那一天把其余的平均数拉低
            trips_left = sum(1 for x in queue if cands[x.stop.place_id].place.zone.value == "day_trip")
            target = max(1, min(day_cap, -(-(len(queue) - trips_left) // max(1, len(usable) - n - trips_left)) + slack))
            while queue and len(group) < target:
                it = queue[0]
                place = cands[it.stop.place_id].place
                is_trip = place.zone.value == "day_trip"
                if not _open_on(place, w.day):
                    skipped.append(queue.pop(0))
                    deferred_log.append((it, w))
                    continue
                if is_trip and group:                         # 一日游独占一天：留到下一天
                    skipped.append(queue.pop(0))
                    continue
                trial = [*group, it]
                if timeline(w, [cands[x.stop.place_id] for x in trial]) is None:
                    if is_trip:                               # 这天放不下（比如到达日太晚），留给后面的日子
                        skipped.append(queue.pop(0))
                        continue
                    break
                group = trial
                queue.pop(0)
                if is_trip:
                    break
            groups.append(group)
            pending = skipped + queue
        return groups, pending, deferred_log

    def never_open(it: Item) -> bool:
        return all(not _open_on(cands[it.stop.place_id].place, w.day) for w in usable)

    def pick_one() -> tuple[list[list[Item]], list[tuple[Item, str]]]:
        """每天每一章（一类）各挑一家：这一天的市场、午餐、下午茶、咖啡馆各去一个，不一天去三家咖啡馆。
        章里先挑核心、权重高的；那天闭馆的换下一家；一天排不下（超过节奏上限 / 活动量）就先去掉可选的、权重低的。"""
        by_chapter = {ch.id: sorted((it for it in items if it.chapter.id == ch.id),
                                    key=lambda it: (it.stop.priority != "core", -it.stop.weight, it.index))
                      for ch in route.chapters}
        taken: set[str] = set()
        out: list[list[Item]] = []
        for w in usable:
            group: list[Item] = []
            for ch in route.chapters:
                for it in by_chapter[ch.id]:
                    pid = it.stop.place_id
                    if pid not in taken and _open_on(cands[pid].place, w.day):
                        group.append(it)
                        taken.add(pid)
                        break
            while group and (len(group) > day_cap or timeline(w, [cands[x.stop.place_id] for x in group]) is None):
                victim = min(group, key=_drop_key)
                group.remove(victim)
                taken.discard(victim.stop.place_id)
            out.append(group)
        left = [(it, "每天每一类只去一家，天数不够") for it in items if it.stop.place_id not in taken]
        return out, left

    kept = list(items)
    dropped: list[tuple[Item, str]] = []
    reordered.clear()
    groups: list[list[Item]] = [[] for _ in usable]
    deferred_log: list[tuple[Item, Window]] = []
    if route.layout == "pick-one" and usable:
        groups, dropped = pick_one()
        kept = []                                    # 下面「平均着排」的循环不用走了
    while kept and usable:
        for slack in range(day_cap):             # 先平均着排；排不下就让前几天多排一站再试，之后才开始砍站
            reordered.clear()
            groups, leftover, deferred_log = fill(kept, slack)
            if not leftover:
                break
        if not leftover:
            break
        closed = [it for it in leftover if never_open(it)]
        homeless_trips = [it for it in leftover if cands[it.stop.place_id].place.zone.value == "day_trip"
                          and all(timeline(w, [cands[it.stop.place_id]]) is None for w in usable)]
        if closed:
            victim, why = closed[0], "你选的日期它都闭馆"
        elif homeless_trips:
            victim, why = homeless_trips[0], "一日游要独占大半天，这几天里没有够长的一天"
        else:
            victim, why = min(kept, key=_drop_key), "天数 / 节奏排不下"
        kept.remove(victim)
        dropped.append((victim, why))
    if route.layout != "pick-one" and (not usable or not kept):
        groups = [[] for _ in usable]
        deferred_log = []

    # 不留空天：某一天（比如周一，好几个馆都闭）一站都没有时，先把被砍掉的站请回来，
    # 不行就从站最多的那天里挑一个这天开放的站挪过来（源头那天至少还剩一站）。
    for gi, w in enumerate(usable):
        if groups[gi]:
            continue
        pool = [(it, "dropped") for it, _why in dropped] + [
            (it, gj) for gj in sorted(range(len(groups)), key=lambda j: -len(groups[j])) if len(groups[gj]) > 1
            for it in reversed(groups[gj])]
        for it, src in pool:
            c = cands[it.stop.place_id]
            if not _open_on(c.place, w.day) or timeline(w, [c]) is None:
                continue
            groups[gi] = [it]
            if src == "dropped":
                dropped[:] = [(x, why) for x, why in dropped if x != it]
            else:
                groups[src].remove(it)
                name = lib.places[it.stop.place_id].name_zh
                notes.append(f"{w.day} 这天原本没有可排的站，把「{name}」从别的一天挪了过来。")
            break

    if route.layout == "pick-one":
        if dropped:
            names = "、".join(lib.places[it.stop.place_id].name_zh for it, _why in dropped)
            notes.append(f"每天每一类只去一家，这几天里没轮到的：{names}（天数多一些就能去）。")
    else:
        for it, why in dropped:
            name = lib.places[it.stop.place_id].name_zh
            kind = "可选站" if it.stop.priority == "optional" else "核心站"
            notes.append(f"路线里的{kind}「{name}」没有排进来：{why}。")
    seen_deferred: set[str] = set()
    placed_day = {it.stop.place_id: w.day for w, g in zip(usable, groups, strict=True) for it in g}
    for it, w in deferred_log:
        pid = it.stop.place_id
        if pid in seen_deferred or pid not in placed_day:
            continue
        seen_deferred.add(pid)
        notes.append(f"周{WEEKDAY_ZH[w.day.weekday()]}「{lib.places[pid].name_zh}」闭馆，已挪到 {placed_day[pid]}。")

    # 路线之外的自由日：用路线对应的主题补充（成员按权重、离住处近优先，开放、不重复）
    theme_name = lib.get_theme(route.base_theme).name_zh
    used = set(route.place_ids())            # 路线上的站（含你没勾的）不再当补充用：没勾就是不想去
    free_by_day: dict = {}
    empty_route_days = [w for w, g in zip(usable, groups, strict=True) if not g and items]
    if empty_route_days:                     # 路线的天里一站都排不进（举办期外 / 当天排不下）：也用主题补充，不留空天
        notes.append("、".join(str(w.day) for w in empty_route_days)
                     + f" 这天路线里没有能排的站（日期不在举办期内或当天排不下），用「{theme_name}」主题补充。")
        free_windows = [*empty_route_days, *free_windows]
    for w in free_windows:
        pool = [p for p, m in sorted(lib.theme_members(route.base_theme), key=lambda pm: -pm[1].weight)
                if p.id not in used and p.id not in avoided and p.zone.value == "core" and not p.long_term_closed
                and _open_on(p, w.day)]
        group: list[Cand] = []
        for p in pool:
            if len(group) >= max(2, day_cap - 1):
                break
            trial = [*group, Cand(p, 3, 1, "support", theme_name, route.base_theme)]
            if timeline(w, trial) is not None:
                group = trial
        if not group:                                  # 主题成员都排不进：退一步，借地点库里任何开放的小点
            for p in sorted(lib.places.values(), key=lambda p: p.id):
                if p.id in used or p.id in avoided or p.zone.value != "core" or p.long_term_closed:
                    continue
                if not _open_on(p, w.day):
                    continue
                if timeline(w, [Cand(p, 2, 3, "filler", theme_name, route.base_theme)]) is not None:
                    group = [Cand(p, 2, 3, "filler", theme_name, route.base_theme)]
                    break
        used |= {c.place.id for c in group}
        free_by_day[w.day] = group

    by_day = {w.day: g for w, g in zip(usable, groups, strict=True)}
    days: list[DayPlan] = []
    reordered.clear()
    for w in windows:
        group = by_day.get(w.day, [])
        free = free_by_day.get(w.day, [])
        if group:
            tl = timeline(w, [cands[x.stop.place_id] for x in group])
        else:
            tl = timeline(w, free) if free else None
        stops = list(tl.stops) if tl else []
        if w.arrival:
            stops.insert(0, w.arrival)
        if w.departure:
            stops.append(w.departure)
        reasons: list[str] = []
        chapter_ids = list(dict.fromkeys(x.chapter.id for x in group))
        for cid in chapter_ids:
            ch = next(c for c in route.chapters if c.id == cid)
            reasons.append(f"这一天讲「{ch.title}」。")
        for x in group:
            reasons.append(f"选了「{lib.places[x.stop.place_id].name_zh}」：{x.stop.why_here or '路线上的一站'}")
        label = None
        if free:
            label = f"自由日 · {theme_name}主题补充"
            names = "、".join(c.place.name_zh for c in free)
            reasons.append(f"路线的内容已经排完，这一天用「{theme_name}」主题补充：{names}。")
        if not group and not free and w.arrival and not w.usable:
            reasons.append("抵达较晚，当天只安排抵达和前往住宿，不排景点。")
        elif not group and not free and w.departure and not w.usable:
            reasons.append(f"离开较早，当天只留出前往{w.leave_place or '出发地点'}的时间，不排景点。")
        days.append(DayPlan(date=w.day, stops=stops, reasons=reasons, chapters=chapter_ids, label=label))

    for d in sorted(reordered):
        notes.append(f"{d} 这天为了配合开放时间或当天容量，调整了几站的先后顺序。")

    return ScheduleDraft(
        theme_id=route.base_theme, theme_ids=[route.base_theme], theme_name=route.name_zh,
        hotel=hot[windows[0].day] if windows else request.city.center, days=days, notes=notes,
        route_id=route.id, route_skipped=[it.stop.place_id for it, _why in dropped],
    )
