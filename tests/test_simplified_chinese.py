"""一日游目的地的中文一律是简体（规格 08 3.7，你的要求：全部显示简体）。

转换在开发时用中文维基百科的繁简转换做（scripts/simplify_chinese.py）；这里用一份常见的「繁体独有字」当哨兵，
数据里出现任何一个就失败。台湾译名（史特拉斯堡 / 馬斯垂克 / 烏姆）也一并拦住。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.cities import list_cities

DATA = Path(__file__).resolve().parents[1] / "data"
# 常见繁体独有字（地名、建筑、博物馆、教堂简介里最容易出现的）
TRADITIONAL = set(
    "館於為區邊爾歐聖靈烏術藝廣學鐘紀劇園議會華書樓蘭羅賓農莊橋閣島灣體戰禮傑庫鎮廳條創遺跡廢築與從這個們來時說開關"
    "東車見話過還對產務動點線經義習網電總種設計認識視臺實現間頭親靜歲題辦優寫請賽圖濟錄複雜齊僅幣價儀億兒養鄰預選釋療獲環"
    "樂雲遊數據啟啓舊斷標錢願錶鏡後創辦雖飾饒馬魯維爲獨輝適團團圍態畫壁壇團噴鑰鑑隊際陽陰階陸險須顯顧領頓類風飛餐驛"
)
BAD_TERMS = ("史特拉斯堡", "馬斯垂克", "马斯垂克", "烏姆", "乌姆市")
CJK = re.compile(r"[\u4e00-\u9fff]")


def light_ids() -> list[str]:
    return [c.id for c in list_cities() if c.tier == "light"]


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for x in node:
            yield from strings(x)
    elif isinstance(node, dict):
        for v in node.values():
            yield from strings(v)


@pytest.mark.parametrize("kind", ["cities", "places", "themes"])
def test_the_day_trip_data_has_no_traditional_characters_and_no_taiwanese_names(kind):
    ids = light_ids()
    assert len(ids) >= 14
    bad = []
    for cid in ids:
        doc = json.loads((DATA / kind / f"{cid}.json").read_text(encoding="utf-8"))
        for s in strings(doc):
            if not CJK.search(s):
                continue
            hit = sorted(set(s) & TRADITIONAL) or [t for t in BAD_TERMS if t in s]
            if hit:
                bad.append((cid, s[:30], hit[:4]))
    assert not bad, bad[:10]
