"""Delete the NAS source of a backed-up folder. This is the only code that removes the user's data.

Rules:
  * A source file is deleted only if an ARCHIVED record of the same owner has the same relative
    path, the same size and the same modification time (within 2 seconds). Anything else stays:
    files added or changed after the backup, files whose backup failed, links.
  * Each file is checked again immediately before it is removed.
  * Folders are removed only when they have become empty.
  * The recycle folder is emptied only at <share root>\\<recycle dir>\\<path of the source inside the
    share>, never the recycle folder of the whole share.

The engine decides when this runs (after the backup is complete, the tape has been re-checked
and the waiting time has passed); nothing here touches the tape or the staging disk.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from typing import Callable

from .copier import Stopped, long_path, prune_empty_dirs

MTIME_TOLERANCE = 2.0  # seconds; file systems round modification times differently


@dataclass
class Candidate:
    """A source file that is on tape and may be deleted."""
    rel_path: str
    size: int
    mtime: float
    kind: str            # 'raw' or 'nonraw'
    tape_id: str
    tar_name: str | None
    batch_id: int


def recycle_path(source: str, recycle_dir: str) -> str | None:
    r"""Where the NAS keeps deleted files of `source`: Z:\LiHua -> Z:\#recycle\LiHua.

    None when the recycle bin is switched off in the settings or `source` is the share root itself
    (emptying that would hit everybody's deleted files).
    """
    recycle_dir = recycle_dir.strip().strip("\\/")
    drive, tail = os.path.splitdrive(os.path.abspath(source))
    inside = tail.strip("\\/")
    if not recycle_dir or not drive or not inside:
        return None
    if inside.lower() == recycle_dir.lower() or inside.lower().startswith(recycle_dir.lower() + os.sep):
        return None  # the source is the recycle folder itself
    return os.path.join(drive + os.sep, recycle_dir, inside)


def plan_deletion(
    con: sqlite3.Connection,
    owner: str,
    source: str,
    should_stop: Callable[[], bool] = lambda: False,
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """Walk the source and sort its files into (deletable, [(path, why it stays)])."""
    records: dict[str, list[sqlite3.Row]] = {}
    for row in con.execute(
        "SELECT f.rel_path, f.size, f.mtime, f.kind, f.tape_id, f.tar_name, f.batch_id FROM files f "
        "JOIN jobs j ON j.id=f.job_id "
        "WHERE j.owner=? COLLATE NOCASE AND j.legacy=0 AND f.status='ARCHIVED' AND f.tape_id IS NOT NULL "
        "ORDER BY f.id DESC",  # newest record first
        (owner,),
    ):
        records.setdefault(row["rel_path"].lower(), []).append(row)

    candidates: list[Candidate] = []
    kept: list[tuple[str, str]] = []
    stack = [""]
    while stack:
        if should_stop():
            raise Stopped()
        rel_dir = stack.pop()
        abs_dir = os.path.join(source, rel_dir) if rel_dir else source
        try:
            entries = os.scandir(long_path(abs_dir))
        except OSError as e:
            kept.append(((rel_dir or ".") + os.sep, f"无法读取这个文件夹：{e}"))
            continue
        with entries:
            for entry in entries:
                rel = os.path.join(rel_dir, entry.name) if rel_dir else entry.name
                try:
                    if entry.is_symlink() or entry.is_junction():
                        kept.append((rel, "是链接，未备份"))
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(rel)
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError as e:
                    kept.append((rel, f"无法读取：{e}"))
                    continue
                known = records.get(rel.lower())
                match = next((r for r in known or () if _same(r, st.st_size, st.st_mtime)), None)
                if match is None:
                    kept.append((rel, "备份后改动过，磁带上不是这个版本" if known else "不在磁带上（备份后新增，或备份失败）"))
                    continue
                candidates.append(Candidate(rel, st.st_size, st.st_mtime, match["kind"], match["tape_id"],
                                            match["tar_name"], match["batch_id"]))
    candidates.sort(key=lambda c: c.rel_path.lower())
    kept.sort(key=lambda item: item[0].lower())
    return candidates, kept


def delete_files(
    source: str,
    candidates: list[Candidate],
    should_stop: Callable[[], bool] = lambda: False,
    on_file: Callable[[str], None] = lambda rel: None,
) -> tuple[int, int, list[tuple[str, str]]]:
    """Remove the candidates, then the folders that became empty. Returns (files, bytes, [(path, why not)])."""
    deleted = deleted_bytes = 0
    failed: list[tuple[str, str]] = []
    for item in candidates:
        if should_stop():
            raise Stopped()
        on_file(item.rel_path)
        path = long_path(os.path.join(source, item.rel_path))
        try:
            st = os.stat(path)
            if st.st_size != item.size or abs(st.st_mtime - item.mtime) > MTIME_TOLERANCE:
                failed.append((item.rel_path, "刚刚被改动过，已保留"))
                continue
            _remove(path)
        except FileNotFoundError:
            continue  # already gone
        except OSError as e:
            failed.append((item.rel_path, f"无法删除：{e}"))
            continue
        deleted += 1
        deleted_bytes += item.size
    prune_empty_dirs(source)
    return deleted, deleted_bytes, failed


def purge_tree(path: str, should_stop: Callable[[], bool] = lambda: False) -> tuple[int, int, int]:
    """Delete a folder with everything in it. Returns (files removed, bytes removed, entries that could not be removed)."""
    removed = removed_bytes = failed = 0
    if not os.path.isdir(long_path(path)):
        return 0, 0, 0
    for dirpath, dirnames, filenames in os.walk(long_path(path), topdown=False):
        if should_stop():
            raise Stopped()
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                size = os.lstat(full).st_size
                _remove(full)
            except FileNotFoundError:
                continue
            except OSError:
                failed += 1
                continue
            removed += 1
            removed_bytes += size
        for name in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, name))  # empty by now; a junction is unlinked, not followed
            except OSError:
                failed += 1
    try:
        os.rmdir(long_path(path))
    except OSError:
        failed += 1
    return removed, removed_bytes, failed


def _same(record: sqlite3.Row, size: int, mtime: float) -> bool:
    return (
        record["size"] == size
        and record["mtime"] is not None
        and abs(record["mtime"] - mtime) <= MTIME_TOLERANCE
    )


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)  # a read-only file: clear the flag and try once more
        os.remove(path)
