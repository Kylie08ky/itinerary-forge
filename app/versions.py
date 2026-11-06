"""M5：行程的版本存储（SQLite）。

plans 一行一个行程（需求快照），versions 一行一个版本（完整行程 JSON + 版本记录）。
接口沿用最初的文件版 PlanStore，调用方不用改；数据库文件放在 root/plans.db。

旧版把每个行程存成 root/<plan_id>/ 下的 request.json、vN.json、history.json。
打开存储时会把这些旧行程导入数据库（只导入数据库里还没有的，旧文件原样保留，不删）。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from app.db import Database
from app.schemas import Itinerary, TravelRequest

# ATP_DATA_DIR：行程库放哪（浏览器验收用它指到临时目录，不碰你平时用的库）；不设就是 data/plans
_DEFAULT_ROOT = Path(os.getenv("ATP_DATA_DIR") or Path(__file__).resolve().parents[1] / "data" / "plans")


class VersionRecord(BaseModel):
    version: int
    created_at: str
    trigger: str            # "initial" | "auto-repair" | "user-feedback" | "manual-save" | "rollback"
    name: str = ""          # 用户给这个版本起的名字，比如「朋友确认版」
    note: str = ""          # 反馈原文 / 修复说明
    change_summary: str = ""
    errors_before: int = 0
    errors_after: int = 0


class PlanSummary(BaseModel):
    """「我的行程」列表用：不含完整行程内容。request 是最新版本的需求（平移日期后日期会变）。"""

    plan_id: str
    created_at: str
    latest_version: int
    request: TravelRequest
    archived: bool = False
    copied_from: str | None = None


class PlanNotFound(RuntimeError):
    pass


@dataclass
class PlanStore:
    root: Path = _DEFAULT_ROOT
    database: Database = field(init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.database = Database(self.root / "plans.db")
        self._import_legacy_files()

    # -- 写 -------------------------------------------------------------- #
    def create(self, request: TravelRequest, itinerary: Itinerary, *, note: str = "",
               copied_from: str | None = None) -> str:
        plan_id = f"{request.destination.lower()}-{request.start_date:%Y%m%d}-{uuid4().hex[:6]}"
        now = _now()
        v1 = itinerary.model_copy(update={"version": "v1"})
        with self.database.connect(write=True) as con:
            con.execute("INSERT INTO plans (id, created_at, request_json, copied_from) VALUES (?, ?, ?, ?)",
                        (plan_id, now, request.model_dump_json(), copied_from))
            con.execute(
                "INSERT INTO versions (plan_id, version, created_at, trigger, note, itinerary_json) "
                "VALUES (?, 1, ?, 'initial', ?, ?)", (plan_id, now, note, v1.model_dump_json()))
        return plan_id

    def add_version(
        self,
        plan_id: str,
        itinerary: Itinerary,
        *,
        trigger: str,
        note: str = "",
        change_summary: str = "",
        errors_before: int = 0,
        errors_after: int = 0,
        name: str = "",
        request: TravelRequest | None = None,
    ) -> int:
        """新增一个版本。request 不传 = 沿用最新版本的需求；平移日期、回滚到旧版本时要显式传（日期会变）。"""
        with self.database.connect(write=True) as con:
            row = con.execute("SELECT MAX(version) AS v FROM versions WHERE plan_id = ?", (plan_id,)).fetchone()
            if row["v"] is None:
                raise PlanNotFound(f"找不到 plan：{plan_id}")
            version = row["v"] + 1
            req_json = (request or self._effective_request(con, plan_id, row["v"])).model_dump_json()
            tagged = itinerary.model_copy(update={"version": f"v{version}"})
            con.execute(
                "INSERT INTO versions (plan_id, version, created_at, trigger, name, note, change_summary, "
                "errors_before, errors_after, itinerary_json, request_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, version, _now(), trigger, name, note, change_summary, errors_before, errors_after,
                 tagged.model_dump_json(), req_json))
        return version

    # -- 读 -------------------------------------------------------------- #
    @staticmethod
    def _effective_request(con, plan_id: str, version: int) -> TravelRequest:
        row = con.execute(
            "SELECT v.request_json AS vr, p.request_json AS pr FROM plans p LEFT JOIN versions v "
            "ON v.plan_id = p.id AND v.version = ? WHERE p.id = ?", (version, plan_id)).fetchone()
        if row is None:
            raise PlanNotFound(f"找不到 plan：{plan_id}")
        return TravelRequest.model_validate_json(row["vr"] or row["pr"])

    def request(self, plan_id: str, version: int | None = None) -> TravelRequest:
        """某个版本对应的需求（不指定 = 最新版本）。版本没单独存需求时，用行程创建时的那份。"""
        with self.database.connect() as con:
            if version is None:
                row = con.execute("SELECT MAX(version) AS v FROM versions WHERE plan_id = ?", (plan_id,)).fetchone()
                if row["v"] is None:
                    raise PlanNotFound(f"找不到 plan：{plan_id}")
                version = row["v"]
            return self._effective_request(con, plan_id, version)

    def latest_version(self, plan_id: str) -> int:
        with self.database.connect() as con:
            row = con.execute("SELECT MAX(version) AS v FROM versions WHERE plan_id = ?", (plan_id,)).fetchone()
        if row["v"] is None:
            raise PlanNotFound(f"找不到 plan：{plan_id}")
        return row["v"]

    def load(self, plan_id: str, version: int | None = None) -> Itinerary:
        version = version or self.latest_version(plan_id)
        with self.database.connect() as con:
            row = con.execute("SELECT itinerary_json FROM versions WHERE plan_id = ? AND version = ?",
                              (plan_id, version)).fetchone()
        if row is None:
            raise PlanNotFound(f"{plan_id} 没有 v{version}")
        return Itinerary.model_validate_json(row["itinerary_json"])

    def history(self, plan_id: str) -> list[VersionRecord]:
        with self.database.connect() as con:
            rows = con.execute(
                "SELECT version, created_at, trigger, name, note, change_summary, errors_before, errors_after "
                "FROM versions WHERE plan_id = ? ORDER BY version", (plan_id,)).fetchall()
        if not rows:
            raise PlanNotFound(f"找不到 plan：{plan_id}")
        return [VersionRecord(**dict(r)) for r in rows]

    def list_plans(self) -> list[str]:
        with self.database.connect() as con:
            return [r["id"] for r in con.execute("SELECT id FROM plans ORDER BY id")]

    def summaries(self, *, archived: bool = False) -> list[PlanSummary]:
        """行程摘要，新建的在前。默认只给没归档的；archived=True 只给已归档的。"""
        with self.database.connect() as con:
            rows = con.execute(
                "SELECT p.id, p.created_at, p.archived_at, p.copied_from, MAX(v.version) AS latest "
                "FROM plans p JOIN versions v ON v.plan_id = p.id "
                f"WHERE p.archived_at IS {'NOT ' if archived else ''}NULL GROUP BY p.id ORDER BY p.seq DESC").fetchall()
            return [PlanSummary(plan_id=r["id"], created_at=r["created_at"], latest_version=r["latest"],
                                request=self._effective_request(con, r["id"], r["latest"]),
                                archived=r["archived_at"] is not None, copied_from=r["copied_from"]) for r in rows]

    def summaries_with_itineraries(self, *, archived: bool = False) -> list[tuple[PlanSummary, Itinerary]]:
        """和 `summaries` 一样，每份再带上最新版本的内容；只开一次数据库连接（卡片接口用，行程多了也不变慢）。"""
        with self.database.connect() as con:
            rows = con.execute(
                "SELECT p.id, p.created_at, p.archived_at, p.copied_from, v.version AS latest, "
                "v.request_json AS vr, p.request_json AS pr, v.itinerary_json AS it "
                "FROM plans p JOIN versions v ON v.plan_id = p.id "
                "AND v.version = (SELECT MAX(version) FROM versions WHERE plan_id = p.id) "
                f"WHERE p.archived_at IS {'NOT ' if archived else ''}NULL ORDER BY p.seq DESC").fetchall()
        return [(PlanSummary(plan_id=r["id"], created_at=r["created_at"], latest_version=r["latest"],
                             request=TravelRequest.model_validate_json(r["vr"] or r["pr"]),
                             archived=r["archived_at"] is not None, copied_from=r["copied_from"]),
                 Itinerary.model_validate_json(r["it"])) for r in rows]

    # -- 归档与复制 ------------------------------------------------------- #
    def set_archived(self, plan_id: str, archived: bool) -> None:
        with self.database.connect(write=True) as con:
            cur = con.execute("UPDATE plans SET archived_at = ? WHERE id = ?",
                              (_now() if archived else None, plan_id))
            if cur.rowcount == 0:
                raise PlanNotFound(f"找不到 plan：{plan_id}")

    def is_archived(self, plan_id: str) -> bool:
        with self.database.connect() as con:
            row = con.execute("SELECT archived_at FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise PlanNotFound(f"找不到 plan：{plan_id}")
        return row["archived_at"] is not None

    def copied_from(self, plan_id: str) -> str | None:
        with self.database.connect() as con:
            row = con.execute("SELECT copied_from FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise PlanNotFound(f"找不到 plan：{plan_id}")
        return row["copied_from"]

    def delete(self, plan_id: str) -> None:
        """永久删除：行程、所有版本、分享链接、生成它的任务记录。副本不受影响（只是不再标「复制自」）。

        模型调用的统计（llm_calls）不删：那是用量记录，不属于这份行程。
        """
        with self.database.connect(write=True) as con:
            if con.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
                raise PlanNotFound(f"找不到 plan：{plan_id}")
            con.execute("DELETE FROM shares WHERE plan_id = ?", (plan_id,))
            con.execute("DELETE FROM reservations WHERE plan_id = ?", (plan_id,))
            con.execute("DELETE FROM budget_settings WHERE plan_id = ?", (plan_id,))
            for table in ("packing_items", "todos", "day_notes", "attachments"):
                con.execute(f"DELETE FROM {table} WHERE plan_id = ?", (plan_id,))
            con.execute("DELETE FROM tasks WHERE plan_id = ?", (plan_id,))
            con.execute("DELETE FROM versions WHERE plan_id = ?", (plan_id,))
            con.execute("UPDATE plans SET copied_from = NULL WHERE copied_from = ?", (plan_id,))
            con.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
            con.execute("INSERT OR IGNORE INTO deleted_plans (id, deleted_at) VALUES (?, ?)", (plan_id, _now()))
        shutil.rmtree(self.root / "attachments" / plan_id, ignore_errors=True)        # 附件文件一并删除（M10 S5e）

    def duplicate(self, plan_id: str) -> str:
        """复制：新行程的 v1 = 原行程最新版本的内容和需求；之后两边各改各的。"""
        new_id = self.create(self.request(plan_id), self.load(plan_id), note=f"复制自 {plan_id}",
                             copied_from=plan_id)
        with self.database.connect(write=True) as con:           # 预订记录一起复制（副本是同一趟旅行的另一种改法）
            con.execute(
                "INSERT INTO reservations (plan_id, place_id, status, confirmation_code, slot, note, visit_date, "
                "updated_at, entry_from, entry_to, locked) SELECT ?, place_id, status, confirmation_code, slot, "
                "note, visit_date, updated_at, entry_from, entry_to, locked FROM reservations WHERE plan_id = ?",
                (new_id, plan_id))
            con.execute(                                          # 预算设置也带上
                "INSERT INTO budget_settings (plan_id, party_json, currency, cap_amount, cap_currency, "
                "meal_eur_per_day, updated_at) SELECT ?, party_json, currency, cap_amount, cap_currency, "
                "meal_eur_per_day, updated_at FROM budget_settings WHERE plan_id = ?",
                (new_id, plan_id))
            con.execute(                                          # 随行清单 / 待办 / 笔记也一起复制
                "INSERT INTO packing_items (plan_id, key, category, text, packed, position, dismissed) "
                "SELECT ?, key, category, text, packed, position, dismissed FROM packing_items WHERE plan_id = ?",
                (new_id, plan_id))
            con.execute("INSERT INTO todos (plan_id, key, text, due, done) SELECT ?, key, text, due, done "
                        "FROM todos WHERE plan_id = ?", (new_id, plan_id))
            con.execute("INSERT INTO day_notes (plan_id, ref, text, updated_at) SELECT ?, ref, text, updated_at "
                        "FROM day_notes WHERE plan_id = ?", (new_id, plan_id))
        return new_id

    # -- 预订记录（规格 M06 S4）------------------------------------------ #
    def get_reservations(self, plan_id: str) -> dict[str, dict]:
        with self.database.connect() as con:
            rows = con.execute("SELECT * FROM reservations WHERE plan_id = ?", (plan_id,)).fetchall()
        return {r["place_id"]: dict(r) for r in rows}

    def save_reservation(self, plan_id: str, place_id: str, *, status: str, confirmation_code: str | None,
                         slot: str | None, note: str | None, visit_date: str | None,
                         entry_from: str | None = None, entry_to: str | None = None) -> None:
        """合并保存：传 None 的字段保持原值，传空字符串表示清掉。订成时记下当时的参观日（之后平移日期能提示）。"""
        with self.database.connect(write=True) as con:
            if con.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
                raise PlanNotFound(f"找不到 plan：{plan_id}")
            old = con.execute("SELECT * FROM reservations WHERE plan_id = ? AND place_id = ?",
                              (plan_id, place_id)).fetchone()

            def pick(new: str | None, key: str) -> str | None:
                if new is None:
                    return old[key] if old else None
                return new.strip() or None

            keep_date = old["visit_date"] if old else None
            con.execute(
                "INSERT INTO reservations (plan_id, place_id, status, confirmation_code, slot, note, visit_date, "
                "updated_at, entry_from, entry_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(plan_id, place_id) DO UPDATE SET "
                "status = excluded.status, confirmation_code = excluded.confirmation_code, slot = excluded.slot, "
                "note = excluded.note, visit_date = excluded.visit_date, updated_at = excluded.updated_at, "
                "entry_from = excluded.entry_from, entry_to = excluded.entry_to",
                (plan_id, place_id, status, pick(confirmation_code, "confirmation_code"), pick(slot, "slot"),
                 pick(note, "note"), visit_date if status == "booked" else keep_date, _now(),
                 pick(entry_from, "entry_from"), pick(entry_to, "entry_to")))

    def set_reservation_locked(self, plan_id: str, place_id: str, locked: bool) -> None:
        """记下行程里的锁是不是由这笔预订加的（取消预订时只解自己加的锁）。"""
        with self.database.connect(write=True) as con:
            con.execute("UPDATE reservations SET locked = ? WHERE plan_id = ? AND place_id = ?",
                        (int(locked), plan_id, place_id))

    def delete_reservation(self, plan_id: str, place_id: str) -> None:
        with self.database.connect(write=True) as con:
            con.execute("DELETE FROM reservations WHERE plan_id = ? AND place_id = ?", (plan_id, place_id))

    # -- 预算设置（规格 M09 S1h / S2a）------------------------------------- #
    def get_budget_settings(self, plan_id: str) -> dict | None:
        """用户改过的预算设置；没改过返回 None（预算按行程需求里的初始值算）。"""
        with self.database.connect() as con:
            row = con.execute("SELECT party_json, currency, cap_amount, cap_currency, meal_eur_per_day "
                              "FROM budget_settings WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None or not row["party_json"]:
            return None
        return {"travelers": json.loads(row["party_json"]), "currency": row["currency"] or "CNY",
                "cap_amount": row["cap_amount"], "cap_currency": row["cap_currency"] or row["currency"] or "CNY",
                "meal_eur_per_day": row["meal_eur_per_day"]}

    def put_budget_settings(self, plan_id: str, *, travelers: list[dict], currency: str, cap_amount: float | None,
                            cap_currency: str, meal_eur_per_day: float | None) -> None:
        """整体保存（调用方先把改动合并到当前值上）。不产生行程版本。"""
        with self.database.connect(write=True) as con:
            if con.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
                raise PlanNotFound(f"找不到 plan：{plan_id}")
            con.execute(
                "INSERT INTO budget_settings (plan_id, party_json, currency, cap_amount, cap_currency, "
                "meal_eur_per_day, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(plan_id) DO UPDATE SET "
                "party_json = excluded.party_json, currency = excluded.currency, cap_amount = excluded.cap_amount, "
                "cap_currency = excluded.cap_currency, meal_eur_per_day = excluded.meal_eur_per_day, "
                "updated_at = excluded.updated_at",
                (plan_id, json.dumps(travelers, ensure_ascii=False), currency, cap_amount, cap_currency,
                 meal_eur_per_day, _now()))

    # -- 行李清单（规格 M10 S2）------------------------------------------- #
    def _need_plan(self, con, plan_id: str) -> None:
        if con.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
            raise PlanNotFound(f"找不到 plan：{plan_id}")

    def packing_list(self, plan_id: str) -> list[dict]:
        with self.database.connect() as con:
            rows = con.execute("SELECT id, key, category, text, packed, position FROM packing_items "
                               "WHERE plan_id = ? AND dismissed = 0 ORDER BY position, id", (plan_id,)).fetchall()
        return [{**dict(r), "packed": bool(r["packed"])} for r in rows]

    def packing_add(self, plan_id: str, category: str, text: str, key: str | None = None) -> int:
        with self.database.connect(write=True) as con:
            self._need_plan(con, plan_id)
            pos = con.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM packing_items WHERE plan_id = ?",
                              (plan_id,)).fetchone()[0]
            cur = con.execute(
                "INSERT INTO packing_items (plan_id, key, category, text, position) VALUES (?, ?, ?, ?, ?)",
                (plan_id, key, category, text, pos))
            return int(cur.lastrowid)

    def packing_update(self, plan_id: str, item_id: int, **fields) -> bool:
        allowed = {"packed": int, "text": str, "category": str}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"不能改的字段：{k}")
            sets.append(f"{k} = ?")
            vals.append(int(v) if k == "packed" else v)
        if not sets:
            return True
        with self.database.connect(write=True) as con:
            cur = con.execute(
                f"UPDATE packing_items SET {', '.join(sets)} WHERE plan_id = ? AND id = ? AND dismissed = 0",
                (*vals, plan_id, item_id))
            return cur.rowcount > 0

    def packing_delete(self, plan_id: str, item_id: int) -> bool:
        """推荐项只是藏起来（重新生成时不会再冒出来）；自己加的真删。"""
        with self.database.connect(write=True) as con:
            row = con.execute("SELECT key FROM packing_items WHERE plan_id = ? AND id = ? AND dismissed = 0",
                              (plan_id, item_id)).fetchone()
            if row is None:
                return False
            if row["key"]:
                con.execute("UPDATE packing_items SET dismissed = 1, packed = 0 WHERE plan_id = ? AND id = ?",
                            (plan_id, item_id))
            else:
                con.execute("DELETE FROM packing_items WHERE plan_id = ? AND id = ?", (plan_id, item_id))
            return True

    def packing_merge(self, plan_id: str, items: list[dict]) -> int:
        """补上还没有的推荐项（按 key 去重，包括被删掉藏起来的）；已有的不动。返回新增数。"""
        added = 0
        with self.database.connect(write=True) as con:
            self._need_plan(con, plan_id)
            have = {r["key"] for r in con.execute("SELECT key FROM packing_items WHERE plan_id = ? AND key IS NOT NULL",
                                                  (plan_id,))}
            pos = con.execute("SELECT COALESCE(MAX(position), 0) FROM packing_items WHERE plan_id = ?",
                              (plan_id,)).fetchone()[0]
            for it in items:
                if it["key"] in have:
                    continue
                pos += 1
                con.execute("INSERT INTO packing_items (plan_id, key, category, text, position) VALUES (?, ?, ?, ?, ?)",
                            (plan_id, it["key"], it["category"], it["text"], pos))
                added += 1
        return added

    def packing_restore(self, plan_id: str) -> int:
        with self.database.connect(write=True) as con:
            self._need_plan(con, plan_id)
            return con.execute("UPDATE packing_items SET dismissed = 0 WHERE plan_id = ? AND dismissed = 1",
                               (plan_id,)).rowcount

    def packing_hidden_count(self, plan_id: str) -> int:
        with self.database.connect() as con:
            return con.execute("SELECT COUNT(*) FROM packing_items WHERE plan_id = ? AND dismissed = 1",
                               (plan_id,)).fetchone()[0]

    def packing_has_generated(self, plan_id: str) -> bool:
        with self.database.connect() as con:
            return con.execute("SELECT 1 FROM packing_items WHERE plan_id = ? AND key IS NOT NULL LIMIT 1",
                               (plan_id,)).fetchone() is not None

    # -- 待办（规格 M10 S3）------------------------------------------------- #
    def todos_list(self, plan_id: str) -> list[dict]:
        with self.database.connect() as con:
            rows = con.execute("SELECT id, key, text, due, done FROM todos WHERE plan_id = ? ORDER BY done, "
                               "(due IS NULL), due, id", (plan_id,)).fetchall()
        return [{**dict(r), "done": bool(r["done"])} for r in rows]

    def todo_add(self, plan_id: str, text: str, due: str | None, key: str | None = None) -> int:
        with self.database.connect(write=True) as con:
            self._need_plan(con, plan_id)
            cur = con.execute("INSERT INTO todos (plan_id, key, text, due) VALUES (?, ?, ?, ?)",
                              (plan_id, key, text, due))
            return int(cur.lastrowid)

    def todo_update(self, plan_id: str, todo_id: int, **fields) -> bool:
        allowed = {"text", "due", "done"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"不能改的字段：{k}")
            sets.append(f"{k} = ?")
            vals.append(int(v) if k == "done" else v)
        if not sets:
            return True
        with self.database.connect(write=True) as con:
            return con.execute(f"UPDATE todos SET {', '.join(sets)} WHERE plan_id = ? AND id = ?",
                               (*vals, plan_id, todo_id)).rowcount > 0

    def todo_delete(self, plan_id: str, todo_id: int) -> bool:
        with self.database.connect(write=True) as con:
            return con.execute("DELETE FROM todos WHERE plan_id = ? AND id = ?", (plan_id, todo_id)).rowcount > 0

    # -- 行程备注（规格 M10 S4）---------------------------------------------- #
    def notes_get(self, plan_id: str) -> dict[str, str]:
        """{备注对象: 文本}；备注对象见 `companion.note_ref`。"""
        with self.database.connect() as con:
            rows = con.execute("SELECT ref, text FROM day_notes WHERE plan_id = ? ORDER BY ref",
                               (plan_id,)).fetchall()
        return {r["ref"]: r["text"] for r in rows}

    def note_put(self, plan_id: str, ref: str, text: str) -> None:
        """空文本 = 删除这条备注。"""
        with self.database.connect(write=True) as con:
            self._need_plan(con, plan_id)
            if not text.strip():
                con.execute("DELETE FROM day_notes WHERE plan_id = ? AND ref = ?", (plan_id, ref))
                return
            con.execute("INSERT INTO day_notes (plan_id, ref, text, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(plan_id, ref) DO UPDATE SET text = excluded.text, "
                        "updated_at = excluded.updated_at", (plan_id, ref, text, _now()))

    def notes_shift(self, plan_id: str, delta_days: int) -> None:
        """平移日期时，按日期记的备注（整天笔记、没有地点的安排）跟着换成新日期；按地点记的不受影响（M10 S4d）。"""
        from datetime import date as _date
        from datetime import timedelta as _td

        def moved(day: str) -> str:
            return (_date.fromisoformat(day) + _td(days=delta_days)).isoformat()

        with self.database.connect(write=True) as con:
            rows = con.execute("SELECT ref, text, updated_at FROM day_notes WHERE plan_id = ?", (plan_id,)).fetchall()
            con.execute("DELETE FROM day_notes WHERE plan_id = ?", (plan_id,))
            for r in rows:
                ref = r["ref"]
                try:
                    if ref.startswith("day|"):
                        ref = "day|" + moved(ref[4:])
                    elif "|" in ref:
                        day, title = ref.split("|", 1)
                        ref = f"{moved(day)}|{title}"
                except ValueError:                    # 不是日期开头的就原样保留
                    pass
                con.execute("INSERT INTO day_notes (plan_id, ref, text, updated_at) VALUES (?, ?, ?, ?)",
                            (plan_id, ref, r["text"], r["updated_at"]))

    # -- 公开只读分享 ------------------------------------------------------ #
    def create_share(self, plan_id: str) -> str:
        """生成分享令牌；已经有一个有效链接就原样返回它（一个行程同一时间只有一个有效链接）。"""
        with self.database.connect(write=True) as con:
            if con.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
                raise PlanNotFound(f"找不到 plan：{plan_id}")
            row = con.execute("SELECT token FROM shares WHERE plan_id = ? AND revoked_at IS NULL",
                              (plan_id,)).fetchone()
            if row:
                return row["token"]
            token = secrets.token_urlsafe(24)
            con.execute("INSERT INTO shares (token, plan_id, created_at) VALUES (?, ?, ?)",
                        (token, plan_id, _now()))
        return token

    SHARE_OPTIONS = ("packing", "todos", "notes")

    def share_options(self, plan_id: str) -> dict[str, bool]:
        """当前有效链接一起分享了什么；没有有效链接 = 全部没勾。"""
        with self.database.connect() as con:
            row = con.execute("SELECT share_packing, share_todos, share_notes FROM shares "
                              "WHERE plan_id = ? AND revoked_at IS NULL", (plan_id,)).fetchone()
        return {k: bool(row[f"share_{k}"]) if row else False for k in self.SHARE_OPTIONS}

    def set_share_options(self, plan_id: str, options: dict[str, bool]) -> bool:
        """改有效链接的分享选项；没有有效链接返回 False。链接本身（令牌）不变。"""
        with self.database.connect(write=True) as con:
            if con.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
                raise PlanNotFound(f"找不到 plan：{plan_id}")
            cur = con.execute(
                "UPDATE shares SET share_packing = ?, share_todos = ?, share_notes = ? "
                "WHERE plan_id = ? AND revoked_at IS NULL",
                (int(options.get("packing", False)), int(options.get("todos", False)),
                 int(options.get("notes", False)), plan_id))
            return cur.rowcount > 0

    def token_options(self, token: str) -> dict[str, bool]:
        with self.database.connect() as con:
            row = con.execute("SELECT share_packing, share_todos, share_notes FROM shares WHERE token = ?",
                              (token,)).fetchone()
        return {k: bool(row[f"share_{k}"]) if row else False for k in self.SHARE_OPTIONS}

    def active_share(self, plan_id: str) -> str | None:
        with self.database.connect() as con:
            if con.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
                raise PlanNotFound(f"找不到 plan：{plan_id}")
            row = con.execute("SELECT token FROM shares WHERE plan_id = ? AND revoked_at IS NULL",
                              (plan_id,)).fetchone()
        return row["token"] if row else None

    def revoke_share(self, plan_id: str) -> None:
        with self.database.connect(write=True) as con:
            if con.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
                raise PlanNotFound(f"找不到 plan：{plan_id}")
            con.execute("UPDATE shares SET revoked_at = ? WHERE plan_id = ? AND revoked_at IS NULL",
                        (_now(), plan_id))

    def resolve_share(self, token: str) -> tuple[str, bool] | None:
        """令牌 -> (行程号, 是否已失效)；令牌不存在返回 None。"""
        with self.database.connect() as con:
            row = con.execute("SELECT plan_id, revoked_at FROM shares WHERE token = ?", (token,)).fetchone()
        return (row["plan_id"], row["revoked_at"] is not None) if row else None

    # -- 旧文件版行程的导入 ---------------------------------------------- #
    def _import_legacy_files(self) -> None:
        if not self.root.is_dir():
            return
        with self.database.connect() as con:
            known = {r["id"] for r in con.execute("SELECT id FROM plans")}
            known |= {r["id"] for r in con.execute("SELECT id FROM deleted_plans")}
        for d in sorted(p for p in self.root.iterdir() if p.is_dir() and p.name not in known):
            if (d / "history.json").exists() and (d / "request.json").exists():
                self._import_one(d)

    def _import_one(self, d: Path) -> None:
        history = json.loads((d / "history.json").read_text(encoding="utf-8"))
        request = (d / "request.json").read_text(encoding="utf-8")
        TravelRequest.model_validate_json(request)               # 坏数据宁可报错，也不写进库
        with self.database.connect(write=True) as con:
            con.execute("INSERT INTO plans (id, created_at, request_json) VALUES (?, ?, ?)",
                        (d.name, history[0]["created_at"], request))
            for rec in history:
                itinerary = (d / f"v{rec['version']}.json").read_text(encoding="utf-8")
                Itinerary.model_validate_json(itinerary)
                con.execute(
                    "INSERT INTO versions (plan_id, version, created_at, trigger, note, change_summary, "
                    "errors_before, errors_after, itinerary_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (d.name, rec["version"], rec["created_at"], rec["trigger"], rec.get("note", ""),
                     rec.get("change_summary", ""), rec.get("errors_before", 0), rec.get("errors_after", 0),
                     itinerary))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
