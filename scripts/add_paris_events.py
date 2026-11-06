"""把巴黎冬季有日期的活动加进地点库（规格 09 十八，你 2026-10-28 选 A：只收有依据、互相不矛盾的）。

用法：
    python scripts/add_paris_events.py                 # 加进 data/places/paris.json（已有的更新）+ 加进「购物」「亲子」主题
    python scripts/add_paris_events.py --dry-run

每个活动是一个带 `season`（举办期、来源、核对日期）的地点：行程日期不在期内当作闭馆，不会排进去。坐标由 OSM（Nominatim）按
`osm_query` 搜出来，必须落在 `centre` 周围 1500 米内，不手写。**这是按年的数据**：今年的举办期过了就自动失效，
明年冬天要重新查一遍、改下面的日期和来源。

2026-10-28 核对记录（Sortiraparis 页面原文 + 另外两家聚合站点的摘要，官方网站没能打开）：
- 圣母院旁的圣诞市场：Sortiraparis「The Notre-Dame Christmas Market is back from November 26 to December 27 2026 … Square René Viviani …
  open daily from 10am to 8pm with extended hours until 9pm on Fridays Saturdays and Sundays. Admission is free … around forty chalets」；
  agendaculturel、JDS 的摘要日期一致（时间写法略有出入）。
- 阿尔萨斯圣诞市场：Sortiraparis「back on the forecourt of Paris's Gare de l'Est running from December 2 to 18 2026 … 44th edition of the oldest
  Alsatian Christmas market outside its region … about fifteen producers and artisans … Open from 9am to 7:30pm except Sundays from 10am to 7pm
  (closing at 6pm on December 16th) … Free」；agendaculturel、lebonbon、parisselectbook 的摘要日期一致；**周日是否营业各处说法不一**，所以不写闭馆日。
- 没收的（来源矛盾 / 缺结束日 / 只有单一来源）见规格 09 十八。
"""

# ruff: noqa: E501  （策展数据脚本：长句是数据，不折行）
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from add_seasonal_markets import UA, resolve_point  # noqa: E402
from apply_verification import dump  # noqa: E402

CHECKED = "2026-10-28"
SEASON_NOTE = "日期没能在官方网站核对（来自多个聚合网站，彼此一致），出发前请确认"
THEMES = ("shopping", "family")           # 加进这两个主题的辅助点

