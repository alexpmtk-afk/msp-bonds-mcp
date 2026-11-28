"""Metadata-only, idempotent SQLite journal for a future VTB importer.

No database is opened or created when the package is imported. Tests and callers
must pass a path; the default path is used only when a caller explicitly opens it.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .archive import REPORT_CATEGORIES

STATUSES = frozenset({
    "NEW_DOCUMENT", "EXACT_DUPLICATE", "REPEAT_MESSAGE",
    "CONFLICT_VERSION", "NEEDS_REVIEW", "ERROR", "IMPORTED",
})


@dataclass(frozen=True)
class JournalResult:
    gmail_message_id: str
    attachment_id: Optional[str]
    status: str
    reason: Optional[str]
    attempts: int
    last_processed_at: str
    drive_file_id: Optional[str]


def default_journal_path() -> Path:
    """Return the project runtime path without creating it."""
    return Path(__file__).resolve().parents[4] / "runtime" / "vtb_imports.sqlite3"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ImportJournal:
    """Create/use a local metadata database only after explicit construction."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_journal_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS imports (
                    id INTEGER PRIMARY KEY,
                    gmail_message_id TEXT NOT NULL,
                    attachment_id TEXT,
                    part_id TEXT,
                    attachment_key TEXT NOT NULL,
                    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
                    category TEXT,
                    account_ids_json TEXT NOT NULL,
                    period_start TEXT,
                    period_end TEXT,
                    report_date TEXT,
                    generated_date TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts >= 1),
                    last_processed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    drive_file_id TEXT,
                    UNIQUE(gmail_message_id, attachment_key, sha256)
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_imports_sha256 ON imports(sha256)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_imports_version ON imports(category, account_ids_json, period_start, period_end)"
            )

    @staticmethod
    def _attachment_key(attachment_id: Optional[str], part_id: Optional[str]) -> str:
        if attachment_id and attachment_id.strip():
            return "attachment:" + attachment_id.strip()
        if part_id and part_id.strip():
            return "part:" + part_id.strip()
        raise ValueError("attachment_id or part_id is required for idempotency")

    @staticmethod
    def _validate_sha256(value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return normalized

    @staticmethod
    def _account_ids_json(account_ids: Sequence[str]) -> str:
        values = sorted({value.strip() for value in account_ids if value and value.strip()}, key=str.casefold)
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

    def register_attempt(
        self,
        *,
        gmail_message_id: str,
        attachment_id: Optional[str],
        sha256: str,
        category: Optional[str],
        account_ids: Sequence[str] = (),
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        report_date: Optional[str] = None,
        generated_date: Optional[str] = None,
        classification_status: str = "CLASSIFIED",
        reason: Optional[str] = None,
        error_reason: Optional[str] = None,
        part_id: Optional[str] = None,
    ) -> JournalResult:
        """Assess and upsert one attachment atomically, using only recorded metadata."""
        message_id = gmail_message_id.strip()
        if not message_id:
            raise ValueError("gmail_message_id is required")
        attachment_key = self._attachment_key(attachment_id, part_id)
        digest = self._validate_sha256(sha256)
        if category is not None and category not in REPORT_CATEGORIES:
            raise ValueError("category is not one of the supported VTB report categories")
        accounts_json = self._account_ids_json(account_ids)
        timestamp = _now_utc()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM imports WHERE gmail_message_id=? AND attachment_key=? AND sha256=?",
                (message_id, attachment_key, digest),
            ).fetchone()
            current_id = existing["id"] if existing else None
            key_conflict = connection.execute(
                "SELECT id FROM imports WHERE gmail_message_id=? AND attachment_key=? AND sha256<>? LIMIT 1",
                (message_id, attachment_key, digest),
            ).fetchone()

            if existing and existing["status"] == "IMPORTED":
                status, final_reason = "IMPORTED", None
            elif error_reason:
                status, final_reason = "ERROR", error_reason.strip()
            elif key_conflict:
                status = "CONFLICT_VERSION"
                final_reason = "Same Gmail message and attachment identity produced different bytes; prior entry preserved"
            else:
                # A byte-for-byte match is sufficient to stop a duplicate even if
                # the current attachment has incomplete extracted metadata.
                same_hash = connection.execute(
                    "SELECT gmail_message_id FROM imports WHERE sha256=? AND id IS NOT ? ORDER BY id",
                    (digest, current_id),
                ).fetchall()
                if same_hash:
                    if any(row["gmail_message_id"] != message_id for row in same_hash):
                        status, final_reason = "REPEAT_MESSAGE", "Same SHA-256 is already recorded from another Gmail message"
                    else:
                        status, final_reason = "EXACT_DUPLICATE", "Same SHA-256 is already recorded in this Gmail message"
                elif (
                    classification_status != "CLASSIFIED" or category is None
                    or accounts_json == "[]" or not period_start or not period_end
                ):
                    status = "NEEDS_REVIEW"
                    final_reason = reason or "Classification, account or complete period metadata is incomplete"
                else:
                    conflict = connection.execute(
                        """SELECT id FROM imports
                           WHERE category=? AND account_ids_json=? AND period_start=? AND period_end=?
                             AND sha256<>? AND id IS NOT ? LIMIT 1""",
                        (category, accounts_json, period_start, period_end, digest, current_id),
                    ).fetchone()
                    if conflict:
                        status, final_reason = "CONFLICT_VERSION", "Same category, account set and exact period have different bytes"
                    else:
                        status, final_reason = "NEW_DOCUMENT", None
            if existing:
                attempts = int(existing["attempts"]) + 1
                drive_id = existing["drive_file_id"]
                connection.execute(
                    """UPDATE imports SET attachment_id=?, part_id=?, sha256=?, category=?,
                       account_ids_json=?, period_start=?, period_end=?, report_date=?, generated_date=?,
                       status=?, reason=?, attempts=?, last_processed_at=?, updated_at=?, drive_file_id=?
                       WHERE id=?""",
                    (
                        attachment_id, part_id, digest, category, accounts_json, period_start, period_end,
                        report_date, generated_date, status, final_reason, attempts, timestamp, timestamp,
                        drive_id, current_id,
                    ),
                )
            else:
                attempts = 1
                drive_id = None
                connection.execute(
                    """INSERT INTO imports (
                       gmail_message_id, attachment_id, part_id, attachment_key, sha256, category,
                       account_ids_json, period_start, period_end, report_date, generated_date,
                       status, reason, attempts, last_processed_at, created_at, updated_at, drive_file_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        message_id, attachment_id, part_id, attachment_key, digest, category,
                        accounts_json, period_start, period_end, report_date, generated_date,
                        status, final_reason, attempts, timestamp, timestamp, timestamp, drive_id,
                    ),
                )
            connection.execute("COMMIT")
            return JournalResult(message_id, attachment_id, status, final_reason, attempts, timestamp, drive_id)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def mark_imported(
        self,
        *,
        gmail_message_id: str,
        attachment_id: Optional[str],
        sha256: str,
        drive_file_id: str,
        part_id: Optional[str] = None,
    ) -> JournalResult:
        """Record a completed external upload; never performs a Drive operation."""
        message_id = gmail_message_id.strip()
        drive_id = drive_file_id.strip()
        if not message_id:
            raise ValueError("gmail_message_id is required")
        if not drive_id:
            raise ValueError("drive_file_id is required after a confirmed upload")
        attachment_key = self._attachment_key(attachment_id, part_id)
        digest = self._validate_sha256(sha256)
        timestamp = _now_utc()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM imports WHERE gmail_message_id=? AND attachment_key=? AND sha256=?",
                (message_id, attachment_key, digest),
            ).fetchone()
            if row is None:
                raise LookupError("No matching journal record exists")
            if row["status"] == "IMPORTED":
                if row["drive_file_id"] != drive_id:
                    raise ValueError("An imported record cannot be rebound to a different Drive file")
                connection.execute("COMMIT")
                return JournalResult(
                    message_id, attachment_id, "IMPORTED", None,
                    int(row["attempts"]), row["last_processed_at"], drive_id,
                )
            if row["status"] != "NEW_DOCUMENT":
                raise ValueError("Only a confirmed NEW_DOCUMENT record can be marked imported")
            connection.execute(
                "UPDATE imports SET status='IMPORTED', reason=NULL, drive_file_id=?, "
                "last_processed_at=?, updated_at=? WHERE id=?",
                (drive_id, timestamp, timestamp, row["id"]),
            )
            connection.execute("COMMIT")
            return JournalResult(
                message_id, attachment_id, "IMPORTED", None,
                int(row["attempts"]), timestamp, drive_id,
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def count(self) -> int:
        with self._session() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0])

    def records(self) -> list[dict[str, object]]:
        """Return metadata only; attachment bytes and message bodies are never stored."""
        with self._session() as connection:
            rows = connection.execute(
                "SELECT gmail_message_id, attachment_id, part_id, sha256, category, account_ids_json, "
                "period_start, period_end, report_date, generated_date, status, reason, attempts, "
                "last_processed_at, drive_file_id FROM imports ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def columns(self) -> tuple[str, ...]:
        with self._session() as connection:
            rows = connection.execute("PRAGMA table_info(imports)").fetchall()
        return tuple(row["name"] for row in rows)
