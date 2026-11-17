"""Independent tests for the tape-backup core: engine, copier and db.

Every test works in pytest's tmp_path: folders stand in for the NAS, the staging disk,
the tape drive (plus a "shelf" of ejected tapes) and the data dir. Small disks are
simulated through Engine(disk_free=...); crashes are simulated by raising an exception
that is neither Stopped nor EngineError from a patched name inside tape_backup.engine.

Tests marked xfail(strict=True) document suspected bugs in the core: they assert the
behaviour a backup program should have and currently fail.
"""

from __future__ import annotations

import builtins
import csv
import hashlib
import os
import random
import shutil
import sqlite3
import sys
import tarfile
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape_backup import copier, db  # noqa: E402
from tape_backup import engine as eng  # noqa: E402
from tape_backup.config import Config  # noqa: E402
from tape_backup.copier import CopyError, Stopped, TarWriter, copy_file, hash_file, long_path, with_retry  # noqa: E402
from tape_backup.engine import (  # noqa: E402
    Engine,
    raw_media_path,
    read_tape_marker,
    tar_media_path,
    tar_member_name,
)

BIG = 10 ** 12


# ====================================================================== file helpers

def lp(path) -> str:
    """Long-path form, so the tests themselves never trip over MAX_PATH."""
    p = str(path)
    if p.startswith("\\\\?\\"):
        return p
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    # no abspath() on absolute paths: it would strip a trailing '.' or ' ' from the name
    return "\\\\?\\" + p.replace("/", "\\")


def norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def under(path, root) -> bool:
    return norm(path).startswith(norm(root) + os.sep)


def blob(rel: str, n: int, salt: str = "") -> bytes:
    return random.Random(f"{rel}|{n}|{salt}").randbytes(n)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path, data: bytes) -> None:
    os.makedirs(lp(os.path.dirname(os.path.abspath(str(path)))), exist_ok=True)
    with open(lp(path), "wb") as f:
        f.write(data)


def read(path) -> bytes:
    with open(lp(path), "rb") as f:
        return f.read()


def exists(path) -> bool:
    return os.path.exists(lp(path))


def files_under(root) -> dict[str, int]:
    """rel path -> size of every file under root (long paths included)."""
    root_lp = lp(root)
    out: dict[str, int] = {}
    if not os.path.isdir(root_lp):
        return out
    for dirpath, _dirs, names in os.walk(root_lp):
        for name in names:
            full = os.path.join(dirpath, name)
            out[full[len(root_lp) + 1:]] = os.stat(full).st_size
    return out


def dir_bytes(root) -> int:
    return sum(files_under(root).values())


def flip_byte(path, offset: int) -> None:
    with open(lp(path), "r+b") as f:
        f.seek(offset)
        b = f.read(1)
        f.seek(offset)
        f.write(bytes([b[0] ^ 0xFF]))


class Crash(Exception):
    """Stands in for a power loss / process kill: neither Stopped nor EngineError."""


# ====================================================================== environment

class Env:
    def __init__(self, tmp_path):
        self.base = tmp_path
        self.nas = tmp_path / "nas"
        self.staging = tmp_path / "staging"
        self.drive = tmp_path / "tape"
        self.shelf = tmp_path / "shelf"
        self.data = tmp_path / "data"
        for d in (self.nas, self.drive, self.shelf):
            d.mkdir()
        self.cfg = Config(
            staging_root=str(self.staging),
            tape_root=str(self.drive),
            data_dir=str(self.data),
            reserve_staging_gb=0,
            reserve_tape_gb=0,
            threads=4,
            raw_extensions=[".raw"],
            db_backup_dir="",
        )
        self.stage_cap: int | None = None   # simulated capacity of the staging disk
        self.tape_cap: int | None = None    # simulated capacity of every tape
        self.logs: list[str] = []
        self._blank = 0

    # ---- disks
    def disk_free(self, path: str) -> int:
        if norm(path) == norm(self.staging) and self.stage_cap is not None:
            return self.stage_cap - dir_bytes(self.staging)
        if norm(path) == norm(self.drive) and self.tape_cap is not None:
            return self.tape_cap - dir_bytes(self.drive)
        return BIG

    def staging_files(self) -> dict[str, int]:
        return files_under(self.staging)

    # ---- engine
    def engine(self, ask_tape=None, on_log=None, **kw) -> Engine:
        def log(line: str) -> None:
            self.logs.append(line)
            if on_log:
                on_log(line)

        return Engine(self.cfg, on_log=log, ask_tape=ask_tape, disk_free=self.disk_free, retry_delay=0, **kw)

    def run(self, tape="T1", ask=None, on_log=None):
        return self.engine(ask_tape=ask, on_log=on_log).run(tape)

    # ---- source / jobs
    def make_source(self, owner: str, spec: dict[str, bytes]) -> str:
        src = self.nas / owner
        src.mkdir(exist_ok=True)
        for rel, data in spec.items():
            write(src / rel, data)
        return str(src)

    def src(self, owner: str, rel: str) -> str:
        return os.path.join(str(self.nas / owner), rel)

    def add_job(self, owner: str, src: str | None = None) -> int:
        con = db.connect(self.cfg.db_path)
        try:
            return db.add_job(con, owner, src or str(self.nas / owner))
        finally:
            con.close()

    def rows(self, sql: str, *args) -> list[dict]:
        con = db.connect(self.cfg.db_path)
        try:
            return [dict(r) for r in con.execute(sql, args)]
        finally:
            con.close()

    def one(self, sql: str, *args) -> dict:
        rows = self.rows(sql, *args)
        assert len(rows) == 1, rows
        return rows[0]

    def file_row(self, rel: str, job_id: int | None = None) -> dict:
        if job_id is None:
            return self.one("SELECT * FROM files WHERE rel_path=?", rel)
        return self.one("SELECT * FROM files WHERE rel_path=? AND job_id=?", rel, job_id)

    # ---- tapes
    def marker(self) -> str | None:
        return read_tape_marker(str(self.drive)) if self.drive.is_dir() else None

    def eject(self) -> None:
        """Take the tape out of the drive and put it on the shelf (under its marker)."""
        if not self.drive.is_dir():
            self.drive.mkdir()
            return
        label = self.marker()
        if label is None:
            if not any(self.drive.iterdir()):
                return
            self._blank += 1
            label = f"_UNLABELED{self._blank}"
        dest = self.shelf / label
        assert not dest.exists(), f"two tapes labelled {label}"
        shutil.move(str(self.drive), str(dest))
        self.drive.mkdir()

    def insert(self, tape_id: str) -> None:
        """Eject the current tape and insert tape_id from the shelf (a blank one if it does not exist)."""
        self.eject()
        stored = self.shelf / tape_id
        if stored.is_dir():
            self.drive.rmdir()
            shutil.move(str(stored), str(self.drive))

    def tapes(self) -> dict[str, str]:
        out = {p.name: str(p) for p in self.shelf.iterdir() if p.is_dir()}
        m = self.marker()
        if m:
            out[m] = str(self.drive)
        return out

    def tape_folder(self, tape_id: str) -> str:
        t = self.tapes()
        assert tape_id in t, f"tape {tape_id} not in drive or on shelf ({sorted(t)})"
        return t[tape_id]

    def fill_tape(self, nbytes: int, name="junk.bin") -> None:
        write(self.drive / name, b"\0" * nbytes)


def swap_to(env: Env, *ids, calls: list | None = None):
    """ask_tape that inserts the next tape of `ids` (blank if new) and answers its id; None ends."""
    it = iter(ids)

    def ask(reason, suggested):
        if calls is not None:
            calls.append((reason, suggested))
        tid = next(it, None)
        if tid is not None:
            env.insert(tid)
        return tid

    return ask


def patch_copy(monkeypatch, hook):
    """Replace engine.copy_file with hook(real, src, dst, kw)."""
    real = eng.copy_file

    def wrapper(src, dst, **kw):
        return hook(real, src, dst, kw)

    monkeypatch.setattr(eng, "copy_file", wrapper)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ====================================================================== verification

def tape_content(env: Env, row: dict, owner: str) -> bytes | None:
    folder = env.tape_folder(row["tape_id"])
    if row["kind"] == "raw":
        path = raw_media_path(folder, owner, row["rel_path"])
        return read(path) if exists(path) else None
    path = tar_media_path(folder, owner, row["tar_name"])
    if not exists(path):
        return None
    with tarfile.open(lp(path), "r:") as tar:
        try:
            member = tar.getmember(tar_member_name(owner, row["rel_path"]))
        except KeyError:
            return None
        return tar.extractfile(member).read()


