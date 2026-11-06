"""M4：把 Itinerary 渲染成可读交付物 —— Markdown / HTML / PDF。

- Markdown：给 GitHub / 编辑器预览，也方便二次编辑。
- HTML：自包含单文件，A4 打印优化。
- PDF：调用系统里的 Chrome / Edge 无头模式 `--print-to-pdf`，不额外装 Python 库。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from math import cos, radians
from pathlib import Path

from jinja2 import Template

from app.companion import day_note_ref, note_ref
from app.culture import story_text
from app.geo import DayRoute, RouteSummary, build_route, by_city, haversine_km
from app.library import PlaceLibrary, library_for_itinerary, library_for_request
from app.media import load_media, photo_path
from app.names import local_label
from app.schemas import Itinerary, ItineraryItem, TravelRequest

_TEMPLATE = Template(
    (Path(__file__).resolve().parent / "templates" / "itinerary_html.jinja").read_text(encoding="utf-8")
)

_WEEKDAY = "一二三四五六日"
_TYPE_ZH = {
    "sight": "观景", "museum": "博物馆", "food": "餐饮", "meal": "用餐",
    "logistics": "交通", "activity": "活动", "rest": "休息",
}
_STYLE_ZH = {"relaxed": "轻松节奏", "balanced": "均衡节奏", "intense": "紧凑节奏"}
_BUDGET_ZH = {"low": "低预算", "medium": "中等预算", "high": "高预算"}


class RenderError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
def _fmt_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} 分钟"
    h, m = divmod(minutes, 60)
    return f"{h} 小时" + (f" {m} 分钟" if m else "")


def _weekday(d: date) -> str:
    return _WEEKDAY[d.weekday()]


def _profile_text(itinerary: Itinerary, request: TravelRequest | None) -> str:
    p = itinerary.trip.traveler_profile
    interests = "、".join(request.interests) if request else "、".join(p.interests)
    return f"{_STYLE_ZH.get(p.style, p.style)} · {_BUDGET_ZH.get(p.budget, p.budget)} · {interests}"


def _weather_text(w: dict | None) -> str | None:
    """每天标题下的一行天气：天气文字 · 最高/最低温 · 降雨概率（或「去年同期，仅作参考」）；雨天补一句室外点提醒。"""
    if not w:
        return None

    def deg(v) -> str:
        return "–" if v is None else f"{round(v)}°"

    bits = [w["label"], f"{deg(w.get('t_max'))} / {deg(w.get('t_min'))}"]
    if w.get("source") == "history":
        bits.append("去年同期，仅作参考")
    elif w.get("rain_prob") is not None:
        bits.append(f"降雨 {w['rain_prob']}%")
    text = " · ".join(bits)
    if w.get("rainy"):
        outdoor = [p["title"] for p in w.get("outdoor_places") or []]
        text += f"。预报降雨概率 {w['rain_prob']}%"
        text += f"，室外点：{'、'.join(outdoor)}，建议备选室内" if outdoor else "，当天的点基本都在室内"
    return text


def _money(eur: float | None, cny: float | None, main: str = "CNY") -> str:
    """「¥1,863 · €249」：结算货币在前，另一种并列；拿不到汇率时只有欧元。"""
    if eur is None:
        return "待补"
    eur_s = f"€{eur:,.0f}" if abs(eur - round(eur)) < 0.005 else f"€{eur:,.2f}"
    if cny is None:
        return eur_s
    cny_s = f"¥{cny:,.0f}"
    return f"{eur_s} · {cny_s}" if main == "EUR" else f"{cny_s} · {eur_s}"


def _budget_view(b: dict | None) -> dict | None:
    """费用估算一节（规格 M09 S1f）：把预算接口的结果整理成导出里要写的文字；没有预算就整节省略。"""
    if not b:
        return None
    main = b.get("currency", "CNY")
    n = len(b["travelers"])
    rows = []
    for c in b["categories"]:
        pend = f"；待补：{'、'.join(c['pending'])}" if c["pending"] else ""
        unknown = c.get("unknown")
        rows.append({"label": c["label"],
                     "person": "待补" if unknown else _money(c["eur_person"], c["cny_person"], main),
                     "total": "待补" if unknown else _money(c["eur_total"], c["cny_total"], main),
                     "note": f"{c['source_note']}{pend}"})
    t = b["total"]
    fx = b["fx"]
    cap = None
    if b.get("cap") and b["status"] in ("over", "within"):
        sym = "¥" if b["cap"]["currency"] == "CNY" else "€"
        out_sym = "¥" if b["status_currency"] == "CNY" else "€"
        diff = f"{out_sym}{b['status_amount']:,.0f}"
        cap = (f"预算上限 {sym}{b['cap']['amount']:,.0f}：估算超出 {diff}" if b["status"] == "over"
               else f"预算上限 {sym}{b['cap']['amount']:,.0f}：在预算内，还剩 {diff}")
    party = "、".join(f"{x['label']}" for x in b["travelers"])
    discounts = [f"第 {d['traveler']} 位（{d['who']}）省 €{d['saved_eur']:,.2f}：{'、'.join(d['places'])}"
                 for d in b.get("discounts", [])]
    return {
        "travelers": n,
        "party": party,
        "rows": rows,
        "total_person": _money(t["eur_person"], t["cny_person"], main),
        "total_total": _money(t["eur_total"], t["cny_total"], main),
        "rate": None if fx is None else (f"汇率 1 欧元 = {fx['rate']:g} 元（欧洲央行参考汇率，{fx['date']}"
                                         + ("，旧数据" if fx["stale"] else "") + "）"),
        "cap": cap,
        "discounts": discounts,
        "notes": [x for x in b["notes"] if not x.startswith("暂时拿不到汇率") and not x.startswith("汇率为")],
    }


def _res_text(res: dict | None) -> str | None:
    """清单行后面跟的预订状态：已订（确认码、入场时段）或 建议最晚订票日 / 建议尽早。"""
    if not res:
        return None
    if res["status"] == "booked":
        bits = ["已订"]
        if res.get("confirmation_code"):
            bits.append(f"确认码 {res['confirmation_code']}")
        if res.get("entry_from"):
            bits.append(f"入场 {res['entry_from']}" + (f"–{res['entry_to']}（时段）" if res.get("entry_to") else ""))
        elif res.get("slot"):
            bits.append(f"入场 {res['slot']}")
        if res.get("date_changed"):
            bits.append(f"注意：行程日期变了，这笔是为 {res['booked_for']} 订的")
        return " · ".join(bits)
    if res.get("book_by"):
        return f"建议最晚 {res['book_by'][5:]} 前订（{res['lead_basis']}）"
    return "建议尽早订"


def _booking_rows(it: Itinerary, reservations: dict[str, dict] | None) -> list[dict]:
    rows = []
    for b in it.booking_checklist:
        d = b.model_dump()
        d["res_text"] = _res_text((reservations or {}).get(b.place_id or ""))
        rows.append(d)
    return rows


def _qa_context(report) -> dict | None:
    if report is None:
        return None
    return {
        "text": f"{len(report.errors)} error / {len(report.warnings)} warning",
        "clean": report.ok and not report.warnings,
    }


_TICKET_GROUPS = ((True, "必须提前线上购票"), (False, "建议线上购票（现场也可排队购买）"))


def _catalog_index(request: TravelRequest) -> dict[str, dict]:
    """地点库里的每个点：官网链接、参观重点（导出时按行程条目对上它）。"""
    lib = library_for_request(request)
    return {
        p.name_zh: {"id": p.id, "name_zh": p.name_zh, "name_local": local_label(p), "url": p.public_url,
                    "address": p.address, "highlights": p.highlights, "note": p.note}
        for p in lib.places.values()
    }


def _match_catalog(title: str, index: dict[str, dict], place_id: str | None = None) -> dict | None:
    """行程条目对上地点库：先看 place_id，再按标题（复用地点别名表）。"""
    from app.validator import PlaceIndex  # 延迟导入，避免模块级循环依赖

    if place_id:
        for entry in index.values():
            if entry["id"] == place_id:
                return entry
    text = title.strip().lower()
    if not text:
        return None
    if title.strip() in index:
        return index[title.strip()]
    places = PlaceIndex.load()
    for name, entry in index.items():
        if any(s in text for s in places.surfaces_for(name)):
            return entry
    return None


# --------------------------------------------------------------------------- #
def render_markdown(
    itinerary: Itinerary,
    request: TravelRequest | None = None,
    report=None,
    *,
    reservations: dict[str, dict] | None = None,
    weather: dict[str, dict] | None = None,
    budget: dict | None = None,
    notes: dict[str, str] | None = None,
    stories: dict[str, list[dict]] | None = None,
) -> str:
    it = itinerary
    catalog = _catalog_index(request)
    lines: list[str] = []
    lines.append(f"# {it.trip.destination} · {len(it.days)} 天个性化行程\n")
    lines.append(f"**日期**：{it.trip.date_range.start} → {it.trip.date_range.end}"
                 f"（{len(it.days)} 天）  ")
    lines.append(f"**旅行者**：{_profile_text(it, request)}  ")
    lines.append(f"**版本**：{it.version}")
    qa = _qa_context(report)
    if qa:
        lines.append(f"\n> 质检：{qa['text']}")
    lines.append("\n---\n")

    for i, day in enumerate(it.days, start=1):
        lines.append(f"## 第 {i} 天 · {day.date}（周{_weekday(day.date)}）")
        if day.chapter:                                              # 深度路线（规格 09）
            lines.append(f"**{day.chapter}**\n")
        lines.append(f"*{day.theme}*\n")
        if day.guide:
            lines.extend(f"> {p}" for p in day.guide.split("\n") if p)
            lines.append("")
        wtext = _weather_text((weather or {}).get(day.date.isoformat()))
        if wtext:
            lines.append(f"天气：{wtext}\n")
        note = (notes or {}).get(day_note_ref(day.date))            # 以前写的整天笔记
        if note:
            lines.append("备注：" + note.replace("\n", "  \n> ") + "\n")
        lines.append("| 时间 | 安排 | 时长 | 交通 | 备注 |")
        lines.append("|---|---|---|---|---|")
        for item in day.items:
            badge = " 🎫需预约" if item.booking_required else ""
            tag = _TYPE_ZH.get(item.type, item.type)
            note = (item.notes or "").replace("|", "／").replace("\n", " ")
            transport = (item.transport or "—").replace("|", "／")
            hit = _match_catalog(item.title, catalog, item.place_id)
            shown = f"{item.title} · {hit['name_local']}" if hit and hit.get("name_local") else item.title
            lines.append(
                f"| {item.time} | {shown} `{tag}`{badge} | {_fmt_duration(item.duration_minutes)} "
                f"| {transport} | {note} |"
            )
        lines.append("")
        looks = [(item, item.look_for) for item in day.items if item.look_for]
        for item, look in looks:                                     # 深度路线：这一站为什么在这里 / 看什么
            if item.why_here:
                lines.append(f"- 为什么在这里（{item.time} {item.title}）：{item.why_here}")
            lines.append(f"- 看什么（{item.time} {item.title}）：" + "；".join(look))
        if looks:
            lines.append("")
        told = [(item, (stories or {}).get(item.place_id or "")) for item in day.items]
        told = [(item, found) for item, found in told if found]
        for item, found in told:                                      # 地点 → 故事（规格 09 十五）
            lines.append(f"- 这里的故事（{item.time} {item.title}）：" + "；".join(story_text(s) for s in found))
        if told:
            lines.append("")
        written = [(item, (notes or {}).get(note_ref(day.date, item))) for item in day.items]
        written = [(item, text) for item, text in written if text]
        for item, text in written:                                  # 每条安排自己的备注（M10 S4）
            lines.append(f"- 备注（{item.time} {item.title}）：" + text.replace("\n", "  \n  "))
        if written:
            lines.append("")

        matches = [(item, _match_catalog(item.title, catalog, item.place_id)) for item in day.items]
        tip_lines = []
        for item, entry in matches:
            if not entry or not (entry.get("highlights") or entry.get("url") or entry.get("address")):
                continue
            bits = []
            if entry.get("highlights"):
                bits.append("参观重点：" + "；".join(entry["highlights"]))
            if entry.get("url"):
                bits.append(f"[官网购票]({entry['url']})")
            if entry.get("address"):
                bits.append(f"地址：{entry['address']}")
            shown = f"{item.title} · {entry['name_local']}" if entry.get("name_local") else item.title
            tip_lines.append(f"- **{shown}** {' · '.join(bits)}")
        if tip_lines:
            lines.extend(tip_lines)
            lines.append("")

    if it.booking_checklist:
        lines.append("## 购票清单\n")
        for must, title in _TICKET_GROUPS:
            rows = [b for b in _booking_rows(it, reservations) if b["must_online"] == must]
            if not rows:
                continue
            lines.append(f"**{title}**\n")
            for b in rows:
                hint = f"（{b['deadline_hint']}）" if b["deadline_hint"] else ""
                link = f" · [官网购票]({b['url']})" if b["url"] else ""
                note = f"\n  - 购票提示：{b['note']}" if b["note"] else ""
                res = f"\n  - 预订：{b['res_text']}" if b["res_text"] else ""
                box = "x" if b["res_text"] and b["res_text"].startswith("已订") else " "
                lines.append(f"- [{box}] **{b['title']}** — {b['reason']}{hint}{link}{note}{res}")
            lines.append("")

    bv = _budget_view(budget)
    if bv:
        lines.append("## 费用估算\n")
        lines.append(f"出行人：{bv['party']}\n")
        lines.append(f"| 项目 | 人均 | 总计（{bv['travelers']} 人） | 来源 |")
        lines.append("|---|---|---|---|")
        for r in bv["rows"]:
            lines.append(f"| {r['label']} | {r['person']} | {r['total']} | {r['note'].replace('|', '／')} |")
        lines.append(f"| **合计** | **{bv['total_person']}** | **{bv['total_total']}** | |")
        lines.append("")
        for extra in (bv["cap"], bv["rate"], *bv["discounts"], *bv["notes"]):
            if extra:
                lines.append(f"- {extra}")
        lines.append("")

    route = it.generation.route if it.generation else None
    if route is not None and route.reading:
        lines.append(f"## 延伸阅读 / 观影（{route.name_zh}）\n")
        for r in route.reading:
            lines.append(f"- {r.title} · {r.creator}，{r.year}：{r.note}（{r.source_url}）")
        lines.append("")

    if it.risk_notes:
        lines.append("## 风险提醒\n")
        for r in it.risk_notes:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("---\n*由 AI Travel Planner Agent 生成 · 出发前请以各景点官网信息为准*")
    return "\n".join(lines)


def _photo_view(city_id: str | None, place_id: str | None, *, inline: bool) -> dict | None:
    """导出里一个景点的图片 + 简介（地点库里核对过的那一份，和地点卡 / 行程页同源）。
    inline = True（PDF / 长图）：图片直接嵌进文件；否则用本机的图片地址（浏览器里直接打开 HTML）。没有就是 None。"""
    if not city_id or not place_id:
        return None
    try:
        media = load_media(city_id).get(place_id)
    except ValueError:                                             # 图片文件缺了：当作没有图，不让导出失败
        return None
    if media is None:
        return None
    out: dict = {}
    if media.image:
        src = f"/media/places/{media.image.file}"
        if inline:
            path = photo_path(media.image.file)
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            src = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        out["src"] = src
        out["credit"] = f"图片：{media.image.author}，{media.image.license}（Wikimedia Commons）"
    if media.intro:
        out["intro"] = media.intro.text
        out["intro_credit"] = f"简介：{media.intro.source_name}（{media.intro.license}）"
    return out or None


def _item_view(item: ItineraryItem, catalog: dict[str, dict], user_note: str | None = None,
               stories: list[dict] | None = None, photo: dict | None = None) -> dict:
    entry = _match_catalog(item.title, catalog, item.place_id)
    return {
        "time": item.time,
        "title": item.title,
        "title_local": entry.get("name_local") if entry else None,
        "type_zh": _TYPE_ZH.get(item.type, item.type),
        "duration": _fmt_duration(item.duration_minutes),
        "transport": item.transport,
        "booking_required": item.booking_required,
        "notes": item.notes,
        "user_note": user_note,
        "url": entry.get("url") if entry else None,
        "address": entry.get("address") if entry else None,
        "highlights": entry.get("highlights") if entry else None,
        "look_for": item.look_for,
        "why_here": item.why_here,
        "stories": [story_text(s) for s in stories or []],
        "photo": photo,
    }


def cover_stats(itinerary: Itinerary, library: PlaceLibrary | None = None) -> dict:
    """封面统计：天数、地点数、直线距离累计、库里有票价的点的门票合计（不含餐饮和交通）。"""
    lib = library or library_for_itinerary(itinerary)
    route = build_route(itinerary, lib)
    ids = {s.place_id for d in route.days for s in d.stops}
    prices = [lib.places[i].price_eur for i in ids if lib.places[i].price_eur is not None]
    return {
        "days": len(itinerary.days),
        "places": route.place_count,
        "distance_km": round(route.total_km, 1),
        "ticket_eur": sum(prices) if prices else None,        # 这座城市没有核对过的票价（现建的）：None，页面写「待补」
    }


def _day_label(first: int, last: int) -> str:
    return f"第 {first} 天" if first == last else f"第 {first}–{last} 天"


def city_maps(itinerary: Itinerary, route: RouteSummary) -> list[dict]:
    """多城市行程的封面：每座城市一张路线图 + 一段图例（只含这座城市的点；换城市那天两座城市各列各的）。
    单城市行程返回空列表（封面还是原来的一张全程图）。"""
    out: list[dict] = []
    for block in itinerary.legs or []:
        cid = block.city_id
        days = [i for i, d in enumerate(itinerary.days, start=1) if d.city_id == cid or cid in d.theme_by_city]
        sub: list[DayRoute] = []
        legend: list[dict] = []
        for n, day in enumerate(route.days, start=1):
            stops = by_city(day.stops).get(cid)
            if not stops:
                continue
            km = sum(haversine_km((a.lat, a.lng), (b.lat, b.lng)) for a, b in zip(stops, stops[1:], strict=False))
            sub.append(DayRoute(date=day.date, color=day.color, stops=stops, distance_km=km))
            legend.append({"n": n, "color": day.color, "km": round(km, 1), "names": [s.name for s in stops]})
        summary = RouteSummary(days=sub, total_km=sum(d.distance_km for d in sub),
                               place_count=len({s.place_id for d in sub for s in d.stops}))
        out.append({"title": f"{block.trip.destination} · {_day_label(min(days), max(days))}" if days
                    else block.trip.destination,
                    "map_svg": route_map(summary), "legend": legend})
    return out


def route_map(route: RouteSummary) -> str:
    """导出封面的路线图：有街道底图（OpenStreetMap）就用，底图下不到退回自绘的示意图。"""
    from app.maps import basemap

    days = [(d.color, [(s.lat, s.lng) for s in d.stops]) for d in route.days]
    return basemap.render(days) or route_svg(route)


def route_svg(route: RouteSummary, *, width: int = 700, height: int = 430, pad: int = 34) -> str:
    """全程分色路线图：不依赖地图瓦片（打印、离线都能出图），每天一种颜色，圆点里是当天第几站。"""
    pts = [(s.lat, s.lng) for d in route.days for s in d.stops]
    if not pts:
        return ""
    lat0 = sum(p[0] for p in pts) / len(pts)
    kx = cos(radians(lat0))                                  # 经度方向按纬度压缩，避免图被拉宽
    xs = [p[1] * kx for p in pts]
    ys = [-p[0] for p in pts]
    dx, dy = (max(xs) - min(xs)) or 1e-4, (max(ys) - min(ys)) or 1e-4
    scale = min((width - 2 * pad) / dx, (height - 2 * pad) / dy)
    ox = (width - dx * scale) / 2 - min(xs) * scale
    oy = (height - dy * scale) / 2 - min(ys) * scale

    def proj(lat: float, lng: float) -> tuple[float, float]:
        return lng * kx * scale + ox, -lat * scale + oy

    out = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
           f'aria-label="全程路线图"><rect width="{width}" height="{height}" rx="10" fill="#f6f5f2" stroke="#e3e0d9"/>']
    for d in route.days:
        if len(d.stops) >= 2:
            line = " ".join(f"{x:.1f},{y:.1f}" for x, y in (proj(s.lat, s.lng) for s in d.stops))
            out.append(f'<polyline points="{line}" fill="none" stroke="{d.color}" stroke-width="3" '
                       f'stroke-linejoin="round" stroke-linecap="round" opacity="0.85"/>')
    for d in route.days:
        for k, s in enumerate(d.stops, start=1):
            x, y = proj(s.lat, s.lng)
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8.5" fill="{d.color}" stroke="#fff" stroke-width="1.5"/>'
                       f'<text x="{x:.1f}" y="{y + 3.6:.1f}" text-anchor="middle" font-size="10" '
                       f'font-weight="700" fill="#fff">{k}</text>')
    out.append("</svg>")
    return "".join(out)


def render_html(
    itinerary: Itinerary,
    request: TravelRequest | None = None,
    report=None,
    *,
    page_per_day: bool = True,
    reservations: dict[str, dict] | None = None,
    weather: dict[str, dict] | None = None,
    budget: dict | None = None,
    notes: dict[str, str] | None = None,
    image: bool = False,
    stories: dict[str, list[dict]] | None = None,
    inline_images: bool = False,
) -> str:
    it = itinerary
    catalog = _catalog_index(request)
    lib = library_for_request(request)
    route = build_route(it, lib)
    return _TEMPLATE.render(
        stats=cover_stats(it, lib),
        map_svg=route_map(route),
        city_maps=city_maps(it, route),
        legend=[{"n": i, "color": d.color, "km": round(d.distance_km, 1), "names": [s.name for s in d.stops]}
                for i, d in enumerate(route.days, start=1)],
        unresolved=len(report.errors) if report is not None else 0,
        page_per_day=page_per_day,
        image=image,
        dest=it.trip.destination,
        date_start=it.trip.date_range.start,
        date_end=it.trip.date_range.end,
        version=it.version,
        profile=_profile_text(it, request),
        qa=_qa_context(report),
        days=[
            {
                "date": d.date.isoformat(),
                "weekday": _weekday(d.date),
                "theme": d.theme,
                "chapter": d.chapter,
                "guide": [p for p in (d.guide or "").split("\n") if p],
                "weather": _weather_text((weather or {}).get(d.date.isoformat())),
                "weather_rainy": bool(((weather or {}).get(d.date.isoformat()) or {}).get("rainy")),
                "note": (notes or {}).get(day_note_ref(d.date)),
                "items": [_item_view(i, catalog, (notes or {}).get(note_ref(d.date, i)),
                                     (stories or {}).get(i.place_id or ""),
                                     _photo_view(i.city_id or request.city_id, i.place_id, inline=inline_images))
                          for i in d.items],
            }
            for d in it.days
        ],
        booking=[b.model_dump() for b in it.booking_checklist],
        ticket_groups=[(title, [b for b in _booking_rows(it, reservations) if b["must_online"] == must])
                       for must, title in _TICKET_GROUPS],
        risks=it.risk_notes,
        route=it.generation.route if it.generation else None,
        budget=_budget_view(budget),
    )


# --------------------------------------------------------------------------- #
# PDF：走系统浏览器无头打印
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Browser:
    path: str


_WIN_CANDIDATES = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]
_MAC_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def find_browser(override: str | None = None) -> str | None:
    if override:
        return override if Path(override).exists() else None
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    candidates = _WIN_CANDIDATES if sys.platform == "win32" else _MAC_CANDIDATES
    for raw in candidates:
        expanded = os.path.expandvars(raw)
        if "%" not in expanded and Path(expanded).exists():
            return expanded
    return None


def _html_to_pdf_playwright(html_path: Path, pdf_path: Path, *, timeout: int) -> Path:
    """首选路径：用 Playwright 自带的 Chromium，跟系统装的浏览器完全隔离，
    不会撞上「已有一个 Edge 在跑，新进程把参数转发过去就退出」这种单实例坑。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RenderError("playwright 未安装") from exc

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(html_path.resolve().as_uri(), timeout=timeout * 1000)
                page.pdf(path=str(pdf_path), print_background=True, prefer_css_page_size=True)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - playwright 的浏览器未安装等各种运行时错误都归一处理
        raise RenderError(f"Playwright 生成 PDF 失败：{exc}") from exc

    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RenderError("Playwright 生成 PDF 失败：输出文件为空")
    return pdf_path


