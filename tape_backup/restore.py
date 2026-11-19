"""Restore files from tape: read each tape once, verify every file against its recorded SHA-256.

No UI code here. A front end drives it like the Engine:

    restorer = Restorer(cfg, files, dest_dir, on_log=..., ask_tape=...)
    result, message = restorer.run()      # blocking; call it from a worker thread
    restorer.stop()                       # from any thread; run() returns ("stopped", "")
    restorer.progress.snapshot()          # from any thread; poll it with a timer
    restorer.failures                     # [(owner, rel_path, reason)] after run() returns
    restorer.restored                     # number of files restored and verified

`files` are rows of db.search_files / db.files_of_job (sqlite3.Row or dict).

Callbacks (called from the thread that runs run()):
    on_log(text)                     one human-readable line
    ask_tape(tape_id, reason) -> bool  the wanted tape is not in the drive; block until the user has
                                       inserted it (True = check again) or gave up (False = stop)

Tape is sequential: tapes are handled one at a time, each tar is read in one forward pass, and raw
files are read in path order (the order they were written). The tape is identified by the name of
its marker file only, never by reading file contents.

Two steps, like a backup in reverse: tape -> staging folder on the local disk (<staging>\\TapeRestore),
then staging -> destination (usually a NAS folder). The tape is read in one go at full speed and never
waits for the network. Each step checks the SHA-256 recorded at backup time.

Files are written to "<name>.part" first and renamed when complete and verified, so the destination
never holds a partial or corrupt file; a file of the same name is replaced by the good copy.
The staging folder is emptied at the start and at the end of every run (no resume: restores are small).

Where a file goes: <dest>\\<row["restore_as"]> if the row has it, else <dest>\\<owner>\\<rel_path>.
When several rows go to the same place (a file backed up again after it changed), the newest backup wins.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import threading
import traceback
from contextlib import closing
from datetime import datetime
from typing import Callable, Iterable, Mapping

from . import db
from .config import Config
from .copier import TAPE_CHUNK, CopyError, HashMismatch, Stopped, copy_file, format_bytes, long_path, remove_quietly
from .engine import (
    TAPE_DRIVE,
    _overlaps,
    _real,
    normalize_tape_id,
    raw_media_path,
    read_tape_marker,
    tar_media_path,
    tar_member_name,
)

RESTORE_DIR = "TapeRestore"  # staging sub-folder used only by restores

PART_SUFFIX = ".part"


class RestoreError(Exception):
    """The run cannot continue (the destination is unusable). The message is shown to the user."""


def plan_tapes(files: Iterable[Mapping]) -> list[tuple[str, int]]:
    """[(tape_id, number of files)] in the order the tapes are asked for ('' = no tape id recorded, last)."""
    counts: dict[str, int] = {}
    for f in files:
        tape_id = normalize_tape_id(f["tape_id"])
        counts[tape_id] = counts.get(tape_id, 0) + 1
    return sorted(counts.items(), key=lambda item: (item[0] == "", item[0]))


class Progress:
    """Live numbers for the UI. Only the worker thread writes; the UI polls snapshot()."""

    def __init__(self, total_bytes: int, total_files: int, steps: int = 1) -> None:
        self._lock = threading.Lock()
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.steps = steps  # each byte is moved this many times; done_bytes reports the average
        self.current_file = ""
        self.tape_id = ""
        self._finished_bytes = 0  # sizes of files already handled (restored or failed)
        self._inflight = 0        # bytes of the file being written now
        self._done_files = 0

    def add(self, n: int) -> None:
        with self._lock:
            self._inflight += n

    def file_done(self, size: int, files: int = 1) -> None:
        """A file is finished, successfully or not (files=0: only one of its two steps is)."""
        with self._lock:
            self._inflight = 0
            self._finished_bytes += size
            self._done_files += files

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "done_bytes": max(0, min((self._finished_bytes + self._inflight) // self.steps, self.total_bytes)),
                "total_bytes": self.total_bytes,
                "done_files": self._done_files,
                "total_files": self.total_files,
                "current_file": self.current_file,
                "tape_id": self.tape_id,
            }


class Restorer:
    def __init__(
        self,
        cfg: Config,
        files: Iterable[Mapping],
        dest_dir: str,
        *,
        on_log: Callable[[str], None] | None = None,
        ask_tape: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.cfg = cfg
        self.dest_dir = dest_dir
        self._on_log = on_log
        self._ask_tape = ask_tape
        self._stop = threading.Event()
        self.failures: list[tuple[str, str, str]] = []
        self.restored = 0

        self.staging_dir = os.path.join(cfg.staging_root, RESTORE_DIR)
        self._staged: list[dict] = []  # verified on the staging disk, waiting to be delivered

        # One row per destination path; the newest backup wins (rows: copies, so sqlite3.Row and dict both work).
        newest: dict[str, dict] = {}
        given = 0
        for f in map(dict, files):
            given += 1
            f["tape_id"] = normalize_tape_id(f["tape_id"])
            f["size"] = int(f["size"] or 0)
            f["restore_as"] = f.get("restore_as") or os.path.join(f["owner"], f["rel_path"])
            key = os.path.normcase(f["restore_as"])
            if key not in newest or _age(f) > _age(newest[key]):
                newest[key] = f
        self.files = list(newest.values())
        self.older_versions = given - len(self.files)  # skipped: an older backup of the same file
        # Every byte is moved twice (tape -> staging -> destination); progress counts each move as half.
        self.progress = Progress(sum(f["size"] for f in self.files), len(self.files), steps=2)

    # ------------------------------------------------------------ public

    def stop(self) -> None:
        """Ask the running restore to stop. The file in flight is abandoned and removed."""
        self._stop.set()

    def run(self) -> tuple[str, str]:
        """Restore everything. Returns ('done'|'stopped'|'error', message)."""
        self._stop.clear()
        self.failures = []
        self.restored = 0
        self._staged = []
        if not TAPE_DRIVE.acquire(blocking=False):
            return "error", "正在备份，磁带机被占用。请等备份完成或停止后再取回。"
        staging_made = False
        try:
            self._check_destination()
            self._check_space()
            try:
                os.makedirs(long_path(self.dest_dir), exist_ok=True)
                shutil.rmtree(long_path(self.staging_dir), ignore_errors=True)  # leftovers of a stopped run
                os.makedirs(long_path(self.staging_dir), exist_ok=True)
                staging_made = True
            except OSError as e:
                raise RestoreError(f"无法使用目标文件夹或中转文件夹：{e}") from e
            if self.older_versions:
                self.log(f"{self.older_versions} 个文件备份过不止一次，只取最近一次备份的版本。")

            # Step 1: tape -> staging, one tape at a time.
            for tape_id, _count in plan_tapes(self.files):
                self._restore_tape(tape_id)
            # Step 2: staging -> destination.
            if self._staged:
                self.log(f"磁带读取完毕，开始把 {len(self._staged)} 个文件从中转盘送到 {self.dest_dir}")
            for row in self._staged:
                if self._stop.is_set():
                    raise Stopped()
                self._deliver(row)
            message = f"取回完成：成功 {self.restored} 个文件"
            if self.failures:
                message += f"，失败 {len(self.failures)} 个文件"
            self.log(message + "。")
            return "done", message + "。"
        except Stopped:
            self.log(f"已停止。已取回 {self.restored} 个文件。")
            return "stopped", ""
        except RestoreError as e:
            self.log(f"错误：{e}")
            return "error", str(e)
        except Exception as e:  # a bug or an unexpected system error: stop rather than guess
            self.log("意外错误：\n" + traceback.format_exc())
            return "error", f"意外错误：{e!r}"
        finally:
            self.progress.current_file = ""
            if staging_made:  # staging holds only copies; the tape still has everything
                shutil.rmtree(long_path(self.staging_dir), ignore_errors=True)
            TAPE_DRIVE.release()

    def _check_destination(self) -> None:
        """Refuse destinations where restored files could be deleted or mixed up with the program's own files."""
        if not self.dest_dir.strip():
            raise RestoreError("请先选择要保存到的文件夹。")
        dest = _real(self.dest_dir)
        for name, other in (("中转文件夹", self.staging_dir), ("磁带", self.cfg.tape_root), ("记录存放目录", self.cfg.data_dir)):
            if _overlaps(dest, _real(other)):
                raise RestoreError(f"不能取回到 {self.dest_dir}：它和{name}（{other}）重叠。请换一个文件夹。")
        # The top folders the files land in must not be (inside) a backup source that is still to be deleted.
        tops = {_real(os.path.join(self.dest_dir, f["restore_as"].split(os.sep)[0])) for f in self.files}
        with closing(db.connect(self.cfg.db_path)) as con:
            doomed = db.sources_to_delete(con)
        for source in doomed:
            if any(_overlaps(top, _real(source)) for top in tops):
                raise RestoreError(
                    f"不能取回到 {self.dest_dir}：{source} 正在等待“备份后删除”，取回到这里的文件会被一起删掉。"
                    "请换一个文件夹。"
                )

    def _check_space(self) -> None:
        need = sum(f["size"] for f in self.files)
        try:
            os.makedirs(long_path(self.cfg.staging_root), exist_ok=True)
            free = shutil.disk_usage(self.cfg.staging_root).free - self.cfg.reserve_staging_bytes
        except OSError as e:
            raise RestoreError(f"无法使用中转盘 {self.cfg.staging_root}：{e}") from e
        if need > free:
            raise RestoreError(
                f"中转盘 {self.cfg.staging_root} 空间不够：需要 {format_bytes(need)}，可用 {format_bytes(max(free, 0))}。"
                "请少选一些，或先清理中转盘。"
            )

    def log(self, text: str) -> None:
        if self._on_log:
            self._on_log(f"{datetime.now():%H:%M:%S}  {text}")

    # ------------------------------------------------------------ one tape

    def _restore_tape(self, tape_id: str) -> None:
        rows = [f for f in self.files if f["tape_id"] == tape_id]
        if not tape_id:
            for row in rows:
                self._fail(row, "备份记录里没有磁带号，无法确定在哪盘磁带上")
            return

        self._ensure_tape(tape_id)
        self.progress.tape_id = tape_id
        self.log(f"开始读取磁带 {tape_id}：{len(rows)} 个文件")

        tars: dict[tuple[str, str], list[dict]] = {}
        raws: list[dict] = []
        for row in rows:
            if row["kind"] == "raw":
                raws.append(row)
            elif row.get("tar_name"):
                tars.setdefault((row["owner"], row["tar_name"]), []).append(row)
            else:
                self._fail(row, "备份记录里没有 tar 包名")

        for (owner, tar_name), members in sorted(tars.items(), key=lambda item: (item[0][0].lower(), item[0][1])):
            self._restore_tar(owner, tar_name, members)
        for row in sorted(raws, key=lambda r: (r["owner"].lower(), r["rel_path"].lower())):
            self._restore_raw(row)

    def _ensure_tape(self, tape_id: str) -> None:
        """Wait until the wanted tape is in the drive; ask the user otherwise."""
        allow_unmarked = False
        while True:
            if self._stop.is_set():
                raise Stopped()
            problem, unmarked = self._tape_problem(tape_id, allow_unmarked)
            if problem is None:
                return
            self.log(problem)
            self.progress.current_file = ""
            if not (self._ask_tape and self._ask_tape(tape_id, problem)):
                raise Stopped()
            allow_unmarked = unmarked  # the user confirmed this unmarked tape

    def _tape_problem(self, tape_id: str, allow_unmarked: bool) -> tuple[str | None, bool]:
        """(why the inserted tape is not usable or None, whether the tape has no marker)."""
        root = self.cfg.tape_root
        if not os.path.isdir(root):
            return f"找不到磁带盘 {root}，请插入磁带 {tape_id}。", False
        try:
            marker = read_tape_marker(root)
        except OSError as e:
            return f"无法读取磁带 {root}（{e}），请检查磁带。", False
        if marker == tape_id:
            return None, False
        if marker is None:
            if allow_unmarked:
                return None, True
            return (f"这盘磁带上没有磁带号标记，无法确认是不是磁带 {tape_id}。"
                    "如果确定就是这一盘，请继续；否则请换成磁带 " + tape_id + "。"), True
        return f"现在插入的是磁带 {marker}，需要的是磁带 {tape_id}，请更换。", False

    # ------------------------------------------------------------ raw files

    def _restore_raw(self, row: dict) -> None:
        dst = self._staged_path(row)
        if dst is None:
            return
        tmp = dst + PART_SUFFIX
        src = raw_media_path(self.cfg.tape_root, row["owner"], row["rel_path"])
        self.progress.current_file = row["rel_path"]
        try:
            _size, sha = copy_file(
                src, tmp, should_stop=self._stop.is_set, on_bytes=self.progress.add, chunk=TAPE_CHUNK
            )
        except CopyError as e:
            if e.side == "dst":
                raise RestoreError(f"无法写入目标文件夹（磁盘已满或没有权限？）：{e.cause}") from e
            self._fail(row, _source_reason(e.cause))
            return
        self._finish_file(tmp, dst, row, sha, mtime=None)  # copy_file already set the mtime

    # ------------------------------------------------------------ tar members

    def _restore_tar(self, owner: str, tar_name: str, rows: list[dict]) -> None:
        """One forward pass over the tar; stops reading as soon as every wanted member was written."""
        wanted: dict[str, dict] = {}
        for row in rows:
            if self._staged_path(row) is not None:
                wanted[tar_member_name(owner, row["rel_path"])] = row
        if not wanted:
            return

        self.progress.current_file = tar_name
        path = tar_media_path(self.cfg.tape_root, owner, tar_name)
        try:
            tar = tarfile.open(long_path(path), "r:")
        except FileNotFoundError:
            reason = f"磁带上找不到 tar 包 {tar_name}"
        except (OSError, tarfile.TarError) as e:
            reason = f"无法读取磁带上的 tar 包 {tar_name}（{e}）"
        else:
            reason = f"在 tar 包 {tar_name} 里找不到这个文件"
            with tar:
                try:
                    for member in tar:
                        if self._stop.is_set():
                            raise Stopped()
                        row = wanted.get(member.name)
                        if row is None:
                            continue
                        self.progress.current_file = row["rel_path"]
                        if member.isfile():
                            self._extract_member(tar, member, row)
                        else:
                            self._fail(row, f"tar 包里的 {member.name} 不是普通文件")
                        del wanted[member.name]
                        if not wanted:
                            break
                except (OSError, tarfile.TarError, EOFError) as e:  # reading the tar broke: it cannot be trusted further
                    reason = f"读取磁带上的 tar 包 {tar_name} 时出错（{e}）"
        for row in wanted.values():
            self._fail(row, reason)

    def _extract_member(self, tar: tarfile.TarFile, member: tarfile.TarInfo, row: dict) -> None:
        """Write one member's bytes ourselves (never tar.extract), hashing as we go."""
        dst = self._staged_path(row)
        assert dst is not None
        tmp = dst + PART_SUFFIX
        src = tar.extractfile(member)
        assert src is not None
        digest = hashlib.sha256()
        written = 0
        try:
            try:
                os.makedirs(long_path(os.path.dirname(dst)), exist_ok=True)
                out = open(long_path(tmp), "wb")
            except OSError as e:
                raise RestoreError(f"无法写入目标文件夹（磁盘已满或没有权限？）：{e}") from e
            with out:
                while True:
                    if self._stop.is_set():
                        raise Stopped()
                    data = src.read(TAPE_CHUNK)  # a read error here is a tape problem and propagates as such
                    if not data:
                        break
                    try:
                        out.write(data)
                    except OSError as e:
                        raise RestoreError(f"无法写入目标文件夹（磁盘已满或没有权限？）：{e}") from e
                    digest.update(data)
                    written += len(data)
                    self.progress.add(len(data))
            if written != member.size:
                raise tarfile.ReadError(f"数据不完整（{written} / {member.size} 字节）")
        except BaseException:
            remove_quietly(tmp)
            raise
        self._finish_file(tmp, dst, row, digest.hexdigest(), mtime=member.mtime)

    # ------------------------------------------------------------ common

    def _finish_file(self, tmp: str, dst: str, row: dict, sha: str, mtime: float | None) -> None:
        """Verify the written temp file and move it into place."""
        expected = (row.get("sha256") or "").lower()
        if expected and sha.lower() != expected:
            remove_quietly(tmp)
            self._fail(row, "校验失败：磁带上的内容与备份时不一致（磁带可能已损坏），未保存这个文件")
            return
        try:
            if mtime is not None:
                os.utime(long_path(tmp), (mtime, mtime))
            os.replace(long_path(tmp), long_path(dst))
        except OSError as e:
            remove_quietly(tmp)
            self._fail(row, f"无法保存到目标位置（文件被占用？）：{e}")
            return
        if not expected:
            self.log(f"未校验（备份记录里没有校验值）：{row['owner']}\\{row['rel_path']}")
        self._staged.append(row)
        self.progress.file_done(row["size"], files=0)  # step 1 of 2 done

    def _deliver(self, row: dict) -> None:
        """Step 2: copy a verified file from staging to the destination, checking its SHA-256 again."""
        src = os.path.join(self.staging_dir, row["restore_as"])
        dst = _inside(self.dest_dir, row["restore_as"])
        assert dst is not None  # same relative path as the staged copy, which passed the check
        self.progress.current_file = row["restore_as"]
        try:
            copy_file(src, dst, should_stop=self._stop.is_set, on_bytes=self.progress.add,
                      expect_sha=row.get("sha256") or None)
        except HashMismatch:
            self._fail(row, "送到目标时校验失败（中转盘读写出错？），未保存这个文件", left=row["size"])
            return
        except CopyError as e:
            if e.side == "dst":
                raise RestoreError(f"无法写入目标文件夹（磁盘已满、没有权限或网络断开？）：{e.cause}") from e
            self._fail(row, f"无法读取中转盘上的文件（{e.cause}）", left=row["size"])
            return
        remove_quietly(src)  # delivered and verified: the staged copy is no longer needed
        self.restored += 1
        self.progress.file_done(row["size"])

    def _fail(self, row: dict, reason: str, left: int | None = None) -> None:
        """Record a failed file. `left`: bytes of progress still to account for (default: both steps)."""
        self.failures.append((row["owner"], row["rel_path"], reason))
        self.log(f"失败：{row['owner']}\\{row['rel_path']}：{reason}")
        self.progress.file_done(2 * row["size"] if left is None else left)

    def _staged_path(self, row: dict) -> str | None:
        """Where step 1 puts the file, or None (and a recorded failure) if the path would leave the folder."""
        path = _inside(self.staging_dir, row["restore_as"])
        if path is None:
            self._fail(row, "文件路径不安全（会跑到目标文件夹之外），已跳过")
        return path


def _inside(base: str, rel: str) -> str | None:
    """<base>\\<rel>, or None if rel is empty or would lead outside base (e.g. '..\\x')."""
    path = os.path.abspath(os.path.join(base, rel))
    root = os.path.normcase(os.path.abspath(base)).rstrip(os.sep) + os.sep
    return path if rel and os.path.normcase(path).startswith(root) else None


def _age(row: Mapping) -> tuple[str, int]:
    """Sort key: later backups are larger."""
    return (row.get("archived_at") or "", int(row.get("job_id") or 0))


def _source_reason(cause: BaseException | str) -> str:
    if isinstance(cause, FileNotFoundError):
        return "磁带上找不到这个文件"
    return f"无法读取磁带上的文件（{cause}）"
