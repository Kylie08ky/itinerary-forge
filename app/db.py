"""SQLite 访问层（Python 自带 sqlite3，不引 ORM）。

- 每次操作单独开一个连接：请求线程和后台生成线程可以同时读写，不用共享连接。
- WAL 模式：读不挡写；写事务用 BEGIN IMMEDIATE，保证「取最大版本号 +1」这类操作不会撞号。
- 表结构用 PRAGMA user_version 记版本；以后要改表就加一个迁移函数，不要手改线上库。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 13

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    request_json TEXT NOT NULL,
    archived_at  TEXT,
    copied_from  TEXT
);
CREATE TABLE IF NOT EXISTS versions (
    plan_id        TEXT NOT NULL REFERENCES plans(id),
    version        INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    trigger        TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    change_summary TEXT NOT NULL DEFAULT '',
    errors_before  INTEGER NOT NULL DEFAULT 0,
    errors_after   INTEGER NOT NULL DEFAULT 0,
    itinerary_json TEXT NOT NULL,
    request_json   TEXT,
    PRIMARY KEY (plan_id, version)
);
CREATE TABLE IF NOT EXISTS shares (
    token      TEXT PRIMARY KEY,
    plan_id    TEXT NOT NULL REFERENCES plans(id),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    share_packing INTEGER NOT NULL DEFAULT 0,   -- 分享时一起分享什么（默认都不分享）
    share_todos   INTEGER NOT NULL DEFAULT 0,
    share_notes   INTEGER NOT NULL DEFAULT 0
);
-- 删除过的行程号：旧版文件夹不删（不动用户的文件），靠这张表防止它们重启后又被导入回来
CREATE TABLE IF NOT EXISTS deleted_plans (
    id         TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);
-- 预订记录（规格 M06 S4）：用户自己的状态，不进行程版本
CREATE TABLE IF NOT EXISTS reservations (
    plan_id           TEXT NOT NULL REFERENCES plans(id),
    place_id          TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'todo',
    confirmation_code TEXT,
    slot              TEXT,
    note              TEXT,
    visit_date        TEXT,
    updated_at        TEXT NOT NULL,
    entry_from        TEXT,
    entry_to          TEXT,
    locked            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plan_id, place_id)
);
-- 预算设置（规格 M09 S2a）：每份行程一行，用户自己的状态，不进行程版本
CREATE TABLE IF NOT EXISTS budget_settings (
    plan_id          TEXT PRIMARY KEY REFERENCES plans(id),
    party_json       TEXT,
    currency         TEXT,
    cap_amount       REAL,
    cap_currency     TEXT,
    meal_eur_per_day REAL,
    updated_at       TEXT NOT NULL
);
-- 汇率：只存最后一次成功拿到的值，接口失败时拿它顶上并标日期（规格 M09 S1e）
CREATE TABLE IF NOT EXISTS fx_rates (
    base       TEXT NOT NULL,
    quote      TEXT NOT NULL,
    rate       REAL NOT NULL,
    rate_date  TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (base, quote)
);
-- 随行清单（规格 M10 S2–S4）：用户自己的状态，不进行程版本
CREATE TABLE IF NOT EXISTS packing_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id   TEXT NOT NULL REFERENCES plans(id),
    key       TEXT,                           -- 推荐项的编号（自己加的为空）；用来去重
    category  TEXT NOT NULL,
    text      TEXT NOT NULL,
    packed    INTEGER NOT NULL DEFAULT 0,
    position  INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0      -- 删掉的推荐项只是藏起来，重新生成时不会再冒出来
);
CREATE INDEX IF NOT EXISTS idx_packing_plan ON packing_items(plan_id);
CREATE TABLE IF NOT EXISTS todos (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    key     TEXT,
    text    TEXT NOT NULL,
    due     TEXT,
    done    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_todos_plan ON todos(plan_id);
-- 行程备注：ref = 地点库的 place_id / 「日期|名称」/ 「day|日期」（以前的整天笔记）
CREATE TABLE IF NOT EXISTS day_notes (
    plan_id    TEXT NOT NULL REFERENCES plans(id),
    ref        TEXT NOT NULL,
    text       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, ref)
);
-- 文件附件（规格 M10 S5）：记录在这里，文件在 <数据目录>/attachments/<行程id>/<id>.<扩展名>；不进行程版本
CREATE TABLE IF NOT EXISTS attachments (
    id          TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL REFERENCES plans(id),
    name        TEXT NOT NULL,                 -- 显示用的文件名（清洗过）
    kind        TEXT NOT NULL,                 -- pdf | png | jpg（按文件内容判断）
    size        INTEGER NOT NULL,
    target_kind TEXT NOT NULL DEFAULT 'plan',  -- plan | place | day | reservation
    target_ref  TEXT,
    uploaded_at TEXT NOT NULL,
    deleted_at  TEXT                           -- 进回收站的时间；30 天后永久删除
);
CREATE INDEX IF NOT EXISTS idx_attachments_plan ON attachments(plan_id);
CREATE TABLE IF NOT EXISTS llm_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    at               TEXT NOT NULL,
    task_id          TEXT,
    purpose          TEXT NOT NULL,
    backend          TEXT NOT NULL,
    model            TEXT NOT NULL,
    ok               INTEGER NOT NULL,
    error            TEXT,
    latency_ms       INTEGER NOT NULL,
    prompt_chars     INTEGER NOT NULL,
    response_chars   INTEGER NOT NULL,
    prompt_tokens    INTEGER,
    completion_tokens INTEGER,
    cost_usd         REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_task ON llm_calls(task_id);
CREATE TABLE IF NOT EXISTS map_usage (
    month   TEXT NOT NULL,                       -- 自然月 YYYY-MM
    service TEXT NOT NULL,                       -- lbs | search | map_init（规格 M05 S7c）
    n       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, service)
);
CREATE TABLE IF NOT EXISTS tasks (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL,
    theme_id     TEXT NOT NULL,
    request_json TEXT NOT NULL,
    steps_json   TEXT NOT NULL,
    plan_id      TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:                 # executescript 自己管事务，所以这里不套 BEGIN
            con.executescript(_SCHEMA)
            self._migrate(con)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        """旧库升级：只做「加列」这类向前兼容的改动，已有数据原样保留。"""
        cols = {r["name"] for r in con.execute("PRAGMA table_info(versions)")}
        if "name" not in cols:                       # v1 -> v2：版本可以有名字（「朋友确认版」）
            con.execute("ALTER TABLE versions ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        if "request_json" not in cols:               # v4 -> v5：每个版本带自己的需求；NULL = 沿用创建时的
            con.execute("ALTER TABLE versions ADD COLUMN request_json TEXT")
        res_cols = {r["name"] for r in con.execute("PRAGMA table_info(reservations)")}
        if res_cols and "entry_from" not in res_cols:   # v7 -> v8：入馆时间 / 时段结束 / 是否由预订加的锁
            con.execute("ALTER TABLE reservations ADD COLUMN entry_from TEXT")
            con.execute("ALTER TABLE reservations ADD COLUMN entry_to TEXT")
            con.execute("ALTER TABLE reservations ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        bs = {r["name"] for r in con.execute("PRAGMA table_info(budget_settings)")}
        if bs and "party_json" not in bs:            # v9（第一版预算）-> 出行人 / 结算货币 / 上限货币
            for col, typ in (("party_json", "TEXT"), ("currency", "TEXT"), ("cap_amount", "REAL"),
                             ("cap_currency", "TEXT")):
                con.execute(f"ALTER TABLE budget_settings ADD COLUMN {col} {typ}")
            for row in con.execute("SELECT plan_id, cap_cny, travelers FROM budget_settings").fetchall():
                adult = {"age": 30, "residence": "none", "student": False}
                party = json.dumps([adult] * max(int(row["travelers"] or 1), 1))
                con.execute("UPDATE budget_settings SET party_json = ?, currency = 'CNY', cap_amount = ?, "
                            "cap_currency = 'CNY' WHERE plan_id = ?", (party, row["cap_cny"], row["plan_id"]))
        note_cols = {r["name"] for r in con.execute("PRAGMA table_info(day_notes)")}
        if "date" in note_cols:                      # v10 -> v11：笔记改成每条安排一条；旧的整天笔记保留
            con.execute("ALTER TABLE day_notes RENAME COLUMN date TO ref")
            con.execute("UPDATE day_notes SET ref = 'day|' || ref")
        share_cols = {r["name"] for r in con.execute("PRAGMA table_info(shares)")}
        for col in ("share_packing", "share_todos", "share_notes"):
            if share_cols and col not in share_cols:  # v10 -> v11：分享时可选一起分享清单 / 待办 / 备注
                con.execute(f"ALTER TABLE shares ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        con.execute("DROP TABLE IF EXISTS judge_results")   # 做过又撤掉的功能（评估结果不再进产品界面）
        plan_cols = {r["name"] for r in con.execute("PRAGMA table_info(plans)")}
        if "archived_at" not in plan_cols:           # v4 -> v5：归档、复制
            con.execute("ALTER TABLE plans ADD COLUMN archived_at TEXT")
        if "copied_from" not in plan_cols:
            con.execute("ALTER TABLE plans ADD COLUMN copied_from TEXT")

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            if write:
                con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                if write:
                    con.execute("ROLLBACK")
                raise
            else:
                if write:
                    con.execute("COMMIT")
        finally:
            con.close()
