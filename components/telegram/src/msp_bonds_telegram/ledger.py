"""Persistent local idempotency ledger; it stores hashes and delivery state only."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .errors import (
    TelegramDuplicateEvent,
    TelegramEventConflict,
    TelegramLedgerError,
    TelegramRetryNotReady,
)

_FINAL_STATES = {"sent", "rejected", "uncertain", "rate_limited"}


def default_ledger_path(environ: dict[str, str] | None = None) -> Path:
    """Return an application-data path outside the checkout; never use HOME or repo."""

    if os.name != "nt":
        raise TelegramLedgerError("Telegram delivery state requires the local Windows application-data folder.")
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise TelegramLedgerError("The Windows local application-data folder is unavailable.")
    root = Path(local_app_data)
    if not root.is_absolute():
        raise TelegramLedgerError("The Windows local application-data folder is invalid.")
    return root / "MSP-Bonds-MCP" / "telegram" / "delivery.sqlite3"


class SQLiteDeliveryLedger:
    """Deduplicate event IDs across stdio process restarts and fail closed on ambiguity."""

    def __init__(
        self,
        database_path: Path | str | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(database_path) if database_path is not None else default_ledger_path()
        self._clock = clock

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path, timeout=2.0)
            connection.execute("PRAGMA busy_timeout=2000")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS telegram_delivery_events (
                    event_key TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    retry_not_before REAL
                )"""
            )
            connection.commit()
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise TelegramLedgerError("Local Telegram delivery state could not be opened.") from None

    def claim(self, event_id: str, text: str) -> None:
        """Persist a pre-send claim; existing uncertain/sent events are never resent."""

        event_key = self._hash(event_id)
        payload_hash = self._hash(text)
        now = float(self._clock())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_hash, status, retry_not_before "
                "FROM telegram_delivery_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO telegram_delivery_events "
                    "(event_key, payload_hash, status, created_at, updated_at, retry_not_before) "
                    "VALUES (?, ?, 'sending', ?, ?, NULL)",
                    (event_key, payload_hash, now, now),
                )
                connection.commit()
                return

            previous_hash, status, retry_not_before = row
            if previous_hash != payload_hash:
                raise TelegramEventConflict()
            if status == "rate_limited" and retry_not_before is not None:
                remaining = float(retry_not_before) - now
                if remaining <= 0:
                    connection.execute(
                        "UPDATE telegram_delivery_events SET status='sending', "
                        "updated_at=?, retry_not_before=NULL WHERE event_key=?",
                        (now, event_key),
                    )
                    connection.commit()
                    return
                raise TelegramRetryNotReady(int(remaining + 0.999))
            raise TelegramDuplicateEvent()
        except (TelegramDuplicateEvent, TelegramEventConflict, TelegramRetryNotReady):
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise TelegramLedgerError("Local Telegram delivery state could not be claimed.") from None
        finally:
            connection.close()

    def finish(
        self,
        event_id: str,
        status: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        if status not in _FINAL_STATES:
            raise TelegramLedgerError("Invalid local Telegram delivery state.")
        event_key = self._hash(event_id)
        now = float(self._clock())
        retry_not_before = None
        if status == "rate_limited" and retry_after_seconds is not None:
            retry_not_before = now + max(1, int(retry_after_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE telegram_delivery_events SET status=?, updated_at=?, retry_not_before=? "
                "WHERE event_key=? AND status='sending'",
                (status, now, retry_not_before, event_key),
            )
            if cursor.rowcount != 1:
                raise TelegramLedgerError("Local Telegram delivery state transition failed.")
            connection.commit()
        except TelegramLedgerError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise TelegramLedgerError("Local Telegram delivery state could not be updated.") from None
        finally:
            connection.close()

    def status_for(self, event_id: str) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT status FROM telegram_delivery_events WHERE event_key=?",
                (self._hash(event_id),),
            ).fetchone()
            return None if row is None else str(row[0])
        except Exception:
            raise TelegramLedgerError("Local Telegram delivery state could not be read.") from None
        finally:
            connection.close()