def check_backup(env: Env, owner: str, expected: dict[str, bytes], *, failed=(), strict_tape=True) -> None:
    """Everything in `expected` is ARCHIVED exactly once with the right sha, and the tape really holds it."""
    rows = env.rows(
        "SELECT f.*, j.owner AS owner FROM files f JOIN jobs j ON j.id=f.job_id "
        "WHERE j.owner=? COLLATE NOCASE AND j.legacy=0",
        owner,
    )
    archived = [r for r in rows if r["status"] == "ARCHIVED"]
    for rel, data in expected.items():
        cand = [r for r in archived if r["rel_path"].lower() == rel.lower() and r["size"] == len(data)]
        assert len(cand) == 1, f"{rel}: expected exactly one ARCHIVED record of size {len(data)}, got {cand}"
        r = cand[0]
        assert r["sha256"] == sha(data), f"{rel}: sha in database is not the sha of the source"
        assert (r["tar_name"] is None) == (r["kind"] == "raw"), r
        content = tape_content(env, r, r["owner"])
        assert content is not None, f"{rel}: recorded on tape {r['tape_id']} but not there"
        assert content == data, f"{rel}: bytes on tape differ from the source"
    for rel in failed:
        assert any(r["rel_path"] == rel and r["status"] == "FAILED" for r in rows), f"{rel} should be FAILED"

    # every ARCHIVED/REPLACED record of this owner points at something real
    for r in archived:
        content = tape_content(env, r, r["owner"])
        assert content is not None and sha(content) == r["sha256"], f"ARCHIVED record is wrong on tape: {r}"

    # nothing in flight, nothing duplicated
    assert not [r for r in rows if r["status"] in ("PENDING", "STAGED")], "files left PENDING/STAGED"
    seen = set()
    for r in rows:
        key = (r["job_id"], r["rel_path"].lower())
        assert key not in seen, f"duplicate file row {key}"
        seen.add(key)
    jobs = env.rows("SELECT * FROM jobs WHERE owner=? COLLATE NOCASE AND legacy=0 AND status!='CANCELLED'", owner)
    for job in jobs:
        assert job["status"] == "DONE", job
        mine = [r for r in rows if r["job_id"] == job["id"]]
        done = [r for r in mine if r["status"] in ("ARCHIVED", "REPLACED")]
        assert job["done_files"] == len(done), job
        assert job["done_bytes"] == sum(r["size"] for r in done), job
        assert job["failed_files"] == sum(1 for r in mine if r["status"] == "FAILED"), job
        for b in env.rows("SELECT * FROM batches WHERE job_id=?", job["id"]):
            assert b["status"] == "ARCHIVED" and b["cleaned"] == 1, b
    assert env.staging_files() == {}, f"staging not empty: {env.staging_files()}"

    if strict_tape:
        # what is physically on the tapes == what is recorded, nothing twice
        for tape_id, folder in env.tapes().items():
            arch = os.path.join(folder, eng.archive_dir_name(owner))
            raw_on_tape = {k.lower() for k in files_under(os.path.join(arch, owner))}
            raw_recorded = {
                r["rel_path"].lower() for r in rows
                if r["kind"] == "raw" and r["tape_id"] == tape_id and r["status"] in ("ARCHIVED", "REPLACED")
            }
            assert raw_on_tape == raw_recorded, f"tape {tape_id}: raw files on tape {raw_on_tape} != recorded {raw_recorded}"
            tars_on_tape = {k for k in files_under(arch) if os.sep not in k}
            tars_recorded = {
                b["tar_name"] for b in env.rows(
                    "SELECT b.tar_name FROM batches b JOIN jobs j ON j.id=b.job_id "
                    "WHERE j.owner=? COLLATE NOCASE AND b.status='ARCHIVED' AND b.tape_id=? AND b.tar_name IS NOT NULL",
                    owner, tape_id,
                )
            }
            assert tars_on_tape == tars_recorded, f"tape {tape_id}: tars {tars_on_tape} != recorded {tars_recorded}"
        members = []
        for tape_id, folder in env.tapes().items():
            arch = os.path.join(folder, eng.archive_dir_name(owner))
            for name in files_under(arch):
                if os.sep not in name and name.endswith(".tar"):
                    with tarfile.open(lp(os.path.join(arch, name)), "r:") as tar:
                        members += [(m.name, m.size) for m in tar]
        assert len(members) == len(set(members)), "a non-raw file is in more than one tar"


# ====================================================================== sample data

LONG_DIR = "long\\" + "x" * 110 + "\\" + "y" * 60


def sample_spec() -> dict[str, bytes]:
    spec = {
        "a.raw": 50_000,
        "sub\\b.raw": 30_001,
        "sub\\深层\\样本一.raw": 20_000,
        "zero.raw": 0,
        "doc.txt": 10_000,
        "sub\\报告.docx": 7_777,
        "empty.txt": 0,
        LONG_DIR + "\\" + "r" * 60 + ".raw": 12_345,
        LONG_DIR + "\\" + "n" * 60 + ".bin": 4_321,
    }
    return {rel: blob(rel, n) for rel, n in spec.items()}


def mixed_spec() -> dict[str, bytes]:
    spec = {
        "n1.txt": 6_000, "n2.txt": 6_000, "目录\\n3.txt": 6_000, "n4.txt": 6_000, "n5.txt": 0,
        "r1.raw": 15_000, "r2.raw": 15_000, "目录\\r3.raw": 15_000, "r4.raw": 15_000,
    }
    return {rel: blob(rel, n) for rel, n in spec.items()}


# ====================================================================== 1. single batch

def test_single_batch_layout_hashes_and_records(env):
    spec = sample_spec()
    src = env.make_source("Alice", spec)
    assert len(os.path.join(str(env.drive), "Alice_archive", "Alice", next(k for k in spec if k.endswith("r.raw")))) > 260
    job_id = env.add_job("Alice", src)

    status, message = env.run("T1")
    assert status == "done" and "1 个任务" in message and "未能备份" not in message
    result = env.engine().run("T1")
    assert result[0] == "done"  # second run: nothing queued
    assert "没有" in result[1]

    # tape identity: blank tape got an empty marker
    assert (env.drive / "TAPE_ID_T1.txt").is_file()
    assert (env.drive / "TAPE_ID_T1.txt").stat().st_size == 0

    check_backup(env, "Alice", spec)

    raw = {k: v for k, v in spec.items() if k.endswith(".raw")}
    nonraw = {k: v for k, v in spec.items() if not k.endswith(".raw")}
    for rel, data in raw.items():
        assert read(raw_media_path(str(env.drive), "Alice", rel)) == data

    batch = env.one("SELECT * FROM batches WHERE job_id=?", job_id)
    assert batch["status"] == "ARCHIVED" and batch["cleaned"] == 1 and batch["tape_id"] == "T1"
    assert batch["archived_at"]
    tar_path = tar_media_path(str(env.drive), "Alice", batch["tar_name"])
    assert os.path.getsize(lp(tar_path)) == batch["tar_size"]
    assert sha(read(tar_path)) == batch["tar_sha256"]
    assert batch["bytes"] == sum(len(v) for v in raw.values()) + batch["tar_size"]
    with tarfile.open(lp(tar_path), "r:") as tar:
        names = {m.name: tar.extractfile(m).read() for m in tar}
    assert names == {f"Alice/{rel.replace(os.sep, '/')}": data for rel, data in nonraw.items()}

    # only the marker and Alice_archive on tape
    assert sorted(os.listdir(env.drive)) == ["Alice_archive", "TAPE_ID_T1.txt"]

    job = env.one("SELECT * FROM jobs WHERE id=?", job_id)
    assert job["status"] == "DONE" and job["scanned"] == 1
    assert job["total_files"] == len(spec) == job["done_files"]
    assert job["total_bytes"] == sum(len(v) for v in spec.values()) == job["done_bytes"]
    assert job["failed_files"] == 0
    assert job["started_at"] and job["finished_at"]
    for r in env.rows("SELECT * FROM files WHERE job_id=?", job_id):
        assert r["batch_id"] == batch["id"] and r["tape_id"] == "T1" and r["attempts"] == 0 and r["error"] is None
        assert (r["tar_name"] == batch["tar_name"]) == (r["kind"] == "nonraw")
    assert env.staging_files() == {}


# ====================================================================== 2. multiple batches

def test_multiple_batches_small_staging_disk(env, monkeypatch):
    spec = {f"n{i:02}.txt": blob(f"n{i}", 9_000) for i in range(10)}
    spec.update({f"raw\\r{i:02}.raw": blob(f"r{i}", 20_000) for i in range(9)})
    env.make_source("Bob", spec)
    env.add_job("Bob")
    env.stage_cap = 60_000

    peak = []

    def hook(real, src, dst, kw):
        if under(dst, env.drive):
            peak.append(dir_bytes(env.staging))  # the whole batch is on staging now
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)
    assert env.run("T1")[0] == "done"
    check_backup(env, "Bob", spec)
    batches = env.rows("SELECT * FROM batches ORDER BY seq")
    assert len(batches) >= 3
    assert [b["seq"] for b in batches] == list(range(1, len(batches) + 1))
    assert sum(1 for b in batches if b["tar_name"]) >= 2, "non-raw files should be split over several tars"
    assert max(peak) <= env.stage_cap, f"staging disk overfilled: {max(peak)} > {env.stage_cap}"


# ====================================================================== 3. interruption and resume

def _crash_stage_raw_partial(env, monkeypatch):
    state = {"n": 0}

    def hook(real, src, dst, kw):
        if under(dst, env.staging):
            state["n"] += 1
            if state["n"] == 1:
                write(dst, read(src)[:100])  # power loss leaves a partial copy
                raise Crash("during raw staging")
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)


def _crash_stage_raw_after_copy(env, monkeypatch):
    state = {"n": 0}

    def hook(real, src, dst, kw):
        out = real(src, dst, **kw)
        if under(dst, env.staging):
            state["n"] += 1
            if state["n"] == 1:
                raise Crash("copied but not recorded")
        return out

    patch_copy(monkeypatch, hook)


def _crash_tar_build(env, monkeypatch):
    class CrashingTar(TarWriter):
        calls = 0

        def add(self, src, arcname, **kw):
            CrashingTar.calls += 1
            if CrashingTar.calls == 2:
                raise Crash("during tar build")
            return super().add(src, arcname, **kw)

    monkeypatch.setattr(eng, "TarWriter", CrashingTar)


def _crash_before_tape(env, monkeypatch):
    real = Engine._to_tape
    state = {"n": 0}

    def wrapper(self, job, batch):
        state["n"] += 1
        if state["n"] == 1:
            raise Crash("between STAGED and tape write")
        return real(self, job, batch)

    monkeypatch.setattr(Engine, "_to_tape", wrapper)


