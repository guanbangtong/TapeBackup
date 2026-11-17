"""SQLite schema and the queries used by the UI.

The engine's state-machine SQL lives in engine.py; everything the UI needs
(queue management, search, record list, legacy import, backup) is here.

Status values
    jobs.status     QUEUED -> RUNNING -> DONE        (CANCELLED when abandoned)
    batches.status  STAGING -> STAGED -> ARCHIVED    (same meaning as the old scripts)
    files.status    PENDING -> STAGED -> ARCHIVED    (FAILED = could not be read, REPLACED = overwritten on tape)
"""

from __future__ import annotations

import csv
import os
import shutil
import sqlite3
from datetime import datetime

TB = 1024 ** 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    owner        TEXT NOT NULL,              -- folder name, e.g. LiHua
    source_path  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'QUEUED',
    legacy       INTEGER NOT NULL DEFAULT 0, -- 1 = imported from the old archive_index.csv (no file list)
    scanned      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    total_files  INTEGER NOT NULL DEFAULT 0, -- files this job has to back up (after skipping archived ones)
    total_bytes  INTEGER NOT NULL DEFAULT 0,
    done_files   INTEGER NOT NULL DEFAULT 0, -- archived on tape
    done_bytes   INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    delete_source     INTEGER NOT NULL DEFAULT 0, -- 1 = delete the NAS source once the backup is complete
    source_deleted_at TEXT,                       -- set when that deletion has been done
    deleted_files     INTEGER NOT NULL DEFAULT 0,
    deleted_bytes     INTEGER NOT NULL DEFAULT 0,
    kept_files        INTEGER NOT NULL DEFAULT 0  -- files left on the NAS because they are not (or no longer) on tape
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    seq         INTEGER NOT NULL,
    tape_id     TEXT,
    status      TEXT NOT NULL DEFAULT 'STAGING',
    cleaned     INTEGER NOT NULL DEFAULT 0,  -- staging copies deleted after archiving
    tar_name    TEXT,                        -- tar holding this batch's non-raw files, if any
    tar_size    INTEGER,
    tar_sha256  TEXT,                        -- of the whole tar, taken while it was built
    tar_written TEXT,                        -- tape id the tar was completely written to (before ARCHIVED)
    bytes       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id        INTEGER PRIMARY KEY,
    job_id    INTEGER NOT NULL REFERENCES jobs(id),
    batch_id  INTEGER REFERENCES batches(id),
    rel_path  TEXT NOT NULL,                 -- relative to the source folder, backslash separated
    size      INTEGER NOT NULL,
    mtime     REAL,
    kind      TEXT NOT NULL,                 -- 'raw' (copied as-is) or 'nonraw' (inside the batch tar)
    sha256    TEXT,
    status    TEXT NOT NULL DEFAULT 'PENDING',
    attempts  INTEGER NOT NULL DEFAULT 0,
    written   TEXT,                          -- tape id a raw file was completely written to (before ARCHIVED)
    tape_id   TEXT,
    tar_name  TEXT,
    error     TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_job_status ON files(job_id, status);
