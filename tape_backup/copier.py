"""File copying with in-flight SHA-256, tar building, and small filesystem helpers.

Everything here is pure Python (no robocopy / tar.exe): data is hashed while it is
copied, progress is exact to the byte, and a tar can be built from an explicit file list.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import time
from typing import Callable

CHUNK = 4 * 1024 * 1024
TAPE_CHUNK = 8 * 1024 * 1024
TAR_BLOCK = 512
PART_SUFFIX = ".part"  # a file being written; it gets its real name only when complete

ShouldStop = Callable[[], bool]
OnBytes = Callable[[int], None]


class Stopped(Exception):
    """The user asked to stop; the current file was abandoned cleanly."""


class CopyError(OSError):
    """A copy failed. `side` says whether the source ('src') or the destination ('dst') is at fault.

    A source error concerns one file; a destination error (disk full, tape fault) concerns the whole run.
    """

    def __init__(self, side: str, path: str, cause: BaseException | str):
        super().__init__(f"{path}: {cause}")
        self.side = side
        self.path = path
        self.cause = cause


class HashMismatch(CopyError):
    """The data read from the source does not have the SHA-256 it is supposed to have."""

    def __init__(self, path: str):
        super().__init__("src", path, "checksum mismatch")


def long_path(path: str) -> str:
    r"""Add the \\?\ prefix to an absolute path.

    With the prefix Windows takes the path literally: no 260-character limit, and a name
    ending in a dot or a space stays that name instead of silently becoming another file.
    """
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    path = path.replace("/", "\\")
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    if len(path) >= 3 and path[1] == ":" and path[2] == "\\":
        return "\\\\?\\" + path
    return path  # relative path: leave it to Windows


def _never() -> bool:
    return False


def _ignore(_n: int) -> None:
    pass


def copy_file(
    src: str,
    dst: str,
    *,
    should_stop: ShouldStop = _never,
    on_bytes: OnBytes = _ignore,
    chunk: int = CHUNK,
    expect_sha: str | None = None,
) -> tuple[int, str]:
    """Copy src to dst, returning (bytes copied, sha256 hex) computed from the data as it was read.

    The data goes to "<dst>.part" and is renamed to dst only when it is complete (and, if
    expect_sha is given, has that checksum; otherwise HashMismatch). So dst is never a partial
    file, and a file that already exists at dst is replaced only by a complete new one.
    On any failure or stop the .part file is removed and the progress already reported
    through on_bytes is taken back.
    """
    digest = hashlib.sha256()
    copied = 0
    tmp = dst + PART_SUFFIX
    created = False
    try:
        fsrc = open(long_path(src), "rb", buffering=0)
    except OSError as e:
        raise CopyError("src", src, e) from e

    try:
        with fsrc:
            if _same_file(src, dst):
                raise CopyError("dst", dst, "source and destination are the same file")
            try:
                os.makedirs(long_path(os.path.dirname(dst)), exist_ok=True)
                fdst = open(long_path(tmp), "wb")
            except OSError as e:
                raise CopyError("dst", dst, e) from e
            created = True
            with fdst:
                buf = bytearray(chunk)
                view = memoryview(buf)
                while True:
                    if should_stop():
                        raise Stopped()
                    try:
                        n = fsrc.readinto(buf)
                    except OSError as e:
                        raise CopyError("src", src, e) from e
                    if not n:
                        break
                    try:
                        fdst.write(view[:n])
                    except OSError as e:
                        raise CopyError("dst", dst, e) from e
                    digest.update(view[:n])
                    copied += n
                    on_bytes(n)
                try:
                    fdst.flush()
                except OSError as e:
                    raise CopyError("dst", dst, e) from e
            source_stat = os.fstat(fsrc.fileno())

        try:
            written = os.stat(long_path(tmp)).st_size
        except OSError as e:
            raise CopyError("dst", dst, e) from e
        if written != copied:
            raise CopyError("dst", dst, f"size on disk {written} != bytes written {copied}")
        sha = digest.hexdigest()
        if expect_sha and sha != expect_sha.lower():
            raise HashMismatch(src)
        try:  # keep the original modification time, like robocopy /COPY:DAT
            os.utime(long_path(tmp), (source_stat.st_atime, source_stat.st_mtime))
        except OSError:
            pass
        try:
            _replace(tmp, dst)
        except OSError as e:
            raise CopyError("dst", dst, e) from e
    except BaseException:
        on_bytes(-copied)
        if created:
            remove_quietly(tmp)
        raise
    return copied, sha


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(long_path(a), long_path(b))
    except OSError:  # b does not exist yet
        return False


def _replace(tmp: str, dst: str) -> None:
    """Rename tmp onto dst, also on file systems that cannot rename over an existing file."""
    try:
        os.replace(long_path(tmp), long_path(dst))
    except OSError:
        if not os.path.exists(long_path(dst)):
            raise
        os.remove(long_path(dst))
        os.rename(long_path(tmp), long_path(dst))


def hash_file(path: str, *, should_stop: ShouldStop = _never, chunk: int = CHUNK) -> str:
    digest = hashlib.sha256()
    with open(long_path(path), "rb", buffering=0) as f:
        buf = bytearray(chunk)
        view = memoryview(buf)
        while True:
            if should_stop():
                raise Stopped()
            n = f.readinto(buf)
            if not n:
                break
            digest.update(view[:n])
    return digest.hexdigest()


def with_retry(fn: Callable[[], tuple[int, str]], *, attempts: int = 3, delay: float = 2.0,
               should_stop: ShouldStop = _never) -> tuple[int, str]:
    """Run fn, retrying source-side CopyError with exponential backoff (NAS hiccups are usually transient).

    Destination errors are not retried: on tape every attempt consumes space that is never reclaimed.
    Neither is a checksum mismatch: the copy being read is bad, reading it again does not help.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except CopyError as e:
            if e.side == "dst" or isinstance(e, HashMismatch) or attempt == attempts:
                raise
            deadline = time.monotonic() + delay * (2 ** (attempt - 1))
            while time.monotonic() < deadline:
                if should_stop():
                    raise Stopped()
                time.sleep(0.05)
    raise AssertionError("unreachable")


