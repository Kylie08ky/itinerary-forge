"""M10 S5：文件附件（酒店确认 PDF、预订截图……）。你自己的文件，只存在本机。

- 类型只认 PDF / PNG / JPG，而且**看文件内容的开头字节**，不信扩展名和浏览器报的类型；
- 单个 ≤ 10MB，一个行程最多 50 个（回收站里的不占名额）；
- 磁盘上用随机 id 命名（`<数据目录>/attachments/<行程id>/<id>.<扩展名>`），**不用你给的文件名**，所以不可能目录穿越；
  显示用的文件名清洗过（去路径 / 控制字符 / 非法字符 / Windows 保留名，长度 ≤ 100）；
- 删除先进回收站，30 天后自动清理；永久删除才真删文件；
- 不解析、不执行文件内容；不进分享页 / PDF / 长图 / 导出。
"""

from __future__ import annotations

import re
import secrets
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.db import Database

MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 50
TRASH_DAYS = 30
KINDS = ("pdf", "png", "jpg")
TARGETS = ("plan", "place", "day", "reservation")
MEDIA_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg"}
_EXTENSIONS = {"pdf": "pdf", "png": "png", "jpg": "jpg", "jpeg": "jpg"}
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_ID = re.compile(r"^[0-9a-f]{16}$")
_REF = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_COLS = "id, plan_id, name, kind, size, target_kind, target_ref, uploaded_at, deleted_at"


