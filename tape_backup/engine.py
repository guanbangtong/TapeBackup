"""Backup engine: scan -> stage on the local disk -> write to tape -> verify -> clean up, batch by batch.

No UI code here. A front end drives it like this:

    engine = Engine(cfg, on_log=..., ask_tape=..., on_job_update=...)
    result, message = engine.run(tape_id)      # blocking; call it from a worker thread
    engine.stop()                              # from any thread; run() returns ("stopped", "")
    engine.progress.snapshot()                 # from any thread; poll it with a timer

Callbacks (all called from the thread that runs run()):
    on_log(text)                       one human-readable line
    ask_tape(reason, suggested) -> id  a tape must be inserted/changed; block until the user answers,
                                       return the tape id they confirmed, or None to stop
    on_job_update(job_id)              a job's row changed (status, counters)

Every step is recorded in the database before the next one starts, so an interrupted
run (stop button, crash, power loss) continues where it left off the next time run() is called.

The one rule that protects data: staging copies are deleted only in _cleanup, and _cleanup
only runs for a batch whose row says ARCHIVED, which is written only after every file of the
batch was found on tape with the right size.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import sqlite3
import tarfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable

from . import db
from .config import Config
from .copier import (
    TAPE_CHUNK,
    CopyError,
    Stopped,
    TarWriter,
    copy_file,
    format_bytes,
    hash_file,
    list_tar,
    long_path,
    prune_empty_dirs,
    remove_quietly,
    with_retry,
)

# Space a file needs inside a tar beyond its own size: header, PAX record for long or
# non-ASCII names, and padding to a 512-byte boundary.
TAR_MEMBER_OVERHEAD = 2048
TAR_END_OVERHEAD = tarfile.RECORDSIZE
MAX_ATTEMPTS = 3

PHASES = ("scan", "stage", "tape", "verify", "cleanup")


class EngineError(Exception):
    """The run cannot continue (tape, staging disk or NAS problem). The message is shown to the user."""


# ---------------------------------------------------------------- on-media layout

def archive_dir_name(owner: str) -> str:
    return f"{owner}_archive"


def raw_media_path(root: str, owner: str, rel_path: str) -> str:
    """Where a raw file lives under a staging or tape root: <root>\\<owner>_archive\\<owner>\\<rel_path>."""
    return os.path.join(root, archive_dir_name(owner), owner, rel_path)


def tar_media_path(root: str, owner: str, tar_name: str) -> str:
    return os.path.join(root, archive_dir_name(owner), tar_name)


def tar_member_name(owner: str, rel_path: str) -> str:
    """Name of a non-raw file inside its batch tar: <owner>/<rel_path with forward slashes>."""
    return f"{owner}/{rel_path.replace(os.sep, '/')}"


# ---------------------------------------------------------------- tape identity
#
# The tape id is kept in the NAME of an empty file in the tape root (TAPE_ID_MB2FEQ.txt).
# LTFS keeps file names in its index, which is in memory once the tape is mounted, so
# checking the id never makes the drive seek away from where it is writing.

TAPE_MARKER_PREFIX = "TAPE_ID_"
TAPE_MARKER_SUFFIX = ".txt"
_TAPE_ID_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]*")


def normalize_tape_id(text: str | None) -> str:
    """Upper-case and validate a tape id; '' if it is not usable."""
    text = (text or "").strip().upper()
    return text if _TAPE_ID_RE.fullmatch(text) else ""


def read_tape_marker(tape_root: str) -> str | None:
    """The tape id recorded on the inserted tape, or None for a tape never used by this program."""
    for name in sorted(os.listdir(tape_root)):
        upper = name.upper()
        if upper.startswith(TAPE_MARKER_PREFIX) and upper.endswith(TAPE_MARKER_SUFFIX.upper()):
            tape_id = normalize_tape_id(name[len(TAPE_MARKER_PREFIX): -len(TAPE_MARKER_SUFFIX)])
            if tape_id:
                return tape_id
    return None


def write_tape_marker(tape_root: str, tape_id: str) -> None:
    with open(os.path.join(tape_root, f"{TAPE_MARKER_PREFIX}{tape_id}{TAPE_MARKER_SUFFIX}"), "wb"):
        pass


# ---------------------------------------------------------------- progress

class Progress:
    """Live numbers for the UI. Copy threads only add to counters; the UI polls snapshot()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.job_id: int | None = None
        self.owner = ""
        self.phase = "idle"          # idle | scan | stage | tape | verify | cleanup
        self.phase_unit = "bytes"    # bytes | files
        self.phase_done = 0
        self.phase_total = 0
        self.job_done = 0            # every byte counts twice: once staged, once on tape
        self.job_total = 0
        self.batch_seq = 0
        self.batch_estimate = 0      # expected number of batches for this job
        self.current_file = ""
        self.scan_files = 0
        self.scan_bytes = 0
        self.tape_id = ""

    def add(self, n: int) -> None:
        with self._lock:
            self.phase_done += n
            self.job_done += n

    def step(self) -> None:
        """One item done in a phase counted in files."""
        with self._lock:
            self.phase_done += 1

    def drop(self, size: int) -> None:
        """A file left the current phase without being processed (failed or put back in the queue)."""
        with self._lock:
            self.phase_total -= size

    def snapshot(self) -> dict:
        with self._lock:
            job_total = self.job_total
            return {
                "job_id": self.job_id,
                "owner": self.owner,
                "phase": self.phase,
                "phase_unit": self.phase_unit,
                "phase_done": max(0, min(self.phase_done, self.phase_total)),
                "phase_total": max(0, self.phase_total),
                "job_done": max(0, min(self.job_done, job_total)),
                "job_total": job_total,
                "batch_seq": self.batch_seq,
                "batch_estimate": self.batch_estimate,
                "current_file": self.current_file,
                "scan_files": self.scan_files,
                "scan_bytes": self.scan_bytes,
                "tape_id": self.tape_id,
            }


