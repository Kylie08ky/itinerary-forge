"""校验器的热路径缓存（规格 M01「数据与接口」）：认城市、读别名表不随调用次数重复做。

背景：/plan-cards 对每份行程都校验一次，一次校验里会认几十次城市、读几次别名文件；
行程多了，这些重复的扫描和读盘就是剩下的主要耗时。
"""

from __future__ import annotations

import json
from pathlib import Path

from app import validator
from app.cities import City, city_of, list_cities, reload_cities
from app.validator import PlaceIndex

NAMES = ["Paris", "paris", "巴黎", " 巴黎 ", "Stuttgart", "斯图加特", "Nowhere-land", ""]


def test_city_of_does_not_rescan_every_city_on_each_call(monkeypatch):
    city_of("Paris")                                       # 预热
    scans: list[int] = []
    original = City.matches

    def counting(self, text):
        scans.append(1)
        return original(self, text)

    monkeypatch.setattr(City, "matches", counting)
    for _ in range(50):
        for name in NAMES:
            city_of(name)
    assert scans == [], f"city_of 重复扫描了 {len(scans)} 次城市"


def test_city_of_keeps_its_answers_and_follows_a_reload(monkeypatch):
    slow = {n: next((c for c in list_cities() if c.supported and c.matches(n)), None) for n in NAMES}
    assert {n: city_of(n) for n in NAMES} == slow           # 和逐个城市扫描的旧答案一致
    assert city_of("巴黎").id == "paris" and city_of("Nowhere-land") is None
    reload_cities()
    assert {n: city_of(n) for n in NAMES} == slow           # 重新加载之后仍然一致


def test_place_index_reads_the_alias_file_once(monkeypatch):
    PlaceIndex.load()                                      # 预热
    reads: list[Path] = []
    original = Path.read_text

    def counting(self, *args, **kwargs):
        reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    for _ in range(20):
        PlaceIndex.load()
    assert reads == [], f"别名表被重复读了 {len(reads)} 次"


def test_place_index_follows_edits_to_the_alias_file(tmp_path, monkeypatch):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"groups": [["a", "b"]], "closed_weekday": {}}), encoding="utf-8")
    monkeypatch.setattr(validator, "_ALIASES_PATH", path)
    assert PlaceIndex.load().groups == [["a", "b"]]
    path.write_text(json.dumps({"groups": [["a", "b", "c"]], "closed_weekday": {}}), encoding="utf-8")
    assert PlaceIndex.load().groups == [["a", "b", "c"]]    # 文件改了就读新的
