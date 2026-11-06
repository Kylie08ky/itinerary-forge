"""规格 06-ui-design 版本足迹（场景 S21）：更新记录的校验和接口。"""

# ruff: noqa: E501
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import create_app
from app.changelog import Changelog, load_changelog
from app.versions import PlanStore


def test_the_real_changelog_is_valid_and_the_first_entry_is_current():
    rows = load_changelog()
    assert len(rows) >= 10
    assert [r["current"] for r in rows] == [True] + [False] * (len(rows) - 1)
    assert all(1 <= len(r["points"]) <= 5 for r in rows)


def test_entries_must_be_newest_first():
    entry = {"title": "某次更新", "points": ["做了一件事"]}
    with pytest.raises(ValidationError):
        Changelog.model_validate({"entries": [{"date": "2026-10-01", **entry}, {"date": "2026-10-02", **entry}]})
    Changelog.model_validate({"entries": [{"date": "2026-10-02", **entry}, {"date": "2026-10-02", **entry}]})   # 同一天可以


def test_blank_points_and_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        Changelog.model_validate({"entries": [{"date": "2026-10-02", "title": "某次更新", "points": [" "]}]})
    with pytest.raises(ValidationError):
        Changelog.model_validate({"entries": [{"date": "2026-10-02", "title": "某次更新", "points": ["x"], "extra": 1}]})


def test_a_missing_file_is_an_empty_list(tmp_path):
    assert load_changelog(tmp_path / "nope.json") == []


def test_the_api_serves_the_changelog_newest_first(tmp_path):
    client = TestClient(create_app(store=PlanStore(tmp_path)))
    rows = client.get("/changelog").json()
    assert rows and rows[0]["current"] is True
    assert [r["date"] for r in rows] == sorted((r["date"] for r in rows), reverse=True)
    assert json.dumps(rows, ensure_ascii=False)           # 全是可序列化的白话
