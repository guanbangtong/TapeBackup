"""How much space each first-level folder (one per person) of a NAS share takes. Read-only.

No UI code here:

    scan = NasScan(r"Z:\\")
    result, message = scan.run()     # blocking; call it from a worker thread
    scan.stop()                      # from any thread
    scan.snapshot()                  # from any thread: finished folders and running totals

Only lists folders and reads file sizes (os.scandir); never opens, writes or deletes anything.
Several folders are measured at once: on a network share most of the time is spent waiting
for the server, so parallel listing is much faster than one folder after another.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .copier import long_path

# Top-level folders that are not people: recycle bins, snapshots, system and hidden folders
# (#recycle, #snapshot, @eaDir, .something, $RECYCLE.BIN, System Volume Information).
SKIP_PREFIXES = ("#", "@", ".", "$")
SKIP_NAMES = {"system volume information"}


def normalize_root(text: str) -> str:
    """'z' / 'Z:' / 'z:/' -> 'Z:\\'; other paths unchanged apart from slashes."""
    path = text.strip().strip('"').replace("/", "\\")
    if len(path) == 1 and path.isalpha():
        path += ":"
    if len(path) == 2 and path[1] == ":":
        path += "\\"
    return path[0].upper() + path[1:] if len(path) >= 2 and path[1] == ":" else path


def is_person_folder(name: str) -> bool:
    return not name.startswith(SKIP_PREFIXES) and name.lower() not in SKIP_NAMES


def top_folders(root: str) -> list[str]:
    """Names of the first-level folders to measure, sorted."""
    with os.scandir(long_path(root)) as it:
        names = [e.name for e in it if e.is_dir(follow_symlinks=False) and not e.is_junction() and is_person_folder(e.name)]
    return sorted(names, key=str.lower)


@dataclass
class FolderUsage:
    name: str
    bytes: int = 0
    files: int = 0
    errors: int = 0  # files or folders that could not be read (skipped)


class NasScan:
    def __init__(self, root: str, workers: int = 8) -> None:
        self.root = normalize_root(root)
        self.workers = workers
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.folders: list[str] = []
        self.done: list[FolderUsage] = []   # finished folders, in finishing order
        self.running: set[str] = set()
        self.files_seen = 0
        self.bytes_seen = 0
        self.started = 0.0
        self.seconds = 0.0
        self.result: tuple[str, str] | None = None  # what run() returned, once it has

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "done": list(self.done),
                "folders": len(self.folders),
                "running": sorted(self.running, key=str.lower),
                "files": self.files_seen,
                "bytes": self.bytes_seen,
                "seconds": (time.monotonic() - self.started) if self.started and not self.seconds else self.seconds,
            }

    def run(self) -> tuple[str, str]:
        """Measure every folder. Returns ('done'|'stopped'|'error', message)."""
        self.started = time.monotonic()
        try:
            if not os.path.isdir(long_path(self.root)):
                return "error", f"找不到 {self.root}，请检查盘符或路径，以及 NAS 是否已连接。"
            try:
                self.folders = top_folders(self.root)
            except OSError as e:
                return "error", f"无法读取 {self.root}：{e}"
            if not self.folders:
                return "done", f"{self.root} 下没有要统计的文件夹。"
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for future in [pool.submit(self._measure, name) for name in self.folders]:
                    future.result()  # re-raises a bug in _measure instead of hiding it
            if self._stop.is_set():
                return "stopped", ""
            return "done", ""
        finally:
            self.seconds = time.monotonic() - self.started

    def _measure(self, name: str) -> None:
        if self._stop.is_set():
            return
        usage = FolderUsage(name)
        with self._lock:
            self.running.add(name)
        pending = [long_path(os.path.join(self.root, name))]
        files = size = 0  # reported to the shared counters in steps, not per file
        while pending:
            if self._stop.is_set():
                break
            try:
                it = os.scandir(pending.pop())
            except OSError:
                usage.errors += 1
                continue
            with it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not entry.is_junction():  # links could lead in circles or out of the share
                                pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            n = entry.stat(follow_symlinks=False).st_size
                            usage.bytes += n
                            usage.files += 1
                            size += n
                            files += 1
                    except OSError:
                        usage.errors += 1
            if files >= 2000:
                self._count(files, size)
                files = size = 0
        self._count(files, size)
        with self._lock:
            self.running.discard(name)
            if not self._stop.is_set():  # a stopped folder's total would be too small: leave it out
                self.done.append(usage)

    def _count(self, files: int, size: int) -> None:
        with self._lock:
            self.files_seen += files
            self.bytes_seen += size
