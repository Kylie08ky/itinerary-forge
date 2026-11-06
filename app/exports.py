"""日历（ICS）和地图（KML）导出。两种格式都是纯文本，手写即可，不引第三方库。

- ICS（RFC 5545）：每个行程条目一个事件，带地点、坐标、时长；必须提前线上购票的条目额外带「前一天提醒」。
  时间用目的地城市的时区（城市配置里的 timezone）并附 VTIMEZONE，手机日历导入后不会错位。
- KML：每天一个文件夹（地点标记 + 一条按顺序连起来的折线），Google 地图「我的地图」、Google Earth 等都能导入。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.cities import DEFAULT_CITY, city_of, load_city
from app.geo import RouteSummary, build_route
from app.library import PlaceLibrary, library_for_itinerary
from app.names import bilingual
from app.schemas import Itinerary, ItineraryItem

_PRODID = "-//AI Travel Planner Agent//ZH//"

_VTIMEZONES: dict[str, list[str]] = {
    "Europe/Paris": [
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Paris",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ],
    "Asia/Shanghai": [
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Shanghai",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "DTSTART:19700101T000000",
        "END:STANDARD",
        "END:VTIMEZONE",
    ],
}


# 中欧时间的其他城市（德国、荷兰、比利时）：夏令时规则和巴黎完全一样，只是时区名不同
for _tz in ("Europe/Berlin", "Europe/Brussels", "Europe/Amsterdam"):
    _VTIMEZONES[_tz] = [line.replace("Europe/Paris", _tz) for line in _VTIMEZONES["Europe/Paris"]]


def vtimezone_lines(tz: str) -> list[str]:
    """日历文件里的时区定义：有现成规则的用现成的；没有夏令时的时区按固定时差生成；有夏令时又没有现成规则的只写
    TZID，由日历软件自带的时区库解释（Apple / Google / Outlook 都认 IANA 时区名）。"""
    if tz in _VTIMEZONES:
        return _VTIMEZONES[tz]
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        return []
    jan, jul = zone.utcoffset(datetime(2026, 1, 1, 12)), zone.utcoffset(datetime(2026, 7, 1, 12))
    if jan is None or jan != jul:
        return []
    mins = int(jan.total_seconds() // 60)
    off = f"{'+' if mins >= 0 else '-'}{abs(mins) // 60:02d}{abs(mins) % 60:02d}"
    return ["BEGIN:VTIMEZONE", f"TZID:{tz}", "BEGIN:STANDARD", f"TZOFFSETFROM:{off}", f"TZOFFSETTO:{off}",
            "TZNAME:" + tz.rsplit("/", 1)[-1], "DTSTART:19700101T000000", "END:STANDARD", "END:VTIMEZONE"]


def _esc(text: str) -> str:
    """ICS 文本值转义：反斜杠、分号、逗号、换行。"""
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> list[str]:
    """一行最多 75 字节（不含换行），超出就折行，续行以一个空格开头；按 UTF-8 字节算，且不切断一个字。"""
    out: list[str] = []
    cur = ""
    cur_bytes = 0
    limit = 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if cur_bytes + n > limit:
            out.append(cur)
            cur, cur_bytes, limit = " ", 1, 75          # 续行开头的空格占 1 字节
        cur += ch
        cur_bytes += n
    out.append(cur)
    return out


def _local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _start_end(day_date, item: ItineraryItem) -> tuple[datetime, datetime]:
    h, m = (int(x) for x in item.time.split(":")[:2])
    start = datetime(day_date.year, day_date.month, day_date.day, h, m)
    return start, start + timedelta(minutes=max(item.duration_minutes, 15))


def render_ics(
    itinerary: Itinerary,
    *,
    uid_prefix: str,
    library: PlaceLibrary | None = None,
    now: datetime | None = None,
) -> str:
    blocks = itinerary.legs or []
    city = load_city(blocks[0].city_id) if blocks else (city_of(itinerary.trip.destination) or load_city(DEFAULT_CITY))
    lib = library or library_for_itinerary(itinerary)
    tz = city.timezone
    zones = {b.city_id: load_city(b.city_id).timezone for b in blocks}       # 多城市：每个条目用它所在城市的时区
    used = list(dict.fromkeys(zones.values())) or [tz]
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    online_must = {b.place_id for b in itinerary.booking_checklist if b.place_id and b.must_online}
    title = f"{itinerary.trip.destination} · {len(itinerary.days)} 天行程"

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{_PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_esc(title)}", f"X-WR-TIMEZONE:{tz}", *[ln for z in used for ln in vtimezone_lines(z)]]

    for day in itinerary.days:
        for idx, item in enumerate(day.items):
            start, end = _start_end(day.date, item)
            item_tz = zones.get(item.city_id or day.city_id or "", tz)
            place = lib.places.get(item.place_id) if item.place_id else None
            must_book = bool(place and place.id in online_must)

            desc: list[str] = []
            if must_book:
                desc.append("⚠ 必须提前线上购票，当天不买票进不去。")
            if item.transport:
                desc.append(f"交通：{item.transport}")
            if item.notes:
                desc.append(item.notes)
            if place and place.ticket_note and place.public_url:
                desc.append(f"购票提示：{place.ticket_note}")
            if place and place.public_url:
                desc.append(f"官网购票：{place.public_url}")

            location = item.location
            if place and place.address:
                location = f"{bilingual(place)}, {place.address}"

            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid_prefix}-{day.date:%Y%m%d}-{idx}@ai-travel-planner",
                f"DTSTAMP:{stamp}",
                f"DTSTART;TZID={item_tz}:{_local(start)}",
                f"DTEND;TZID={item_tz}:{_local(end)}",
                f"SUMMARY:{_esc(item.title)}",
                f"LOCATION:{_esc(location)}",
            ]
            if place:
                lines.append(f"GEO:{place.lat:.6f};{place.lng:.6f}")
            if desc:
                lines.append(f"DESCRIPTION:{_esc(chr(10).join(desc))}")
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc('马上出发：' + item.title)}",
                      "TRIGGER:-PT30M", "END:VALARM"]
            if must_book or item.booking_required:
                lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                          f"DESCRIPTION:{_esc('需预订：明天去「' + item.title + '」，确认已提前线上购票')}",
                          "TRIGGER:-P1D", "END:VALARM"]
            lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(seg for line in lines for seg in _fold(line)) + "\r\n"


# --------------------------------------------------------------------------- #
def _kml_color(hex_rgb: str, alpha: str = "ff") -> str:
    """KML 的颜色是 aabbggrr，不是 #rrggbb。"""
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"{alpha}{b}{g}{r}"


