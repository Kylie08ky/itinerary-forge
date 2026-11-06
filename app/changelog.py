"""版本足迹（规格 06-ui-design）：产品每次更新了什么，写给用户看的白话记录。

数据在 `data/changelog.json`，每轮改动由人（或 Claude）追加一条；这里只负责读和校验，不联网、不调模型。
最新的在最前面；第一条就是「当前版本」。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

_PATH = Path(__file__).resolve().parents[1] / "data" / "changelog.json"


class ChangelogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    title: str = Field(min_length=2, max_length=40)
    points: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def _points_are_text(self) -> ChangelogEntry:
        if any(not p.strip() for p in self.points):
            raise ValueError("更新要点不能是空的")
        return self


class Changelog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[ChangelogEntry]

    @model_validator(mode="after")
    def _newest_first(self) -> Changelog:
        dates = [e.date for e in self.entries]
        if dates != sorted(dates, reverse=True):
            raise ValueError("更新记录要按日期从新到旧排（同一天的按先后：新的在前）")
        return self


def load_changelog(path: Path = _PATH) -> list[dict]:
    """全部更新记录（新→旧）；第一条带 current = True。文件不存在 = 空，不是错误。"""
    if not path.exists():
        return []
    log = Changelog.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return [{"date": e.date.isoformat(), "title": e.title, "points": e.points, "current": i == 0}
            for i, e in enumerate(log.entries)]
