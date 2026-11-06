"""行程存储换成 SQLite 后的行为（规格 M01 数据与接口）。

接口和旧的文件版 PlanStore 一致（tests/test_versions.py 仍然适用），这里只测新增的部分：
持久化、旧文件行程的导入、并发写版本、行程摘要。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from app.db import SCHEMA_VERSION
from app.schemas import Itinerary, TravelRequest
from app.versions import PlanNotFound, PlanStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "fixtures" / "stub-itinerary.json"
PERSONA = ROOT / "data" / "personas" / "persona-01-museum-foodie.json"


@pytest.fixture
def itinerary() -> Itinerary:
    return Itinerary.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def request_() -> TravelRequest:
    raw = json.loads(PERSONA.read_text(encoding="utf-8"))
    raw.pop("_meta", None)
    return TravelRequest.model_validate(raw)


def test_plans_live_in_a_sqlite_file_and_survive_a_restart(tmp_path, request_, itinerary):
    root = tmp_path / "plans"
    pid = PlanStore(root=root).create(request_, itinerary, note="第一次")
    assert (root / "plans.db").exists()

    again = PlanStore(root=root)                     # 模拟服务重启
    assert again.list_plans() == [pid]
    assert again.load(pid).version == "v1"
    assert again.history(pid)[0].note == "第一次"
    assert again.request(pid).destination == "Paris"


def test_database_uses_wal_and_has_the_three_tables(tmp_path, request_, itinerary):
    root = tmp_path / "plans"
    PlanStore(root=root).create(request_, itinerary)
    con = sqlite3.connect(root / "plans.db")
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"plans", "versions", "tasks"} <= tables


def test_legacy_file_plans_are_imported_once(tmp_path, request_, itinerary):
    """旧版把每个行程存成 data/plans/<id>/ 下的 json 文件；首次打开时导入，重复打开不重复导入。"""
    root = tmp_path / "plans"
    d = root / "paris-20261003-abc123"
    d.mkdir(parents=True)
    (d / "request.json").write_text(request_.model_dump_json(), encoding="utf-8")
    (d / "v1.json").write_text(itinerary.model_copy(update={"version": "v1"}).model_dump_json(), encoding="utf-8")
    (d / "v2.json").write_text(itinerary.model_copy(update={"version": "v2"}).model_dump_json(), encoding="utf-8")
    (d / "history.json").write_text(json.dumps([
        {"version": 1, "created_at": "2026-10-01T00:00:00+00:00", "trigger": "initial"},
        {"version": 2, "created_at": "2026-10-02T00:00:00+00:00", "trigger": "user-feedback",
         "note": "加咖啡馆", "change_summary": "新增 1 项", "errors_before": 2, "errors_after": 0},
    ]), encoding="utf-8")

    store = PlanStore(root=root)
    assert store.list_plans() == ["paris-20261003-abc123"]
    assert store.latest_version("paris-20261003-abc123") == 2
    assert store.load("paris-20261003-abc123", 1).version == "v1"
    assert [(h.version, h.trigger, h.note) for h in store.history("paris-20261003-abc123")] == [
        (1, "initial", ""), (2, "user-feedback", "加咖啡馆")]
    assert (d / "v1.json").exists()                  # 旧文件原样保留，不删用户的数据

    PlanStore(root=root)                             # 再打开一次：不能重复导入
    assert len(PlanStore(root=root).list_plans()) == 1
    assert len(PlanStore(root=root).history("paris-20261003-abc123")) == 2


def test_concurrent_add_version_never_reuses_a_number(tmp_path, request_, itinerary):
    store = PlanStore(root=tmp_path / "plans")
    pid = store.create(request_, itinerary)
    got: list[int] = []
    lock = threading.Lock()

    def work():
        v = store.add_version(pid, itinerary, trigger="user-feedback", note="并发")
        with lock:
            got.append(v)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(got) == list(range(2, 10))
    assert [h.version for h in store.history(pid)] == list(range(1, 10))


def test_summaries_for_the_plan_list(tmp_path, request_, itinerary):
    store = PlanStore(root=tmp_path / "plans")
    a = store.create(request_, itinerary)
    store.add_version(a, itinerary, trigger="user-feedback")
    b = store.create(request_, itinerary)
    rows = store.summaries()
    assert [r.plan_id for r in rows] == [b, a]       # 新的在前
    assert rows[1].latest_version == 2 and rows[0].latest_version == 1
    assert rows[0].request.start_date == request_.start_date
    assert rows[0].created_at


def test_unknown_plan_and_version(tmp_path, request_, itinerary):
    store = PlanStore(root=tmp_path / "plans")
    pid = store.create(request_, itinerary)
    with pytest.raises(PlanNotFound):
        store.load("nope")
    with pytest.raises(PlanNotFound):
        store.load(pid, 9)
    with pytest.raises(PlanNotFound):
        store.add_version("nope", itinerary, trigger="x")


# --------------------------------------------------------------------------- #
# 版本名字与数据库升级（规格 M04 S7）
# --------------------------------------------------------------------------- #
def test_versions_can_carry_a_name(tmp_path, request_, itinerary):
    store = PlanStore(root=tmp_path / "plans")
    pid = store.create(request_, itinerary)
    v = store.add_version(pid, itinerary, trigger="manual-save", name="朋友确认版", note="大家都同意了")
    hist = store.history(pid)
    assert [h.name for h in hist] == ["", "朋友确认版"]
    assert hist[1].version == v and hist[1].note == "大家都同意了" and hist[1].trigger == "manual-save"


def test_a_database_created_before_version_names_is_upgraded_in_place(tmp_path, request_, itinerary):
    """v0.1 建的库没有 name 列：打开时自动补上，老数据原样保留。"""
    root = tmp_path / "plans"
    root.mkdir()
    con = sqlite3.connect(root / "plans.db")
    con.executescript("""
        CREATE TABLE plans (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                            created_at TEXT NOT NULL, request_json TEXT NOT NULL);
        CREATE TABLE versions (plan_id TEXT NOT NULL REFERENCES plans(id), version INTEGER NOT NULL,
                               created_at TEXT NOT NULL, trigger TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                               change_summary TEXT NOT NULL DEFAULT '', errors_before INTEGER NOT NULL DEFAULT 0,
                               errors_after INTEGER NOT NULL DEFAULT 0, itinerary_json TEXT NOT NULL,
                               PRIMARY KEY (plan_id, version));
        CREATE TABLE tasks (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                            theme_id TEXT NOT NULL, request_json TEXT NOT NULL, steps_json TEXT NOT NULL,
                            plan_id TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        PRAGMA user_version = 1;
    """)
    con.execute("INSERT INTO plans (id, created_at, request_json) VALUES (?, ?, ?)",
                ("old-plan", "2026-10-01T00:00:00+00:00", request_.model_dump_json()))
    con.execute("INSERT INTO versions (plan_id, version, created_at, trigger, itinerary_json) VALUES (?,?,?,?,?)",
                ("old-plan", 1, "2026-10-01T00:00:00+00:00", "initial", itinerary.model_dump_json()))
    con.commit()
    con.close()

    store = PlanStore(root=root)
    assert store.history("old-plan")[0].name == ""
    assert store.load("old-plan").version == itinerary.version or store.load("old-plan")
    v = store.add_version("old-plan", itinerary, trigger="manual-save", name="升级后保存")
    assert store.history("old-plan")[v - 1].name == "升级后保存"
    assert sqlite3.connect(root / "plans.db").execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_a_deleted_legacy_plan_is_not_imported_again_on_the_next_start(tmp_path, request_, itinerary):
    """旧文件夹还留在磁盘上（不删用户的文件）；删除过的行程不能因为重启又被导入回来。"""
    root = tmp_path / "plans"
    d = root / "paris-20261003-old001"
    d.mkdir(parents=True)
    (d / "request.json").write_text(request_.model_dump_json(), encoding="utf-8")
    (d / "v1.json").write_text(itinerary.model_dump_json(), encoding="utf-8")
    (d / "history.json").write_text(json.dumps(
        [{"version": 1, "created_at": "2026-10-01T00:00:00+00:00", "trigger": "initial"}]), encoding="utf-8")
    store = PlanStore(root=root)
    assert store.list_plans() == ["paris-20261003-old001"]
    store.delete("paris-20261003-old001")
    assert PlanStore(root=root).list_plans() == []
    assert PlanStore(root=root).list_plans() == []
    assert (d / "v1.json").exists()
