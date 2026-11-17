"""Settings, stored as config.json next to the exe (or the project root when run from source)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields

GB = 1024 ** 3


def app_dir() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_config_path() -> str:
    return os.path.join(app_dir(), "config.json")


@dataclass
class Config:
    staging_root: str = "D:\\"            # local disk used as the relay between NAS and tape
    tape_root: str = "F:\\"               # LTFS drive letter
    data_dir: str = "D:\\Archive_Index"   # database and logs
    reserve_staging_gb: float = 5.0       # free space always kept on the staging disk
    reserve_tape_gb: float = 10.0         # free space always kept on a tape
    threads: int = 14                     # parallel NAS -> staging copies
    raw_extensions: list[str] = field(default_factory=lambda: [".raw"])  # copied as-is; the rest is tarred
    db_backup_dir: str = ""               # NAS folder that receives a copy of the database ("" = off)

    def __post_init__(self) -> None:
        # "D:/x" -> "D:\x", "D:" -> "D:\" (a bare "D:" would mean "the current folder on D")
        for name in ("staging_root", "tape_root", "data_dir"):
            path = os.path.normpath(getattr(self, name))
            if len(path) == 2 and path[1] == ":":
                path += os.sep
            setattr(self, name, path)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "tape_archive.db")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.data_dir, "logs")

    @property
    def reserve_staging_bytes(self) -> int:
        return int(self.reserve_staging_gb * GB)

    @property
    def reserve_tape_bytes(self) -> int:
        return int(self.reserve_tape_gb * GB)

    def is_raw(self, name: str) -> bool:
        return os.path.splitext(name)[1].lower() in {e.lower() for e in self.raw_extensions}

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or default_config_path()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | None = None) -> None:
        path = path or default_config_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