EVENTS: list[dict] = [
    {
        "centre": (48.8523, 2.3472), "osm_query": "Square René Viviani, Paris",
        "place": {
            "id": "paris-notre-dame-christmas-market", "name_zh": "圣诞市场（勒内-维维亚尼广场）", "name_en": "Christmas Market at Square René Viviani",
            "name_local": "Marché de Noël de Notre-Dame de Paris", "area": "5区",
            "address": "Square René Viviani, Quai de Montebello, 75005 Paris",
            "category": "shopping", "setting": "outdoor", "zone": "core", "duration_min": 90, "open_access": True,
            "price_note": "入场免费；吃喝购物自费。日期和时间没能在官方网站核对，出发前请确认",
            "best_time": "evening", "closed_weekdays": [], "long_term_closed": False,
            "booking": {"required": False}, "ticketing": "none",
            "metro": "地铁 4 号线 Cité 或 Saint-Michel，RER B / C Saint-Michel Notre-Dame",
            "highlights": ["约 40 个法国手工木屋：银饰、陶器、纺织品、皮具、木制玩具、手工蜡烛", "每天有圣诞老人和合唱、爵士、铜管乐表演"],
            "note": "当季活动：2026-11-26 到 2026-12-27，每天 10:00–20:00，周五到周日 21:00 关（Sortiraparis）；在塞纳河边，可以和巴黎圣母院串联，晚上有灯光",
            "interests": ["christmas", "street"], "hours_verified": False, "coords_verified": False,
            "season": {"start": "2026-11-26", "end": "2026-12-27",
                       "source_url": "https://www.sortiraparis.com/en/what-to-visit-in-paris/walks/articles/58110-notre-dame-de-paris-christmas-market-2022-is-coming",
                       "checked_at": CHECKED, "confirmed_by": "listing", "note": SEASON_NOTE},
        },
    },
    {
        "centre": (48.8768, 2.3592), "osm_query": "Gare de l'Est, Paris",
        "place": {
            "id": "paris-alsace-christmas-market", "name_zh": "阿尔萨斯圣诞市场（东站）", "name_en": "Alsace Christmas Market at Gare de l'Est",
            "name_local": "Marché de Noël alsacien de la Gare de l'Est", "area": "10区",
            "address": "Parvis de la Gare de l'Est (côté rue d'Alsace), 75010 Paris",
            "category": "shopping", "setting": "mixed", "zone": "core", "duration_min": 60, "open_access": True,
            "price_note": "入场免费；吃喝购物自费。日期和时间没能在官方网站核对，出发前请确认",
            "best_time": "any", "closed_weekdays": [], "long_term_closed": False,
            "booking": {"required": False}, "ticketing": "none",
            "metro": "地铁 4 / 5 / 7 号线 Gare de l'Est",
            "highlights": ["约 15 家阿尔萨斯生产者和手艺人：葡萄酒、啤酒、奶酪、火腿肠、蜂蜜", "咕咕洛夫蛋糕、姜饼小人（Mannele）"],
            "note": "当季活动：2026-12-02 到 2026-12-18，每天 9:00–19:30，周日 10:00–19:00，12 月 16 日 18:00 关（Sortiraparis）；有的来源写周日不营业，说法不一，出发前请确认；在东站前广场（rue d'Alsace 一侧），第 44 届，是阿尔萨斯以外最老的阿尔萨斯圣诞市场",
            "interests": ["christmas", "street"], "hours_verified": False, "coords_verified": False,
            "season": {"start": "2026-12-02", "end": "2026-12-18",
                       "source_url": "https://www.sortiraparis.com/en/news/christmas-and-holidays/articles/57104-the-2026-alsatian-christmas-market-at-gare-de-l-est-is-a-plunge-into-the-terroir-right-in-paris",
                       "checked_at": CHECKED, "confirmed_by": "listing", "note": SEASON_NOTE},
        },
    },
]


def build(ev: dict, client: httpx.Client, today: date) -> dict:
    hit = resolve_point(client, ev["osm_query"], ev["centre"])
    if hit is None:
        raise SystemExit(f"OSM 里按「{ev['osm_query']}」找不到落在 {ev['centre']} 周围的点：{ev['place']['id']}")
    lat, lng, osm = hit
    p = dict(ev["place"])
    p["lat"], p["lng"] = round(lat, 6), round(lng, 6)
    p["name_local_check"] = {"source": "manual", "status": "verified", "checked_at": CHECKED, "wikidata": None}
    p["data_status"] = "draft"
    p["draft_source"] = {"osm": osm, "wikidata": None, "built_at": today.isoformat()}
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    today = date.fromisoformat(CHECKED)
    path = ROOT / "data" / "places" / "paris.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    with httpx.Client(headers={"User-Agent": UA}) as client:
        built = [build(ev, client, today) for ev in EVENTS]
    for p in built:
        print(f"{p['id']}: {p['lat']}, {p['lng']}  {p['draft_source']['osm']}  {p['season']['start']} → {p['season']['end']}")
    if args.dry_run:
        return 0
    ids = {p["id"] for p in built}
    data["places"] = [x for x in data["places"] if x["id"] not in ids] + built
    dump(path, data)
    tpath = ROOT / "data" / "themes" / "paris.json"
    raw = tpath.read_bytes()
    crlf = b"\r\n" in raw
    themes = json.loads(raw.decode("utf-8"))
    for th in themes["themes"]:
        if th["id"] in THEMES:
            have = {m["place_id"] for m in th["members"]}
            th["members"] += [{"place_id": i, "role": "support", "weight": 3} for i in sorted(ids) if i not in have]
    out = json.dumps(themes, ensure_ascii=False, indent=2) + "\n"
    tpath.write_bytes((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
    print("已写入地点库和主题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
