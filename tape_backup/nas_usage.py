"""How much space each first-level folder (one per person) of a NAS share takes. Read-only.

No UI code here:

    scan = NasScan(r"Z:\\")
    result, message = scan.run()     # blocking; call it from a worker thread
    scan.stop()                      # from any thread
    scan.snapshot()                  # from any thread: finished folders and running totals

Only lists folders and reads file sizes (os.scandir); never opens, writes or deletes anything.

Speed: on a network share most of the time is spent waiting for the server, so several folders are
listed at once. The unit of work is one folder at any depth, not one person: every listed subfolder
goes back into a shared to-do stack that any idle thread takes from, so one person with most of the
files is still measured by all threads together. The stack is last-in-first-out, so the threads
work on the same person and finish people one after another (results appear early, little memory).
Each person counts the folders still to list; at 0 that person is done.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass

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


RESULT_FILE = "nas_usage.json"  # last complete result, in the data folder next to the database


def save_result(path: str, root: str, measured_at: str, seconds: float, done: list[FolderUsage]) -> None:
    """Keep a complete result so the next start of the program shows it without measuring again."""
    data = {"root": root, "measured_at": measured_at, "seconds": seconds, "folders": [asdict(u) for u in done]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # never leaves a half-written file


def load_result(path: str) -> dict | None:
    """The saved result with folders as FolderUsage, or None if there is none (or it cannot be read)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["folders"] = [FolderUsage(**u) for u in data["folders"]]
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


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
        self._todo: queue.LifoQueue = queue.LifoQueue()  # (person, folder) still to list; None = end
        self._usage: dict[str, FolderUsage] = {}
        self._left: dict[str, int] = {}  # per person: folders taken or waiting, not yet listed
        self._bug: Exception | None = None

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

            # The to-do stack holds (person, folder to list). A person is done when their count reaches 0.
            for name in reversed(self.folders):  # reversed: the stack hands out the first name first
                self._usage[name] = FolderUsage(name)
                self._left[name] = 1
                self._todo.put((name, long_path(os.path.join(self.root, name))))
            threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.workers)]
            for t in threads:
                t.start()
            self._todo.join()  # every folder taken has been reported finished (task_done)
            for _ in threads:
                self._todo.put(None)  # tells each thread to end
            for t in threads:
                t.join()
            if self._bug is not None:  # a programming error in a worker: don't pass off partial totals as done
                return "error", f"统计时出现意外错误：{self._bug!r}"
            if self._stop.is_set():
                return "stopped", ""
            return "done", ""
        finally:
            self.seconds = time.monotonic() - self.started

    def _worker(self) -> None:
        while True:
            item = self._todo.get()
            if item is None:
                return
            try:
                if not self._stop.is_set() and self._bug is None:  # stopping: just empty the stack
                    self._list_folder(*item)
            except Exception as e:  # noqa: BLE001 - must still call task_done, or run() waits forever
                self._bug = e
            finally:
                self._todo.task_done()

    def _list_folder(self, name: str, path: str) -> None:
        """List one folder: add up its files, put its subfolders on the to-do stack."""
        subfolders: list[str] = []
        files = size = errors = 0
        try:
            it = os.scandir(path)
        except OSError:
            errors += 1
        else:
            with it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not entry.is_junction():  # links could lead in circles or out of the share
                                subfolders.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            size += entry.stat(follow_symlinks=False).st_size  # came with the listing: no extra request
                            files += 1
                    except OSError:
                        errors += 1
        with self._lock:  # once per folder, not per file
            usage = self._usage[name]
            usage.bytes += size
            usage.files += files
            usage.errors += errors
            self.files_seen += files
            self.bytes_seen += size
            self.running.add(name)
            # Count the subfolders in before they go on the stack, so the count never reaches 0 too early.
            self._left[name] += len(subfolders) - 1
            if self._left[name] == 0:
                self.running.discard(name)
                if not self._stop.is_set():  # a stopped person's total would be too small: leave it out
                    self.done.append(usage)
        for sub in subfolders:
            self._todo.put((name, sub))