def _html_to_pdf_system_browser(
    html_path: Path, pdf_path: Path, *, browser: str | None, timeout: int
) -> Path:
    """兜底路径：shell 出系统已装的 Chrome / Edge。不依赖额外 pip 包，
    但如果系统里已经有一个该浏览器实例在跑，新进程有时只是把参数转发过去就退出，不产出 PDF。
    """
    exe = find_browser(browser)
    if not exe:
        raise RenderError(
            "找不到 Chrome / Edge，也没装 playwright，无法生成 PDF。"
            "可先打开 HTML 用浏览器「打印为 PDF」，或 pip install playwright && playwright install chromium。"
        )
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="atp_pdf_"))
    try:
        tmp_html = tmp_dir / "page.html"
        tmp_pdf = tmp_dir / "page.pdf"
        tmp_html.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")
        cmd = [
            exe, "--headless", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
            "--disable-crash-reporter", "--disable-crashpad-forced-startup-collect",
            f"--user-data-dir={tmp_dir / 'profile'}",
            f"--print-to-pdf={tmp_pdf}", tmp_html.as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if not tmp_pdf.exists() or tmp_pdf.stat().st_size == 0:
            output = (proc.stderr or proc.stdout).strip()[:400]
            output = output or "（进程无输出，可能是已有实例把参数转发过去了）"
            raise RenderError(f"PDF 生成失败（{exe}）：{output}")
        shutil.copyfile(tmp_pdf, pdf_path)
    finally:
        # Edge/Chrome 的 crashpad handler 有时在主进程退出后仍短暂占着 profile 目录，
        # 严格删除会报 WinError 145；忽略错误即可，反正是临时目录，OS 迟早会清。
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return pdf_path


def html_to_pdf(html_path: Path, pdf_path: Path, *, browser: str | None = None, timeout: int = 90) -> Path:
    """把 HTML 转 PDF：优先 Playwright（隔离、可靠），装不了就退回系统 Chrome/Edge。"""
    try:
        return _html_to_pdf_playwright(html_path, pdf_path, timeout=timeout)
    except RenderError:
        return _html_to_pdf_system_browser(html_path, pdf_path, browser=browser, timeout=timeout)


IMAGE_WIDTH = 760            # 长图的页面宽度（CSS 像素）
IMAGE_MAX_SIDE = 16000       # 单边像素上限：再高微信 / 各种图片查看器会出问题


def html_to_png(html_path: Path, png_path: Path, *, width: int = IMAGE_WIDTH, timeout: int = 90) -> Path:
    """把 HTML 整页截成一张长图（规格 M08 S6）：2 倍清晰度；太高时降到 1 倍，保证单边不超过 IMAGE_MAX_SIDE。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RenderError("playwright 未安装，无法导出长图") from exc
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": 1000})
                page.goto(html_path.resolve().as_uri(), timeout=timeout * 1000)
                height = page.evaluate("document.documentElement.scrollHeight")
                scale = 2 if height * 2 <= IMAGE_MAX_SIDE else 1
                page.close()
                ctx = browser.new_context(viewport={"width": width, "height": 1000}, device_scale_factor=scale)
                page = ctx.new_page()
                page.goto(html_path.resolve().as_uri(), timeout=timeout * 1000)
                page.screenshot(path=str(png_path), full_page=True, type="png")
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - 浏览器没装等运行时错误统一处理
        raise RenderError(f"生成长图失败：{exc}") from exc
    if not png_path.exists() or png_path.stat().st_size == 0:
        raise RenderError("生成长图失败：输出文件为空")
    return png_path


@dataclass
class RenderResult:
    markdown: Path | None = None
    html: Path | None = None
    pdf: Path | None = None


def render_files(
    itinerary: Itinerary,
    out_dir: Path,
    *,
    name: str,
    formats: tuple[str, ...] = ("md", "html", "pdf"),
    request: TravelRequest | None = None,
    report=None,
    browser: str | None = None,
    page_per_day: bool = True,
    reservations: dict[str, dict] | None = None,
    weather: dict[str, dict] | None = None,
    budget: dict | None = None,
    notes: dict[str, str] | None = None,
    stories: dict[str, list[dict]] | None = None,
) -> RenderResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = RenderResult()

    if "md" in formats:
        p = out_dir / f"{name}.md"
        p.write_text(render_markdown(itinerary, request, report, reservations=reservations,
                                        weather=weather, budget=budget, notes=notes, stories=stories),
                     encoding="utf-8")
        result.markdown = p

    need_html = "html" in formats or "pdf" in formats
    html_path = out_dir / f"{name}.html"
    if need_html:
        html_path.write_text(render_html(itinerary, request, report, page_per_day=page_per_day,
                                         reservations=reservations, weather=weather, budget=budget, notes=notes,
                                         stories=stories), encoding="utf-8")
        if "html" in formats:
            result.html = html_path

    if "pdf" in formats:
        result.pdf = html_to_pdf(html_path, out_dir / f"{name}.pdf", browser=browser)
        if "html" not in formats:
            html_path.unlink(missing_ok=True)

    return result