# ---------------------------------------------------------------- engine

class Engine:
    def __init__(
        self,
        cfg: Config,
        *,
        on_log: Callable[[str], None] | None = None,
        ask_tape: Callable[[str, str | None], str | None] | None = None,
        on_job_update: Callable[[int], None] | None = None,
        disk_free: Callable[[str], int] | None = None,
        retry_delay: float = 2.0,
    ) -> None:
        self.cfg = cfg
        self.progress = Progress()
        self.tape_id = ""
        self._on_log = on_log
        self._ask_tape = ask_tape
        self._on_job_update = on_job_update
        self._free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self._retry_delay = retry_delay
        self._stop = threading.Event()
        self.con: sqlite3.Connection = None  # type: ignore[assignment]  # opened in run()

    # ------------------------------------------------------------ public

    def stop(self) -> None:
        """Ask the running engine to stop. The file in flight is abandoned and redone next time."""
        self._stop.set()

    def run(self, tape_id: str) -> tuple[str, str]:
        """Process every unfinished and queued job. Returns ('done'|'stopped'|'error', message)."""
        self._stop.clear()
        self.tape_id = normalize_tape_id(tape_id)
        self.progress.tape_id = self.tape_id
        try:
            os.makedirs(self.cfg.staging_root, exist_ok=True)
            self.con = db.connect(self.cfg.db_path)
        except (OSError, sqlite3.Error) as e:
            return "error", f"无法打开记录数据库或中转目录：{e}"
        try:
            jobs = db.active_jobs(self.con)
            if not jobs:
                return "done", "队列里没有需要备份的任务。"
            failed = 0
            for job in jobs:
                failed += self._run_job(job)
            message = f"全部完成，共 {len(jobs)} 个任务。"
            if failed:
                message += f"有 {failed} 个文件未能备份，详见记录页。"
            return "done", message
        except Stopped:
            self.log("已停止。下次点击开始会从中断处继续。")
            return "stopped", ""
        except EngineError as e:
            self.log(f"错误：{e}")
            return "error", str(e)
        except Exception as e:  # a bug or an unexpected system error: stop rather than guess
            self.log("意外错误：\n" + traceback.format_exc())
            return "error", f"意外错误：{e!r}"
        finally:
            self.progress.phase = "idle"
            self.progress.current_file = ""
            self.con.close()

    def abandon_job(self, job_id: int) -> None:
        """Give up an unfinished job. Call only while the engine is not running.

        Staging copies of batches that never reached tape are deleted. What is already
        on tape stays recorded; the job is kept as CANCELLED if it archived anything.
        """
        con = db.connect(self.cfg.db_path)
        try:
            job = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] not in ("RUNNING", "QUEUED"):
                return
            stage_root = os.path.join(self.cfg.staging_root, archive_dir_name(job["owner"]))
            for batch in con.execute("SELECT * FROM batches WHERE job_id=? AND cleaned=0", (job_id,)).fetchall():
                for row in con.execute(
                    "SELECT rel_path FROM files WHERE batch_id=? AND kind='raw'", (batch["id"],)
                ):
                    remove_quietly(raw_media_path(self.cfg.staging_root, job["owner"], row["rel_path"]))
                if batch["tar_name"]:
                    remove_quietly(os.path.join(stage_root, batch["tar_name"]))
            prune_empty_dirs(stage_root)
            with con:
                con.execute("DELETE FROM files WHERE job_id=? AND status NOT IN ('ARCHIVED','REPLACED')", (job_id,))
                con.execute("DELETE FROM batches WHERE job_id=? AND status!='ARCHIVED'", (job_id,))
                con.execute("UPDATE batches SET cleaned=1 WHERE job_id=?", (job_id,))
                kept = con.execute("SELECT COUNT(*) FROM files WHERE job_id=?", (job_id,)).fetchone()[0]
                if kept:
                    self._update_counters(con, job_id)
                    con.execute(
                        "UPDATE jobs SET status='CANCELLED', finished_at=? WHERE id=?", (db.now(), job_id)
                    )
                else:
                    con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        finally:
            con.close()

    def log(self, text: str) -> None:
        line = f"{datetime.now():%H:%M:%S}  {text}"
        try:
            os.makedirs(self.cfg.log_dir, exist_ok=True)
            with open(os.path.join(self.cfg.log_dir, f"{datetime.now():%Y-%m-%d}.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if self._on_log:
            self._on_log(line)

    # ------------------------------------------------------------ job loop

    def _run_job(self, job: sqlite3.Row) -> int:
        """Run one job to completion. Returns the number of files that could not be backed up."""
        con = self.con
        job_id = job["id"]
        p = self.progress
        p.job_id, p.owner = job_id, job["owner"]
        p.batch_seq = p.batch_estimate = 0
        p.job_done = p.job_total = 0
        self.log(f"开始任务：{job['owner']}（{job['source_path']}）")

        con.execute(
            "UPDATE jobs SET status='RUNNING', started_at=COALESCE(started_at, ?) WHERE id=?", (db.now(), job_id)
        )
        con.commit()
        self._notify(job_id)

        if not job["scanned"]:
            self._scan(job)

        while True:
            self._check_stop()
            batch = con.execute(
                "SELECT * FROM batches WHERE job_id=? AND (status!='ARCHIVED' OR cleaned=0) ORDER BY id LIMIT 1",
                (job_id,),
            ).fetchone()
            if batch is None:
                batch = self._plan_batch(job)
                if batch is None:
                    break
            else:
                self.log(f"继续未完成的第 {batch['seq']} 批（状态 {batch['status']}）")
            batch_id = batch["id"]
            self._set_batch_estimate(job_id, batch)

            if batch["status"] == "STAGING":
                if not self._stage(job, batch):
                    continue
                batch = self._batch(batch_id)
            if batch["status"] == "STAGED":
                if not self._to_tape(job, batch):
                    continue
                batch = self._batch(batch_id)
            self._cleanup(job, batch)
            self._notify(job_id)

        return self._finish(job)

    def _finish(self, job: sqlite3.Row) -> int:
        con = self.con
        job_id = job["id"]
        with con:
            self._update_counters(con, job_id)
            con.execute("UPDATE jobs SET status='DONE', finished_at=? WHERE id=?", (db.now(), job_id))
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        tapes = con.execute(
            "SELECT GROUP_CONCAT(DISTINCT tape_id) FROM batches WHERE job_id=? AND status='ARCHIVED'", (job_id,)
        ).fetchone()[0]
        text = f"任务完成：{row['owner']}，{row['done_files']} 个文件，{format_bytes(row['done_bytes'])}"
        if tapes:
            text += f"，磁带 {tapes}"
        if row["failed_files"]:
            text += f"；{row['failed_files']} 个文件未能备份"
        self.log(text)
        self._notify(job_id)

        if self.cfg.db_backup_dir:
            try:
                dest = db.backup_database(con, self.cfg.db_path, self.cfg.db_backup_dir)
                self.log(f"记录数据库已备份到 {dest}")
            except (OSError, sqlite3.Error) as e:
                self.log(f"警告：记录数据库备份到 NAS 失败（{e}），不影响已完成的磁带备份。")
        return row["failed_files"]

    # ------------------------------------------------------------ scan

    def _scan(self, job: sqlite3.Row) -> None:
        """List the source folder and queue every file that is not on tape yet (same path and size)."""
        con = self.con
        source = job["source_path"]
        p = self.progress
        p.phase, p.phase_unit = "scan", "files"
        p.phase_done = p.phase_total = 0
        p.scan_files = p.scan_bytes = 0
        p.current_file = ""
        self.log(f"扫描 {source} ...")

        archived = {
            (row["rel_path"].lower(), row["size"])
            for row in con.execute(
                "SELECT f.rel_path, f.size FROM files f JOIN jobs j ON j.id=f.job_id "
                "WHERE j.owner=? COLLATE NOCASE AND f.status='ARCHIVED'",
                (job["owner"],),
            )
        }

        found: list[tuple[str, int, float, str]] = []
        skipped = unreadable = 0
        stack = [""]
        while stack:
            self._check_stop()
            rel_dir = stack.pop()
            abs_dir = os.path.join(source, rel_dir) if rel_dir else source
            try:
                entries = os.scandir(long_path(abs_dir))
            except OSError as e:
                if not rel_dir:
                    raise EngineError(f"无法读取源文件夹 {source}：{e}") from e
                unreadable += 1
                self.log(f"警告：无法读取子文件夹，已跳过：{rel_dir}（{e}）")
                continue
            with entries:
                for entry in entries:
                    rel = os.path.join(rel_dir, entry.name) if rel_dir else entry.name
                    try:
                        if entry.is_symlink() or entry.is_junction():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(rel)
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        unreadable += 1
                        self.log(f"警告：无法读取，已跳过：{rel}（{e}）")
                        continue
                    if (rel.lower(), st.st_size) in archived:
                        skipped += 1
                        continue
                    kind = "raw" if self.cfg.is_raw(entry.name) else "nonraw"
                    found.append((rel, st.st_size, st.st_mtime, kind))
                    p.scan_files += 1
                    p.scan_bytes += st.st_size
                    p.current_file = rel

        found.sort(key=lambda item: item[0].lower())
        total_bytes = sum(item[1] for item in found)
        with con:  # all or nothing: a half-recorded scan would silently drop files
            con.executemany(
                "INSERT INTO files(job_id, rel_path, size, mtime, kind) VALUES (?,?,?,?,?)",
                [(job["id"], rel, size, mtime, kind) for rel, size, mtime, kind in found],
            )
            con.execute(
                "UPDATE jobs SET scanned=1, total_files=?, total_bytes=? WHERE id=?",
                (len(found), total_bytes, job["id"]),
            )
        text = f"扫描完成：{len(found)} 个文件待备份，共 {format_bytes(total_bytes)}"
        if skipped:
            text += f"；{skipped} 个文件已在磁带上，跳过"
        if unreadable:
            text += f"；{unreadable} 项无法读取"
        self.log(text)
        self._notify(job["id"])

    # ------------------------------------------------------------ batch planning

    def _plan_batch(self, job: sqlite3.Row) -> sqlite3.Row | None:
        """Pick the pending files for the next batch: as many as fit on the staging disk and on the tape."""
        con = self.con
        job_id = job["id"]
        while True:
            pending = con.execute(
                "SELECT id, size, kind FROM files WHERE job_id=? AND status='PENDING' AND batch_id IS NULL "
                "ORDER BY (kind='raw'), id",  # non-raw first so the tar goes out with the first batch
                (job_id,),
            ).fetchall()
            if not pending:
                return None

            self._ensure_tape(0)
            stage_cap = self._free(self.cfg.staging_root) - self.cfg.reserve_staging_bytes
            tape_cap = self._free(self.cfg.tape_root) - self.cfg.reserve_tape_bytes
            remaining = min(stage_cap, tape_cap)
            if any(row["kind"] == "nonraw" for row in pending):
                remaining -= TAR_END_OVERHEAD

            selected: list[int] = []
            has_tar = False
            smallest = None
            for row in pending:
                cost = row["size"] + (TAR_MEMBER_OVERHEAD if row["kind"] == "nonraw" else 0)
                if smallest is None or cost < smallest:
                    smallest = cost
                if cost <= remaining:
                    selected.append(row["id"])
                    remaining -= cost
                    has_tar = has_tar or row["kind"] == "nonraw"
            if selected:
                break

            assert smallest is not None
            if smallest + TAR_END_OVERHEAD <= stage_cap:
                # The staging disk has room but the tape does not: change tape, then plan again.
                self._ensure_tape(smallest + TAR_END_OVERHEAD)
                continue
            reason = (
                f"文件大于中转盘可用空间（可用 {format_bytes(max(stage_cap, 0))}）。"
                "请清理中转盘后把这个文件夹重新加入队列，只会备份剩下的文件。"
            )
            with con:
                con.execute(
                    "UPDATE files SET status='FAILED', error=? "
                    "WHERE job_id=? AND status='PENDING' AND batch_id IS NULL",
                    (reason, job_id),
                )
            self.log(f"警告：剩余 {len(pending)} 个文件无法暂存：{reason}")
            return None

        seq = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM batches WHERE job_id=?", (job_id,)).fetchone()[0]
        with con:
            cur = con.execute(
                "INSERT INTO batches(job_id, seq, tape_id, status, created_at) VALUES (?,?,?, 'STAGING', ?)",
                (job_id, seq, self.tape_id, db.now()),
            )
            batch_id = cur.lastrowid
            assert batch_id is not None
            if has_tar:
                # Timestamp + batch id: never collides with a tar already on a tape.
                tar_name = f"{job['owner']}_nonraw_{datetime.now():%Y%m%d_%H%M%S}_b{batch_id}.tar"
                con.execute("UPDATE batches SET tar_name=? WHERE id=?", (tar_name, batch_id))
            con.executemany("UPDATE files SET batch_id=? WHERE id=?", [(batch_id, fid) for fid in selected])
        self.log(f"第 {seq} 批：{len(selected)} 个文件")
        return self._batch(batch_id)

    def _set_batch_estimate(self, job_id: int, batch: sqlite3.Row) -> None:
        con = self.con
        in_batch = con.execute(
            "SELECT COALESCE(SUM(size), 0) FROM files WHERE batch_id=?", (batch["id"],)
        ).fetchone()[0]
        waiting = con.execute(
            "SELECT COALESCE(SUM(size), 0) FROM files WHERE job_id=? AND status='PENDING' AND batch_id IS NULL",
            (job_id,),
        ).fetchone()[0]
        self.progress.batch_seq = batch["seq"]
        self.progress.batch_estimate = batch["seq"] + (math.ceil(waiting / in_batch) if in_batch else 0)

    # ------------------------------------------------------------ stage: NAS -> staging disk

    def _stage(self, job: sqlite3.Row, batch: sqlite3.Row) -> bool:
        """Bring the batch onto the staging disk. Returns False if nothing could be staged."""
        con = self.con
        owner, batch_id = job["owner"], batch["id"]
        stage_root = os.path.join(self.cfg.staging_root, archive_dir_name(owner))
        tar_path = os.path.join(stage_root, batch["tar_name"]) if batch["tar_name"] else None

        rows = con.execute(
            "SELECT * FROM files WHERE batch_id=? AND status IN ('PENDING','STAGED') ORDER BY id", (batch_id,)
        ).fetchall()
        nonraw = [r for r in rows if r["kind"] == "nonraw"]
        raw = [r for r in rows if r["kind"] == "raw"]

        # What an interrupted earlier run left behind is reused only if it is provably complete.
        tar_ready = bool(
            nonraw
            and tar_path
            and batch["tar_size"] is not None
            and all(r["status"] == "STAGED" for r in nonraw)
            and _size(tar_path) == batch["tar_size"]
        )
        raw_todo = [
            r for r in raw
            if not (r["status"] == "STAGED" and _size(raw_media_path(self.cfg.staging_root, owner, r["rel_path"])) == r["size"])
        ]
        with con:
            if nonraw and not tar_ready:
                con.execute(
                    "UPDATE files SET status='PENDING', sha256=NULL WHERE batch_id=? AND kind='nonraw'", (batch_id,)
                )
                con.execute("UPDATE batches SET tar_size=NULL, tar_sha256=NULL WHERE id=?", (batch_id,))
            con.executemany(
                "UPDATE files SET status='PENDING', sha256=NULL WHERE id=?", [(r["id"],) for r in raw_todo]
            )

        todo_bytes = sum(r["size"] for r in raw_todo) + (0 if tar_ready else sum(r["size"] for r in nonraw))
        total_bytes = sum(r["size"] for r in rows)
        self._begin_phase(job["id"], "stage", done=total_bytes - todo_bytes, total=total_bytes)
        self.log(f"第 {batch['seq']} 批：暂存到中转盘，{format_bytes(todo_bytes)}")

        if nonraw and not tar_ready:
            assert tar_path is not None
            self._build_tar(job, batch, nonraw, tar_path)
        if raw_todo:
            self._stage_raw(job, raw_todo)

        staged = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN kind='raw' THEN size END), 0) "
            "FROM files WHERE batch_id=? AND status='STAGED'",
            (batch_id,),
        ).fetchone()
        if staged[0] == 0:
            with con:
                con.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            prune_empty_dirs(stage_root)
            self.log(f"第 {batch['seq']} 批没有可备份的文件，已取消。")
            return False
        tar_size = self._batch(batch_id)["tar_size"] or 0
        with con:
            con.execute("UPDATE batches SET status='STAGED', bytes=? WHERE id=?", (staged[1] + tar_size, batch_id))
        return True

    def _build_tar(self, job: sqlite3.Row, batch: sqlite3.Row, nonraw: list[sqlite3.Row], tar_path: str) -> None:
        con = self.con
        owner, source = job["owner"], job["source_path"]
        remove_quietly(tar_path)
        done: list[tuple[int, str, int]] = []  # (size, sha256, file id)
        with TarWriter(tar_path) as tar:
            for row in nonraw:
                self._check_stop()
                self.progress.current_file = row["rel_path"]
                src = os.path.join(source, row["rel_path"])
                arcname = tar_member_name(owner, row["rel_path"])
                try:
                    size, sha = with_retry(
                        lambda: tar.add(src, arcname, should_stop=self._stop.is_set, on_bytes=self.progress.add),
                        delay=self._retry_delay,
                        should_stop=self._stop.is_set,
                    )
                except CopyError as e:
                    if e.side == "dst":
                        raise EngineError(f"写入中转盘失败（空间不足？）：{e}") from e
                    self._source_failure(job, row, e)
                    continue
                done.append((size, sha, row["id"]))

        if not done:
            remove_quietly(tar_path)
            with con:
                con.execute("UPDATE batches SET tar_name=NULL, tar_size=NULL WHERE id=?", (batch["id"],))
            return
        try:  # read the tar back: every member must be there with the size we wrote
            members = list_tar(tar_path)
        except (tarfile.TarError, OSError) as e:
            raise EngineError(f"tar 包校验失败：{tar_path}（{e}）") from e
        if len(members) != len(done) or sum(m[1] for m in members) != sum(d[0] for d in done):
            raise EngineError(f"tar 包内容与预期不符：{tar_path}")
        with con:  # members and tar size are recorded together: that pair is what marks the tar complete
            con.executemany("UPDATE files SET status='STAGED', size=?, sha256=? WHERE id=?", done)
            con.execute("UPDATE batches SET tar_size=? WHERE id=?", (_size(tar_path), batch["id"]))

    def _stage_raw(self, job: sqlite3.Row, rows: list[sqlite3.Row]) -> None:
        con = self.con
        owner, source = job["owner"], job["source_path"]
        abort = threading.Event()  # set when the main thread hits a fatal error, so workers stop too

        def should_stop() -> bool:
            return self._stop.is_set() or abort.is_set()

        def work(rel_path: str) -> tuple[int, str]:
            self.progress.current_file = rel_path
            src = os.path.join(source, rel_path)
            dst = raw_media_path(self.cfg.staging_root, owner, rel_path)
            return with_retry(
                lambda: copy_file(src, dst, should_stop=should_stop, on_bytes=self.progress.add),
                delay=self._retry_delay,
                should_stop=should_stop,
            )

        pool = ThreadPoolExecutor(max_workers=max(1, int(self.cfg.threads)))
        try:
            futures = {pool.submit(work, r["rel_path"]): r for r in rows}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    size, sha = future.result()
                except CopyError as e:
                    if e.side == "dst":
                        raise EngineError(f"写入中转盘失败（空间不足？）：{e}") from e
                    self._source_failure(job, row, e)
                    continue
                con.execute("UPDATE files SET status='STAGED', size=?, sha256=? WHERE id=?", (size, sha, row["id"]))
                con.commit()
        except BaseException:
            abort.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def _source_failure(self, job: sqlite3.Row, row: sqlite3.Row, error: CopyError) -> None:
        """A source file could not be read after retries: skip that file, or stop if the NAS itself is gone."""
        if not os.path.isdir(long_path(job["source_path"])):
            raise EngineError(
                f"源文件夹无法访问：{job['source_path']}（{error.cause}）。"
                "请检查 NAS 连接后重新点击开始，会从中断处继续。"
            )
        with self.con:
            self.con.execute(
                "UPDATE files SET status='FAILED', batch_id=NULL, error=? WHERE id=?", (str(error.cause), row["id"])
            )
        self.progress.drop(row["size"])
        self.log(f"警告：读取失败，已跳过：{row['rel_path']}（{error.cause}）")

    # ------------------------------------------------------------ tape: staging disk -> tape

    def _to_tape(self, job: sqlite3.Row, batch: sqlite3.Row) -> bool:
        """Write the staged batch to tape and verify it. Returns False if nothing was left to archive."""
        con = self.con
        owner, batch_id = job["owner"], batch["id"]
        staging, tape = self.cfg.staging_root, self.cfg.tape_root

        rows = con.execute("SELECT * FROM files WHERE batch_id=? AND status='STAGED'", (batch_id,)).fetchall()
        nonraw = [r for r in rows if r["kind"] == "nonraw"]
        raw = sorted((r for r in rows if r["kind"] == "raw"), key=lambda r: r["rel_path"].lower())

        # (label, source on staging disk, destination on tape, size, file row or None for the tar)
        items: list[tuple[str, str, str, int, sqlite3.Row | None]] = []
        if nonraw:
            name = batch["tar_name"]
            items.append((name, tar_media_path(staging, owner, name), tar_media_path(tape, owner, name),
                          batch["tar_size"], None))
        for r in raw:
            items.append((r["rel_path"], raw_media_path(staging, owner, r["rel_path"]),
                          raw_media_path(tape, owner, r["rel_path"]), r["size"], r))

        # After an interruption, files that are already on tape with the right size are not written again.
        on_tape_already = {i for i, item in enumerate(items) if _size(item[2]) == item[3]}
        need = sum(item[3] for i, item in enumerate(items) if i not in on_tape_already)
        self._ensure_tape(need)
        if batch["tape_id"] != self.tape_id:
            with con:
                con.execute("UPDATE batches SET tape_id=? WHERE id=?", (self.tape_id, batch_id))
            self.log(f"第 {batch['seq']} 批改为写入磁带 {self.tape_id}")

        total = sum(item[3] for item in items)
        self._begin_phase(job["id"], "tape", done=total - need, total=total, on_tape=total - need)
        self.log(f"第 {batch['seq']} 批：写入磁带 {self.tape_id}，{format_bytes(need)}")

        for index, (label, src, dst, size, row) in enumerate(items):
            self._check_stop()
            self.progress.current_file = label
            if index in on_tape_already:
                if row is None and self._batch(batch_id)["tar_sha256"] is None:
                    try:
                        sha = hash_file(src, should_stop=self._stop.is_set)
                    except OSError:
                        sha = None
                    con.execute("UPDATE batches SET tar_sha256=? WHERE id=?", (sha, batch_id))
                    con.commit()
                continue
            try:
                _copied, sha = with_retry(
                    lambda: copy_file(src, dst, should_stop=self._stop.is_set,
                                      on_bytes=self.progress.add, chunk=TAPE_CHUNK),
                    delay=self._retry_delay,
                    should_stop=self._stop.is_set,
                )
            except CopyError as e:
                if e.side == "dst":
                    raise EngineError(
                        f"写入磁带失败：{e}。中转盘上的数据已保留，处理好磁带后重新点击开始即可继续。"
                    ) from e
                self._requeue(job, batch, row, nonraw, size, f"中转盘上的文件无法读取（{e.cause}）")
                continue
            if row is None:
                con.execute("UPDATE batches SET tar_sha256=? WHERE id=?", (sha, batch_id))
                con.commit()
            elif sha != row["sha256"]:
                # What was read back from the staging disk is not what came from the NAS.
                remove_quietly(dst)
                self.progress.add(-size)
                self._requeue(job, batch, row, nonraw, size, "中转盘上的副本与源文件校验值不一致")

        # Verify: every file of the batch must exist on tape with the expected size.
        rows = con.execute("SELECT * FROM files WHERE batch_id=? AND status='STAGED'", (batch_id,)).fetchall()
        if not rows:
            with con:
                con.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            self.log(f"第 {batch['seq']} 批的文件都需要重新暂存，已退回队列。")
            return False
        staged_ids = {r["id"] for r in rows}
        has_tar = any(r["kind"] == "nonraw" for r in rows)
        checks = [item for item in items if (item[4] is None and has_tar) or (item[4] is not None and item[4]["id"] in staged_ids)]
        self._begin_phase(job["id"], "verify", done=0, total=len(checks), unit="files",
                          on_tape=sum(item[3] for item in checks))
        for label, _src, dst, size, _row in checks:
            self._check_stop()
            self.progress.current_file = label
            on_tape = _size(dst)
            if on_tape != size:
                found = "不存在" if on_tape is None else f"大小为 {on_tape}"
                raise EngineError(
                    f"磁带校验失败：{label} 在磁带上{found}，应为 {size} 字节。"
                    "中转盘上的数据已保留，重新点击开始会重写这个文件。"
                )
            self.progress.step()

        # Only now is the batch recorded as archived; _cleanup deletes staging copies based on this record.
        stamp = db.now()
        tar_size = batch["tar_size"] if has_tar else 0
        with con:
            earlier = con.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner=? COLLATE NOCASE AND id!=? AND legacy=0",
                (owner, job["id"]),
            ).fetchone()[0]
            if earlier:
                # A changed file was written over its old copy on this tape: the old record no longer exists there.
                con.execute(
                    "UPDATE files SET status='REPLACED' "
                    "WHERE status='ARCHIVED' AND kind='raw' AND tape_id=? "
                    "AND job_id IN (SELECT id FROM jobs WHERE owner=? COLLATE NOCASE) "
                    "AND lower(rel_path) IN (SELECT lower(rel_path) FROM files "
                    "                        WHERE batch_id=? AND kind='raw' AND status='STAGED')",
                    (self.tape_id, owner, batch_id),
                )
            con.execute(
                "UPDATE files SET status='ARCHIVED', tape_id=?, "
                "tar_name=CASE WHEN kind='nonraw' THEN ? END WHERE batch_id=? AND status='STAGED'",
                (self.tape_id, batch["tar_name"], batch_id),
            )
            con.execute(
                "UPDATE batches SET status='ARCHIVED', archived_at=?, bytes=? WHERE id=?",
                (stamp, sum(r["size"] for r in rows if r["kind"] == "raw") + tar_size, batch_id),
            )
            self._update_counters(con, job["id"])
        self.log(f"第 {batch['seq']} 批已写入磁带 {self.tape_id} 并校验通过（{len(rows)} 个文件）")
        return True

    def _requeue(self, job: sqlite3.Row, batch: sqlite3.Row, row: sqlite3.Row | None,
                 nonraw: list[sqlite3.Row], size: int, reason: str) -> None:
        """A staged copy turned out unusable: send the file (or all members of the tar) back to be staged again."""
        con = self.con
        owner = job["owner"]
        if row is None:  # the tar
            targets = nonraw
            remove_quietly(tar_media_path(self.cfg.staging_root, owner, batch["tar_name"]))
            label = batch["tar_name"]
        else:
            targets = [row]
            remove_quietly(raw_media_path(self.cfg.staging_root, owner, row["rel_path"]))
            label = row["rel_path"]
        with con:
            for target in targets:
                if target["attempts"] + 1 >= MAX_ATTEMPTS:
                    con.execute(
                        "UPDATE files SET status='FAILED', batch_id=NULL, error=?, attempts=attempts+1 WHERE id=?",
                        (reason, target["id"]),
                    )
                else:
                    con.execute(
                        "UPDATE files SET status='PENDING', batch_id=NULL, sha256=NULL, attempts=attempts+1 "
                        "WHERE id=?",
                        (target["id"],),
                    )
            if row is None:
                con.execute(
                    "UPDATE batches SET tar_name=NULL, tar_size=NULL, tar_sha256=NULL WHERE id=?", (batch["id"],)
                )
        self.progress.drop(size)
        self.log(f"警告：{label}：{reason}，将重新暂存。")

    # ------------------------------------------------------------ cleanup

    def _cleanup(self, job: sqlite3.Row, batch: sqlite3.Row) -> None:
        """Delete the staging copies of a batch that is recorded as ARCHIVED."""
        con = self.con
        batch = self._batch(batch["id"])
        if batch["status"] != "ARCHIVED":  # the safety rule from the module docstring
            raise EngineError(f"内部错误：第 {batch['seq']} 批尚未归档，拒绝删除中转文件。")
        owner = job["owner"]
        stage_root = os.path.join(self.cfg.staging_root, archive_dir_name(owner))
        self._begin_phase(job["id"], "cleanup", done=0, total=0, unit="files")

        paths = [
            raw_media_path(self.cfg.staging_root, owner, r["rel_path"])
            for r in con.execute(
                "SELECT rel_path FROM files WHERE batch_id=? AND kind='raw' AND status IN ('ARCHIVED','REPLACED')",
                (batch["id"],),
            )
        ]
        if batch["tar_name"]:
            paths.append(os.path.join(stage_root, batch["tar_name"]))
        for path in paths:
            try:
                os.remove(long_path(path))
            except FileNotFoundError:
                pass
            except OSError as e:
                raise EngineError(
                    f"磁带已写入并校验通过，但无法删除中转文件 {path}（{e}）。"
                    "请关闭占用它的程序后重新点击开始。"
                ) from e
        prune_empty_dirs(stage_root)
        with con:
            con.execute("UPDATE batches SET cleaned=1 WHERE id=?", (batch["id"],))

    # ------------------------------------------------------------ tape handling

    def _ensure_tape(self, need_bytes: int) -> None:
        """Make sure the right tape is inserted, identified, and has need_bytes free; ask the user otherwise."""
        root = self.cfg.tape_root
        while True:
            self._check_stop()
            suggested = None
            if not os.path.isdir(root):
                reason = f"找不到磁带盘 {root}，请插入磁带。"
            else:
                try:
                    marker = read_tape_marker(root)
                except OSError as e:
                    marker = None
                    reason = f"无法读取磁带 {root}（{e}），请检查磁带。"
                else:
                    free = self._free(root) - self.cfg.reserve_tape_bytes
                    if marker and marker != self.tape_id:
                        suggested = marker
                        reason = (
                            f"磁带上记录的磁带号是 {marker}，与输入的 {self.tape_id or '（空）'} 不一致。"
                            "请确认磁带号。"
                        )
                    elif not self.tape_id:
                        reason = "请输入当前磁带的磁带号。"
                    elif free < need_bytes:
                        reason = (
                            f"磁带 {self.tape_id} 剩余空间不足（可用 {format_bytes(max(free, 0))}，"
                            f"需要 {format_bytes(need_bytes)}）。请换一盘磁带，并输入新磁带的磁带号。"
                        )
                    else:
                        if not marker:
                            try:
                                write_tape_marker(root, self.tape_id)
                            except OSError as e:
                                raise EngineError(f"无法写入磁带 {root}（是否写保护？）：{e}") from e
                            self.log(f"已在磁带上标记磁带号 {self.tape_id}")
                        return
            self.log(reason)
            answer = self._ask_tape(reason, suggested) if self._ask_tape else None
            new_id = normalize_tape_id(answer)
            if not new_id:
                raise Stopped()
            self.tape_id = new_id
            self.progress.tape_id = new_id

    # ------------------------------------------------------------ helpers

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise Stopped()

    def _notify(self, job_id: int) -> None:
        if self._on_job_update:
            self._on_job_update(job_id)

    def _batch(self, batch_id: int) -> sqlite3.Row:
        return self.con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()

    def _begin_phase(self, job_id: int, phase: str, *, done: int, total: int, unit: str = "bytes",
                     on_tape: int = 0) -> None:
        """Switch the progress display to a new phase and re-base the job total from the database.

        on_tape: bytes of the current batch that are on tape but not yet recorded as ARCHIVED.
        """
        sums = dict(
            self.con.execute(
                "SELECT status, COALESCE(SUM(size), 0) FROM files WHERE job_id=? GROUP BY status", (job_id,)
            ).fetchall()
        )
        archived = sums.get("ARCHIVED", 0) + sums.get("REPLACED", 0)
        staged = sums.get("STAGED", 0) + archived
        failed = sums.get("FAILED", 0)
        p = self.progress
        with p._lock:
            p.phase, p.phase_unit = phase, unit
            p.phase_done, p.phase_total = done, total
            p.job_total = 2 * sum(sums.values())
            p.job_done = staged + archived + 2 * failed + on_tape
            p.current_file = ""

    @staticmethod
    def _update_counters(con: sqlite3.Connection, job_id: int) -> None:
        con.execute(
            "UPDATE jobs SET "
            " done_files=(SELECT COUNT(*) FROM files WHERE job_id=:j AND status IN ('ARCHIVED','REPLACED')),"
            " done_bytes=(SELECT COALESCE(SUM(size),0) FROM files WHERE job_id=:j AND status IN ('ARCHIVED','REPLACED')),"
            " failed_files=(SELECT COUNT(*) FROM files WHERE job_id=:j AND status='FAILED') "
            "WHERE id=:j",
            {"j": job_id},
        )


def _size(path: str | None) -> int | None:
    """Size of a file, or None if it does not exist or cannot be read."""
    if not path:
        return None
    try:
        return os.stat(long_path(path)).st_size
    except OSError:
        return None
