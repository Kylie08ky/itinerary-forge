"""M10 S5：文件附件——类型 / 大小 / 数量 / 文件名清洗 / 路径穿越 / 回收站 / 响应头 / 不外露。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.attachments import (
    MAX_BYTES,
    MAX_FILES,
    TRASH_DAYS,
    AttachmentError,
    Attachments,
    clean_filename,
    detect_kind,
)
from app.generator import generate_from_theme
from app.library import load_library
from app.llm import LLMError
from app.schemas import TravelRequest
from app.versions import PlanStore

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\n%%EOF\n" + b"x" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
LIB = load_library("paris")


def make_request() -> TravelRequest:
    return TravelRequest.model_validate({
        "destination": "Paris", "start_date": "2026-10-03", "end_date": "2026-10-06", "budget_level": "medium",
        "travel_style": "balanced", "interests": ["art"], "arrival_time": "2026-10-03T10:30:00",
        "departure_time": "2026-10-06T18:00:00"})


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path):
    return PlanStore(root=tmp_path / "plans")


@pytest.fixture
def pid(store):
    req = make_request()
    return store.create(req, generate_from_theme(req, "art", client=None, library=LIB).itinerary)


@pytest.fixture
def files(store, clock):
    return Attachments(store.database, store.root, now=clock)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_kind_is_decided_by_the_first_bytes_not_by_the_name():
    assert (detect_kind(PDF), detect_kind(PNG), detect_kind(JPG)) == ("pdf", "png", "jpg")
    for bad in (b"<html><script>alert(1)</script>", b"MZ\x90\x00", b"GIF89a", b"", b"PK\x03\x04", b" %PDF-1.4"):
        assert detect_kind(bad) is None


@pytest.mark.parametrize("raw, clean", [
    ("酒店确认单.pdf", "酒店确认单.pdf"),
    ("../../etc/passwd.png", "passwd.png"),
    ("C:\\Users\\me\\订单 2026.jpg", "订单 2026.jpg"),
    ("a\x00b\x1f.pdf", "ab.pdf"),
    ("...hidden.png", "hidden.png"),
    ('we<ird>:na"me|?*.jpg', "weirdname.jpg"),
    ("   spaced   out   .PDF", "spaced out .PDF"),
    ("", "未命名"), ("....", "未命名"), ("/", "未命名"),
    ("CON", "CON_"), ("nul.txt", "nul_.txt"),
])
def test_filenames_are_cleaned(raw, clean):
    assert clean_filename(raw) == clean


def test_long_names_are_cut_but_keep_the_extension():
    name = clean_filename("长" * 300 + ".pdf")
    assert len(name) <= 100 and name.endswith(".pdf")


# --------------------------------------------------------------------------- #
# 存取
# --------------------------------------------------------------------------- #
def test_add_stores_bytes_under_a_random_id_and_never_under_the_given_name(files, pid, store):
    rec = files.add(pid, "../../酒店确认单.pdf", PDF)
    assert rec["name"] == "酒店确认单.pdf" and rec["kind"] == "pdf" and rec["size"] == len(PDF)
    assert rec["target_kind"] == "plan" and rec["target_ref"] is None and rec["deleted_at"] is None
    path = files.path(pid, rec)
    assert path.read_bytes() == PDF
    assert path.parent == store.root / "attachments" / pid and path.name == f"{rec['id']}.pdf"
    assert "酒店" not in str(path.relative_to(store.root))
    assert files.get(pid, rec["id"])["id"] == rec["id"] and files.get("other-plan", rec["id"]) is None


@pytest.mark.parametrize("name, data, code", [
    ("a.exe", PDF, "type"), ("a.html", b"<html>", "type"), ("a.pdf", b"<html><script>", "type"),
    ("a.pdf", PNG, "mismatch"), ("a.png", JPG, "mismatch"), ("a.jpg", PDF, "mismatch"),
    ("a.pdf", b"", "empty"), ("noext", PDF, None), ("a.JPEG", JPG, None), ("a.jpeg", JPG, None),
])
def test_type_checks(files, pid, name, data, code):
    if code is None:
        assert files.add(pid, name, data)["id"]
        return
    with pytest.raises(AttachmentError) as err:
        files.add(pid, name, data)
    assert err.value.code == code and err.value.message


def test_error_messages_are_plain_chinese(files, pid):
    for name, data, word in [("a.exe", PDF, "PDF / PNG / JPG"), ("a.pdf", PNG, "对不上"), ("a.pdf", b"", "空")]:
        with pytest.raises(AttachmentError, match=word):
            files.add(pid, name, data)
    with pytest.raises(AttachmentError, match=r"超过 10MB.*10\.\d MB"):
        files.add(pid, "big.pdf", PDF + b"\0" * MAX_BYTES)


def test_size_limit_is_exact(files, pid):
    ok = PDF + b"\0" * (MAX_BYTES - len(PDF))
    assert files.add(pid, "edge.pdf", ok)["size"] == MAX_BYTES
    with pytest.raises(AttachmentError) as err:
        files.add(pid, "over.pdf", ok + b"\0")
    assert err.value.code == "too_big"


def test_file_count_limit_counts_only_live_files_and_restoring_respects_it(files, pid):
    first = files.add(pid, "f0.pdf", PDF)
    for i in range(1, MAX_FILES):
        files.add(pid, f"f{i}.pdf", PDF)
    with pytest.raises(AttachmentError, match="50 个上限") as err:
        files.add(pid, "one-too-many.pdf", PDF)
    assert err.value.code == "too_many"
    files.trash(pid, first["id"])
    files.add(pid, "fits-now.pdf", PDF)                                      # 回收站里的不占名额
    with pytest.raises(AttachmentError, match="50 个上限"):
        files.restore(pid, first["id"])


# --------------------------------------------------------------------------- #
# 挂靠
# --------------------------------------------------------------------------- #
def test_targets_are_stored_and_can_be_changed(files, pid):
    rec = files.add(pid, "a.png", PNG, target_kind="place", target_ref="orsay")
    assert (rec["target_kind"], rec["target_ref"]) == ("place", "orsay")
    moved = files.retarget(pid, rec["id"], "day", "2026-10-04")
    assert (moved["target_kind"], moved["target_ref"]) == ("day", "2026-10-04")
    back = files.retarget(pid, rec["id"], "plan", None)
    assert (back["target_kind"], back["target_ref"]) == ("plan", None)
    assert files.retarget(pid, "nope", "plan", None) is None


@pytest.mark.parametrize("kind, ref", [("nonsense", None), ("place", None), ("day", None), ("plan", "x"),
                                       ("day", "not-a-date"), ("place", "../x"), ("reservation", "")])
def test_bad_targets_are_refused(files, pid, kind, ref):
    with pytest.raises(AttachmentError) as err:
        files.add(pid, "a.pdf", PDF, target_kind=kind, target_ref=ref)
    assert err.value.code == "target"


# --------------------------------------------------------------------------- #
# 回收站
# --------------------------------------------------------------------------- #
def test_trash_restore_and_permanent_delete(files, pid):
    rec = files.add(pid, "a.pdf", PDF)
    path = files.path(pid, rec)
    assert files.trash(pid, rec["id"]) is True
    assert files.list(pid) == [] and [r["id"] for r in files.list(pid, trash=True)] == [rec["id"]]
    assert files.get(pid, rec["id"]) is None and files.get(pid, rec["id"], include_trash=True)["deleted_at"]
    assert path.exists()                                                      # 进回收站不删文件
    back = files.restore(pid, rec["id"])
    assert back["deleted_at"] is None and [r["id"] for r in files.list(pid)] == [rec["id"]]
    assert files.purge(pid, rec["id"]) is True
    assert not path.exists() and files.list(pid) == [] and files.list(pid, trash=True) == []
    assert files.purge(pid, rec["id"]) is False


def test_trash_is_emptied_after_thirty_days(files, pid, clock):
    old, fresh = files.add(pid, "old.pdf", PDF), files.add(pid, "fresh.pdf", PDF)
    files.trash(pid, old["id"])
    clock.t += timedelta(days=TRASH_DAYS - 1)
    files.trash(pid, fresh["id"])
    files.purge_expired()
    assert {r["id"] for r in files.list(pid, trash=True)} == {old["id"], fresh["id"]}
    clock.t += timedelta(days=2)                                              # old 进回收站 31 天，fresh 才 2 天
    files.purge_expired()
    assert [r["id"] for r in files.list(pid, trash=True)] == [fresh["id"]]
    assert not files.path(pid, old).exists() and files.path(pid, fresh).exists()


def test_deleting_the_plan_deletes_its_files_but_duplicating_does_not_copy_them(files, pid, store):
    rec = files.add(pid, "a.pdf", PDF)
    path = files.path(pid, rec)
    copy = store.duplicate(pid)
    assert files.list(copy) == []
    store.delete(pid)
    assert not path.exists() and not (store.root / "attachments" / pid).exists()
    assert files.list(pid) == []


def test_shifting_the_dates_moves_day_attachments_only(files, pid):
    day = files.add(pid, "d.png", PNG, target_kind="day", target_ref="2026-10-04")
    place = files.add(pid, "p.png", PNG, target_kind="place", target_ref="orsay")
    whole = files.add(pid, "w.png", PNG)
    files.shift(pid, 7)
    assert files.get(pid, day["id"])["target_ref"] == "2026-10-11"
    assert files.get(pid, place["id"])["target_ref"] == "orsay" and files.get(pid, whole["id"])["target_ref"] is None


def test_a_database_from_before_attachments_is_upgraded(tmp_path):
    import sqlite3

    root = tmp_path / "plans"
    PlanStore(root=root)
    with sqlite3.connect(root / "plans.db") as con:
        con.execute("DROP TABLE attachments")
        con.execute("PRAGMA user_version = 11")
    s2 = PlanStore(root=root)
    assert Attachments(s2.database, s2.root).list("whatever") == []


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
class DeadLLM:
    def complete_json(self, *, system, user, schema):
        raise LLMError("模型没启动")


@pytest.fixture
def client(store):
    return TestClient(create_app(store=store, client_factory=DeadLLM))


def upload(client, pid, name, data, **params):
    return client.post(f"/plans/{pid}/attachments", params={"name": name, **params}, content=data,
                       headers={"content-type": "application/octet-stream"})


def test_upload_list_preview_download(client, pid):
    r = upload(client, pid, "酒店确认单.pdf", PDF, target_kind="place", target_ref="orsay")
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["name"] == "酒店确认单.pdf" and rec["kind"] == "pdf" and rec["target_ref"] == "orsay"
    listed = client.get(f"/plans/{pid}/attachments").json()
    assert [a["id"] for a in listed["attachments"]] == [rec["id"]] and listed["trash"] == []
    assert listed["limits"] == {"max_bytes": MAX_BYTES, "max_files": MAX_FILES, "trash_days": TRASH_DAYS,
                                "kinds": ["pdf", "png", "jpg"]}
    inline = client.get(f"/plans/{pid}/attachments/{rec['id']}/file")
    assert inline.status_code == 200 and inline.content == PDF and inline.headers["content-type"] == "application/pdf"
    assert inline.headers["content-disposition"].startswith("inline")
    down = client.get(f"/plans/{pid}/attachments/{rec['id']}/file", params={"download": 1})
    assert down.headers["content-disposition"].startswith("attachment")
    assert "filename*=UTF-8''%E9%85%92%E5%BA%97" in down.headers["content-disposition"]        # 中文名可下载


def test_served_files_can_never_run_as_pages(client, pid):
    rec = upload(client, pid, "a.png", PNG).json()
    h = client.get(f"/plans/{pid}/attachments/{rec['id']}/file").headers
    assert h["x-content-type-options"] == "nosniff" and h["content-security-policy"] == "sandbox"
    assert h["content-type"] == "image/png" and "no-store" in h["cache-control"]


@pytest.mark.parametrize("name, data, code, word", [
    ("a.exe", PDF, "type", "只支持 PDF / PNG / JPG"),
    ("a.pdf", b"<html><script>alert(1)</script>", "type", "只支持"),
    ("a.pdf", PNG, "mismatch", "对不上"),
    ("a.pdf", b"", "empty", "空"),
])
def test_bad_uploads_get_a_clear_422(client, pid, name, data, code, word):
    r = upload(client, pid, name, data)
    d = r.json()["detail"]
    assert r.status_code == 422 and d["code"] == code and word in d["message"]


def test_too_big_is_refused_by_content_length_before_reading(client, pid):
    r = client.post(f"/plans/{pid}/attachments", params={"name": "big.pdf"}, content=PDF + b"\0" * MAX_BYTES)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "too_big" and "10MB" in r.json()["detail"]["message"]
    assert client.get(f"/plans/{pid}/attachments").json()["attachments"] == []


def test_too_big_without_a_content_length_stops_reading(client, pid):
    def chunks():
        yield PDF
        for _ in range(12):
            yield b"\0" * (1024 * 1024)

    r = client.post(f"/plans/{pid}/attachments", params={"name": "big.pdf"}, content=chunks())
    assert r.status_code == 413


def test_missing_name_unknown_plan_and_bad_targets(client, pid):
    nameless = upload(client, pid, "", PDF)                                    # 没名字：叫「未命名」，扩展名按内容补
    assert nameless.status_code == 201 and nameless.json()["name"] == "未命名.pdf"
    assert upload(client, "paris-nope", "a.pdf", PDF).status_code == 404
    for params in ({"target_kind": "place", "target_ref": "no-such-place"},
                   {"target_kind": "day", "target_ref": "2027-01-01"},
                   {"target_kind": "reservation", "target_ref": "no-such-place"},
                   {"target_kind": "weird"}):
        r = upload(client, pid, "a.pdf", PDF, **params)
        d = r.json()["detail"]
        assert r.status_code == 422 and d["code"] == "target" and "挂靠" in d["message"]
    ok = upload(client, pid, "a.pdf", PDF, target_kind="day", target_ref="2026-10-04")
    assert ok.status_code == 201


def test_patch_retarget_delete_restore_and_permanent_delete(client, pid):
    rec = upload(client, pid, "a.png", PNG).json()
    base = f"/plans/{pid}/attachments/{rec['id']}"
    moved = client.patch(base, json={"target_kind": "place", "target_ref": "louvre"})
    assert moved.status_code == 200 and moved.json()["target_ref"] == "louvre"
    assert client.patch(base, json={"target_kind": "place", "target_ref": "nope"}).status_code == 422
    assert client.delete(base).status_code == 204
    assert client.get(base + "/file").status_code == 404                      # 回收站里的不能预览
    listed = client.get(f"/plans/{pid}/attachments").json()
    assert listed["attachments"] == [] and [a["id"] for a in listed["trash"]] == [rec["id"]]
    assert client.patch(base, json={"restore": True}).json()["deleted_at"] is None
    assert client.get(base + "/file").status_code == 200
    assert client.delete(base, params={"permanent": 1}).status_code == 204
    assert client.get(f"/plans/{pid}/attachments").json() == {
        "attachments": [], "trash": [], "limits": client.get(f"/plans/{pid}/attachments").json()["limits"]}
    assert client.delete(base).status_code == 404


def test_one_plan_cannot_read_anothers_files(client, store, pid):
    other = store.create(make_request(), generate_from_theme(make_request(), "art", client=None,
                                                                library=LIB).itinerary)
    rec = upload(client, pid, "a.pdf", PDF).json()
    assert client.get(f"/plans/{other}/attachments/{rec['id']}/file").status_code == 404
    assert client.delete(f"/plans/{other}/attachments/{rec['id']}").status_code == 404


@pytest.mark.parametrize("aid", ["../../plans.db", "..%2F..%2Fplans.db", "a/b", "x" * 200])
def test_attachment_ids_cannot_be_used_to_walk_the_disk(client, pid, aid):
    assert client.get(f"/plans/{pid}/attachments/{aid}/file").status_code in (404, 405, 422)


def test_files_never_leak_to_share_or_any_export(client, pid):
    rec = upload(client, pid, "ZZ9-确认单.pdf", PDF, target_kind="day", target_ref="2026-10-04").json()
    token = client.post(f"/plans/{pid}/share").json()["token"]
    shared = client.get(f"/share/{token}").text
    assert rec["id"] not in shared and "ZZ9" not in shared and "attachment" not in shared
    for path in ("itinerary.md", "itinerary.html", "itinerary.ics", "itinerary.kml"):
        text = client.get(f"/plans/{pid}/{path}").text
        assert "ZZ9" not in text and rec["id"] not in text, path
    assert client.get(f"/share/{token}/attachments").status_code == 404


def test_shift_moves_day_attachments_through_the_api(client, pid):
    rec = upload(client, pid, "d.png", PNG, target_kind="day", target_ref="2026-10-04").json()
    assert client.post(f"/plans/{pid}/shift", json={"start_date": "2026-10-10"}).status_code == 201
    got = client.get(f"/plans/{pid}/attachments").json()["attachments"][0]
    assert got["id"] == rec["id"] and got["target_ref"] == "2026-10-11"


def test_deleting_a_plan_removes_its_attachment_folder(client, store, pid):
    upload(client, pid, "a.pdf", PDF)
    folder = store.root / "attachments" / pid
    assert folder.exists() and any(folder.iterdir())
    assert client.delete(f"/plans/{pid}").status_code == 204
    assert not folder.exists()


def test_file_on_disk_is_not_executable_and_name_is_not_a_path(client, pid, store):
    upload(client, pid, "../../../escape.pdf", PDF)
    folder = store.root / "attachments" / pid
    assert [p.suffix for p in folder.iterdir()] == [".pdf"]
    assert not (store.root.parent / "escape.pdf").exists() and not (store.root / "escape.pdf").exists()
    if os.name != "nt":
        assert all(not (p.stat().st_mode & 0o111) for p in folder.iterdir())
