"""把深度路线要用的、地点库里还没有的小众地点加进巴黎地点库（规格 09）。

和别的地点一样严格：每个事实都来自官方页面（或写明来源不是官方），带来源网址和核对日期；
坐标、官网、当地名、闭馆日之后由核对脚本补：
    python scripts/add_route_places.py                       # 加进 data/places/paris.json（已有的不动）
    python scripts/verify_library.py --city paris --only <新增的 id> --out <报告>
    python scripts/apply_verification.py --city paris --report <报告> --today 2026-10-25
    python scripts/fetch_local_names.py --only <新增的 id>

2026-10-25 核对记录：
- 居斯塔夫·莫罗博物馆：官网 musee-moreau.fr/en/practical-information 写明「除周二外每天 10:00–18:00（闭馆前 15 分钟停止入场），
  1 月 1 日、5 月 1 日、12 月 25 日闭馆，全价 8 欧，不用预约（每月第一个周日强烈建议预约），地址 14 rue de La Rochefoucauld」。
- 雅克马尔-安德烈博物馆：官网对自动请求返回 403，没能打开；每天开放、10:00–18:00 起（周五晚场、周末晚闭馆时间各来源不一致）、
  全价约 18–18.5 欧（含临展）来自巴黎旅游局页面和多个聚合站点，**不是官方页面**，所以不写 price_eur（预算里算「待补」），
  hours_verified=false，页面提醒以官网为准。
- 尼西姆·德·卡蒙多博物馆：官网写明自 2024-08-04 起整修闭馆，预计 2030 年重开 → 不收进路线。
- 人文浪漫生活博物馆（Vie romantique）：官网两个页面 503 / 页面说法互相矛盾（是否强制预约），没能核实 → 不收进路线。
"""

# ruff: noqa: E501  （策展数据脚本：长句是数据，不折行）
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apply_verification import dump  # noqa: E402

PATH = ROOT / "data" / "places" / "paris.json"
CHECKED = "2026-10-25"

NEW_PLACES: list[dict] = [
    {
        "id": "musee-gustave-moreau", "name_zh": "居斯塔夫·莫罗博物馆", "name_en": "Musée Gustave Moreau",
        "category": "museum", "setting": "indoor", "zone": "core", "area": "9区",
        "lat": 48.8779, "lng": 2.3345, "duration_min": 75,
        "price_eur": 8.0, "price_source_url": "https://musee-moreau.fr/en/practical-information",
        "price_checked_at": CHECKED, "price_note": "全价 8 欧（官网）；每月第一个周日强烈建议预约，不预约不保证能进",
        "best_time": "any", "closed_weekdays": [1], "closed_dates": ["01-01", "05-01", "12-25"],
        "typical_hours": {"open": "10:00", "close": "18:00"},
        "long_term_closed": False, "booking": {"required": False, "lead_time_hint": None},
        "official_url": "https://musee-moreau.fr/en",
        "ticketing": "online_or_walkin",
        "ticket_note": "官网（2026-10-25 查）：平时不用预约；每月第一个周日强烈建议预约；闭馆前 15 分钟停止入场",
        "ticketing_checked_at": CHECKED,
        "address": "14 Rue de La Rochefoucauld, 75009 Paris",
        "highlights": ["画家本人的故居兼工作室：一层是起居室，二、三层是大画室", "《朱庇特与塞墨勒》《幻影》等象征主义代表作"],
        "note": "象征主义画家莫罗把自己的家变成了博物馆；马蒂斯、鲁奥都曾是他在美术学院的学生",
        "interests": ["museum", "art"], "hours_verified": True, "coords_verified": False,
    },
    {
        "id": "musee-jacquemart-andre", "name_zh": "雅克马尔-安德烈博物馆", "name_en": "Musée Jacquemart-André",
        "category": "museum", "setting": "indoor", "zone": "core", "area": "8区",
        "lat": 48.8754, "lng": 2.3105, "duration_min": 120,
        "price_note": "全价约 18–18.5 欧（含临展，随展览变化）；官网没能自动打开核对，价格来自巴黎旅游局页面和聚合站点，以官网为准",
        "best_time": "any", "closed_weekdays": [],
        "typical_hours": {"open": "10:00", "close": "18:00"},
        "long_term_closed": False, "booking": {"required": False, "lead_time_hint": None},
        "official_url": "https://www.musee-jacquemart-andre.com/en",
        "ticketing": "online_or_walkin",
        "ticket_note": "每天开放；建议线上订时段票，现场也可买（官网对自动请求返回 403，信息来自巴黎旅游局页面和聚合站点，以官网为准）",
        "ticketing_checked_at": CHECKED,
        "address": "158 Boulevard Haussmann, 75008 Paris",
        "highlights": ["乌切罗《圣乔治屠龙》等意大利文艺复兴收藏", "银行家夫妇的私人宅邸：沙龙、冬季花园、楼梯"],
        "note": "银行家爱德华·安德烈和画家妻子内莉·雅克马尔在 19 世纪留下的私人收藏；宅邸 1875 年落成，1913 年成为博物馆",
        "interests": ["museum", "art"], "hours_verified": False, "coords_verified": False,
    },
]


def main() -> None:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    have = {p["id"] for p in data["places"]}
    added = []
    for p in NEW_PLACES:
        if p["id"] in have:
            continue
        data["places"].append(p)
        added.append(p["id"])
    if added:
        dump(PATH, data)
    print("新增：", ", ".join(added) or "（没有，已经都在了）")


if __name__ == "__main__":
    main()