CREATE INDEX IF NOT EXISTS idx_files_batch ON files(batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_job ON batches(job_id);
"""


# Columns added after the first release: (table, column, definition). A database created by an
# older version gets them on open, so nobody has to delete their records to upgrade.
ADDED_COLUMNS = (
    ("batches", "tar_written", "TEXT"),
    ("files", "written", "TEXT"),
    ("jobs", "delete_source", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "source_deleted_at", "TEXT"),
    ("jobs", "deleted_files", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "deleted_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "kept_files", "INTEGER NOT NULL DEFAULT 0"),
)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: str) -> sqlite3.Connection:
    """Open (and create if needed) the database. Use one connection per thread."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")    # UI can read while the engine writes
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    for table, column, definition in ADDED_COLUMNS:
        if column not in {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    con.commit()
    return con


# ---------------------------------------------------------------- queue

def add_job(con: sqlite3.Connection, owner: str, source_path: str, delete_source: bool = False) -> int:
    cur = con.execute(
        "INSERT INTO jobs(owner, source_path, status, created_at, delete_source) VALUES (?,?, 'QUEUED', ?, ?)",
        (owner, source_path, now(), int(delete_source)),
    )
    con.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def set_delete_source(con: sqlite3.Connection, job_id: int, delete_source: bool) -> None:
    """Switch 'delete the NAS source after backup' for a job whose source has not been deleted yet."""
    con.execute(
        "UPDATE jobs SET delete_source=? WHERE id=? AND source_deleted_at IS NULL", (int(delete_source), job_id)
    )
    con.commit()


def pending_deletions(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Finished jobs whose NAS source is still waiting to be deleted (the run was stopped before it happened)."""
    return con.execute(
        "SELECT * FROM jobs WHERE status='DONE' AND legacy=0 AND delete_source=1 AND source_deleted_at IS NULL "
        "ORDER BY id"
    ).fetchall()


def remove_queued_job(con: sqlite3.Connection, job_id: int) -> bool:
    """Remove a job that has not started yet. Started jobs must go through Engine.abandon_job."""
    cur = con.execute("DELETE FROM jobs WHERE id=? AND status='QUEUED' AND scanned=0", (job_id,))
    con.commit()
    return cur.rowcount > 0


def active_jobs(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Jobs shown in the backup queue: unfinished (interrupted) ones first, then queued."""
    return con.execute(
        "SELECT * FROM jobs WHERE status IN ('RUNNING','QUEUED') AND legacy=0 "
        "ORDER BY (status='RUNNING') DESC, id"
    ).fetchall()


def queue_jobs(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everything the next run will work on: finished jobs still waiting for their source to be
    deleted, then unfinished jobs, then queued ones."""
    return pending_deletions(con) + active_jobs(con)


def owner_history(con: sqlite3.Connection, owner: str) -> dict:
    """What is already known about an owner, for the 'already backed up' notice when queueing.

    archived_files > 0  -> the new job will be incremental
    legacy_only         -> only old CSV records exist (no file list), so the new job is a full backup
    sources             -> folders that were backed up under this name before; a different folder with
                           the same name must not be mixed in unless it really is the same data
    """
    sources = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT source_path FROM jobs WHERE owner=? COLLATE NOCASE AND legacy=0 "
            "AND status IN ('DONE','CANCELLED')",
            (owner,),
        )
    ]
    row = con.execute(
        "SELECT COUNT(*) AS n, MAX(j.source_path) AS src FROM files f JOIN jobs j ON j.id=f.job_id "
        "WHERE j.owner=? COLLATE NOCASE AND f.status='ARCHIVED'",
        (owner,),
    ).fetchone()
    legacy = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE owner=? COLLATE NOCASE AND legacy=1", (owner,)
    ).fetchone()[0]
    active = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE owner=? COLLATE NOCASE AND status IN ('RUNNING','QUEUED')",
        (owner,),
    ).fetchone()[0]
    return {
        "archived_files": row["n"],
        "last_source": row["src"],
        "legacy_only": legacy > 0 and row["n"] == 0,
        "active": active > 0,
        "sources": sources,
    }


# ---------------------------------------------------------------- records

def list_jobs(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Job-level history (the successor of archive_index.csv), newest first.

    Extra columns: tapes (comma separated tape ids), batch_count.
    """
    return con.execute(
        "SELECT j.*, "
        " (SELECT GROUP_CONCAT(DISTINCT b.tape_id) FROM batches b "
        "   WHERE b.job_id=j.id AND b.status='ARCHIVED') AS tapes, "
        " (SELECT COUNT(*) FROM batches b WHERE b.job_id=j.id AND b.status='ARCHIVED') AS batch_count "
        "FROM jobs j ORDER BY COALESCE(j.finished_at, j.created_at) DESC, j.id DESC"
    ).fetchall()


def list_owners(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT owner FROM jobs ORDER BY owner COLLATE NOCASE")]


def list_tapes(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every tape that holds data: tape_id, bytes, last_used."""
    return con.execute(
        "SELECT tape_id, SUM(bytes) AS bytes, MAX(archived_at) AS last_used "
        "FROM batches WHERE status='ARCHIVED' AND tape_id IS NOT NULL "
        "GROUP BY tape_id ORDER BY last_used DESC"
    ).fetchall()


def failed_files(con: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT rel_path, size, error FROM files WHERE job_id=? AND status='FAILED' ORDER BY rel_path",
        (job_id,),
    ).fetchall()


# ---------------------------------------------------------------- search

SEARCH_COLUMNS = (
    "f.id, f.rel_path, f.size, f.kind, f.sha256, f.tape_id, f.tar_name, "
    "j.owner, j.id AS job_id, b.archived_at"
)


def search_files(
    con: sqlite3.Connection,
    keyword: str = "",
    owner: str | None = None,
    tape_id: str | None = None,
    limit: int = 2000,
) -> list[sqlite3.Row]:
    """Files on tape whose path contains every space-separated word of `keyword`."""
    where = ["f.status='ARCHIVED'"]
    args: list = []
    for word in keyword.split():
        escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("f.rel_path LIKE ? ESCAPE '\\'")
        args.append(f"%{escaped}%")
    if owner:
        where.append("j.owner=? COLLATE NOCASE")
        args.append(owner)
    if tape_id:
        where.append("f.tape_id=?")
        args.append(tape_id)
    args.append(limit)
    return con.execute(
        f"SELECT {SEARCH_COLUMNS} FROM files f "
        "JOIN jobs j ON j.id=f.job_id JOIN batches b ON b.id=f.batch_id "
        f"WHERE {' AND '.join(where)} ORDER BY j.owner COLLATE NOCASE, f.rel_path LIMIT ?",
        args,
    ).fetchall()


def files_of_job(con: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """All archived files of one job (same columns as search_files), e.g. to restore a whole backup."""
    return con.execute(
        f"SELECT {SEARCH_COLUMNS} FROM files f "
        "JOIN jobs j ON j.id=f.job_id JOIN batches b ON b.id=f.batch_id "
        "WHERE f.job_id=? AND f.status='ARCHIVED' ORDER BY f.rel_path",
        (job_id,),
    ).fetchall()


# ---------------------------------------------------------------- legacy import

def import_legacy_csv(con: sqlite3.Connection, csv_path: str) -> tuple[int, int]:
    """Import the old archive_index.csv (date, tape_id, owner, size_tb).

    Each row becomes a finished job with one archived batch and no file list.
    Returns (imported, skipped_as_duplicates).
    """
    imported = skipped = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            date = (row.get("date") or "").strip()
            tape = (row.get("tape_id") or "").strip().upper()  # tape ids are upper case everywhere
            owner = (row.get("owner") or "").strip()
            if not owner or not tape:
                continue
            try:
                size = int(float(row.get("size_tb") or 0) * TB)
            except ValueError:
                size = 0
            stamp = f"{date} 00:00:00" if date else now()
            dup = con.execute(
                "SELECT 1 FROM jobs j JOIN batches b ON b.job_id=j.id "
                "WHERE j.legacy=1 AND j.owner=? AND b.tape_id=? AND j.finished_at=?",
                (owner, tape, stamp),
            ).fetchone()
            if dup:
                skipped += 1
                continue
            cur = con.execute(
                "INSERT INTO jobs(owner, source_path, status, legacy, scanned, created_at, started_at, "
                "finished_at, total_bytes, done_bytes) VALUES (?, '', 'DONE', 1, 1, ?, ?, ?, ?, ?)",
                (owner, stamp, stamp, stamp, size, size),
            )
            con.execute(
                "INSERT INTO batches(job_id, seq, tape_id, status, cleaned, bytes, created_at, archived_at) "
                "VALUES (?, 1, ?, 'ARCHIVED', 1, ?, ?, ?)",
                (cur.lastrowid, tape, size, stamp, stamp),
            )
            imported += 1
    con.commit()
    return imported, skipped


# ---------------------------------------------------------------- backup

def backup_database(con: sqlite3.Connection, db_path: str, dest_dir: str) -> str:
    """Write a consistent copy of the database into dest_dir (e.g. a NAS folder)."""
    os.makedirs(dest_dir, exist_ok=True)
    local_tmp = db_path + ".backup.tmp"
    target = sqlite3.connect(local_tmp)
    try:
        con.backup(target)  # consistent snapshot even while the database is in use
    finally:
        target.close()
    dest = os.path.join(dest_dir, os.path.basename(db_path))
    shutil.copyfile(local_tmp, dest + ".tmp")
    os.replace(dest + ".tmp", dest)
    os.remove(local_tmp)
    return dest