class TarWriter:
    """Write an uncompressed PAX tar member by member, with rollback of a failed member.

    Built on the public TarInfo.tobuf() so a member whose source cannot be read is cut
    back out of the archive (seek + truncate) and the tar stays valid without it.
    """

    def __init__(self, path: str):
        self.path = path
        self._f = None
        self.members = 0
        self._digest = hashlib.sha256()  # of every byte of the archive, so damage to the tar can be detected later
        self.sha256: str | None = None   # set when the archive is closed successfully

    def _write(self, data) -> None:
        assert self._f is not None
        try:
            self._f.write(data)
        except OSError as e:
            raise CopyError("dst", self.path, e) from e
        self._digest.update(data)

    def __enter__(self) -> "TarWriter":
        try:
            os.makedirs(long_path(os.path.dirname(self.path)), exist_ok=True)
            self._f = open(long_path(self.path), "wb")
        except OSError as e:
            raise CopyError("dst", self.path, e) from e
        return self

    def add(self, src: str, arcname: str, *, should_stop: ShouldStop = _never,
            on_bytes: OnBytes = _ignore, chunk: int = CHUNK) -> tuple[int, str]:
        """Append src as arcname. Returns (size, sha256 hex) of the member's data."""
        out = self._f
        assert out is not None
        start = out.tell()
        archive_digest = self._digest.copy()  # restored if this member is rolled back
        digest = hashlib.sha256()
        copied = 0
        try:
            try:
                fsrc = open(long_path(src), "rb", buffering=0)
            except OSError as e:
                raise CopyError("src", src, e) from e
            with fsrc:
                st = os.fstat(fsrc.fileno())
                info = tarfile.TarInfo(arcname)
                info.size = st.st_size
                info.mtime = int(st.st_mtime)
                info.mode = 0o644
                self._write(info.tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape"))

                remaining = info.size  # the header promised exactly this many bytes
                buf = bytearray(chunk)
                view = memoryview(buf)
                while remaining:
                    if should_stop():
                        raise Stopped()
                    try:
                        n = fsrc.readinto(view[: min(chunk, remaining)])
                    except OSError as e:
                        raise CopyError("src", src, e) from e
                    if not n:
                        raise CopyError("src", src, "file shrank while it was being read")
                    self._write(view[:n])
                    digest.update(view[:n])
                    copied += n
                    remaining -= n
                    on_bytes(n)
                self._write(b"\0" * (-info.size % TAR_BLOCK))
        except BaseException:
            on_bytes(-copied)
            self._digest = archive_digest
            try:  # cut the half-written member back out
                out.seek(start)
                out.truncate()
            except OSError:
                pass
            raise
        self.members += 1
        return copied, digest.hexdigest()

    def __exit__(self, exc_type, exc, tb) -> None:
        out = self._f
        assert out is not None
        try:
            if exc_type is None:
                self._write(b"\0" * (2 * TAR_BLOCK))  # end-of-archive marker
                self._write(b"\0" * (-out.tell() % tarfile.RECORDSIZE))
                try:
                    out.flush()
                except OSError as e:
                    raise CopyError("dst", self.path, e) from e
                self.sha256 = self._digest.hexdigest()
        finally:
            out.close()
            if self.sha256 is None:  # aborted, or closing failed: never leave a half-written tar behind
                remove_quietly(self.path)


def list_tar(path: str) -> list[tuple[str, int]]:
    """Read back every member header: [(name, size)]. Raises tarfile.TarError if the tar is damaged."""
    with tarfile.open(long_path(path), "r:") as tar:
        return [(m.name, m.size) for m in tar]


def remove_quietly(path: str) -> None:
    try:
        os.remove(long_path(path))
    except OSError:
        pass


def prune_empty_dirs(root: str) -> None:
    """Delete empty directories under root, and root itself if it ends up empty."""
    if not os.path.isdir(long_path(root)):
        return
    for dirpath, _dirs, _files in os.walk(long_path(root), topdown=False):
        try:
            os.rmdir(dirpath)  # only succeeds when empty
        except OSError:
            pass


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"
