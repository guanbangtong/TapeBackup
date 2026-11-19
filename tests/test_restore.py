"""Restore tests. The "tape" is produced by the real Engine into a temp folder; nothing touches real drives."""

from __future__ import annotations

import dataclasses
import os
import random
import shutil
import tarfile
from types import SimpleNamespace

import pytest

from tape_backup import db, restore
from tape_backup.config import Config
from tape_backup.engine import Engine, raw_media_path, tar_media_path
from tape_backup.restore import Restorer, plan_tapes

OWNER = "LiHua"


def blob(seed: int, size: int) -> bytes:
    return random.Random(seed).randbytes(size)


# Relative path (forward slashes) -> content. .raw files go to tape as-is, everything else into a tar.
SAMPLE = {
    "a.raw": blob(1, 5000),
    "sub/deep/b.raw": blob(2, 20000),
    "数据/实验一.raw": blob(3, 3000),
    "empty.raw": b"",
    "notes.txt": blob(4, 1500),
    "图像/结果 图.tif": blob(5, 30000),
    "x/y/z/deep.dat": blob(6, 2500),
    "empty.txt": b"",
}


# ---------------------------------------------------------------- helpers

def make_cfg(tmp_path) -> Config:
    return Config(
        staging_root=str(tmp_path / "staging"),
        tape_root=str(tmp_path / "tape"),
        data_dir=str(tmp_path / "data"),
        reserve_staging_gb=0,
        reserve_tape_gb=0,
        threads=2,
    )


def write_tree(root, files: dict[str, bytes]):
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def backup(cfg: Config, owner: str, source, tape_id: str, tape_dir) -> None:
    """Back a source folder up with the real engine onto the folder tape_dir (a pretend tape)."""
    os.makedirs(tape_dir, exist_ok=True)
    cfg.tape_root = str(tape_dir)
    con = db.connect(cfg.db_path)
    try:
        db.add_job(con, owner, str(source))
    finally:
        con.close()
    result, message = Engine(cfg, ask_tape=lambda reason, suggested: None, retry_delay=0).run(tape_id)
    assert result == "done", message


def archived_rows(cfg: Config, **kwargs):
    con = db.connect(cfg.db_path)
    try:
        return db.search_files(con, **kwargs)
    finally:
        con.close()


def insert(drive, tape_dir) -> None:
    """Put a pretend tape into the pretend drive."""
    if os.path.exists(drive):
        shutil.rmtree(drive)
    shutil.copytree(tape_dir, drive)


def no_ask(tape_id, reason):
    raise AssertionError(f"the tape should already be inserted, but asked for {tape_id}: {reason}")


def dest_files(dest) -> set[str]:
    """Every file under dest as a forward-slash path relative to dest."""
    return {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}


@pytest.fixture
def world(tmp_path):
    """One owner backed up to tape TAPE01; the tape is also inserted into the drive."""
    cfg = make_cfg(tmp_path)
    source = write_tree(tmp_path / "source", SAMPLE)
    backup(cfg, OWNER, source, "TAPE01", tmp_path / "tape01")
    drive = tmp_path / "drive"
    insert(drive, tmp_path / "tape01")
    rcfg = dataclasses.replace(cfg, tape_root=str(drive))
    return SimpleNamespace(
        cfg=cfg, rcfg=rcfg, drive=drive, tape=tmp_path / "tape01", dest=tmp_path / "dest", rows=archived_rows(cfg)
    )


def restore_all(w, rows=None, ask_tape=no_ask):
    r = Restorer(w.rcfg, w.rows if rows is None else rows, str(w.dest), ask_tape=ask_tape)
    return r, r.run()


def rows_named(w, *names):
    wanted = {n.replace("/", os.sep) for n in names}
    rows = [r for r in w.rows if r["rel_path"] in wanted]
    assert len(rows) == len(wanted)
    return rows


# ---------------------------------------------------------------- tests

def test_sample_is_split_into_raw_and_tar(world):
    kinds = {r["rel_path"].replace(os.sep, "/"): r["kind"] for r in world.rows}
    assert set(kinds) == set(SAMPLE)
    assert {k for k, v in kinds.items() if v == "raw"} == {"a.raw", "sub/deep/b.raw", "数据/实验一.raw", "empty.raw"}


