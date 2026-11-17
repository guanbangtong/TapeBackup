"""Backup engine: scan -> stage on the local disk -> write to tape -> verify -> clean up, batch by batch.

No UI code here. A front end drives it like this:

    engine = Engine(cfg, on_log=..., ask_tape=..., on_job_update=...)
    result, message = engine.run(tape_id)      # blocking; call it from a worker thread
    engine.stop()                              # from any thread; run() returns ("stopped", "")
    engine.progress.snapshot()                 # from any thread; poll it with a timer

Create a new Engine for every run: one that was stopped stays stopped.

Callbacks (all called from the thread that runs run()):
    on_log(text)                       one human-readable line
    ask_tape(reason, suggested) -> id  a tape must be inserted/changed/confirmed; block until the user
                                       answers, return the tape id they confirmed, or None to stop
    on_job_update(job_id)              a job's row changed (status, counters)

Every step is recorded in the database before the next one starts, so an interrupted
run (stop button, crash, power loss) continues where it left off the next time run() is called.

Rules that protect data:
  * Staging copies are deleted only in _cleanup, and _cleanup only runs for a batch whose row says
    ARCHIVED, which is written only after every file of the batch was found on tape with the right size.
  * A file gets its real name (on the staging disk and on tape) only when it is complete and its
    SHA-256 is right; until then it is "<name>.part". So an archived file on tape is only ever
    replaced by a complete, verified newer version.
  * What is "already on tape" after an interruption is decided by what this program recorded as
    completely written (files.written / batches.tar_written), never by a same-sized file alone.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import sqlite3
import tarfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable

from . import db, source_cleanup
from .config import Config
from .copier import (
    PART_SUFFIX,
    TAPE_CHUNK,
    CopyError,
    Stopped,
    TarWriter,
    copy_file,
    format_bytes,
    list_tar,
    long_path,
    prune_empty_dirs,
    remove_quietly,
    with_retry,
)

TAR_END_OVERHEAD = 2 * tarfile.RECORDSIZE  # end-of-archive blocks plus padding to a full record
MAX_ATTEMPTS = 3
MTIME_TOLERANCE = 2.0  # seconds; file systems round modification times differently

# "delete" (optional, per job): wait, then remove the NAS source. Its unit is "seconds" while waiting.
PHASES = ("scan", "stage", "tape", "verify", "cleanup", "delete")

EJECT_REMINDER = "取出磁带前，请先在 LTFS 软件里正常弹出磁带，确保内容已完整写入。"


class EngineError(Exception):
    """The run cannot continue (tape or staging disk problem). The message is shown to the user."""


class JobError(Exception):
    """This job cannot run now (its source is missing or unusable). Other jobs in the queue still can."""


class _StagingWriteError(EngineError):
    """Writing to the staging disk failed, most likely because it is full."""


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


def tar_member_cost(owner: str, rel_path: str, size: int) -> int:
    """Upper bound of the space a file takes inside a tar: data, header, padding, PAX record for the name."""
    name_bytes = len(tar_member_name(owner, rel_path).encode("utf-8"))
    return size + 2048 + 512 * (name_bytes // 512 + 1)


_BAD_NAME_CHARS = set('\\/:*?"<>|')


def valid_owner_name(name: str) -> bool:
    """An owner name becomes a folder name on the staging disk and on tape, so it must be a plain one."""
    return bool(
        name
        and name == name.strip()
        and not name.endswith(".")
        and not any(c in _BAD_NAME_CHARS or ord(c) < 32 for c in name)
    )


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
        disk_total: Callable[[str], int] | None = None,
        retry_delay: float = 2.0,
    ) -> None:
        self.cfg = cfg
        self.progress = Progress()
        self.tape_id = ""
        self._on_log = on_log
        self._ask_tape = ask_tape
        self._on_job_update = on_job_update
        self._free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self._total = disk_total or (lambda path: shutil.disk_usage(path).total)
        self._retry_delay = retry_delay
        self._stop = threading.Event()
        self._confirmed_unmarked = ""  # tape id the user confirmed for a tape that carries no marker
        self.con: sqlite3.Connection = None  # type: ignore[assignment]  # opened in run()

    # ------------------------------------------------------------ public

    def stop(self) -> None:
        """Ask the engine to stop. The file in flight is abandoned and redone next time."""
        self._stop.set()

    def run(self, tape_id: str) -> tuple[str, str]:
        """Process every unfinished and queued job. Returns ('done'|'stopped'|'error', message)."""
        self.tape_id = normalize_tape_id(tape_id)
        self.progress.tape_id = self.tape_id
        try:
            os.makedirs(self.cfg.staging_root, exist_ok=True)
            self.con = db.connect(self.cfg.db_path)
        except (OSError, sqlite3.Error) as e:
            return "error", f"无法打开记录数据库或中转目录：{e}"
        try:
            pending = db.pending_deletions(self.con)  # backed up earlier, source not deleted yet
            jobs = db.active_jobs(self.con)
            if not jobs and not pending:
                return "done", "队列里没有需要备份的任务。"
            failed = 0
            skipped: list[str] = []
            for job in pending + jobs:
                try:
                    if job["status"] == "DONE":
                        self._delete_source(job)
                    else:
                        failed += self._run_job(job)
                except JobError as e:  # this job cannot run; the others still can
                    self.log(f"任务 {job['owner']} 未完成：{e}")
                    skipped.append(f"{job['owner']}：{e}")
            total = len(pending) + len(jobs)
            if skipped:
                return "error", (
                    f"{len(skipped)} 个任务未完成（其余 {total - len(skipped)} 个已完成）。"
                    "问题解决后重新点击开始即可继续：\n\n" + "\n".join(skipped)
                )
            message = f"全部完成，共 {total} 个任务。"
            if failed:
                message += f"\n有 {failed} 个文件未能备份，可在“记录”页查看。"
            return "done", f"{message}\n\n{EJECT_REMINDER}"
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
                    self._remove_staged_raw(job["owner"], row["rel_path"])
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
        self._check_job(job)

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

        failed = self._finish(job)
        self._delete_source(job)
        return failed

    def _check_job(self, job: sqlite3.Row) -> None:
        """Refuse a job whose name or source folder would make the program work on its own files."""
        owner, source = job["owner"], job["source_path"]
        if not valid_owner_name(owner):
            raise JobError(f"名称“{owner}”不能用作文件夹名。请把它从队列移除，换一个名称重新添加。")
        if not os.path.isdir(long_path(source)):
            raise JobError(f"源文件夹不存在或无法访问：{source}")
        real = _real(source)
        own_staging = os.path.join(self.cfg.staging_root, archive_dir_name(owner))
        for label, other in (
            ("这个名称在中转盘上的暂存文件夹", own_staging),
            ("磁带", self.cfg.tape_root),
            ("记录存放目录", self.cfg.data_dir),
        ):
            if _overlaps(real, _real(other)):
                raise JobError(f"源文件夹 {source} 与{label}（{other}）重叠，不能备份。请把它从队列移除。")

    def _finish(self, job: sqlite3.Row) -> int:
        con = self.con
        job_id = job["id"]
        with con:
            self._update_counters(con, job_id)
            con.execute("UPDATE jobs SET status='DONE', finished_at=? WHERE id=?", (db.now(), job_id))
        prune_empty_dirs(os.path.join(self.cfg.staging_root, archive_dir_name(job["owner"])))
        self._begin_phase(job_id, "cleanup", done=0, total=0, unit="files")  # re-base: the job bar ends at 100%
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
        """List the source folder and queue every file that is not on tape yet.

        A file counts as already on tape when an archived record of this owner has the same
        relative path, size and modification time.
        """
        con = self.con
        source = job["source_path"]
        p = self.progress
        p.phase, p.phase_unit = "scan", "files"
        p.phase_done = p.phase_total = 0
        p.scan_files = p.scan_bytes = 0
        p.current_file = ""
        self.log(f"扫描 {source} ...")

        archived: dict[tuple[str, int], list[float | None]] = {}
        for row in con.execute(
            "SELECT f.rel_path, f.size, f.mtime FROM files f JOIN jobs j ON j.id=f.job_id "
            "WHERE j.owner=? COLLATE NOCASE AND f.status='ARCHIVED'",
            (job["owner"],),
        ):
            archived.setdefault((row["rel_path"].lower(), row["size"]), []).append(row["mtime"])

        def on_tape(rel: str, size: int, mtime: float) -> bool:
            return any(
                m is None or abs(m - mtime) <= MTIME_TOLERANCE for m in archived.get((rel.lower(), size), ())
            )

        found: list[tuple[str, int, float, str]] = []
        unreadable: list[tuple[str, str]] = []  # (path, reason): recorded as FAILED so they are not overlooked
        skipped = 0
        stack = [""]
        while stack:
            self._check_stop()
            rel_dir = stack.pop()
            abs_dir = os.path.join(source, rel_dir) if rel_dir else source
            try:
                entries = os.scandir(long_path(abs_dir))
            except OSError as e:
                if not rel_dir:
                    raise JobError(f"无法读取源文件夹 {source}：{e}") from e
                unreadable.append((rel_dir + os.sep, f"扫描时无法读取这个文件夹：{e}"))
                self.log(f"警告：无法读取子文件夹，其中的文件未备份：{rel_dir}（{e}）")
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
                        unreadable.append((rel, f"扫描时无法读取：{e}"))
                        self.log(f"警告：无法读取，未备份：{rel}（{e}）")
                        continue
                    if on_tape(rel, st.st_size, st.st_mtime):
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
            con.executemany(
                "INSERT INTO files(job_id, rel_path, size, kind, status, error) VALUES (?,?,0,'nonraw','FAILED',?)",
                [(job["id"], rel, reason) for rel, reason in unreadable],
            )
            con.execute(
                "UPDATE jobs SET scanned=1, total_files=?, total_bytes=? WHERE id=?",
                (len(found), total_bytes, job["id"]),
            )
        text = f"扫描完成：{len(found)} 个文件待备份，共 {format_bytes(total_bytes)}"
        if skipped:
            text += f"；{skipped} 个文件已在磁带上，跳过"
        if unreadable:
            text += f"；{len(unreadable)} 项无法读取"
        self.log(text)
        self._notify(job["id"])

    # ------------------------------------------------------------ batch planning

    def _plan_batch(self, job: sqlite3.Row) -> sqlite3.Row | None:
        """Pick the pending files for the next batch: as many as fit on the staging disk and on the tape."""
        con = self.con
        job_id, owner = job["id"], job["owner"]
        while True:
            pending = con.execute(
                "SELECT id, size, kind, rel_path FROM files "
                "WHERE job_id=? AND status='PENDING' AND batch_id IS NULL "
                "ORDER BY (kind='raw'), id",  # non-raw first so the tar goes out with the first batch
                (job_id,),
            ).fetchall()
            if not pending:
                return None

            self._ensure_tape(0)
            stage_cap = self._free(self.cfg.staging_root) - self.cfg.reserve_staging_bytes
            tape_cap = self._free(self.cfg.tape_root) - self.cfg.reserve_tape_bytes
            remaining = min(stage_cap, tape_cap)

            selected: list[int] = []
            has_tar = False
            alone: list[tuple[sqlite3.Row, int]] = []  # what each file would need as the only one in a batch
            for row in pending:
                if row["kind"] == "nonraw":
                    cost = tar_member_cost(owner, row["rel_path"], row["size"])
                    alone.append((row, cost + TAR_END_OVERHEAD))
                    if not has_tar:
                        cost += TAR_END_OVERHEAD  # the first member opens the tar
                else:
                    cost = row["size"]
                    alone.append((row, cost))
                if cost <= remaining:
                    selected.append(row["id"])
                    remaining -= cost
                    has_tar = has_tar or row["kind"] == "nonraw"
            if selected:
                break

            # Nothing fits right now. Files that can never fit are failed; anything else is a
            # condition the user can fix (change tape, free the staging disk), so the run pauses.
            stage_max = self._total(self.cfg.staging_root) - self.cfg.reserve_staging_bytes
            tape_max = self._total(self.cfg.tape_root) - self.cfg.reserve_tape_bytes
            hopeless = [(row, need) for row, need in alone if need > stage_max or need > tape_max]
            if hopeless:
                with con:
                    for row, need in hopeless:
                        where = "中转盘" if need > stage_max else "一盘磁带"
                        con.execute(
                            "UPDATE files SET status='FAILED', error=? WHERE id=?",
                            (f"文件（{format_bytes(row['size'])}）比{where}的总容量还大，无法备份", row["id"]),
                        )
                self.log(f"警告：{len(hopeless)} 个文件比中转盘或磁带的容量还大，无法备份。")
                continue
            smallest = min(need for _row, need in alone)
            if smallest <= stage_cap:
                self._ensure_tape(smallest)  # the tape is what is full: this asks for another one
                continue
            raise EngineError(
                f"中转盘 {self.cfg.staging_root} 空间不足：可用 {format_bytes(max(stage_cap, 0))}，"
                f"而剩下最小的文件需要 {format_bytes(smallest)}。请清理中转盘后重新点击开始，会从中断处继续。"
            )

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
                tar_name = f"{owner}_nonraw_{datetime.now():%Y%m%d_%H%M%S}_b{batch_id}.tar"
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

        # What an interrupted earlier run left behind is reused only if it was recorded as complete.
        # (Its content is checked against the recorded SHA-256 when it is written to tape.)
        tar_ready = bool(
            nonraw
            and tar_path
            and batch["tar_size"] is not None
            and batch["tar_sha256"]
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
                con.execute(
                    "UPDATE batches SET tar_size=NULL, tar_sha256=NULL, tar_written=NULL WHERE id=?", (batch_id,)
                )
            con.executemany(
                "UPDATE files SET status='PENDING', sha256=NULL, written=NULL WHERE id=?",
                [(r["id"],) for r in raw_todo],
            )

        todo_bytes = sum(r["size"] for r in raw_todo) + (0 if tar_ready else sum(r["size"] for r in nonraw))
        total_bytes = sum(r["size"] for r in rows)
        self._begin_phase(job["id"], "stage", done=total_bytes - todo_bytes, total=total_bytes)
        self.log(f"第 {batch['seq']} 批：暂存到中转盘，{format_bytes(todo_bytes)}")

        try:
            if nonraw and not tar_ready:
                assert tar_path is not None
                self._build_tar(job, batch, nonraw, tar_path)
            if raw_todo:
                self._stage_raw(job, raw_todo)
        except _StagingWriteError as e:
            # Usually the disk filled up because files grew after the scan. Give back what could not
            # be staged (with fresh sizes, so the next plan is right) and carry on with what is here.
            self._release_unstaged(job, batch_id)
            if not con.execute(
                "SELECT COUNT(*) FROM files WHERE batch_id=? AND status='STAGED'", (batch_id,)
            ).fetchone()[0]:
                raise EngineError(f"{e}。请清理中转盘 {self.cfg.staging_root} 后重新点击开始，会从中断处继续。") from e
            self.log(f"警告：{e}。这一批先处理已经暂存好的文件，其余的留到下一批。")

        staged = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN kind='raw' THEN size END), 0), "
            "       COALESCE(SUM(kind='nonraw'), 0) FROM files WHERE batch_id=? AND status='STAGED'",
            (batch_id,),
        ).fetchone()
        if staged[0] == 0:
            with con:
                con.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            prune_empty_dirs(stage_root)
            self.log(f"第 {batch['seq']} 批没有可备份的文件，已取消。")
            return False
        with con:
            if not staged[2]:  # no tar in this batch after all
                con.execute(
                    "UPDATE batches SET tar_name=NULL, tar_size=NULL, tar_sha256=NULL WHERE id=?", (batch_id,)
                )
            tar_size = self._batch(batch_id)["tar_size"] or 0
            con.execute("UPDATE batches SET status='STAGED', bytes=? WHERE id=?", (staged[1] + tar_size, batch_id))
        return True

    def _build_tar(self, job: sqlite3.Row, batch: sqlite3.Row, nonraw: list[sqlite3.Row], tar_path: str) -> None:
        con = self.con
        owner, source = job["owner"], job["source_path"]
        remove_quietly(tar_path)
        done: list[tuple[int, str, int]] = []  # (size, sha256, file id)
        try:
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
                            raise
                        self._source_failure(job, row, e)
                        continue
                    done.append((size, sha, row["id"]))
        except CopyError as e:  # writing the tar itself failed (also when closing it)
            raise _StagingWriteError(f"写入中转盘失败（空间不足？）：{e}") from e

        if not done:
            remove_quietly(tar_path)
            return
        try:  # read the tar back: every member must be there with the size we wrote
            members = list_tar(tar_path)
        except (tarfile.TarError, OSError) as e:
            raise EngineError(f"tar 包校验失败：{tar_path}（{e}）") from e
        if len(members) != len(done) or sum(m[1] for m in members) != sum(d[0] for d in done):
            raise EngineError(f"tar 包内容与预期不符：{tar_path}")
        with con:  # members, tar size and tar checksum are recorded together: that is what marks the tar complete
            con.executemany("UPDATE files SET status='STAGED', size=?, sha256=? WHERE id=?", done)
            con.execute(
                "UPDATE batches SET tar_size=?, tar_sha256=? WHERE id=?", (_size(tar_path), tar.sha256, batch["id"])
            )

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
                        raise _StagingWriteError(f"写入中转盘失败（空间不足？）：{e}") from e
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
        """A source file could not be read after retries: skip that file, or pause the job if the NAS is gone."""
        if not os.path.isdir(long_path(job["source_path"])):
            raise JobError(f"源文件夹无法访问：{job['source_path']}（{error.cause}）。请检查 NAS 连接。")
        with self.con:
            self.con.execute(
                "UPDATE files SET status='FAILED', batch_id=NULL, error=? WHERE id=?", (str(error.cause), row["id"])
            )
        if row["kind"] == "raw":  # a copy from an earlier interrupted run must not linger on the staging disk
            self._remove_staged_raw(job["owner"], row["rel_path"])
        self.progress.drop(row["size"])
        self.log(f"警告：读取失败，已跳过：{row['rel_path']}（{error.cause}）")

    def _release_unstaged(self, job: sqlite3.Row, batch_id: int) -> None:
        """Take the files that are not staged out of the batch again, with their current sizes."""
        con = self.con
        rows = con.execute("SELECT * FROM files WHERE batch_id=? AND status='PENDING'", (batch_id,)).fetchall()
        updates = []
        for row in rows:
            now_size = _size(os.path.join(job["source_path"], row["rel_path"]))
            updates.append((row["size"] if now_size is None else now_size, row["id"]))
            if row["kind"] == "raw":
                self._remove_staged_raw(job["owner"], row["rel_path"])
        with con:
            con.executemany("UPDATE files SET batch_id=NULL, size=? WHERE id=?", updates)

    def _remove_staged_raw(self, owner: str, rel_path: str) -> None:
        path = raw_media_path(self.cfg.staging_root, owner, rel_path)
        remove_quietly(path)
        remove_quietly(path + PART_SUFFIX)

    # ------------------------------------------------------------ tape: staging disk -> tape

    def _to_tape(self, job: sqlite3.Row, batch: sqlite3.Row) -> bool:
        """Write the staged batch to tape and verify it. Returns False if nothing was left to archive."""
        con = self.con
        owner, batch_id = job["owner"], batch["id"]
        staging, tape = self.cfg.staging_root, self.cfg.tape_root

        rows = con.execute("SELECT * FROM files WHERE batch_id=? AND status='STAGED'", (batch_id,)).fetchall()
        nonraw = [r for r in rows if r["kind"] == "nonraw"]
        raw = sorted((r for r in rows if r["kind"] == "raw"), key=lambda r: r["rel_path"].lower())

        # (label, source on staging disk, destination on tape, size, sha256, tape it was written to, file row or None for the tar)
        items: list[tuple[str, str, str, int, str, str | None, sqlite3.Row | None]] = []
        if nonraw:
            name = batch["tar_name"]
            items.append((name, tar_media_path(staging, owner, name), tar_media_path(tape, owner, name),
                          batch["tar_size"], batch["tar_sha256"], batch["tar_written"], None))
        for r in raw:
            items.append((r["rel_path"], raw_media_path(staging, owner, r["rel_path"]),
                          raw_media_path(tape, owner, r["rel_path"]), r["size"], r["sha256"], r["written"], r))

        # After an interruption, what this program recorded as completely written to the tape that is
        # in the drive now (and is still there) is not written again. Asking for space may lead to a
        # tape change, so repeat until the tape stays the same.
        while True:
            self._ensure_tape(0)
            current = self.tape_id
            written = {i for i, item in enumerate(items) if item[5] == current and _size(item[2]) == item[3]}
            need = sum(item[3] for i, item in enumerate(items) if i not in written)
            self._ensure_tape(need)
            if self.tape_id == current:
                break
        if batch["tape_id"] != self.tape_id:
            with con:
                con.execute("UPDATE batches SET tape_id=? WHERE id=?", (self.tape_id, batch_id))
            self.log(f"第 {batch['seq']} 批改为写入磁带 {self.tape_id}")

        total = sum(item[3] for item in items)
        self._begin_phase(job["id"], "tape", done=total - need, total=total, on_tape=total - need)
        self.log(f"第 {batch['seq']} 批：写入磁带 {self.tape_id}，{format_bytes(need)}")

        for index, (label, src, dst, size, sha, _written, row) in enumerate(items):
            self._check_stop()
            self.progress.current_file = label
            if index in written:
                continue
            try:
                # The data read back from the staging disk must be what was read from the NAS;
                # only then does the file get its real name on tape.
                with_retry(
                    lambda: copy_file(src, dst, should_stop=self._stop.is_set, on_bytes=self.progress.add,
                                      chunk=TAPE_CHUNK, expect_sha=sha),
                    delay=self._retry_delay,
                    should_stop=self._stop.is_set,
                )
            except CopyError as e:
                if e.side == "dst":
                    raise EngineError(
                        f"写入磁带失败：{e}。中转盘上的数据已保留，处理好磁带后重新点击开始即可继续。"
                    ) from e
                self._requeue(job, batch, row, nonraw, size, f"中转盘上的副本无法读取或已损坏（{e.cause}）")
                continue
            if row is None:
                con.execute("UPDATE batches SET tar_written=? WHERE id=?", (self.tape_id, batch_id))
            else:
                con.execute("UPDATE files SET written=? WHERE id=?", (self.tape_id, row["id"]))
            con.commit()

        # Verify: every file of the batch must exist on tape with the expected size.
        rows = con.execute("SELECT * FROM files WHERE batch_id=? AND status='STAGED'", (batch_id,)).fetchall()
        if not rows:
            with con:
                con.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            self.log(f"第 {batch['seq']} 批的文件都需要重新暂存，已退回队列。")
            return False
        staged_ids = {r["id"] for r in rows}
        has_tar = any(r["kind"] == "nonraw" for r in rows)
        checks = [item for item in items if (has_tar if item[6] is None else item[6]["id"] in staged_ids)]
        self._begin_phase(job["id"], "verify", done=0, total=len(checks), unit="files",
                          on_tape=sum(item[3] for item in checks))
        for label, _src, dst, size, _sha, _written, _row in checks:
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
        new_raw_paths = {r["rel_path"].lower() for r in rows if r["kind"] == "raw"}
        with con:
            # A changed file was written over its old copy on this tape: the old record is no longer true.
            old = con.execute(
                "SELECT f.id, f.rel_path FROM files f JOIN jobs j ON j.id=f.job_id "
                "WHERE j.owner=? COLLATE NOCASE AND f.status='ARCHIVED' AND f.kind='raw' AND f.tape_id=?",
                (owner, self.tape_id),
            ).fetchall() if new_raw_paths else []
            con.executemany(
                "UPDATE files SET status='REPLACED' WHERE id=?",
                [(r["id"],) for r in old if r["rel_path"].lower() in new_raw_paths],
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
            self._remove_staged_raw(owner, row["rel_path"])
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
                        "UPDATE files SET status='PENDING', batch_id=NULL, sha256=NULL, written=NULL, "
                        "attempts=attempts+1 WHERE id=?",
                        (target["id"],),
                    )
            if row is None:
                con.execute(
                    "UPDATE batches SET tar_name=NULL, tar_size=NULL, tar_sha256=NULL, tar_written=NULL WHERE id=?",
                    (batch["id"],),
                )
        self.progress.drop(size)
        self.log(f"警告：{label}：{reason}，将重新暂存。")

    # ------------------------------------------------------------ cleanup

    def _cleanup(self, job: sqlite3.Row, batch: sqlite3.Row) -> None:
        """Delete the staging copies of a batch that is recorded as ARCHIVED."""
        con = self.con
        batch = self._batch(batch["id"])
        if batch["status"] != "ARCHIVED":  # the first rule from the module docstring
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

    # ------------------------------------------------------------ delete the NAS source

    def _delete_source(self, job: sqlite3.Row) -> None:
        """For a finished job that asked for it: delete from the NAS what is safely on tape.

        The rules for what may be deleted are in source_cleanup. This method adds the conditions:
        the job is DONE, the waiting time since the last tape write has passed (LTFS writes its
        index to the tape periodically; before that a power cut could lose what was just written),
        and the files on the tape that is in the drive are checked once more.
        """
        con = self.con
        job = con.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if job is None or job["status"] != "DONE" or not job["delete_source"] or job["source_deleted_at"]:
            return
        job_id, owner, source = job["id"], job["owner"], job["source_path"]
        p = self.progress
        p.job_id, p.owner = job_id, owner
        if not os.path.isdir(long_path(source)):
            self.log(f"{owner}：源文件夹 {source} 已经不存在，无需删除。")
            self._record_deletion(job_id, 0, 0, 0)
            return
        self._check_job(job)  # never delete inside the staging folder, the tape or the records folder

        # 1. Wait until the tape has had time to write its index.
        last = con.execute(
            "SELECT MAX(b.archived_at) FROM batches b JOIN jobs j ON j.id=b.job_id "
            "WHERE j.owner=? COLLATE NOCASE AND j.legacy=0",
            (owner,),
        ).fetchone()[0]
        delay = max(0.0, self.cfg.delete_delay_minutes * 60)
        if last and delay:
            since = time.time() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S").timestamp()
            wait = min(delay, max(0.0, delay - since))  # never longer than the delay, whatever the clock says
            if wait > 0:
                self.log(f"{owner}：等待 {math.ceil(wait / 60)} 分钟让磁带机把目录写入磁带，之后删除 NAS 源数据。")
                with p._lock:
                    p.phase, p.phase_unit = "delete", "seconds"
                    p.phase_total, p.phase_done = int(delay), int(delay - wait)
                    p.current_file = ""
                deadline = time.monotonic() + wait
                while (left := deadline - time.monotonic()) > 0:
                    self._check_stop()
                    p.phase_done = int(delay - left)
                    time.sleep(min(0.2, left))

        # 2. Decide file by file, then check the tape in the drive once more.
        with p._lock:
            p.phase, p.phase_unit = "delete", "files"
            p.phase_total = p.phase_done = 0
            p.current_file = ""
        self.log(f"{owner}：核对 NAS 上的文件与备份记录 ...")
        candidates, kept = source_cleanup.plan_deletion(con, owner, source, self._stop.is_set)
        problems = self._recheck_on_tape(owner, candidates)
        if problems:
            raise EngineError(
                f"磁带上有 {len(problems)} 个文件与备份记录不符（例如 {problems[0]}），"
                f"所以没有删除 {source} 里的任何数据。请检查磁带后重新点击开始。"
            )

        # 3. Delete, then empty the NAS recycle folder of this source so the space is really freed.
        p.phase_total = len(candidates)

        def on_file(rel_path: str) -> None:
            p.current_file = rel_path
            p.step()

        self.log(f"{owner}：删除 NAS 上已备份的 {len(candidates)} 个文件 ...")
        deleted, deleted_bytes, not_deleted = source_cleanup.delete_files(
            source, candidates, self._stop.is_set, on_file
        )
        kept += not_deleted
        recycle = source_cleanup.recycle_path(source, self.cfg.nas_recycle_dir)
        if recycle and os.path.isdir(long_path(recycle)):
            p.current_file = recycle
            files, size, errors = source_cleanup.purge_tree(recycle, self._stop.is_set)
            text = f"{owner}：已清空 NAS 回收站 {recycle}（{files} 个文件，{format_bytes(size)}）"
            self.log(text + (f"；{errors} 项无法删除" if errors else ""))

        self._record_deletion(job_id, deleted, deleted_bytes, len(kept))
        text = f"{owner}：已删除 NAS 源数据 {deleted} 个文件，{format_bytes(deleted_bytes)}"
        if kept:
            text += f"；{len(kept)} 个文件不在磁带上或已改动，保留在 {source}"
        self.log(text)
        for rel_path, reason in kept[:20]:
            self.log(f"    保留：{rel_path}（{reason}）")
        if len(kept) > 20:
            self.log(f"    …… 另有 {len(kept) - 20} 个")

    def _recheck_on_tape(self, owner: str, candidates: list[source_cleanup.Candidate]) -> list[str]:
        """Files recorded on the tape that is in the drive must really be there. Returns the ones that are not."""
        root = self.cfg.tape_root
        try:
            marker = read_tape_marker(root) if os.path.isdir(root) else None
        except OSError:
            marker = None
        here = [c for c in candidates if marker and c.tape_id == marker]
        tar_ok: dict[int, bool] = {}
        problems = []
        for item in here:
            self._check_stop()
            if item.kind == "raw":
                ok = _size(raw_media_path(root, owner, item.rel_path)) == item.size
            else:
                if item.batch_id not in tar_ok:
                    batch = self._batch(item.batch_id)
                    tar_ok[item.batch_id] = bool(
                        batch is not None and item.tar_name and batch["tar_size"] is not None
                        and _size(tar_media_path(root, owner, item.tar_name)) == batch["tar_size"]
                    )
                ok = tar_ok[item.batch_id]
            if not ok:
                problems.append(item.rel_path)
        elsewhere = len(candidates) - len(here)
        if here:
            text = f"{owner}：已在磁带 {marker} 上重新核对 {len(here)} 个文件"
            self.log(text + (f"；另有 {elsewhere} 个文件在其他磁带上，按备份记录处理" if elsewhere else ""))
        elif candidates:
            self.log(f"{owner}：这些文件所在的磁带不在磁带机里，按备份记录处理。")
        return problems

    def _record_deletion(self, job_id: int, files: int, size: int, kept: int) -> None:
        with self.con:
            self.con.execute(
                "UPDATE jobs SET source_deleted_at=?, deleted_files=?, deleted_bytes=?, kept_files=? WHERE id=?",
                (db.now(), files, size, kept, job_id),
            )
        self._notify(job_id)

    # ------------------------------------------------------------ tape handling

    def _ensure_tape(self, need_bytes: int) -> None:
        """Make sure the right tape is inserted, identified, and has need_bytes free; ask the user otherwise."""
        root = self.cfg.tape_root
        while True:
            self._check_stop()
            suggested = None
            confirming = ""
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
                    elif not marker and self._confirmed_unmarked != self.tape_id and self._tape_known(self.tape_id):
                        # Two cartridges must never share an id: records would point at the wrong one.
                        suggested = confirming = self.tape_id
                        reason = (
                            f"这盘磁带上还没有磁带号标记，但记录里已经有磁带 {self.tape_id} 的数据。\n"
                            f"如果这就是那一盘（例如以前用旧脚本写过的磁带），请再次确认磁带号 {self.tape_id}；"
                            "如果这是另一盘磁带，请输入一个新的磁带号。"
                        )
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
            if new_id == confirming:
                self._confirmed_unmarked = new_id
            self.tape_id = new_id
            self.progress.tape_id = new_id

    def _tape_known(self, tape_id: str) -> bool:
        return self.con.execute(
            "SELECT 1 FROM batches WHERE tape_id=? AND status='ARCHIVED' LIMIT 1", (tape_id,)
        ).fetchone() is not None

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


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _overlaps(a: str, b: str) -> bool:
    """True if one of the two (normalised) paths is the other or lies inside it."""
    a, b = a.rstrip(os.sep), b.rstrip(os.sep)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)
