"""测试用的小城市：几个景点、够排几天行程，写进「现建城市」目录（CITY_GENERATED_DIR）当作「已经建好的第二座城市」。

单元测试（tests/test_trips.py）和浏览器验收的独立后端（tests/e2e_stack.py 起服务前跑一遍本文件）共用。
"""

from __future__ import annotations

from datetime import date

from app.cities import reload_cities
from app.citybuild.core import build_data, make_city
from app.citybuild.store import save_generated


def _raw(qid, name, en, lat, lng, links, cls="Q570116", cat="sight", theme=None):
    return {"qid": qid, "name_zh": name, "name_en": en, "category": cat, "setting": "outdoor", "minutes": 90,
            "open_access": False, "lat": lat, "lng": lng, "sitelinks": links, "cls": cls, "theme": theme,
            "zone": "core", "osm": None, "coords": "wikidata", "url": None, "url_ok": False, "closed": None,
            "note": "", "area": "", "seed": False}


def write_small_city(city_id: str, zh: str, en: str, centre: tuple[float, float], country: str, tz: str,
                     cur: str, today: date) -> None:
    raws = [_raw(f"Q{900 + i}", f"{zh}景点{i}", f"{en} Sight {i}", centre[0] + 0.004 * i, centre[1] + 0.004 * (i % 3),
                 50 - i, cls="Q33506" if i % 3 == 0 else "Q570116", cat="museum" if i % 3 == 0 else "sight")
            for i in range(8)]
    seed = {"timezone": tz, "country": country, "currency": cur,
            "local_language": "zh" if country == "CN" else "en", "qid": "Q1", "supported": True,
            "aliases": [en.lower(), zh]}
    bbox = {"lat": [centre[0] - 0.25, centre[0] + 0.25], "lng": [centre[1] - 0.3, centre[1] + 0.3]}
    places, themes = build_data(city_id, zh, raws, today)
    save_generated(make_city(city_id, zh, en, centre, bbox, [p["id"] for p in places], today=today, seed=seed),
                   places, themes)
    reload_cities()


if __name__ == "__main__":          # 独立后端起来之前：python -m tests.city_fixtures 2026-10-21（在项目根目录）
    import sys

    day = date.fromisoformat(sys.argv[1])
    write_small_city("lyon", "里昂", "Lyon", (45.7640, 4.8357), "FR", "Europe/Paris", "EUR", day)
    # 境内的城市：地图走高德（验收里没配 Key，退回基础地图）
    write_small_city("kunming", "昆明市", "Kunming", (25.0389, 102.7183), "CN", "Asia/Shanghai", "CNY", day)