def test_full_restore_is_byte_identical(world, tmp_path):
    logs = []
    r = Restorer(world.rcfg, world.rows, str(world.dest), ask_tape=no_ask, on_log=logs.append)
    assert r.run()[0] == "done"
    assert r.failures == []
    assert r.restored == len(SAMPLE)
    assert dest_files(world.dest) == {f"{OWNER}/{rel}" for rel in SAMPLE}
    for rel, data in SAMPLE.items():
        restored = world.dest / OWNER / rel
        assert restored.read_bytes() == data, rel
        assert abs(restored.stat().st_mtime - (tmp_path / "source" / rel).stat().st_mtime) < 2, rel
    snap = r.progress.snapshot()
    assert snap["done_files"] == snap["total_files"] == len(SAMPLE)
    assert snap["done_bytes"] == snap["total_bytes"] == sum(len(d) for d in SAMPLE.values())
    assert snap["tape_id"] == "TAPE01"
    assert not any("未校验" in line for line in logs)


def test_restore_accepts_plain_dict_rows(world):
    rows = [dict(r) for r in world.rows]
    _r, (result, _msg) = restore_all(world, rows)
    assert result == "done"
    assert (world.dest / OWNER / "a.raw").read_bytes() == SAMPLE["a.raw"]


def test_restore_subset_of_tar_members(world):
    rows = rows_named(world, "notes.txt", "x/y/z/deep.dat")
    r, (result, _msg) = restore_all(world, rows)
    assert result == "done" and r.failures == []
    assert r.restored == 2
    assert dest_files(world.dest) == {f"{OWNER}/notes.txt", f"{OWNER}/x/y/z/deep.dat"}
    assert (world.dest / OWNER / "notes.txt").read_bytes() == SAMPLE["notes.txt"]