def _crash_mid_tape(env, monkeypatch):
    state = {"n": 0}

    def hook(real, src, dst, kw):
        if under(dst, env.drive):
            state["n"] += 1
            if state["n"] == 2:
                data = read(src)
                write(dst, data[: len(data) // 2])  # half-written file left on tape
                raise Crash("mid tape write")
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)


def _crash_before_commit(env, monkeypatch):
    real = Engine._update_counters
    state = {"n": 0}

    def wrapper(con, job_id):
        state["n"] += 1
        if state["n"] == 1:
            raise Crash("after tape write, before ARCHIVED commit")
        return real(con, job_id)

    monkeypatch.setattr(Engine, "_update_counters", staticmethod(wrapper))


def _crash_before_cleanup(env, monkeypatch):
    real = Engine._cleanup
    state = {"n": 0}

    def wrapper(self, job, batch):
        state["n"] += 1
        if state["n"] == 1:
            raise Crash("after ARCHIVED, before cleanup")
        return real(self, job, batch)

    monkeypatch.setattr(Engine, "_cleanup", wrapper)


def _crash_mid_cleanup(env, monkeypatch):
    real = Engine._cleanup
    state = {"n": 0}

    def wrapper(self, job, batch):
        state["n"] += 1
        if state["n"] == 1:
            staged = env.staging_files()
            victim = sorted(staged)[0]
            os.remove(lp(os.path.join(str(env.staging), victim)))
            raise Crash("half way through cleanup")
        return real(self, job, batch)

    monkeypatch.setattr(Engine, "_cleanup", wrapper)


CRASH_POINTS = {
    "stage_raw_partial": _crash_stage_raw_partial,
    "stage_raw_after_copy": _crash_stage_raw_after_copy,
    "tar_build": _crash_tar_build,
    "staged_before_tape": _crash_before_tape,
    "mid_tape_write": _crash_mid_tape,
    "after_tape_before_commit": _crash_before_commit,
    "after_archived_before_cleanup": _crash_before_cleanup,
    "mid_cleanup": _crash_mid_cleanup,
}


@pytest.mark.parametrize("point", list(CRASH_POINTS))
def test_crash_and_resume(env, monkeypatch, point):
    spec = mixed_spec()
    env.make_source("Carol", spec)
    job_id = env.add_job("Carol")
    env.cfg.threads = 1
    env.stage_cap = 70_000  # two batches
    CRASH_POINTS[point](env, monkeypatch)

    status, message = env.run("T1")
    assert status == "error" and "Crash" in message, (status, message)
    assert env.one("SELECT status FROM jobs WHERE id=?", job_id)["status"] == "RUNNING"

    if point == "tar_build":  # a power loss would also leave a half tar behind
        b = env.one("SELECT * FROM batches WHERE job_id=?", job_id)
        write(tar_media_path(str(env.staging), "Carol", b["tar_name"]), b"garbage" * 100)

    monkeypatch.undo()
    assert env.run("T1")[0] == "done"
    check_backup(env, "Carol", spec)
    assert len(env.rows("SELECT * FROM batches WHERE job_id=?", job_id)) == 2
    assert env.one("SELECT * FROM jobs WHERE id=?", job_id)["done_files"] == len(spec)


def test_crash_twice_at_same_point_then_resume(env, monkeypatch):
    """Two crashes in a row (mid tape write) must not duplicate anything."""
    spec = mixed_spec()
    env.make_source("Carol", spec)
    env.add_job("Carol")
    env.cfg.threads = 1
    for _ in range(2):
        _crash_mid_tape(env, monkeypatch)
        assert env.run("T1")[0] == "error"
        monkeypatch.undo()
    assert env.run("T1")[0] == "done"
    check_backup(env, "Carol", spec)


# ====================================================================== 4. the safety rule

def _assert_batch_kept_on_staging(env, spec, owner):
    batch = env.one("SELECT * FROM batches")
    assert batch["status"] != "ARCHIVED" and batch["cleaned"] == 0
    assert not env.rows("SELECT * FROM files WHERE status='ARCHIVED'")
    for rel, data in spec.items():
        if rel.endswith(".raw"):
            assert read(raw_media_path(str(env.staging), owner, rel)) == data, f"staging copy of {rel} lost"
    assert exists(tar_media_path(str(env.staging), owner, batch["tar_name"]))


def test_safety_short_file_on_tape(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Dan", spec)
    env.add_job("Dan")

    def hook(real, src, dst, kw):
        out = real(src, dst, **kw)
        if under(dst, env.drive) and dst.endswith("r2.raw"):
            with open(lp(dst), "r+b") as f:  # the tape silently keeps one byte less
                f.truncate(os.path.getsize(lp(dst)) - 1)
        return out

    patch_copy(monkeypatch, hook)
    status, message = env.run("T1")
    assert status == "error" and "r2.raw" in message
    _assert_batch_kept_on_staging(env, spec, "Dan")
    monkeypatch.undo()
    assert env.run("T1")[0] == "done"
    check_backup(env, "Dan", spec)


def test_safety_short_tar_on_tape(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Dan", spec)
    env.add_job("Dan")

    def hook(real, src, dst, kw):
        out = real(src, dst, **kw)
        if under(dst, env.drive) and dst.endswith(".tar"):
            with open(lp(dst), "r+b") as f:
                f.truncate(512)
        return out

    patch_copy(monkeypatch, hook)
    assert env.run("T1")[0] == "error"
    _assert_batch_kept_on_staging(env, spec, "Dan")
    monkeypatch.undo()
    assert env.run("T1")[0] == "done"
    check_backup(env, "Dan", spec)


def test_safety_verify_reports_wrong_size(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Dan", spec)
    env.add_job("Dan")
    real_size = eng._size

    def fake_size(path):
        s = real_size(path)
        if s is not None and path and under(path, env.drive) and path.endswith("r1.raw"):
            return s + 1
        return s

    monkeypatch.setattr(eng, "_size", fake_size)
    assert env.run("T1")[0] == "error"
    _assert_batch_kept_on_staging(env, spec, "Dan")
    monkeypatch.undo()
    assert env.run("T1")[0] == "done"
    check_backup(env, "Dan", spec)


def test_safety_tape_file_vanishes_before_verify(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Dan", spec)
    env.add_job("Dan")
    written = []

    def hook(real, src, dst, kw):
        out = real(src, dst, **kw)
        if under(dst, env.drive):
            written.append(dst)
            if len(written) == 3:
                os.remove(lp(written[1]))  # an earlier file disappears from the tape
        return out

    patch_copy(monkeypatch, hook)
    assert env.run("T1")[0] == "error"
    _assert_batch_kept_on_staging(env, spec, "Dan")
    monkeypatch.undo()
    assert env.run("T1")[0] == "done"
    check_backup(env, "Dan", spec)


def test_safety_tape_write_error_keeps_staging(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Dan", spec)
    env.add_job("Dan")

    def hook(real, src, dst, kw):
        if under(dst, env.drive) and dst.endswith("r3.raw"):
            raise CopyError("dst", dst, OSError(5, "tape I/O error"))
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)
    status, message = env.run("T1")
    assert status == "error" and "写入磁带失败" in message
    _assert_batch_kept_on_staging(env, spec, "Dan")
    monkeypatch.undo()
    assert env.run("T1")[0] == "done"
    check_backup(env, "Dan", spec)


# ====================================================================== 5. stop

@pytest.mark.parametrize("phase", ["stage_raw", "tar", "tape"])
def test_stop_mid_copy(env, monkeypatch, phase):
    spec = {"big1.raw": blob("b1", 300_000), "big2.raw": blob("b2", 200_000),
            "t1.txt": blob("t1", 120_000), "t2.txt": blob("t2", 50_000)}
    env.make_source("Eve", spec)
    job_id = env.add_job("Eve")
    env.cfg.threads = 2
    monkeypatch.setattr(eng, "TAPE_CHUNK", 4096)  # many chunks per file on the way to tape

    e = env.engine()
    real_add = e.progress.add

    def add(n):
        real_add(n)
        if n <= 0:
            return
        cur = e.progress.current_file
        if phase == "tape" and e.progress.phase == "tape" and cur.endswith("big1.raw"):
            e.stop()
        if phase == "tar" and e.progress.phase == "stage" and cur.endswith("t1.txt"):
            e.stop()
        if phase == "stage_raw" and e.progress.phase == "stage" and cur.endswith(".raw"):
            e.stop()

    e.progress.add = add
    if phase == "tar":
        monkeypatch.setattr(copier, "CHUNK", 4096)
        orig_add = TarWriter.add

        def small_chunks(self, src, arcname, **kw):
            kw["chunk"] = 4096
            return orig_add(self, src, arcname, **kw)

        monkeypatch.setattr(TarWriter, "add", small_chunks)

    assert e.run("T1") == ("stopped", "")
    assert e.progress.snapshot()["phase"] == "idle"
    assert env.one("SELECT status FROM jobs WHERE id=?", job_id)["status"] == "RUNNING"

    # no partial file at any destination
    for rel, size in env.staging_files().items():
        if rel.endswith(".tar"):
            b = env.one("SELECT * FROM batches")
            assert b["tar_size"] == size, "partial tar left on staging"
        else:
            src_rel = rel.split(os.sep, 2)[2]
            assert size == len(spec[src_rel]), f"partial staging copy {rel}"
    owner_dir = os.path.join(str(env.drive), "Eve_archive")
    for rel, size in files_under(owner_dir).items():
        if rel.endswith(".tar"):
            assert size == env.one("SELECT tar_size FROM batches")["tar_size"]
        else:
            assert size == len(spec[rel.split(os.sep, 1)[1]]), f"partial tape copy {rel}"

    monkeypatch.undo()
    assert env.run("T1")[0] == "done"
    check_backup(env, "Eve", spec)


def test_stop_from_on_log_and_resume(env):
    spec = mixed_spec()
    env.make_source("Eve", spec)
    env.add_job("Eve")
    holder = {}

    def on_log(line):
        if "写入磁带" in line and "e" in holder:
            holder["e"].stop()

    e = env.engine(on_log=on_log)
    holder["e"] = e
    assert e.run("T1") == ("stopped", "")
    assert env.one("SELECT status FROM batches")["status"] == "STAGED"
    assert env.run("T1")[0] == "done"
    check_backup(env, "Eve", spec)


# ====================================================================== 6. incremental

def test_incremental_second_run(env):
    v1 = {"a.raw": blob("a", 20_000), "b.raw": blob("b", 20_000),
          "c.txt": blob("c", 5_000), "d.txt": blob("d", 5_000)}
    env.make_source("Fay", v1)
    job1 = env.add_job("Fay")
    assert env.run("T1")[0] == "done"
    b_mtime = os.path.getmtime(raw_media_path(str(env.drive), "Fay", "b.raw"))
    old_tar = env.one("SELECT tar_name FROM batches WHERE job_id=?", job1)["tar_name"]

    v2 = dict(v1)
    v2["a.raw"] = blob("a", 25_000, "v2")
    v2["c.txt"] = blob("c", 6_000, "v2")
    v2["e.raw"] = blob("e", 10_000)
    v2["f.txt"] = blob("f", 3_000)
    env.make_source("Fay", v2)
    time.sleep(0.05)
    job2 = env.add_job("Fay")
    assert env.run("T1")[0] == "done"

    j2 = env.one("SELECT * FROM jobs WHERE id=?", job2)
    assert j2["total_files"] == 4
    assert {r["rel_path"] for r in env.rows("SELECT rel_path FROM files WHERE job_id=?", job2)} == {
        "a.raw", "c.txt", "e.raw", "f.txt"}
    assert env.file_row("a.raw", job1)["status"] == "REPLACED"
    assert env.file_row("c.txt", job1)["status"] == "ARCHIVED"  # old tar still holds it
    assert os.path.getmtime(raw_media_path(str(env.drive), "Fay", "b.raw")) == b_mtime  # not rewritten
    check_backup(env, "Fay", v2)
    # the old version of c.txt is still really in the old tar
    with tarfile.open(lp(tar_media_path(str(env.drive), "Fay", old_tar))) as tar:
        assert tar.extractfile("Fay/c.txt").read() == v1["c.txt"]

    con = db.connect(env.cfg.db_path)
    try:
        hits = db.search_files(con, "a.raw")
        assert [(h["size"], h["job_id"]) for h in hits] == [(25_000, job2)]
        hits = db.search_files(con, "c.txt")
        assert sorted(h["size"] for h in hits) == [5_000, 6_000]
        assert db.owner_history(con, "fay")["archived_files"] > 0
    finally:
        con.close()

    # third run, nothing changed
    job3 = env.add_job("Fay")
    assert env.run("T1")[0] == "done"
    j3 = env.one("SELECT * FROM jobs WHERE id=?", job3)
    assert j3["status"] == "DONE" and j3["total_files"] == 0
    assert not env.rows("SELECT * FROM batches WHERE job_id=?", job3)


def test_incremental_case_insensitive_path_and_owner(env):
    v1 = {"Sub\\A.raw": blob("a", 1000), "Sub\\B.txt": blob("b", 1000)}
    env.make_source("Gus", v1)
    env.add_job("Gus")
    assert env.run("T1")[0] == "done"
    # same files, path case differs in the database query: the job for "gus" must skip them
    job2 = env.add_job("gus", str(env.nas / "Gus"))
    assert env.run("T1")[0] == "done"
    assert env.one("SELECT total_files FROM jobs WHERE id=?", job2)["total_files"] == 0


# ====================================================================== 7. tape handling

def test_no_tape_folder_asks_then_continues(env):
    spec = mixed_spec()
    env.make_source("Hal", spec)
    env.add_job("Hal")
    env.drive.rmdir()
    calls = []

    def ask(reason, suggested):
        calls.append((reason, suggested))
        env.drive.mkdir()
        return "t1"  # lower case: normalised

    assert env.run("T1", ask=ask)[0] == "done"
    assert len(calls) == 1 and calls[0][1] is None
    assert env.marker() == "T1"
    check_backup(env, "Hal", spec)


def test_no_tape_and_no_answer_stops(env):
    env.make_source("Hal", mixed_spec())
    env.add_job("Hal")
    env.drive.rmdir()
    assert env.run("T1", ask=lambda r, s: None) == ("stopped", "")
    assert env.run("T1", ask=None) == ("stopped", "")  # no callback = stop, not crash


def test_marker_mismatch_asks_with_suggestion(env):
    spec = mixed_spec()
    env.make_source("Ida", spec)
    env.add_job("Ida")
    write(env.drive / "TAPE_ID_T9.txt", b"")
    calls = []

    def ask(reason, suggested):
        calls.append((reason, suggested))
        return suggested

    assert env.run("T1", ask=ask)[0] == "done"
    assert calls and calls[0][1] == "T9"
    assert {r["tape_id"] for r in env.rows("SELECT tape_id FROM files")} == {"T9"}
    assert {r["tape_id"] for r in env.rows("SELECT tape_id FROM batches")} == {"T9"}
    check_backup(env, "Ida", spec)


def test_marker_mismatch_refused_then_resumed(env):
    spec = mixed_spec()
    env.make_source("Ida", spec)
    env.add_job("Ida")
    write(env.drive / "TAPE_ID_T9.txt", b"")
    assert env.run("T1", ask=lambda r, s: None) == ("stopped", "")
    assert not env.rows("SELECT * FROM batches")
    assert env.run("T9")[0] == "done"
    check_backup(env, "Ida", spec)


def test_empty_tape_id_asks(env):
    spec = mixed_spec()
    env.make_source("Ida", spec)
    env.add_job("Ida")
    calls = []
    assert env.run("", ask=lambda r, s: calls.append(r) or "T5")[0] == "done"
    assert len(calls) == 1
    assert env.marker() == "T5"
    check_backup(env, "Ida", spec)


def test_tape_too_full_at_planning(env):
    spec = mixed_spec()
    env.make_source("Jo", spec)
    env.add_job("Jo")
    write(env.drive / "TAPE_ID_T1.txt", b"")
    env.fill_tape(145_000)
    env.tape_cap = 150_000  # 5 KB free on T1; a blank tape holds the whole job
    calls = []
    status, _ = env.run("T1", ask=swap_to(env, "T2", calls=calls))
    assert status == "done"
    assert len(calls) == 1 and "剩余空间不足" in calls[0][0]
    check_backup(env, "Jo", spec)
    assert sorted(os.listdir(env.tape_folder("T1"))) == ["TAPE_ID_T1.txt", "junk.bin"]


def test_tape_too_full_at_write_time(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Jo", spec)
    job_id = env.add_job("Jo")
    env.tape_cap = 500_000
    real = Engine._to_tape
    state = {"n": 0}

    def wrapper(self, job, batch):
        state["n"] += 1
        if state["n"] == 1:
            env.fill_tape(480_000)  # someone else used the tape meanwhile
        return real(self, job, batch)

    monkeypatch.setattr(Engine, "_to_tape", wrapper)
    calls = []
    assert env.run("T1", ask=swap_to(env, "T2", calls=calls))[0] == "done"
    assert len(calls) == 1
    assert env.one("SELECT tape_id FROM batches WHERE job_id=?", job_id)["tape_id"] == "T2"
    assert {r["tape_id"] for r in env.rows("SELECT tape_id FROM files")} == {"T2"}
    check_backup(env, "Jo", spec)
    assert "Jo_archive" not in os.listdir(env.tape_folder("T1"))


def test_job_spans_two_tapes(env):
    spec = {f"r{i}.raw": blob(f"r{i}", 20_000) for i in range(6)}
    spec.update({f"t{i}.txt": blob(f"t{i}", 3_000) for i in range(3)})
    env.make_source("Kim", spec)
    job_id = env.add_job("Kim")
    env.tape_cap = 75_000
    calls = []
    status, message = env.run("T1", ask=swap_to(env, "T2", "T3", calls=calls))
    assert status == "done"
    check_backup(env, "Kim", spec)
    tapes = {r["tape_id"] for r in env.rows("SELECT tape_id FROM files WHERE job_id=?", job_id)}
    assert len(tapes) >= 2
    for b in env.rows("SELECT * FROM batches WHERE job_id=?", job_id):
        for f in env.rows("SELECT * FROM files WHERE batch_id=?", b["id"]):
            assert f["tape_id"] == b["tape_id"]
    con = db.connect(env.cfg.db_path)
    try:
        listed = {r["tape_id"] for r in db.list_tapes(con)}
        assert tapes <= listed
        jobs = db.list_jobs(con)
        assert set(jobs[0]["tapes"].split(",")) == tapes
    finally:
        con.close()


def test_ask_none_mid_job_stops_then_resumes(env):
    spec = {f"r{i}.raw": blob(f"r{i}", 20_000) for i in range(6)}
    env.make_source("Kim", spec)
    job_id = env.add_job("Kim")
    env.tape_cap = 75_000
    assert env.run("T1", ask=lambda r, s: None) == ("stopped", "")
    assert env.one("SELECT status FROM jobs WHERE id=?", job_id)["status"] == "RUNNING"
    archived = env.rows("SELECT * FROM files WHERE status='ARCHIVED'")
    assert archived and all(r["tape_id"] == "T1" for r in archived)
    assert env.staging_files() == {}
    assert env.run("T1", ask=swap_to(env, "T2"))[0] == "done"
    check_backup(env, "Kim", spec)


def test_marker_write_fails(env, monkeypatch):
    env.make_source("Lu", mixed_spec())
    env.add_job("Lu")

    def boom(root, tape_id):
        raise PermissionError(13, "write protected")

    monkeypatch.setattr(eng, "write_tape_marker", boom)
    status, message = env.run("T1")
    assert status == "error" and "写保护" in message
    monkeypatch.undo()
    assert env.run("T1")[0] == "done"


def test_read_tape_marker_variants(tmp_path):
    assert read_tape_marker(str(tmp_path)) is None
    write(tmp_path / "TAPE_ID_.txt", b"")
    write(tmp_path / "tape_id_ab-1.TXT", b"")
    assert read_tape_marker(str(tmp_path)) == "AB-1"
    assert eng.normalize_tape_id(" mb2feq ") == "MB2FEQ"
    assert eng.normalize_tape_id("bad id") == ""
    assert eng.normalize_tape_id(None) == ""


# ====================================================================== 8. source problems

def test_file_deleted_or_locked_between_scan_and_copy(env):
    import _winapi

    spec = mixed_spec()
    env.make_source("Max", spec)
    job_id = env.add_job("Max")
    handles = []

    def on_log(line):
        if "扫描完成" in line:
            os.remove(env.src("Max", "r2.raw"))
            os.remove(env.src("Max", "n2.txt"))
            handles.append(_winapi.CreateFile(env.src("Max", "r4.raw"), 0x80000000, 0, 0, 3, 0, 0))
            handles.append(_winapi.CreateFile(env.src("Max", "n4.txt"), 0x80000000, 0, 0, 3, 0, 0))

    try:
        status, message = env.run("T1", on_log=on_log)
    finally:
        for h in handles:
            _winapi.CloseHandle(h)
    assert status == "done" and "4 个文件未能备份" in message
    bad = ("r2.raw", "n2.txt", "r4.raw", "n4.txt")
    good = {k: v for k, v in spec.items() if k not in bad}
    check_backup(env, "Max", good, failed=bad)
    for rel in bad:
        r = env.file_row(rel)
        assert r["error"] and r["batch_id"] is None
    assert env.one("SELECT failed_files FROM jobs WHERE id=?", job_id)["failed_files"] == 4


@pytest.mark.parametrize("when", ["raw", "tar"])
def test_source_folder_disappears_mid_run(env, monkeypatch, when):
    spec = mixed_spec()
    src = env.make_source("Ned", spec)
    gone = src + "_gone"
    job_id = env.add_job("Ned")
    env.cfg.threads = 1
    state = {"done": False}

    if when == "raw":
        def hook(real, s, d, kw):
            out = real(s, d, **kw)
            if under(d, env.staging) and not state["done"]:
                state["done"] = True
                os.rename(src, gone)
            return out

        patch_copy(monkeypatch, hook)
    else:
        class VanishingTar(TarWriter):
            def add(self, s, arcname, **kw):
                out = super().add(s, arcname, **kw)
                if not state["done"]:
                    state["done"] = True
                    os.rename(src, gone)
                return out

        monkeypatch.setattr(eng, "TarWriter", VanishingTar)

    status, message = env.run("T1")
    assert status == "error" and "源文件夹无法访问" in message
    assert not env.rows("SELECT * FROM files WHERE status='FAILED'")
    assert env.one("SELECT status FROM jobs WHERE id=?", job_id)["status"] == "RUNNING"
    monkeypatch.undo()
    os.rename(gone, src)
    assert env.run("T1")[0] == "done"
    check_backup(env, "Ned", spec)


def test_source_folder_missing_at_scan(env):
    job_id = env.add_job("Oz", str(env.nas / "Oz"))
    status, message = env.run("T1")
    assert status == "error" and "无法读取源文件夹" in message
    assert env.one("SELECT scanned FROM jobs WHERE id=?", job_id)["scanned"] == 0
    spec = mixed_spec()
    env.make_source("Oz", spec)
    assert env.run("T1")[0] == "done"
    check_backup(env, "Oz", spec)


def test_file_shrinks_or_grows_between_scan_and_copy(env):
    spec = mixed_spec()
    env.make_source("Pat", spec)
    env.add_job("Pat")
    changed = {
        "r1.raw": blob("r1", 9_000, "shrunk"),
        "r2.raw": blob("r2", 26_000, "grown"),
        "n1.txt": blob("n1", 1_000, "shrunk"),
        "n2.txt": blob("n2", 9_000, "grown"),
    }

    def on_log(line):
        if "扫描完成" in line:
            for rel, data in changed.items():
                write(env.src("Pat", rel), data)

    assert env.run("T1", on_log=on_log)[0] == "done"
    final = dict(spec)
    final.update(changed)
    check_backup(env, "Pat", final)


# ====================================================================== 9. file larger than staging

def test_file_larger_than_staging_disk(env):
    spec = {"big.raw": blob("big", 200_000), "big.txt": blob("bigt", 150_000)}
    spec.update({f"s{i}.raw": blob(f"s{i}", 20_000) for i in range(4)})
    spec.update({f"s{i}.txt": blob(f"s{i}t", 5_000) for i in range(3)})
    env.make_source("Quinn", spec)
    job_id = env.add_job("Quinn")
    env.stage_cap = 100_000
    status, message = env.run("T1")
    assert status == "done" and "2 个文件未能备份" in message
    good = {k: v for k, v in spec.items() if not k.startswith("big")}
    check_backup(env, "Quinn", good, failed=("big.raw", "big.txt"))
    assert "中转盘" in env.file_row("big.raw")["error"]
    assert env.one("SELECT * FROM jobs WHERE id=?", job_id)["failed_files"] == 2


# ====================================================================== 10. staging corruption

def test_staged_raw_corrupted_before_tape_after_crash(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Rae", spec)
    env.add_job("Rae")
    _crash_before_tape(env, monkeypatch)
    assert env.run("T1")[0] == "error"
    monkeypatch.undo()
    flip_byte(raw_media_path(str(env.staging), "Rae", "r2.raw"), 1234)
    assert env.run("T1")[0] == "done"
    check_backup(env, "Rae", spec)
    assert env.file_row("r2.raw")["attempts"] == 1


def test_staged_raw_corrupted_during_run(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Rae", spec)
    env.add_job("Rae")
    state = {"n": 0}

    def hook(real, src, dst, kw):
        if under(dst, env.drive) and dst.endswith("r3.raw") and state["n"] == 0:
            state["n"] += 1
            flip_byte(src, 10)
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)
    assert env.run("T1")[0] == "done"
    check_backup(env, "Rae", spec)


def test_staged_raw_always_corrupted_gives_up_after_three(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Rae", spec)
    env.add_job("Rae")

    def hook(real, src, dst, kw):
        if under(dst, env.drive) and dst.endswith("r3.raw"):
            flip_byte(src, 10)
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)
    status, message = env.run("T1")
    assert status == "done"
    r = env.file_row("目录\\r3.raw")
    assert r["status"] == "FAILED" and r["attempts"] == 3
    good = {k: v for k, v in spec.items() if k != "目录\\r3.raw"}
    check_backup(env, "Rae", good, failed=("目录\\r3.raw",))


def test_staging_wiped_between_staged_and_tape(env, monkeypatch):
    """Every staged copy (tar and raw) disappears: the batch is dropped and everything is staged again."""
    spec = mixed_spec()
    env.make_source("Rae", spec)
    env.add_job("Rae")
    _crash_before_tape(env, monkeypatch)
    assert env.run("T1")[0] == "error"
    monkeypatch.undo()
    shutil.rmtree(lp(env.staging))
    assert env.run("T1")[0] == "done"
    check_backup(env, "Rae", spec)
    assert {r["attempts"] for r in env.rows("SELECT attempts FROM files")} == {1}


def test_changed_file_on_another_tape_keeps_old_record(env):
    env.make_source("Fay", {"a.raw": blob("a", 1_000)})
    job1 = env.add_job("Fay")
    assert env.run("T1")[0] == "done"
    v2 = {"a.raw": blob("a", 2_000, "v2")}
    env.make_source("Fay", v2)
    env.insert("T2")
    env.add_job("Fay")
    assert env.run("T2")[0] == "done"
    old = env.file_row("a.raw", job1)
    assert old["status"] == "ARCHIVED" and old["tape_id"] == "T1"
    assert tape_content(env, old, "Fay") == blob("a", 1_000)
    check_backup(env, "Fay", v2)
    con = db.connect(env.cfg.db_path)
    try:
        assert sorted((r["tape_id"], r["size"]) for r in db.search_files(con, "a.raw")) == [("T1", 1000), ("T2", 2000)]
    finally:
        con.close()


@pytest.mark.xfail(strict=True, reason="BUG: corruption of the staged tar is never detected (tar_sha256 is "
                                       "taken from the corrupted staging copy, member hashes are not re-checked)")
def test_staged_tar_corrupted_before_tape(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Rae", spec)
    env.add_job("Rae")
    state = {"n": 0}

    def hook(real, src, dst, kw):
        if under(dst, env.drive) and dst.endswith(".tar") and state["n"] == 0:
            state["n"] += 1
            with tarfile.open(lp(src)) as tar:
                m = next(m for m in tar if m.size > 0)
                offset = m.offset_data
            flip_byte(src, offset + 5)  # same size, one data byte of a member changed
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)
    env.run("T1")
    check_backup(env, "Rae", spec)


# ====================================================================== 11. queue, owners, abandon

def test_queue_with_two_owners(env):
    a = mixed_spec()
    b = {"x.raw": blob("x", 10_000), "y.txt": blob("y", 2_000), "a.raw": blob("bob-a", 3_000)}
    env.make_source("Ann", a)
    env.make_source("Ben", b)
    j1 = env.add_job("Ann")
    j2 = env.add_job("Ben")
    updates = []
    e = env.engine(on_job_update=updates.append)
    status, message = e.run("T1")
    assert status == "done" and "2 个任务" in message
    check_backup(env, "Ann", a)
    check_backup(env, "Ben", b)
    assert {j1, j2} <= set(updates)
    assert e.progress.snapshot()["job_id"] == j2


def test_interrupted_job_runs_before_queued_job(env, monkeypatch):
    a, b = mixed_spec(), {"x.raw": blob("x", 10_000)}
    env.make_source("Ann", a)
    env.make_source("Ben", b)
    env.add_job("Ann")
    env.add_job("Ben")
    _crash_mid_tape(env, monkeypatch)
    env.cfg.threads = 1
    assert env.run("T1")[0] == "error"
    monkeypatch.undo()
    assert env.run("T1")[0] == "done"
    check_backup(env, "Ann", a)
    check_backup(env, "Ben", b)


def test_abandon_half_done_job(env, monkeypatch):
    spec = {f"r{i}.raw": blob(f"r{i}", 20_000) for i in range(6)}
    spec.update({f"t{i}.txt": blob(f"t{i}", 5_000) for i in range(4)})
    env.make_source("Cy", spec)
    job_id = env.add_job("Cy")
    env.stage_cap = 50_000
    env.cfg.threads = 1
    state = {"n": 0}

    def hook(real, src, dst, kw):
        if under(dst, env.drive):
            state["n"] += 1
            if state["n"] == 4:  # inside the second or third batch
                raise Crash()
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)
    assert env.run("T1")[0] == "error"
    monkeypatch.undo()
    archived_before = {r["rel_path"] for r in env.rows("SELECT rel_path FROM files WHERE status='ARCHIVED'")}
    assert archived_before and env.staging_files()

    env.engine().abandon_job(job_id)
    assert env.staging_files() == {}
    job = env.one("SELECT * FROM jobs WHERE id=?", job_id)
    assert job["status"] == "CANCELLED" and job["finished_at"]
    assert job["done_files"] == len(archived_before)
    rows = env.rows("SELECT * FROM files WHERE job_id=?", job_id)
    assert {r["rel_path"] for r in rows} == archived_before and all(r["status"] == "ARCHIVED" for r in rows)
    assert all(b["status"] == "ARCHIVED" and b["cleaned"] == 1
               for b in env.rows("SELECT * FROM batches WHERE job_id=?", job_id))
    con = db.connect(env.cfg.db_path)
    try:
        assert not db.active_jobs(con)
    finally:
        con.close()

    job2 = env.add_job("Cy")
    assert env.run("T1")[0] == "done"
    assert {r["rel_path"] for r in env.rows("SELECT rel_path FROM files WHERE job_id=?", job2)} == \
        set(spec) - archived_before
    check_backup(env, "Cy", spec)


def test_abandon_queued_job_removes_it(env):
    env.make_source("Cy", mixed_spec())
    job_id = env.add_job("Cy")
    env.engine().abandon_job(job_id)
    assert not env.rows("SELECT * FROM jobs")


# ====================================================================== 12. db

def test_import_legacy_csv(env, tmp_path):
    path = tmp_path / "archive_index.csv"
    text = (
        '"date","tape_id","owner","size_tb"\r\n'
        '"2024-03-01","MB2FEQ","LiHua","1.234"\r\n'
        '"2024-05-10","MB2FER","张三","0.5"\r\n'
        '"2024-06-01","","NoTape","0.1"\r\n'
    )
    path.write_bytes(text.encode("utf-8-sig"))
    con = db.connect(env.cfg.db_path)
    try:
        assert db.import_legacy_csv(con, str(path)) == (2, 0)
        assert db.import_legacy_csv(con, str(path)) == (0, 2)
        jobs = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        assert len(jobs) == 2
        j = jobs[0]
        assert (j["owner"], j["status"], j["legacy"], j["finished_at"]) == ("LiHua", "DONE", 1, "2024-03-01 00:00:00")
        assert j["done_bytes"] == int(1.234 * db.TB)
        b = con.execute("SELECT * FROM batches WHERE job_id=?", (j["id"],)).fetchone()
        assert b["tape_id"] == "MB2FEQ" and b["status"] == "ARCHIVED" and b["cleaned"] == 1
        assert jobs[1]["owner"] == "张三"
        assert db.active_jobs(con) == []
        h = db.owner_history(con, "lihua")
        assert h["legacy_only"] and h["archived_files"] == 0
        assert {r["tape_id"] for r in db.list_tapes(con)} == {"MB2FEQ", "MB2FER"}
    finally:
        con.close()

    # a legacy-only owner gets a full backup, and the legacy job is not touched
    spec = mixed_spec()
    env.make_source("LiHua", spec)
    env.add_job("LiHua")
    assert env.run("T1")[0] == "done"
    check_backup(env, "LiHua", spec)
    assert env.one("SELECT status FROM jobs WHERE legacy=1 AND owner='LiHua'")["status"] == "DONE"


def test_search_files(env):
    a = {
        "实验\\数据一.txt": blob("1", 100), "100%_done.txt": blob("2", 100), "100x_done.txt": blob("3", 100),
        "a_b.raw": blob("4", 100), "axb.raw": blob("5", 100), "sub\\report.txt": blob("6", 100),
        "other\\report.raw": blob("7", 100),
    }
    b = {"sub\\report.txt": blob("8", 100)}
    env.make_source("Alice", a)
    env.make_source("Bob", b)
    env.add_job("Alice")
    assert env.run("T1")[0] == "done"
    env.insert("T2")
    env.add_job("Bob")
    assert env.run("T2")[0] == "done"

    con = db.connect(env.cfg.db_path)
    try:
        def s(*args, **kw):
            return sorted((r["owner"], r["rel_path"]) for r in db.search_files(con, *args, **kw))

        assert len(s("")) == len(a) + len(b)
        assert s("report") == [("Alice", "other\\report.raw"), ("Alice", "sub\\report.txt"), ("Bob", "sub\\report.txt")]
        assert s("sub report") == [("Alice", "sub\\report.txt"), ("Bob", "sub\\report.txt")]
        assert s("REPORT  sub") == s("sub report")
        assert s("sub\\report") == s("sub report")
        assert s("report", owner="alice") == [("Alice", "other\\report.raw"), ("Alice", "sub\\report.txt")]
        assert s("report", tape_id="T2") == [("Bob", "sub\\report.txt")]
        assert s("%") == [("Alice", "100%_done.txt")]
        assert s("%_") == [("Alice", "100%_done.txt")]
        assert s("_") == [("Alice", "100%_done.txt"), ("Alice", "100x_done.txt"), ("Alice", "a_b.raw")]
        assert s("a_b") == [("Alice", "a_b.raw")]
        assert s("实验 数据") == [("Alice", "实验\\数据一.txt")]
        assert s("nothing-like-this") == []
        assert len(db.search_files(con, "", limit=3)) == 3
        r = db.search_files(con, "a_b")[0]
        assert r["tape_id"] == "T1" and r["sha256"] == sha(a["a_b.raw"]) and r["archived_at"]
        job_id = r["job_id"]
        assert len(db.files_of_job(con, job_id)) == len(a)
    finally:
        con.close()


def test_backup_database(env, tmp_path):
    dest_dir = tmp_path / "nas_backup"
    env.cfg.db_backup_dir = str(dest_dir)
    spec = mixed_spec()
    env.make_source("Dee", spec)
    env.add_job("Dee")
    assert env.run("T1")[0] == "done"
    copy = dest_dir / "tape_archive.db"
    assert copy.is_file()
    c = sqlite3.connect(str(copy))
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT COUNT(*) FROM files WHERE status='ARCHIVED'").fetchone()[0] == len(spec)
        assert c.execute("SELECT status FROM jobs").fetchone()[0] == "DONE"
    finally:
        c.close()
    assert not [p for p in os.listdir(dest_dir) if p.endswith(".tmp")]
    assert not [p for p in os.listdir(env.data) if p.endswith(".tmp")]
    # direct call, twice (overwrite)
    con = db.connect(env.cfg.db_path)
    try:
        for _ in range(2):
            assert db.backup_database(con, env.cfg.db_path, str(dest_dir)) == str(copy)
    finally:
        con.close()


def test_backup_database_failure_does_not_fail_job(env, monkeypatch):
    env.cfg.db_backup_dir = str(env.base / "unreachable")

    def boom(*a, **k):
        raise OSError("NAS gone")

    monkeypatch.setattr(eng.db, "backup_database", boom)
    spec = mixed_spec()
    env.make_source("Dee", spec)
    env.add_job("Dee")
    assert env.run("T1")[0] == "done"
    assert any("备份到 NAS 失败" in line for line in env.logs)


def test_remove_queued_job(env):
    con = db.connect(env.cfg.db_path)
    try:
        jid = db.add_job(con, "Zed", "x")
        assert db.remove_queued_job(con, jid)
        assert not db.remove_queued_job(con, jid)
    finally:
        con.close()


# ====================================================================== 13. copier units

def test_copy_file_hash_size_mtime(tmp_path):
    data = blob("c", 300_000)
    src, dst = tmp_path / "s.bin", tmp_path / "d" / "e" / "d.bin"
    write(src, data)
    os.utime(src, (1_600_000_000, 1_600_000_000))
    got = []
    assert copy_file(str(src), str(dst), on_bytes=got.append, chunk=65536) == (len(data), sha(data))
    assert read(dst) == data and sum(got) == len(data)
    assert os.path.getmtime(dst) == pytest.approx(1_600_000_000, abs=1)
    assert hash_file(str(dst)) == sha(data)
    # zero bytes
    write(tmp_path / "z", b"")
    assert copy_file(str(tmp_path / "z"), str(tmp_path / "z2")) == (0, sha(b""))


def test_copy_file_long_path(tmp_path):
    data = blob("l", 1000)
    src = tmp_path / ("a" * 120) / ("b" * 120) / "src.bin"
    dst = tmp_path / ("c" * 120) / ("d" * 120) / "dst.bin"
    write(src, data)
    assert copy_file(str(src), str(dst))[1] == sha(data)
    assert read(dst) == data


def test_copy_file_missing_source_keeps_existing_destination(tmp_path):
    write(tmp_path / "d", b"old")
    with pytest.raises(CopyError) as ei:
        copy_file(str(tmp_path / "nope"), str(tmp_path / "d"))
    assert ei.value.side == "src"
    assert read(tmp_path / "d") == b"old"


def test_copy_file_stop_cleans_up(tmp_path):
    write(tmp_path / "s", blob("s", 100_000))
    got = []
    with pytest.raises(Stopped):
        copy_file(str(tmp_path / "s"), str(tmp_path / "d"), should_stop=lambda: len(got) >= 3,
                  on_bytes=got.append, chunk=4096)
    assert not exists(tmp_path / "d")
    assert sum(got) == 0 and len(got) > 3


class _FlakyFile:
    def __init__(self, f, *, fail_read_after=None, eof_after=None, fail_write_after=None):
        self._f = f
        self._reads = 0
        self._written = 0
        self.fail_read_after, self.eof_after, self.fail_write_after = fail_read_after, eof_after, fail_write_after

    def readinto(self, b):
        self._reads += 1
        if self.fail_read_after is not None and self._reads > self.fail_read_after:
            raise OSError(5, "read error")
        if self.eof_after is not None and self._reads > self.eof_after:
            return 0
        return self._f.readinto(b)

    def write(self, b):
        if self.fail_write_after is not None and self._written >= self.fail_write_after:
            raise OSError(28, "No space left on device")
        n = self._f.write(b)
        self._written += n
        return n

    def __getattr__(self, name):
        return getattr(self._f, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()


def _patch_open(monkeypatch, match: str, **flaky):
    def fake_open(path, mode="r", *args, **kw):
        f = builtins.open(path, mode, *args, **kw)
        if match in str(path):
            return _FlakyFile(f, **flaky)
        return f

    monkeypatch.setattr(copier, "open", fake_open, raising=False)


def test_copy_file_read_error_mid_copy(tmp_path, monkeypatch):
    write(tmp_path / "src.bin", blob("s", 50_000))
    _patch_open(monkeypatch, "src.bin", fail_read_after=3)
    got = []
    with pytest.raises(CopyError) as ei:
        copy_file(str(tmp_path / "src.bin"), str(tmp_path / "d"), on_bytes=got.append, chunk=4096)
    assert ei.value.side == "src"
    assert not exists(tmp_path / "d") and sum(got) == 0 and got


def test_copy_file_write_error_mid_copy(tmp_path, monkeypatch):
    write(tmp_path / "s", blob("s", 50_000))
    _patch_open(monkeypatch, "dst.bin", fail_write_after=10_000)
    got = []
    with pytest.raises(CopyError) as ei:
        copy_file(str(tmp_path / "s"), str(tmp_path / "dst.bin"), on_bytes=got.append, chunk=4096)
    assert ei.value.side == "dst"
    assert not exists(tmp_path / "dst.bin") and sum(got) == 0 and got


def test_tarwriter_rollback(tmp_path, monkeypatch):
    good1, good2, shrink, stopme = blob("g1", 7_000), blob("g2", 0), blob("sh", 50_000), blob("st", 50_000)
    write(tmp_path / "g1", good1)
    write(tmp_path / "g2", good2)
    write(tmp_path / "shrinking.bin", shrink)
    write(tmp_path / "st", stopme)
    _patch_open(monkeypatch, "shrinking.bin", eof_after=2)
    tar_path = tmp_path / "out" / "x.tar"
    got = []
    with TarWriter(str(tar_path)) as tw:
        assert tw.add(str(tmp_path / "g1"), "o/g1", on_bytes=got.append) == (len(good1), sha(good1))
        with pytest.raises(CopyError) as ei:
            tw.add(str(tmp_path / "missing"), "o/missing", on_bytes=got.append)
        assert ei.value.side == "src"
        with pytest.raises(CopyError):
            tw.add(str(tmp_path / "shrinking.bin"), "o/shrinking", on_bytes=got.append, chunk=4096)
        with pytest.raises(Stopped):
            tw.add(str(tmp_path / "st"), "o/st", on_bytes=got.append, chunk=4096,
                   should_stop=lambda: sum(got) > len(good1) + 5000)
        assert tw.add(str(tmp_path / "g2"), "o/中文 名.txt", on_bytes=got.append) == (0, sha(b""))
        assert tw.members == 2
    assert sum(got) == len(good1)
    with tarfile.open(lp(tar_path)) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar}
    assert members == {"o/g1": good1, "o/中文 名.txt": good2}
    assert os.path.getsize(tar_path) % tarfile.RECORDSIZE == 0
    assert copier.list_tar(str(tar_path)) == [("o/g1", len(good1)), ("o/中文 名.txt", 0)]


def test_tarwriter_exception_removes_file(tmp_path):
    write(tmp_path / "g", b"x")
    with pytest.raises(RuntimeError):
        with TarWriter(str(tmp_path / "t.tar")) as tw:
            tw.add(str(tmp_path / "g"), "g")
            raise RuntimeError()
    assert not exists(tmp_path / "t.tar")


def test_with_retry():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise CopyError("src", "p", "transient")
        return 1, "x"

    assert with_retry(flaky, delay=0) == (1, "x") and len(calls) == 3

    calls.clear()

    def always():
        calls.append(1)
        raise CopyError("src", "p", "dead")

    with pytest.raises(CopyError):
        with_retry(always, delay=0)
    assert len(calls) == 3

    calls.clear()

    def other():
        calls.append(1)
        raise ValueError()

    with pytest.raises(ValueError):
        with_retry(other, delay=0)
    assert len(calls) == 1

    calls.clear()
    t = time.monotonic()
    with pytest.raises(Stopped):
        with_retry(always, delay=30, should_stop=lambda: True)
    assert time.monotonic() - t < 2 and len(calls) == 1


def test_long_path():
    if os.name != "nt":
        pytest.skip("windows only")
    short = "C:\\a\\b.txt"
    assert long_path(short) == short
    p = "C:\\" + "a" * 300
    assert long_path(p) == "\\\\?\\" + p
    assert long_path(long_path(p)) == long_path(p)
    unc = "\\\\server\\share\\" + "b" * 300
    assert long_path(unc) == "\\\\?\\UNC\\server\\share\\" + "b" * 300


# ====================================================================== 14. progress

def test_progress_snapshot_in_range(env, monkeypatch):
    spec = {f"n{i:02}.txt": blob(f"n{i}", 9_000) for i in range(6)}
    spec.update({f"r{i:02}.raw": blob(f"r{i}", 20_000) for i in range(6)})
    env.make_source("Pia", spec)
    env.add_job("Pia")
    env.stage_cap = 60_000
    e = env.engine()
    bad = []
    stop = threading.Event()

    def check(snap):
        if not (0 <= snap["phase_done"] <= snap["phase_total"] or snap["phase_total"] == 0 == snap["phase_done"]):
            bad.append(snap)
        if not (0 <= snap["job_done"] <= snap["job_total"]):
            bad.append(snap)

    def poll():
        while not stop.is_set():
            check(e.progress.snapshot())
            time.sleep(0.0005)

    t = threading.Thread(target=poll)
    t.start()
    try:
        assert e.run("T1")[0] == "done"
    finally:
        stop.set()
        t.join()
    assert not bad, bad[:3]
    snap = e.progress.snapshot()
    assert snap["job_total"] > 0 and snap["job_done"] == snap["job_total"]
    assert snap["phase"] == "idle"


# ====================================================================== suspected bugs (xfail strict)

@pytest.mark.xfail(strict=True, reason="BUG: _to_tape decides which files are 'already on tape' BEFORE "
                                       "_ensure_tape may switch tapes; after a swap the skipped files are "
                                       "missing on the new tape and verification fails")
def test_tape_swap_on_resume_after_partial_write(env, monkeypatch):
    spec = {"a.raw": blob("a", 20_000), "b.raw": blob("b", 20_000), "c.raw": blob("c", 20_000)}
    env.make_source("Sam", spec)
    env.add_job("Sam")
    env.cfg.threads = 1
    state = {"n": 0}

    def hook(real, src, dst, kw):
        if under(dst, env.drive):
            state["n"] += 1
            if state["n"] == 2:
                raise Crash()
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)
    assert env.run("T1")[0] == "error"
    monkeypatch.undo()
    env.tape_cap = 100_000
    env.fill_tape(100_000 - dir_bytes(env.drive) - 1_000)  # T1 is now too full for the rest of the batch
    status, message = env.run("T1", ask=swap_to(env, "T2"))
    assert status == "done", message
    check_backup(env, "Sam", spec, strict_tape=False)


@pytest.mark.xfail(strict=True, reason="BUG: a changed raw file is written over its archived old version in "
                                       "place; when the new copy is then discarded (sha mismatch) the old "
                                       "version is gone from tape but its record stays ARCHIVED")
def test_overwrite_then_requeue_loses_old_version(env, monkeypatch):
    v1 = {"a.raw": blob("a", 20_000), "b.raw": blob("b", 20_000)}
    env.make_source("Tom", v1)
    env.add_job("Tom")
    assert env.run("T1")[0] == "done"

    write(env.src("Tom", "a.raw"), blob("a", 30_000, "v2"))
    env.add_job("Tom")
    state = {"n": 0}

    def hook(real, src, dst, kw):
        if under(dst, env.drive) and dst.endswith("a.raw") and state["n"] == 0:
            state["n"] += 1
            flip_byte(src, 100)                 # staging copy went bad
            out = real(src, dst, **kw)          # ... and is written over the old version on tape
            os.remove(env.src("Tom", "a.raw"))  # the NAS file is gone before it can be re-staged
            return out
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)
    assert env.run("T1")[0] == "done"
    # every ARCHIVED record must still be true
    for r in env.rows("SELECT * FROM files WHERE status='ARCHIVED'"):
        content = tape_content(env, r, "Tom")
        assert content is not None and sha(content) == r["sha256"], f"record lies: {r['rel_path']} {r['size']}"


@pytest.mark.xfail(strict=True, reason="BUG: stopping while a changed raw file is written over its old "
                                       "version deletes the old version; after abandon_job its record is "
                                       "still ARCHIVED")
def test_stop_while_overwriting_then_abandon(env, monkeypatch):
    v1 = {"a.raw": blob("a", 200_000)}
    env.make_source("Tom", v1)
    env.add_job("Tom")
    assert env.run("T1")[0] == "done"

    write(env.src("Tom", "a.raw"), blob("a", 300_000, "v2"))
    job2 = env.add_job("Tom")
    monkeypatch.setattr(eng, "TAPE_CHUNK", 4096)
    e = env.engine()
    real_add = e.progress.add

    def add(n):
        real_add(n)
        if n > 0 and e.progress.phase == "tape":
            e.stop()

    e.progress.add = add
    assert e.run("T1") == ("stopped", "")
    env.engine().abandon_job(job2)
    for r in env.rows("SELECT * FROM files WHERE status='ARCHIVED'"):
        content = tape_content(env, r, "Tom")
        assert content is not None and sha(content) == r["sha256"], f"record lies: {r['rel_path']} {r['size']}"


@pytest.mark.xfail(strict=True, reason="BUG: 'already on tape' is decided by size only; an unrecorded file "
                                       "left on tape by an abandoned job is taken for the new version of a "
                                       "same-size file, which is then never written")
def test_orphan_on_tape_taken_for_new_version(env, monkeypatch):
    v1 = {"a.raw": blob("a", 20_000)}
    env.make_source("Una", v1)
    job1 = env.add_job("Una")
    _crash_before_commit(env, monkeypatch)
    assert env.run("T1")[0] == "error"
    monkeypatch.undo()
    env.engine().abandon_job(job1)

    v2 = {"a.raw": blob("a", 20_000, "edited, same size")}
    env.make_source("Una", v2)
    env.add_job("Una")
    assert env.run("T1")[0] == "done"
    check_backup(env, "Una", v2)


@pytest.mark.xfail(strict=True, reason="BUG: REPLACED uses SQLite lower(), which folds ASCII only; a case "
                                       "rename of a non-ASCII name overwrites the old file on the "
                                       "case-insensitive tape but leaves its record ARCHIVED")
def test_non_ascii_case_rename_marks_replaced(env):
    env.make_source("Vic", {"Äb.raw": blob("a", 1_000)})
    job1 = env.add_job("Vic")
    assert env.run("T1")[0] == "done"
    os.rename(env.src("Vic", "Äb.raw"), env.src("Vic", "äb.raw"))
    write(env.src("Vic", "äb.raw"), blob("a", 2_000, "v2"))
    env.add_job("Vic")
    assert env.run("T1")[0] == "done"
    old = env.file_row("Äb.raw", job1)
    assert old["status"] == "REPLACED", "old record still claims 1000 bytes that were overwritten on tape"


@pytest.mark.xfail(strict=True, reason="BUG: an unreadable sub-folder at scan time is only logged; its files "
                                       "never become rows, and the job ends DONE with 0 failed files")
def test_unreadable_subfolder_is_not_silently_dropped(env, monkeypatch):
    spec = mixed_spec()
    spec["locked\\secret.raw"] = blob("s", 1000)
    env.make_source("Wes", spec)
    job_id = env.add_job("Wes")
    real_scandir = os.scandir

    def scandir(path):
        if str(path).rstrip("\\").endswith("locked"):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    status, message = env.run("T1")
    monkeypatch.undo()
    job = env.one("SELECT * FROM jobs WHERE id=?", job_id)
    assert job["failed_files"] > 0 or job["status"] != "DONE" or status != "done", (
        f"job finished as {job['status']} with failed_files={job['failed_files']}: {message}")


@pytest.mark.xfail(strict=True, reason="BUG: a staging copy finished but not yet recorded is never cleaned "
                                       "when that source file later fails (FAILED rows are not cleaned)")
def test_no_staging_leak_after_crash_and_source_loss(env, monkeypatch):
    spec = mixed_spec()
    env.make_source("Xia", spec)
    env.add_job("Xia")
    env.cfg.threads = 1

    def hook(real, src, dst, kw):
        out = real(src, dst, **kw)
        if under(dst, env.staging) and dst.endswith("r2.raw"):
            raise Crash()
        return out

    patch_copy(monkeypatch, hook)
    assert env.run("T1")[0] == "error"
    monkeypatch.undo()
    os.remove(env.src("Xia", "r2.raw"))
    assert env.run("T1")[0] == "done"
    good = {k: v for k, v in spec.items() if k != "r2.raw"}
    check_backup(env, "Xia", good, failed=("r2.raw",))


@pytest.mark.xfail(strict=True, reason="BUG: _plan_batch reserves TAR_END_OVERHEAD whenever any non-raw file "
                                       "is pending, then fails ALL remaining files although a raw file fits")
def test_raw_file_that_fits_is_not_failed(env):
    spec = {"fits.raw": blob("f", 95_000), "huge.txt": blob("h", 200_000)}
    env.make_source("Yan", spec)
    env.add_job("Yan")
    env.stage_cap = 100_000
    assert env.run("T1")[0] == "done"
    assert env.file_row("fits.raw")["status"] == "ARCHIVED"


@pytest.mark.xfail(strict=True, reason="BUG: files failed by _plan_batch (too large) are not reflected in the "
                                       "progress; job_done stays below job_total after the job finished")
def test_progress_complete_after_too_large_file(env):
    spec = {"big.raw": blob("big", 200_000), "s.raw": blob("s", 20_000)}
    env.make_source("Zoe", spec)
    env.add_job("Zoe")
    env.stage_cap = 100_000
    e = env.engine()
    assert e.run("T1")[0] == "done"
    snap = e.progress.snapshot()
    assert snap["job_done"] == snap["job_total"], snap


@pytest.mark.xfail(strict=True, reason="BUG: a batch is planned once with scan-time sizes; if a file grew and "
                                       "the batch no longer fits on staging, every resume hits the same "
                                       "'staging disk full' error forever")
def test_file_grown_past_staging_budget_does_not_block_job(env, monkeypatch):
    spec = {"a.raw": blob("a", 25_000), "b.raw": blob("b", 25_000)}
    env.make_source("Abe", spec)
    env.add_job("Abe")
    env.cfg.threads = 1
    env.stage_cap = 60_000

    def hook(real, src, dst, kw):  # a real disk would fail with ENOSPC
        if under(dst, env.staging) and dir_bytes(env.staging) + os.path.getsize(lp(src)) > env.stage_cap:
            raise CopyError("dst", dst, OSError(28, "No space left on device"))
        return real(src, dst, **kw)

    patch_copy(monkeypatch, hook)

    def on_log(line):
        if "扫描完成" in line:
            write(env.src("Abe", "b.raw"), blob("b", 45_000, "grown"))

    results = [env.run("T1", on_log=on_log)]
    for _ in range(3):
        if results[-1][0] == "done":
            break
        results.append(env.run("T1"))
    assert results[-1][0] == "done", results


@pytest.mark.xfail(strict=True, reason="BUG: TAR_MEMBER_OVERHEAD (2048) is too small for long non-ASCII "
                                       "names (PAX record > 512 bytes); the tar outgrows the planned budget")
def test_tar_fits_planned_budget_with_long_chinese_names(env):
    spec = {("子" * 200) + f"{i:02}.txt": blob(str(i), 513) for i in range(40)}
    env.make_source("Bea", spec)
    env.add_job("Bea")
    env.stage_cap = 40 * (513 + eng.TAR_MEMBER_OVERHEAD) + eng.TAR_END_OVERHEAD
    assert env.run("T1")[0] == "done"
    for b in env.rows("SELECT * FROM batches"):
        assert b["tar_size"] <= env.stage_cap, f"tar of {b['tar_size']} bytes on a {env.stage_cap}-byte disk"


@pytest.mark.xfail(strict=True, reason="BUG: names ending in '.' or ' ' are listed via FindFirstFile but opened "
                                       "without the \\\\?\\ prefix, so Windows strips the dot/space and the "
                                       "twin file is backed up instead")
def test_trailing_dot_name_backs_up_its_own_content(env):
    spec = {"notes": blob("plain", 1000), "notes.": blob("dot", 1000), "x.raw": blob("x", 100)}
    env.make_source("Cal", spec)
    env.add_job("Cal")
    assert env.run("T1")[0] == "done"
    check_backup(env, "Cal", spec)
