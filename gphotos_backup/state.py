"""SQLite state database wrapper with typed records."""

import logging
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .config import MAX_RETRIES

logger = logging.getLogger(__name__)

SCHEMA = """\
CREATE TABLE IF NOT EXISTS photos (
  id              INTEGER PRIMARY KEY,
  nas_path        TEXT UNIQUE NOT NULL,
  filename        TEXT NOT NULL,
  device_filename TEXT UNIQUE NOT NULL,
  size_bytes      INTEGER NOT NULL,
  file_mtime      REAL NOT NULL,
  is_video        INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','on_device',
                          'presumed_uploaded','failed','permanently_failed')),
  created_at      REAL NOT NULL DEFAULT (unixepoch('now','subsec')),
  pushed_at       REAL,
  deleted_at      REAL,
  retry_count     INTEGER NOT NULL DEFAULT 0,
  error_msg       TEXT
);

CREATE INDEX IF NOT EXISTS idx_batch_select ON photos(status, is_video, file_mtime);
"""


class FileStatus(StrEnum):
    PENDING = "pending"
    ON_DEVICE = "on_device"
    PRESUMED_UPLOADED = "presumed_uploaded"
    FAILED = "failed"
    PERMANENTLY_FAILED = "permanently_failed"


# Valid status transitions
_VALID_TRANSITIONS: dict[FileStatus, set[FileStatus]] = {
    FileStatus.PENDING: {FileStatus.ON_DEVICE, FileStatus.FAILED},
    FileStatus.ON_DEVICE: {FileStatus.PRESUMED_UPLOADED},
    FileStatus.FAILED: {FileStatus.PENDING, FileStatus.PERMANENTLY_FAILED},
    FileStatus.PERMANENTLY_FAILED: {FileStatus.PENDING},
}


@dataclass
class FileRecord:
    id: int
    nas_path: str
    filename: str
    device_filename: str
    size_bytes: int
    file_mtime: float
    is_video: bool
    status: FileStatus
    pushed_at: float | None
    deleted_at: float | None
    retry_count: int
    error_msg: str | None

    @classmethod
    def from_row(cls, row: tuple) -> "FileRecord":
        return cls(
            id=row[0],
            nas_path=row[1],
            filename=row[2],
            device_filename=row[3],
            size_bytes=row[4],
            file_mtime=row[5],
            is_video=bool(row[6]),
            status=FileStatus(row[7]),
            pushed_at=row[8],
            deleted_at=row[9],
            retry_count=row[10],
            error_msg=row[11],
        )


class StateDB:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), autocommit=True)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_size_limit=67108864")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.autocommit = False

    def init_db(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA)

    def add_files(self, records: list[tuple]) -> None:
        """Batched INSERT OR IGNORE via executemany. Each tuple:
        (nas_path, filename, device_filename, size_bytes, file_mtime, is_video)
        """
        batch: list[tuple] = []
        for record in records:
            batch.append(record)
            if len(batch) >= 1000:
                with self._conn:
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO photos "
                        "(nas_path, filename, device_filename, size_bytes, file_mtime, is_video) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        batch,
                    )
                batch.clear()
        if batch:
            with self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO photos "
                    "(nas_path, filename, device_filename, size_bytes, file_mtime, is_video) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    batch,
                )

    def get_pending_batch(self, batch_size_bytes: int, *, is_video: bool = False) -> list[FileRecord]:
        """Cursor-based iteration — no LIMIT, stops when size threshold reached."""
        cursor = self._conn.execute(
            "SELECT id, nas_path, filename, device_filename, size_bytes, file_mtime, "
            "is_video, status, pushed_at, deleted_at, retry_count, error_msg "
            "FROM photos WHERE status = ? AND is_video = ? "
            "ORDER BY file_mtime ASC",
            (FileStatus.PENDING.value, int(is_video)),
        )

        batch: list[FileRecord] = []
        total_size = 0
        for row in cursor:
            record = FileRecord.from_row(row)
            if total_size + record.size_bytes > batch_size_bytes and batch:
                break
            batch.append(record)
            total_size += record.size_bytes
        return batch

    def update_status(
        self, file_id: int, new_status: FileStatus, *, error_msg: str | None = None
    ) -> None:
        """Update status with transition validation."""
        row = self._conn.execute(
            "SELECT status FROM photos WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"File not found: {file_id}")

        current_status = FileStatus(row[0])
        valid_next = _VALID_TRANSITIONS.get(current_status, set())
        if new_status not in valid_next:
            raise ValueError(
                f"Invalid transition: {current_status} -> {new_status} for file {file_id}"
            )

        timestamp_field = None
        if new_status == FileStatus.ON_DEVICE:
            timestamp_field = "pushed_at"
        elif new_status == FileStatus.PRESUMED_UPLOADED:
            timestamp_field = "deleted_at"

        if timestamp_field:
            with self._conn:
                self._conn.execute(
                    f"UPDATE photos SET status = ?, {timestamp_field} = unixepoch('now','subsec'), "
                    "error_msg = ? WHERE id = ?",
                    (new_status.value, error_msg, file_id),
                )
        else:
            with self._conn:
                self._conn.execute(
                    "UPDATE photos SET status = ?, error_msg = ? WHERE id = ?",
                    (new_status.value, error_msg, file_id),
                )

    def increment_retry(self, file_id: int) -> None:
        """Atomic: bump retry_count, set status to pending or permanently_failed."""
        with self._conn:
            self._conn.execute(
                "UPDATE photos SET "
                "retry_count = retry_count + 1, "
                "status = CASE WHEN retry_count + 1 >= ? THEN 'permanently_failed' ELSE 'failed' END, "
                "error_msg = CASE WHEN retry_count + 1 >= ? THEN error_msg ELSE NULL END "
                "WHERE id = ?",
                (MAX_RETRIES, MAX_RETRIES, file_id),
            )

    def get_on_device_stale(self, upload_window_seconds: float) -> list[FileRecord]:
        cursor = self._conn.execute(
            "SELECT id, nas_path, filename, device_filename, size_bytes, file_mtime, "
            "is_video, status, pushed_at, deleted_at, retry_count, error_msg "
            "FROM photos WHERE status = ? AND pushed_at < unixepoch('now','subsec') - ?",
            (FileStatus.ON_DEVICE.value, upload_window_seconds),
        )
        return [FileRecord.from_row(row) for row in cursor]

    def get_progress(self) -> dict[str, int]:
        cursor = self._conn.execute(
            "SELECT status, COUNT(*) FROM photos GROUP BY status"
        )
        counts = {status.value: 0 for status in FileStatus}
        total = 0
        for status_val, count in cursor:
            counts[status_val] = count
            total += count
        counts["total"] = total
        return counts

    def has_pending_or_on_device(self) -> bool:
        row = self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM photos WHERE status IN (?, ?))",
            (FileStatus.PENDING.value, FileStatus.ON_DEVICE.value),
        ).fetchone()
        return bool(row and row[0])

    def reset_permanently_failed(self) -> int:
        """Reset permanently_failed files to pending. Returns count of reset files."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE photos SET status = ?, retry_count = 0 "
                "WHERE status = ?",
                (FileStatus.PENDING.value, FileStatus.PERMANENTLY_FAILED.value),
            )
            return cursor.rowcount

    def checkpoint(self) -> None:
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        self._conn.close()


def open_readonly(db_path: Path) -> StateDB:
    """Open database in read-only mode for --status queries."""
    db = StateDB.__new__(StateDB)
    db._db_path = db_path
    db._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, autocommit=True)
    db._conn.execute("PRAGMA busy_timeout=5000")
    db._conn.autocommit = False
    return db