def test_tar_reading_stops_once_every_wanted_member_is_found(world):
    # Cut the tar off right after its second member (notes.txt). Asking for that member must still
    # succeed, which proves nothing after it was read.
    row = rows_named(world, "notes.txt")[0]
    tar_path = tar_media_path(str(world.drive), OWNER, row["tar_name"])
    with tarfile.open(tar_path) as tar:
        members = tar.getmembers()
    assert members[1].name == f"{OWNER}/notes.txt"
    with open(tar_path, "r+b") as f:
        f.truncate(members[1].offset_data + ((members[1].size + 511) // 512) * 512)
    r, (result, _msg) = restore_all(world, [row])
    assert result == "done" and r.failures == []
    assert (world.dest / OWNER / "notes.txt").read_bytes() == SAMPLE["notes.txt"]


def test_corrupted_raw_file_is_detected_and_not_left_behind(world):
    victim = raw_media_path(str(world.drive), OWNER, "sub/deep/b.raw".replace("/", os.sep))
    data = bytearray(SAMPLE["sub/deep/b.raw"])
    data[100] ^= 0xFF  # same size, different bytes
    with open(victim, "wb") as f:
        f.write(data)

    r, (result, _msg) = restore_all(world)
    assert result == "done"
    assert [(o, p.replace(os.sep, "/")) for o, p, _ in r.failures] == [(OWNER, "sub/deep/b.raw")]
    assert "校验" in r.failures[0][2]
    assert r.restored == len(SAMPLE) - 1
    assert dest_files(world.dest) == {f"{OWNER}/{rel}" for rel in SAMPLE if rel != "sub/deep/b.raw"}


def test_corrupted_tar_member_is_detected_and_not_left_behind(world):
    row = rows_named(world, "图像/结果 图.tif")[0]
    tar_path = tar_media_path(str(world.drive), OWNER, row["tar_name"])
    with tarfile.open(tar_path) as tar:
        member = tar.getmember("LiHua/图像/结果 图.tif")
    with open(tar_path, "r+b") as f:
        f.seek(member.offset_data + 10)
        byte = f.read(1)
        f.seek(member.offset_data + 10)
        f.write(bytes([byte[0] ^ 0xFF]))

    r, (result, _msg) = restore_all(world)
    assert result == "done"
    assert [p.replace(os.sep, "/") for _o, p, _ in r.failures] == ["图像/结果 图.tif"]
    assert dest_files(world.dest) == {f"{OWNER}/{rel}" for rel in SAMPLE if rel != "图像/结果 图.tif"}


def test_bad_checksum_keeps_the_existing_destination_file(world):
    old = world.dest / OWNER / "a.raw"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old version")
    victim = raw_media_path(str(world.drive), OWNER, "a.raw")
    with open(victim, "wb") as f:
        f.write(b"\0" * len(SAMPLE["a.raw"]))
    r, _ = restore_all(world, rows_named(world, "a.raw"))
    assert len(r.failures) == 1
    assert old.read_bytes() == b"old version"
    assert dest_files(world.dest) == {f"{OWNER}/a.raw"}


def test_existing_destination_files_are_overwritten(world):
    old = world.dest / OWNER / "a.raw"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old version")
    r, (result, _msg) = restore_all(world, rows_named(world, "a.raw"))
    assert result == "done" and r.failures == []
    assert old.read_bytes() == SAMPLE["a.raw"]


def test_missing_raw_file_fails_only_that_file(world):
    os.remove(raw_media_path(str(world.drive), OWNER, "a.raw"))
    r, (result, _msg) = restore_all(world)
    assert result == "done"
    assert [(o, p) for o, p, _ in r.failures] == [(OWNER, "a.raw")]
    assert "找不到" in r.failures[0][2]
    assert r.restored == len(SAMPLE) - 1
    assert dest_files(world.dest) == {f"{OWNER}/{rel}" for rel in SAMPLE if rel != "a.raw"}


def test_missing_tar_fails_its_members_but_not_raw_files(world):
    tar_row = rows_named(world, "notes.txt")[0]
    os.remove(tar_media_path(str(world.drive), OWNER, tar_row["tar_name"]))
    r, (result, _msg) = restore_all(world)
    assert result == "done"
    assert len(r.failures) == 4  # the four non-raw files
    assert dest_files(world.dest) == {f"{OWNER}/{rel}" for rel in SAMPLE if rel.endswith(".raw")}


def test_member_missing_from_tar_is_a_failure(world):
    ghost = dict(rows_named(world, "notes.txt")[0], rel_path="not_in_tar.txt")
    r, (result, _msg) = restore_all(world, [ghost] + rows_named(world, "notes.txt"))
    assert result == "done"
    assert [p for _o, p, _ in r.failures] == ["not_in_tar.txt"]
    assert r.restored == 1


def test_record_without_checksum_is_restored_and_logged_as_unverified(world):
    logs = []
    row = dict(rows_named(world, "a.raw")[0], sha256=None)
    r = Restorer(world.rcfg, [row], str(world.dest), ask_tape=no_ask, on_log=logs.append)
    assert r.run()[0] == "done"
    assert r.restored == 1 and r.failures == []
    assert any("未校验" in line and "a.raw" in line for line in logs)


def test_path_leaving_the_destination_is_refused(world, tmp_path):
    evil = dict(rows_named(world, "a.raw")[0], rel_path="..\\..\\evil.raw")
    r, (result, _msg) = restore_all(world, [evil])
    assert result == "done"
    assert len(r.failures) == 1 and "不安全" in r.failures[0][2]
    assert not (tmp_path / "evil.raw").exists()
    assert not (world.dest.parent / "evil.raw").exists()


def test_unusable_destination_is_an_error(world, tmp_path):
    blocker = tmp_path / "iam_a_file"
    blocker.write_bytes(b"x")
    r = Restorer(world.rcfg, world.rows, str(blocker / "sub"), ask_tape=no_ask)
    result, message = r.run()
    assert result == "error" and message


def test_wrong_tape_is_replaced_through_ask_tape(world):
    shutil.rmtree(world.drive)
    world.drive.mkdir()
    (world.drive / "TAPE_ID_OTHER.txt").write_bytes(b"")
    calls = []

    def ask(tape_id, reason):
        calls.append((tape_id, reason))
        insert(world.drive, world.tape)  # the user swaps in the right tape
        return True

    r, (result, _msg) = restore_all(world, ask_tape=ask)
    assert result == "done" and r.failures == []
    assert r.restored == len(SAMPLE)
    assert len(calls) == 1
    assert calls[0][0] == "TAPE01" and "OTHER" in calls[0][1]


def test_missing_tape_drive_asks_until_inserted(world):
    shutil.rmtree(world.drive)  # nothing in the drive
    calls = []

    def ask(tape_id, reason):
        calls.append(reason)
        if len(calls) == 2:  # the user clicks continue once without doing anything, then inserts
            insert(world.drive, world.tape)
        return True

    r, (result, _msg) = restore_all(world, ask_tape=ask)
    assert result == "done" and r.restored == len(SAMPLE)
    assert len(calls) == 2 and "找不到" in calls[0]


def test_tape_without_marker_needs_user_confirmation(world):
    os.remove(world.drive / "TAPE_ID_TAPE01.txt")
    calls = []

    def ask(tape_id, reason):
        calls.append(reason)
        return True

    r, (result, _msg) = restore_all(world, ask_tape=ask)
    assert result == "done" and r.restored == len(SAMPLE)
    assert len(calls) == 1 and "没有磁带号" in calls[0]


def test_ask_tape_returning_false_stops(world):
    shutil.rmtree(world.drive)
    calls = []

    def ask(tape_id, reason):
        calls.append(tape_id)
        return False

    r, (result, message) = restore_all(world, ask_tape=ask)
    assert (result, message) == ("stopped", "")
    assert calls == ["TAPE01"]
    assert r.restored == 0
    assert not world.dest.exists() or dest_files(world.dest) == set()


def test_without_ask_tape_a_wrong_tape_stops(world):
    shutil.rmtree(world.drive)
    r = Restorer(world.rcfg, world.rows, str(world.dest))
    assert r.run() == ("stopped", "")


def test_two_owners_in_one_restore(tmp_path):
    cfg = make_cfg(tmp_path)
    second = {"p/q.raw": blob(7, 4000), "r.doc": blob(8, 900)}
    backup(cfg, "LiHua", write_tree(tmp_path / "src1", SAMPLE), "TAPE01", tmp_path / "tape01")
    backup(cfg, "ZhaoLiu", write_tree(tmp_path / "src2", second), "TAPE01", tmp_path / "tape01")
    rows = archived_rows(cfg)
    assert len(rows) == len(SAMPLE) + len(second)
    drive = tmp_path / "drive"
    insert(drive, tmp_path / "tape01")

    r = Restorer(dataclasses.replace(cfg, tape_root=str(drive)), rows, str(tmp_path / "dest"), ask_tape=no_ask)
    assert r.run()[0] == "done"
    assert r.failures == [] and r.restored == len(rows)
    for rel, data in SAMPLE.items():
        assert (tmp_path / "dest" / "LiHua" / rel).read_bytes() == data
    for rel, data in second.items():
        assert (tmp_path / "dest" / "ZhaoLiu" / rel).read_bytes() == data


def test_files_spread_over_two_tapes(tmp_path):
    cfg = make_cfg(tmp_path)
    second = {"p/q.raw": blob(7, 4000), "r.doc": blob(8, 900)}
    backup(cfg, "LiHua", write_tree(tmp_path / "src1", SAMPLE), "TAPE01", tmp_path / "tape01")
    backup(cfg, "ZhaoLiu", write_tree(tmp_path / "src2", second), "TAPE02", tmp_path / "tape02")
    rows = archived_rows(cfg)
    assert plan_tapes(rows) == [("TAPE01", len(SAMPLE)), ("TAPE02", len(second))]

    drive = tmp_path / "drive"  # starts empty: the first tape must be asked for
    rcfg = dataclasses.replace(cfg, tape_root=str(drive))
    asked = []

    def ask(tape_id, reason):
        asked.append(tape_id)
        insert(drive, tmp_path / tape_id.lower())
        return True

    r = Restorer(rcfg, rows, str(tmp_path / "dest"), ask_tape=ask)
    assert r.run()[0] == "done"
    assert asked == ["TAPE01", "TAPE02"]
    assert r.failures == [] and r.restored == len(rows)
    assert (tmp_path / "dest" / "ZhaoLiu" / "r.doc").read_bytes() == second["r.doc"]
    assert (tmp_path / "dest" / "LiHua" / "a.raw").read_bytes() == SAMPLE["a.raw"]


def test_plan_tapes_puts_missing_tape_id_last():
    files = [{"tape_id": None}, {"tape_id": "B"}, {"tape_id": "a"}, {"tape_id": "B"}]
    assert plan_tapes(files) == [("A", 1), ("B", 2), ("", 1)]


@pytest.mark.parametrize("name", ["sub/deep/b.raw", "图像/结果 图.tif"])  # one raw file, one tar member
def test_stop_mid_file_leaves_no_partial_file(world, monkeypatch, name):
    monkeypatch.setattr(restore, "TAPE_CHUNK", 1000)  # several chunks per file
    r = Restorer(world.rcfg, rows_named(world, name), str(world.dest), ask_tape=no_ask)
    original_add = restore.Progress.add

    def add_then_stop(self, n):
        original_add(self, n)
        if n > 0:
            r.stop()  # as if the user clicked the stop button after the first chunk

    monkeypatch.setattr(restore.Progress, "add", add_then_stop)
    assert r.run() == ("stopped", "")
    assert r.restored == 0
    assert dest_files(world.dest) == set()  # no file and no .part leftovers


# ---------------------------------------------------------------- staging, folders, safety rules

def test_restore_goes_through_staging_and_leaves_it_empty(world):
    r, (result, message) = restore_all(world)
    assert result == "done", message
    assert dest_files(world.dest) == {f"{OWNER}/{rel}" for rel in SAMPLE}
    assert not os.path.exists(r.staging_dir)  # the staging copies are gone once delivered


def test_folder_restore_lands_under_the_backed_up_folder_name(world):
    from tape_backup.ui_restore_page import build_tree, restore_rows

    con = db.connect(world.cfg.db_path)
    try:
        (root,) = build_tree(db.owner_files(con, OWNER.lower()))  # names match case-insensitively
    finally:
        con.close()
    assert root.count == len(SAMPLE) and root.size == sum(len(d) for d in SAMPLE.values())
    con = db.connect(world.cfg.db_path)
    try:
        files = restore_rows(con, [root.children["sub"]])  # tick the folder "sub"
    finally:
        con.close()

    r = Restorer(world.rcfg, files, str(world.dest), ask_tape=no_ask)
    assert r.run()[0] == "done"
    assert dest_files(world.dest) == {"source/sub/deep/b.raw"}
    assert (world.dest / "source" / "sub" / "deep" / "b.raw").read_bytes() == SAMPLE["sub/deep/b.raw"]


def test_only_the_newest_backup_of_a_file_is_restored(world):
    (row,) = rows_named(world, "a.raw")
    older = dict(row, archived_at="2000-01-01 00:00:00", sha256="0" * 64)  # would fail its check if chosen
    r = Restorer(world.rcfg, [older, row], str(world.dest), ask_tape=no_ask)
    assert r.older_versions == 1
    assert r.run()[0] == "done"
    assert r.failures == [] and r.restored == 1
    assert (world.dest / OWNER / "a.raw").read_bytes() == SAMPLE["a.raw"]


def test_refuses_a_destination_inside_a_source_that_will_be_deleted(world, tmp_path):
    nas = tmp_path / "nas" / OWNER
    nas.mkdir(parents=True)
    con = db.connect(world.cfg.db_path)
    try:
        db.add_job(con, OWNER, str(nas), delete_source=True)  # queued, "delete after backup" ticked
    finally:
        con.close()
    r = Restorer(world.rcfg, world.rows, str(tmp_path / "nas"), ask_tape=no_ask)  # files would land in nas\LiHua
    result, message = r.run()
    assert result == "error" and "备份后删除" in message
    assert dest_files(tmp_path / "nas") == set()


def test_refuses_when_the_staging_disk_is_too_small(world, monkeypatch):
    monkeypatch.setattr(restore.shutil, "disk_usage", lambda path: SimpleNamespace(free=10, total=10, used=0))
    result, message = restore_all(world)[1]
    assert result == "error" and "空间不够" in message
    assert not world.dest.exists() or dest_files(world.dest) == set()


def test_backup_and_restore_never_use_the_drive_at_the_same_time(world):
    from tape_backup.engine import TAPE_DRIVE

    with TAPE_DRIVE:  # as if a backup were running
        result, message = restore_all(world)[1]
        assert result == "error" and "正在备份" in message
        assert Engine(world.cfg, ask_tape=lambda *a: None).run("TAPE01")[0] == "error"
    assert restore_all(world)[1][0] == "done"  # free again afterwards
