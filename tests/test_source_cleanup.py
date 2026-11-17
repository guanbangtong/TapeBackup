"""删除安全回归：所有可变数据均在 tmp_path；xfail 保留期望的安全行为。"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tarfile
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from tape_backup import db, engine as eng, source_cleanup as cleanup
from tape_backup.engine import Engine, raw_media_path, tar_media_path
from test_engine import Env, Crash, files_under, read, write


@pytest.fixture
def env(tmp_path):
    world = Env(tmp_path)
    world.cfg.delete_delay_minutes = 0
    world.cfg.nas_recycle_dir = ""
    return world


def sql(env, statement, *args):
    con = db.connect(env.cfg.db_path)
    try:
        with con:
            con.execute(statement, args)
    finally:
        con.close()


def enqueue(env, source, delete=False, owner="Owner"):
    con = db.connect(env.cfg.db_path)
    try:
        return db.add_job(con, owner, str(source), delete_source=delete)
    finally:
        con.close()


def job(env, job_id):
    return env.one("SELECT * FROM jobs WHERE id=?", job_id)


def prepare(env, spec=None):
    source = Path(env.make_source("Owner", spec or {"a.raw": b"raw-data", "sub/b.txt": b"tar-data"}))
    job_id = enqueue(env, source)
    assert env.run()[0] == "done"
    sql(env, "UPDATE jobs SET delete_source=1 WHERE id=?", job_id)
    return source, job_id


def before_delete(monkeypatch, callback):
    original = Engine._delete_source

    def wrapper(self, row):
        callback(self, row)
        return original(self, row)

    monkeypatch.setattr(Engine, "_delete_source", wrapper)


def assert_counts(env, job_id, files, size, kept):
    row = job(env, job_id)
    assert row["status"] == "DONE"
    assert row["source_deleted_at"]
    assert (row["deleted_files"], row["deleted_bytes"], row["kept_files"]) == (files, size, kept)


def tape_payload(env, row, owner="Owner"):
    if row["kind"] == "raw":
        return read(raw_media_path(str(env.drive), owner, row["rel_path"]))
    with tarfile.open(tar_media_path(str(env.drive), owner, row["tar_name"])) as archive:
        member = archive.extractfile(eng.tar_member_name(owner, row["rel_path"]))
        assert member is not None
        with member:
            return member.read()


def junction(link, target, tmp_path):
    # mklink 只创建 tmp_path 内的链接；清理由 Path.rmdir 解除入口，不递归删除。
    assert link.is_relative_to(tmp_path) and target.resolve().is_relative_to(tmp_path.resolve())
    result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True)
    if result.returncode:
        pytest.skip(f"系统拒绝创建临时目录联接：{result.stdout + result.stderr!r}")


@pytest.mark.parametrize("delete", [False, True])
def test_end_to_end_delete_opt_in(env, delete):
    spec = {"a.raw": b"abc", "sub/b.txt": b"defgh", "empty.raw": b""}
    source = Path(env.make_source("Owner", spec))
    (source / "empty/nested").mkdir(parents=True)
    job_id = enqueue(env, source, delete)
    assert env.run()[0] == "done"
    if delete:
        assert not source.exists()
        assert_counts(env, job_id, 3, 8, 0)
    else:
        assert {rel: read(source / rel) for rel in spec} == spec
        assert (source / "empty/nested").is_dir()
        row = job(env, job_id)
        assert row["status"] == "DONE" and row["source_deleted_at"] is None
        assert (row["deleted_files"], row["deleted_bytes"], row["kept_files"]) == (0, 0, 0)


@pytest.mark.parametrize("change", ["added", "size", "mtime", "failed", "replaced"])
def test_unarchived_or_changed_file_is_kept(env, change):
    source, job_id = prepare(env, {"a.raw": b"original", "sub/safe.txt": b"safe"})
    kept = source / "a.raw"
    if change == "added":
        kept = source / "new/added.raw"
        write(kept, b"new")
    elif change == "size":
        write(kept, b"a different size")
    elif change == "mtime":
        stamp = kept.stat().st_mtime
        write(kept, b"changed!")
        os.utime(kept, (stamp + 10, stamp + 10))
    else:
        sql(env, "UPDATE files SET status=? WHERE job_id=? AND rel_path='a.raw'", change.upper(), job_id)
    preserved = kept.read_bytes()
    (source / "empty/nested").mkdir(parents=True)
    assert env.run()[0] == "done"
    assert kept.read_bytes() == preserved
    assert not (source / "empty").exists()
    assert not (source / "sub").exists()
    assert_counts(env, job_id, 2 if change == "added" else 1, 12 if change == "added" else 4, 1)


def test_actual_failed_backup_file_is_kept(env, monkeypatch):
    source = Path(env.make_source("Owner", {"a.raw": b"failed", "b.raw": b"safe"}))
    original = eng.copy_file

    def fail_one(src, dest, *args, **kwargs):
        if Path(src).name == "a.raw":
            raise eng.CopyError("src", str(src), "模拟源文件读取失败")
        return original(src, dest, *args, **kwargs)

    monkeypatch.setattr(eng, "copy_file", fail_one)
    job_id = enqueue(env, source, True)
    assert env.run()[0] == "done"
    assert env.file_row("a.raw", job_id)["status"] == "FAILED"
    assert (source / "a.raw").read_bytes() == b"failed"
    assert_counts(env, job_id, 1, 4, 1)


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_links_are_kept_and_counted(env, link_kind):
    source = Path(env.make_source("Owner", {"safe.raw": b"safe"}))
    target = env.base / "other"
    write(target / "valuable.raw", b"do not delete")
    link = source / "link"
    if link_kind == "junction":
        junction(link, target, env.base)
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"创建符号链接需要系统权限：{error}")
    try:
        job_id = enqueue(env, source, True)
        assert env.run()[0] == "done"
        assert link.is_dir()
        assert (target / "valuable.raw").read_bytes() == b"do not delete"
        assert_counts(env, job_id, 1, 4, 1)
    finally:
        if link.exists():
            link.rmdir()


@pytest.mark.parametrize("damage", ["raw_missing", "raw_size", "tar_missing", "tar_short"])
def test_tape_damage_aborts_before_any_source_deletion(env, damage):
    source, job_id = prepare(env)
    original = {rel: read(source / rel) for rel in ("a.raw", "sub/b.txt")}
    row = env.file_row("sub\\b.txt", job_id)
    path = Path(raw_media_path(str(env.drive), "Owner", "a.raw") if damage.startswith("raw")
                else tar_media_path(str(env.drive), "Owner", row["tar_name"]))
    if damage.endswith("missing"):
        path.unlink()
    else:
        path.write_bytes(b"x")
    status, message = env.run()
    assert status == "error" and "没有删除" in message
    assert {rel: read(source / rel) for rel in original} == original
    assert job(env, job_id)["source_deleted_at"] is None


@pytest.mark.parametrize("absence", ["directory", "marker", "unmarked"])
def test_absent_matching_tape_currently_uses_records(env, absence):
    # 记录现有明确行为；不能把它当成完成了磁带实物核验。
    source, job_id = prepare(env)
    if absence == "directory":
        env.drive.rename(env.base / "offline_tape")
    else:
        (env.drive / "TAPE_ID_T1.txt").unlink()
        if absence == "marker":
            eng.write_tape_marker(str(env.drive), "OTHER")
    assert env.run()[0] == "done"
    assert not source.exists()
    assert_counts(env, job_id, 2, 16, 0)
    assert any("按备份记录处理" in line for line in env.logs)


@pytest.mark.parametrize("future", [False, True])
def test_delete_delay_uses_bounded_monotonic_wait(env, monkeypatch, future):
    source, job_id = prepare(env)
    env.cfg.delete_delay_minutes = 0.05
    wall = time.time()
    stamp = datetime.fromtimestamp(wall + (86400 if future else 0)).strftime("%Y-%m-%d %H:%M:%S")
    sql(env, "UPDATE batches SET archived_at=? WHERE job_id=?", stamp, job_id)
    engine = env.engine()
    clock = {"elapsed": 0.0}
    snapshots = []

    def sleep(seconds):
        snapshots.append(engine.progress.snapshot())
        assert source.exists()
        clock["elapsed"] += seconds
        assert clock["elapsed"] <= 3.01, "系统时间异常导致无界等待"

    monkeypatch.setattr(eng, "time", SimpleNamespace(time=lambda: wall,
                         monotonic=lambda: 100 + clock["elapsed"], sleep=sleep))
    assert engine.run("T1")[0] == "done"
    assert 2 <= clock["elapsed"] <= 3.01
    assert snapshots and all(s["phase"] == "delete" and s["phase_unit"] == "seconds" for s in snapshots)
    assert all(0 <= s["phase_done"] <= s["phase_total"] == 3 for s in snapshots)
    assert_counts(env, job_id, 2, 16, 0)


def test_stop_during_wait_then_delete_before_new_jobs(env, monkeypatch):
    source, job_id = prepare(env)
    env.cfg.delete_delay_minutes = 1
    sql(env, "UPDATE batches SET archived_at=? WHERE job_id=?", db.now(), job_id)
    engine = env.engine()
    snapshots = []

    def sleep(_seconds):
        snapshots.append(engine.progress.snapshot())
        engine.stop()

    with monkeypatch.context() as patch:
        patch.setattr(eng, "time", SimpleNamespace(time=time.time, monotonic=lambda: 100.0, sleep=sleep))
        assert engine.run("T1")[0] == "stopped"
    assert snapshots[0]["phase"] == "delete" and snapshots[0]["phase_unit"] == "seconds"
    assert len(files_under(source)) == 2
    row = job(env, job_id)
    assert row["status"] == "DONE" and row["source_deleted_at"] is None
    con = db.connect(env.cfg.db_path)
    try:
        assert [r["id"] for r in db.pending_deletions(con)] == [job_id]
    finally:
        con.close()
    fresh = env.make_source("NewOwner", {"new.raw": b"new"})
    new_id = enqueue(env, fresh, owner="NewOwner")
    original = Engine._run_job

    def run_job(self, row):
        assert not source.exists(), "待删除任务必须在新备份前处理"
        return original(self, row)

    monkeypatch.setattr(Engine, "_run_job", run_job)
    env.cfg.delete_delay_minutes = 0
    assert env.run()[0] == "done"
    assert job(env, new_id)["status"] == "DONE"
    assert_counts(env, job_id, 2, 16, 0)


@pytest.mark.parametrize(("source", "recycle", "expected"), [
    (r"Z:\Owner", "#recycle", r"Z:\#recycle\Owner"),
    (r"Z:\team\Owner", " /#recycle/ ", r"Z:\#recycle\team\Owner"),
    (r"\\server\share\Owner", "#recycle", r"\\server\share\#recycle\Owner"),
    (r"\\server\share\team\Owner", "#recycle", r"\\server\share\#recycle\team\Owner"),
    ("Z:\\", "#recycle", None),
    ("\\\\server\\share\\", "#recycle", None),
    (r"Z:\#recycle", "#recycle", None),
    (r"Z:\#RECYCLE\Owner", "#recycle", None),
    (r"\\server\share\#recycle\Owner", "#recycle", None),
    (r"Z:\Owner", "", None),
    (r"Z:\Owner", "  ", None),
    (r"Z:\Owner", "///", None),
])
def test_recycle_path_is_pure_and_scoped(source, recycle, expected):
    # 仅计算字符串，不对这些示例盘符进行任何 I/O。
    assert cleanup.recycle_path(source, recycle) == expected


def test_purge_tree_removes_readonly_and_nested_files(tmp_path):
    root = tmp_path / "recycle/Owner"
    write(root / "a.raw", b"abc")
    write(root / "deep/b.txt", b"12345")
    (root / "deep/empty").mkdir()
    (root / "a.raw").chmod(stat.S_IREAD)
    write(tmp_path / "recycle/Other/keep.raw", b"keep")
    assert cleanup.purge_tree(str(root)) == (2, 8, 0)
    assert not root.exists()
    assert (tmp_path / "recycle/Other/keep.raw").read_bytes() == b"keep"
    assert (tmp_path / "recycle").is_dir()


def test_engine_recycle_purge_is_limited_to_source_subtree(env, monkeypatch):
    source, job_id = prepare(env)
    recycle = env.base / "share/#recycle"
    write(recycle / "Owner/old.raw", b"old")
    write(recycle / "Owner/deep/old.txt", b"old2")
    write(recycle / "Other/valuable.raw", b"other")
    write(recycle / "root.raw", b"root")
    env.cfg.nas_recycle_dir = "#recycle"
    monkeypatch.setattr(cleanup, "recycle_path", lambda src, folder: str(recycle / "Owner"))
    assert env.run()[0] == "done"
    assert not source.exists() and not (recycle / "Owner").exists()
    assert (recycle / "Other/valuable.raw").read_bytes() == b"other"
    assert (recycle / "root.raw").read_bytes() == b"root"
    assert_counts(env, job_id, 2, 16, 0)


@pytest.mark.parametrize(("protected", "relation"), [
    (protected, relation) for protected in ("staging", "tape", "data")
    for relation in ("equal", "child", "parent")
])
def test_delete_rejects_protected_path_overlap(env, protected, relation):
    # 直接建立 DONE 待删除任务，确保命中删除入口的检查，而非仅检查备份扫描。
    base = env.base / "protected"
    source = base / "source"
    source.mkdir(parents=True)
    other = source if relation == "equal" else source / "nested" if relation == "child" else base
    other.mkdir(exist_ok=True)
    setattr(env.cfg, {"staging": "staging_root", "tape": "tape_root", "data": "data_dir"}[protected], str(other))
    if protected == "staging":
        source = other / "Owner_archive" if relation == "equal" else source
        source.mkdir(exist_ok=True)
    sentinel = source / "keep.raw"
    sentinel.write_bytes(b"keep")
    job_id = enqueue(env, source, True)
    sql(env, "UPDATE jobs SET status='DONE' WHERE id=?", job_id)
    status, message = env.run()
    assert status == "error" and "重叠" in message
    assert sentinel.read_bytes() == b"keep"
    assert job(env, job_id)["source_deleted_at"] is None


def test_missing_source_is_recorded_complete(env):
    source = env.nas / "missing"
    job_id = enqueue(env, source, True)
    sql(env, "UPDATE jobs SET status='DONE' WHERE id=?", job_id)
    assert env.run()[0] == "done"
    assert_counts(env, job_id, 0, 0, 0)


@pytest.mark.parametrize("kind", ["raw", "txt"])
@pytest.mark.parametrize("mtime_delta", [0, 1.5])
def test_same_owner_different_location_different_content_is_not_lost(env, kind, mtime_delta):
    rel = f"sub/a.{kind}"
    first = Path(env.make_source("Owner", {rel: b"FIRST-VERSION"}))
    enqueue(env, first)
    assert env.run()[0] == "done"
    second = env.base / "other_nas/Owner"
    write(second / rel, b"OTHER-CONTENT")
    stamp = (first / rel).stat().st_mtime + mtime_delta
    os.utime(second / rel, (stamp, stamp))
    job_id = enqueue(env, second, True, owner="owner")  # owner 大小写也不可规避隔离
    env.run()
    rows = env.rows("SELECT f.*, j.owner FROM files f JOIN jobs j ON j.id=f.job_id WHERE f.status='ARCHIVED'")
    source_safe = (second / rel).exists() and read(second / rel) == b"OTHER-CONTENT"
    tape_safe = any(tape_payload(env, row, row["owner"]) == b"OTHER-CONTENT" for row in rows)
    assert source_safe or tape_safe, f"源已丢失且磁带无其内容，新任务done_files={job(env, job_id)['done_files']}"


def test_same_owner_different_metadata_is_backed_up_before_delete(env):
    first = Path(env.make_source("Owner", {"a.raw": b"old"}))
    enqueue(env, first)
    assert env.run()[0] == "done"
    second = env.base / "other/Owner"
    write(second / "a.raw", b"new-and-longer")
    job_id = enqueue(env, second, True)
    assert env.run()[0] == "done"
    assert read(raw_media_path(str(env.drive), "Owner", "a.raw")) == b"new-and-longer"
    assert_counts(env, job_id, 1, 14, 0)


def test_incremental_deletes_both_generations_and_readonly_file(env):
    source = Path(env.make_source("Owner", {"old.raw": b"old", "old.txt": b"text"}))
    enqueue(env, source)
    assert env.run()[0] == "done"
    write(source / "new.raw", b"new")
    (source / "old.raw").chmod(stat.S_IREAD)
    job_id = enqueue(env, source, True)
    assert env.run()[0] == "done"
    assert not source.exists()
    assert job(env, job_id)["done_files"] == 1
    assert_counts(env, job_id, 3, 10, 0)


def test_locked_file_is_kept_while_others_are_deleted(env):
    source, job_id = prepare(env)
    create = ctypes.windll.kernel32.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    close = ctypes.windll.kernel32.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    # 允许读取但不允许 FILE_SHARE_DELETE：真实 Windows 文件占用，而非抛错模拟。
    handle = create(str(source / "a.raw"), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        assert env.run()[0] == "done"
        assert (source / "a.raw").read_bytes() == b"raw-data"
        assert not (source / "sub/b.txt").exists()
        assert_counts(env, job_id, 1, 8, 1)
    finally:
        assert close(handle)


def interrupt_once(env, monkeypatch, how):
    source, job_id = prepare(env, {"a.raw": b"aaa", "b.raw": b"bb", "keep.txt": b"keep"})
    write(source / "new.raw", b"new")
    engine = env.engine()
    original = cleanup._remove
    calls = []

    def remove(path):
        original(path)
        calls.append(path)
        if len(calls) == 1:
            if how == "stop":
                engine.stop()
            else:
                raise Crash("第一个文件删除后中断")

    with monkeypatch.context() as patch:
        patch.setattr(cleanup, "_remove", remove)
        status, _message = engine.run("T1")
    assert status == ("stopped" if how == "stop" else "error")
    assert not (source / "a.raw").exists()
    assert (source / "b.raw").exists() and (source / "new.raw").read_bytes() == b"new"
    assert job(env, job_id)["status"] == "DONE" and job(env, job_id)["source_deleted_at"] is None
    assert env.run()[0] == "done"
    assert files_under(source) == {"new.raw": 3}
    return job_id


@pytest.mark.parametrize("how", ["stop", "exception"])
def test_interrupted_delete_resumes_and_preserves_new_files(env, monkeypatch, how):
    job_id = interrupt_once(env, monkeypatch, how)
    assert job(env, job_id)["source_deleted_at"]
    assert job(env, job_id)["kept_files"] == 1


@pytest.mark.parametrize("how", ["stop", "exception"])
def test_interrupted_delete_preserves_total_counters(env, monkeypatch, how):
    job_id = interrupt_once(env, monkeypatch, how)
    assert_counts(env, job_id, 3, 9, 1)


def test_file_changed_immediately_before_remove_is_kept(env, monkeypatch):
    source, job_id = prepare(env)
    original = cleanup.delete_files

    def change_then_delete(src, candidates, should_stop, on_file):
        def callback(rel):
            on_file(rel)
            if rel == "a.raw":
                write(source / rel, b"changed-size")
        return original(src, candidates, should_stop, callback)

    monkeypatch.setattr(cleanup, "delete_files", change_then_delete)
    assert env.run()[0] == "done"
    assert (source / "a.raw").read_bytes() == b"changed-size"
    assert_counts(env, job_id, 1, 8, 1)


def test_purge_tree_never_follows_junction_to_other_folder(tmp_path):
    root = tmp_path / "recycle/Owner"
    root.mkdir(parents=True)
    other = tmp_path / "other"
    write(other / "valuable.raw", b"valuable")
    link = root / "redirect"
    junction(link, other, tmp_path)
    try:
        cleanup.purge_tree(str(root))
        assert (other / "valuable.raw").exists(), "清回收站越过junction删了他人文件"
    finally:
        if link.exists():
            link.rmdir()


def test_kept_junction_preserves_targets_empty_directories(env):
    source, _job_id = prepare(env)
    other = env.base / "other"
    (other / "empty/deep").mkdir(parents=True)
    write(other / "valuable.raw", b"valuable")
    link = source / "redirect"
    junction(link, other, env.base)
    try:
        assert env.run()[0] == "done"
        assert (other / "empty/deep").is_dir(), "清理空目录穿越了本应保留的junction"
    finally:
        if link.exists():
            link.rmdir()


@pytest.mark.parametrize("recycle_dir", [".", "..", "#recycle/..", "E:\\"])
def test_invalid_recycle_setting_is_rejected(recycle_dir):
    assert cleanup.recycle_path(r"E:\temporary\nas\Owner", recycle_dir) is None


def test_recycle_alias_cannot_purge_unbacked_source_files(env, monkeypatch):
    source, _job_id = prepare(env)
    write(source / "new.raw", b"unbacked")
    env.cfg.nas_recycle_dir = ".."
    assert cleanup.recycle_path(str(source), "..") is None  # 危险设置在计算阶段就被拒绝
    # 即使回收站位置算出来恰好是源自身（或源的上级），引擎也必须拒绝清空。
    for alias in (source, source.parent):
        monkeypatch.setattr(cleanup, "recycle_path", lambda src, folder, a=alias: str(a))
        assert env.run()[0] == "done"
        assert (source / "new.raw").exists(), "未备份的新文件被错误回收站路径删除"
        sql(env, "UPDATE jobs SET source_deleted_at=NULL")


def test_parent_replaced_with_junction_cannot_redirect_deletion(env, monkeypatch):
    source, _job_id = prepare(env, {"sub/a.raw": b"recorded"})
    other = env.base / "other"
    write(other / "a.raw", b"precious")
    stamp = (source / "sub/a.raw").stat().st_mtime
    os.utime(other / "a.raw", (stamp, stamp))
    original = cleanup.delete_files
    link = source / "sub"

    def redirect(src, candidates, should_stop, on_file):
        link.rename(source / "original_sub")
        junction(link, other, env.base)
        return original(src, candidates, should_stop, on_file)

    monkeypatch.setattr(cleanup, "delete_files", redirect)
    try:
        assert env.run()[0] == "done"
        assert (other / "a.raw").exists(), "候选文件的父目录变成junction后误删了外部数据"
    finally:
        if link.is_junction():
            link.rmdir()


def test_replaced_old_version_is_kept_after_real_incremental_backup(env):
    source = Path(env.make_source("Owner", {"a.raw": b"old"}))
    old_stamp = (source / "a.raw").stat().st_mtime
    old_id = enqueue(env, source)
    assert env.run()[0] == "done"
    write(source / "a.raw", b"new-version")
    new_id = enqueue(env, source)
    assert env.run()[0] == "done"
    assert env.file_row("a.raw", old_id)["status"] == "REPLACED"
    write(source / "a.raw", b"old")
    os.utime(source / "a.raw", (old_stamp, old_stamp))
    sql(env, "UPDATE jobs SET delete_source=1 WHERE id=?", new_id)
    assert env.run()[0] == "done"
    assert (source / "a.raw").read_bytes() == b"old"
    assert read(raw_media_path(str(env.drive), "Owner", "a.raw")) == b"new-version"
    assert_counts(env, new_id, 0, 0, 1)


@pytest.mark.parametrize("kind", ["raw", "txt"])
def test_same_owner_different_location_changed_mtime_gets_new_backup(env, kind):
    rel = f"a.{kind}"
    first = Path(env.make_source("Owner", {rel: b"first"}))
    enqueue(env, first)
    assert env.run()[0] == "done"
    second = env.base / "different/Owner"
    write(second / rel, b"other")
    stamp = (first / rel).stat().st_mtime + 10
    os.utime(second / rel, (stamp, stamp))
    job_id = enqueue(env, second, True)
    assert env.run()[0] == "done"
    assert tape_payload(env, env.file_row(rel, job_id)) == b"other"
    assert_counts(env, job_id, 1, 5, 0)


def test_same_owner_older_tar_history_cannot_authorize_other_source_delete(env):
    first = Path(env.make_source("Owner", {"a.txt": b"first"}))
    old_stamp = (first / "a.txt").stat().st_mtime
    enqueue(env, first)
    assert env.run()[0] == "done"
    write(first / "a.txt", b"latest-and-longer")
    enqueue(env, first)
    assert env.run()[0] == "done"
    assert len(env.rows("SELECT * FROM files WHERE status='ARCHIVED'")) == 2
    other = env.base / "different/Owner"
    write(other / "a.txt", b"other")
    os.utime(other / "a.txt", (old_stamp, old_stamp))
    enqueue(env, other, True)
    env.run()
    rows = env.rows("SELECT * FROM files WHERE status='ARCHIVED'")
    source_safe = (other / "a.txt").exists() and read(other / "a.txt") == b"other"
    assert source_safe or any(tape_payload(env, row) == b"other" for row in rows), "借旧tar记录删掉了新位置的不同内容"


@pytest.mark.parametrize("kind", ["raw", "txt"])
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="删除前磁带复核只检查RAW/tar大小，同大小损坏仍删除全部源文件")
def test_same_size_tape_corruption_blocks_source_deletion(env, monkeypatch, kind):
    source = Path(env.make_source("Owner", {"a.raw": b"raw-data", "b.txt": b"tar-data"}))
    job_id = enqueue(env, source, True)

    def damage(self, row):
        assert self.con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "DONE"
        record = self.con.execute("SELECT * FROM files WHERE job_id=? AND kind=?",
                                  (job_id, "raw" if kind == "raw" else "nonraw")).fetchone()
        path = raw_media_path(str(env.drive), "Owner", record["rel_path"]) if kind == "raw" else tar_media_path(
            str(env.drive), "Owner", record["tar_name"])
        with open(path, "r+b") as stream:
            first = stream.read(1)
            stream.seek(0)
            stream.write(bytes([first[0] ^ 255]))

    before_delete(monkeypatch, damage)
    status, _message = env.run()
    retained = files_under(source) == {"a.raw": 8, "b.txt": 8}
    assert status == "error" and retained, f"同大小磁带损坏后status={status}，源保留={retained}"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="源只复核大小/mtime，原地改写同大小并恢复mtime会删除未备份的新内容")
def test_same_size_source_rewrite_with_preserved_mtime_is_kept(env):
    source, job_id = prepare(env, {"a.raw": b"original"})
    stamp = (source / "a.raw").stat().st_mtime
    write(source / "a.raw", b"changed!")
    os.utime(source / "a.raw", (stamp, stamp))
    env.run()
    tape_bytes = tape_payload(env, env.file_row("a.raw", job_id))
    source_safe = (source / "a.raw").exists() and read(source / "a.raw") == b"changed!"
    assert source_safe or tape_bytes == b"changed!", "同大小/mtime的新内容被删，磁带只有旧内容"


def test_source_inside_staging_sibling_is_rejected_before_delete(env):
    source, job_id = prepare(env, {"a.raw": b"valuable"})
    env.cfg.staging_root = str(source.parent)
    status, _message = env.run()
    assert status == "error" and (source / "a.raw").exists(), "源位于中转根目录下却被删除"


def test_connect_migrates_old_schema_without_losing_records(tmp_path):
    path = tmp_path / "old.db"
    old_schema = """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        owner TEXT NOT NULL,
        source_path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'QUEUED',
        legacy INTEGER NOT NULL DEFAULT 0,
        scanned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        total_files INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        done_files INTEGER NOT NULL DEFAULT 0,
        done_bytes INTEGER NOT NULL DEFAULT 0,
        failed_files INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE batches (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs(id),
        seq INTEGER NOT NULL,
        tape_id TEXT,
        status TEXT NOT NULL DEFAULT 'STAGING',
        cleaned INTEGER NOT NULL DEFAULT 0,
        tar_name TEXT,
        tar_size INTEGER,
        tar_sha256 TEXT,
        bytes INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE TABLE files (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs(id),
        batch_id INTEGER REFERENCES batches(id),
        rel_path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime REAL,
        kind TEXT NOT NULL,
        sha256 TEXT,
        status TEXT NOT NULL DEFAULT 'PENDING',
        attempts INTEGER NOT NULL DEFAULT 0,
        tape_id TEXT,
        tar_name TEXT,
        error TEXT
    );
    """
    old = sqlite3.connect(str(path))
    old.row_factory = sqlite3.Row
    try:
        old.executescript(old_schema)
        old.execute(
            "INSERT INTO jobs VALUES "
            "(41, 'Owner', ?, 'DONE', 0, 1, '2020-01-01 01:02:03', "
            "'2020-01-01 01:03:00', '2020-01-01 01:04:00', 2, 11, 2, 11, 0)",
            (str(tmp_path / "nas" / "Owner"),),
        )
        old.execute(
            "INSERT INTO batches VALUES "
            "(71, 41, 1, 'OLD01', 'ARCHIVED', 1, 'batch.tar', 10240, "
            "'tar-hash', 11, '2020-01-01 01:03:00', '2020-01-01 01:04:00')"
        )
        old.executemany(
            "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (91, 41, 71, "a.raw", 5, 1234.5, "raw", "raw-hash",
                 "ARCHIVED", 1, "OLD01", None, None),
                (92, 41, 71, "notes.txt", 6, 1235.5, "nonraw", "text-hash",
                 "ARCHIVED", 2, "OLD01", "batch.tar", None),
            ],
        )
        old.commit()
        before = {
            table: [dict(row) for row in old.execute(
                f"SELECT * FROM {table} ORDER BY id"
            )]
            for table in ("jobs", "batches", "files")
        }
    finally:
        old.close()

    expected_new = {
        "jobs": {
            "delete_source": 0,
            "source_deleted_at": None,
            "deleted_files": 0,
            "deleted_bytes": 0,
            "kept_files": 0,
        },
        "batches": {"tar_written": None},
        "files": {"written": None},
    }
    con = db.connect(str(path))
    try:
        after = {}
        for table in ("jobs", "batches", "files"):
            rows = [dict(row) for row in con.execute(
                f"SELECT * FROM {table} ORDER BY id"
            )]
            assert len(rows) == len(before[table])
            for original, migrated in zip(before[table], rows):
                assert migrated == original | expected_new[table]
            after[table] = rows
        assert db.pending_deletions(con) == []
        assert db.queue_jobs(con) == []
        assert len(db.files_of_job(con, 41)) == 2
    finally:
        con.close()

    # 二次打开检验迁移幂等和提交持久化。
    con = db.connect(str(path))
    try:
        for table, expected in after.items():
            assert [
                dict(row)
                for row in con.execute(f"SELECT * FROM {table} ORDER BY id")
            ] == expected
    finally:
        con.close()


def test_pending_deletions_and_queue_jobs_filter_and_order(tmp_path):
    con = db.connect(str(tmp_path / "queue.db"))
    try:
        def add(status, delete_source=False, legacy=0, deleted_at=None):
            job_id = db.add_job(
                con, "Owner", str(tmp_path / f"source_{status}"),
                delete_source=delete_source,
            )
            con.execute(
                "UPDATE jobs SET status=?, legacy=?, source_deleted_at=? WHERE id=?",
                (status, legacy, deleted_at, job_id),
            )
            con.commit()
            return job_id

        queued1 = add("QUEUED")
        running1 = add("RUNNING", True)
        pending1 = add("DONE", True)
        add("DONE", False)
        add("DONE", True, deleted_at="2020-01-01 00:00:00")
        add("DONE", True, legacy=1)
        add("CANCELLED", True)
        add("QUEUED", True, legacy=1)
        add("RUNNING", True, legacy=1)
        queued2 = add("QUEUED", True)
        pending2 = add("DONE", True)
        running2 = add("RUNNING")

        assert [row["id"] for row in db.pending_deletions(con)] == [
            pending1, pending2,
        ]
        assert [row["id"] for row in db.queue_jobs(con)] == [
            pending1, pending2, running1, running2, queued1, queued2,
        ]
        db.set_delete_source(con, pending1, False)
        assert [row["id"] for row in db.pending_deletions(con)] == [pending2]
        db.set_delete_source(con, pending1, True)
        assert [row["id"] for row in db.pending_deletions(con)] == [
            pending1, pending2,
        ]
    finally:
        con.close()


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "DONE", "CANCELLED"])
def test_set_delete_source_switches_undeleted_jobs_and_commits(tmp_path, status):
    path = str(tmp_path / "switch.db")
    con = db.connect(path)
    try:
        job_id = db.add_job(con, "Owner", str(tmp_path / "source"))
        con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        con.commit()
        db.set_delete_source(con, job_id, True)
        assert con.execute(
            "SELECT delete_source FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0] == 1
    finally:
        con.close()

    con = db.connect(path)
    try:
        assert con.execute(
            "SELECT delete_source FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0] == 1
        db.set_delete_source(con, job_id, False)
        assert con.execute(
            "SELECT delete_source FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0] == 0
    finally:
        con.close()


@pytest.mark.parametrize("initial", [False, True])
def test_set_delete_source_preserves_completed_deletion_and_missing_id(
    tmp_path, initial,
):
    con = db.connect(str(tmp_path / "completed.db"))
    try:
        job_id = db.add_job(
            con, "Owner", str(tmp_path / "source"), delete_source=initial,
        )
        con.execute(
            "UPDATE jobs SET status='DONE', source_deleted_at=?, "
            "deleted_files=3, deleted_bytes=123, kept_files=1 WHERE id=?",
            ("2020-01-01 00:00:00", job_id),
        )
        con.commit()
        before = dict(con.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone())
        db.set_delete_source(con, job_id, not initial)
        db.set_delete_source(con, job_id + 1000, True)
        assert dict(con.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()) == before
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        con.close()