class AttachmentError(ValueError):
    """文件不能收：code 给程序用（type / mismatch / empty / too_big / too_many / target），message 给人看。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def detect_kind(head: bytes) -> str | None:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None


def clean_filename(name: str) -> str:
    """显示用的文件名：只留最后一段、去控制字符和 Windows 不允许的字符、去开头的点、折空白、≤ 100 字符、避开保留名。"""
    name = re.split(r"[\\/]", name or "")[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip().lstrip(".").strip()
    if not name.strip("."):
        return "未命名"
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    if stem.lower() in _RESERVED:
        name = f"{stem}_{dot}{ext}"
    if len(name) > 100:
        ext = name[name.rfind("."):] if "." in name[-12:] else ""
        name = name[: 100 - len(ext)] + ext
    return name


def check_upload(name: str, data: bytes) -> tuple[str, str]:
    """返回 (显示用文件名, 类型)；不合格抛 AttachmentError。"""
    if not data:
        raise AttachmentError("empty", "这个文件是空的")
    if len(data) > MAX_BYTES:
        raise AttachmentError("too_big", f"文件超过 10MB（当前 {len(data) / 1024 / 1024:.1f} MB）")
    clean = clean_filename(name)
    ext = clean.rpartition(".")[2].lower() if "." in clean else ""
    if ext and ext not in _EXTENSIONS:
        raise AttachmentError("type", "只支持 PDF / PNG / JPG 文件")
    kind = detect_kind(data[:16])
    if kind is None:
        raise AttachmentError("type", "只支持 PDF / PNG / JPG 文件（这个文件的内容不是这三种）")
    if ext and _EXTENSIONS[ext] != kind:
        raise AttachmentError("mismatch", f"文件内容和扩展名对不上：内容是 {kind.upper()}，名字写的是 .{ext}")
    if not ext:
        clean = f"{clean}.{kind}"
    return clean, kind


def _check_target(kind: str, ref: str | None) -> tuple[str, str | None]:
    bad = AttachmentError("target", "挂靠的对象不对")
    if kind not in TARGETS:
        raise bad
    if kind == "plan":
        if ref:
            raise bad
        return kind, None
    if not ref:
        raise bad
    if kind == "day":
        try:
            date.fromisoformat(ref)
        except ValueError as exc:
            raise bad from exc
    elif not _REF.match(ref):
        raise bad
    return kind, ref


class Attachments:
    """附件的记录（SQLite）和文件（数据目录下的 attachments/<行程id>/）。"""

    def __init__(self, database: Database, root: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self.db = database
        self.root = Path(root) / "attachments"
        self._now = now or (lambda: datetime.now(UTC))

    # -- 工具 -------------------------------------------------------------- #
    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def path(self, plan_id: str, rec: dict) -> Path:
        return self.root / plan_id / f"{rec['id']}.{rec['kind']}"

    def _live_count(self, con, plan_id: str) -> int:
        return con.execute("SELECT COUNT(*) FROM attachments WHERE plan_id = ? AND deleted_at IS NULL",
                           (plan_id,)).fetchone()[0]

    @staticmethod
    def _full(con, plan_id: str, aid: str) -> dict | None:
        row = con.execute(f"SELECT {_COLS} FROM attachments WHERE plan_id = ? AND id = ?", (plan_id, aid)).fetchone()
        return dict(row) if row else None

    # -- 读 ---------------------------------------------------------------- #
    def list(self, plan_id: str, *, trash: bool = False) -> list[dict]:
        cond = "IS NOT NULL" if trash else "IS NULL"
        order = "deleted_at DESC" if trash else "uploaded_at DESC, id"
        with self.db.connect() as con:
            rows = con.execute(f"SELECT {_COLS} FROM attachments WHERE plan_id = ? AND deleted_at {cond} "
                               f"ORDER BY {order}", (plan_id,)).fetchall()
        return [dict(r) for r in rows]

    def get(self, plan_id: str, aid: str, *, include_trash: bool = False) -> dict | None:
        if not _ID.match(aid or ""):
            return None
        with self.db.connect() as con:
            rec = self._full(con, plan_id, aid)
        if rec is None or (rec["deleted_at"] and not include_trash):
            return None
        return rec

    # -- 写 ---------------------------------------------------------------- #
    def add(self, plan_id: str, name: str, data: bytes, *, target_kind: str = "plan",
            target_ref: str | None = None) -> dict:
        display, kind = check_upload(name, data)
        target_kind, target_ref = _check_target(target_kind, target_ref)
        aid = secrets.token_hex(8)
        folder = self.root / plan_id
        with self.db.connect(write=True) as con:
            if self._live_count(con, plan_id) >= MAX_FILES:
                raise AttachmentError("too_many", f"这个行程的文件已达 {MAX_FILES} 个上限，先删掉一些再传")
            folder.mkdir(parents=True, exist_ok=True)
            final = folder / f"{aid}.{kind}"
            tmp = folder / f".{aid}.part"
            tmp.write_bytes(data)
            tmp.replace(final)                                       # 写完整了才露面
            try:
                con.execute("INSERT INTO attachments (id, plan_id, name, kind, size, target_kind, target_ref, "
                            "uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (aid, plan_id, display, kind, len(data), target_kind, target_ref, self._stamp()))
            except BaseException:
                final.unlink(missing_ok=True)
                raise
            return self._full(con, plan_id, aid) or {}

    def retarget(self, plan_id: str, aid: str, target_kind: str, target_ref: str | None) -> dict | None:
        target_kind, target_ref = _check_target(target_kind, target_ref)
        if not _ID.match(aid or ""):
            return None
        with self.db.connect(write=True) as con:
            rec = self._full(con, plan_id, aid)
            if rec is None or rec["deleted_at"]:
                return None
            con.execute("UPDATE attachments SET target_kind = ?, target_ref = ? WHERE plan_id = ? AND id = ?",
                        (target_kind, target_ref, plan_id, aid))
            return self._full(con, plan_id, aid)

    def trash(self, plan_id: str, aid: str) -> bool:
        if not _ID.match(aid or ""):
            return False
        with self.db.connect(write=True) as con:
            return con.execute("UPDATE attachments SET deleted_at = ? WHERE plan_id = ? AND id = ? "
                               "AND deleted_at IS NULL", (self._stamp(), plan_id, aid)).rowcount > 0

    def restore(self, plan_id: str, aid: str) -> dict | None:
        if not _ID.match(aid or ""):
            return None
        with self.db.connect(write=True) as con:
            rec = self._full(con, plan_id, aid)
            if rec is None or not rec["deleted_at"]:
                return None
            if self._live_count(con, plan_id) >= MAX_FILES:
                raise AttachmentError("too_many", f"这个行程的文件已达 {MAX_FILES} 个上限，先删掉一些再恢复")
            con.execute("UPDATE attachments SET deleted_at = NULL WHERE plan_id = ? AND id = ?", (plan_id, aid))
            return self._full(con, plan_id, aid)

    def purge(self, plan_id: str, aid: str) -> bool:
        """永久删除（记录 + 文件）。"""
        if not _ID.match(aid or ""):
            return False
        with self.db.connect(write=True) as con:
            rec = self._full(con, plan_id, aid)
            if rec is None:
                return False
            con.execute("DELETE FROM attachments WHERE plan_id = ? AND id = ?", (plan_id, aid))
        self.path(plan_id, rec).unlink(missing_ok=True)
        return True

    def purge_expired(self) -> int:
        """回收站里超过 30 天的，永久删除。"""
        cutoff = (self._now() - timedelta(days=TRASH_DAYS)).isoformat(timespec="seconds")
        with self.db.connect() as con:
            rows = con.execute(f"SELECT {_COLS} FROM attachments WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                               (cutoff,)).fetchall()
        return sum(self.purge(r["plan_id"], r["id"]) for r in rows)

    def shift(self, plan_id: str, delta_days: int) -> None:
        """平移日期时，挂在某一天的文件跟着换成新日期（含回收站里的）。"""
        with self.db.connect(write=True) as con:
            for r in con.execute("SELECT id, target_ref FROM attachments WHERE plan_id = ? AND target_kind = 'day'",
                                 (plan_id,)).fetchall():
                try:
                    moved = (date.fromisoformat(r["target_ref"]) + timedelta(days=delta_days)).isoformat()
                except (ValueError, TypeError):
                    continue
                con.execute("UPDATE attachments SET target_ref = ? WHERE plan_id = ? AND id = ?",
                            (moved, plan_id, r["id"]))

    def delete_plan(self, plan_id: str) -> None:
        """永久删除行程时：记录和文件夹一起删。"""
        with self.db.connect(write=True) as con:
            con.execute("DELETE FROM attachments WHERE plan_id = ?", (plan_id,))
        shutil.rmtree(self.root / plan_id, ignore_errors=True)