def render_kml(itinerary: Itinerary, route: RouteSummary | None = None) -> str:
    route = route or build_route(itinerary)
    name = f"{itinerary.trip.destination} · {len(itinerary.days)} 天行程"
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           f"<name>{escape(name)}</name>"]
    for n, d in enumerate(route.days):
        col = _kml_color(d.color)
        out.append(f'<Style id="day{n + 1}"><LineStyle><color>{col}</color><width>4</width></LineStyle>'
                   f'<IconStyle><color>{col}</color></IconStyle></Style>')
    for n, d in enumerate(route.days):
        out.append(f"<Folder><name>第 {n + 1} 天 · {d.date}</name>")
        for k, s in enumerate(d.stops, start=1):
            addr = f"<br/>{escape(s.address)}" if s.address else ""
            out.append(f"<Placemark><name>{k}. {escape(s.name + (' · ' + s.name_local if s.name_local else ''))}</name>"
                       f"<description><![CDATA[{s.time}{addr}]]></description>"
                       f'<styleUrl>#day{n + 1}</styleUrl>'
                       f"<Point><coordinates>{s.lng:.6f},{s.lat:.6f},0</coordinates></Point></Placemark>")
        if len(d.stops) >= 2:
            coords = " ".join(f"{s.lng:.6f},{s.lat:.6f},0" for s in d.stops)
            out.append(f"<Placemark><name>第 {n + 1} 天路线</name><styleUrl>#day{n + 1}</styleUrl>"
                       f"<LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString></Placemark>")
        out.append("</Folder>")
    out.append("</Document></kml>")
    return "\n".join(out)
