"""Durable lifecycle state for short-lived Discord request interfaces.

The Discord views themselves remain in memory.  This store records only the
message targets and lifecycle state needed to retire abandoned interfaces after
a process restart.  All mutating decisions use ``BEGIN IMMEDIATE`` so an expiry
worker cannot overwrite a concurrent user acceptance.
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from mediabot.core import database


ACTIVE = "active"
DISMISSAL_STATES = frozenset({"dismissed", "expired"})
PRESERVE_STATES = frozenset({"accepted", "kept"})
TERMINAL_STATES = DISMISSAL_STATES | PRESERVE_STATES

BATCH_PENDING = "pending"
BATCH_DELETE_CLAIMED = "delete_claimed"
BATCH_DELETED = "deleted"
BATCH_PRESERVED = "preserved"


@dataclass(frozen=True)
class TransientUIRecord:
    entry_id: str
    batch_id: str
    kind: str
    guild_id: int | None
    channel_id: int
    card_message_id: int
    command_channel_id: int
    command_message_id: int | None
    state: str
    expires_at: float
    claim_token: str | None
    claimed_at: float | None
    created_at: float
    updated_at: float
    terminal_at: float | None


@dataclass(frozen=True)
class BatchCommandClaim:
    batch_id: str
    guild_id: int | None
    channel_id: int
    command_message_id: int
    claim_token: str


def _required_text(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty.")
    return normalized


def _message_id(value: object, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    try:
        numeric = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer.") from exc
    if numeric <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return numeric


def _timestamp(value: float | int | None, *, default_now: bool = False) -> float:
    if value is None:
        if not default_now:
            raise ValueError("A timestamp is required.")
        return time.time()
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Timestamp must be a finite number.") from exc
    if not math.isfinite(numeric):
        raise ValueError("Timestamp must be a finite number.")
    return numeric


def _record(row: sqlite3.Row) -> TransientUIRecord:
    return TransientUIRecord(
        entry_id=str(row["entry_id"]),
        batch_id=str(row["batch_id"]),
        kind=str(row["kind"]),
        guild_id=int(row["guild_id"]) if row["guild_id"] is not None else None,
        channel_id=int(row["channel_id"]),
        card_message_id=int(row["card_message_id"]),
        command_channel_id=int(row["command_channel_id"]),
        command_message_id=(
            int(row["command_message_id"])
            if row["command_message_id"] is not None
            else None
        ),
        state=str(row["state"]),
        expires_at=float(row["expires_at"]),
        claim_token=str(row["claim_token"]) if row["claim_token"] else None,
        claimed_at=float(row["claimed_at"]) if row["claimed_at"] is not None else None,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        terminal_at=(
            float(row["terminal_at"])
            if row["terminal_at"] is not None
            else None
        ),
    )


class TransientUIStore:
    """SQLite-backed registry for actionable Discord message cards."""

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._db_path = os.fspath(db_path) if db_path is not None else None
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self.initialize()

    @property
    def db_path(self) -> str:
        # Resolve lazily when no explicit path was supplied.  This mirrors the
        # existing database module and also respects tests which replace its
        # DB_PATH after import.
        return self._db_path or database.DB_PATH

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS transient_ui_batches (
                    batch_id TEXT PRIMARY KEY,
                    guild_id INTEGER,
                    command_channel_id INTEGER NOT NULL,
                    command_message_id INTEGER,
                    expected_count INTEGER NOT NULL CHECK (expected_count > 0),
                    command_state TEXT NOT NULL DEFAULT 'pending' CHECK (
                        command_state IN (
                            'pending', 'delete_claimed', 'deleted', 'preserved'
                        )
                    ),
                    command_claim_token TEXT,
                    command_claimed_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    terminal_at REAL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS transient_ui_entries (
                    entry_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES transient_ui_batches(batch_id)
                        ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    guild_id INTEGER,
                    channel_id INTEGER NOT NULL,
                    card_message_id INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active' CHECK (
                        state IN ('active', 'dismissed', 'expired', 'accepted', 'kept')
                    ),
                    expires_at REAL NOT NULL,
                    claim_token TEXT,
                    claimed_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    terminal_at REAL
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_transient_ui_active_card
                ON transient_ui_entries (channel_id, card_message_id)
                WHERE state = 'active'
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_transient_ui_expiry
                ON transient_ui_entries (state, expires_at, claimed_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_transient_ui_batch_state
                ON transient_ui_entries (batch_id, state)
                """
            )

    def register(
        self,
        *,
        entry_id: str,
        kind: str,
        channel_id: int,
        card_message_id: int,
        command_message_id: int | None,
        expires_at: float,
        batch_id: str | None = None,
        expected_batch_size: int = 1,
        guild_id: int | None = None,
        command_channel_id: int | None = None,
        now: float | None = None,
    ) -> TransientUIRecord:
        normalized_entry = _required_text(entry_id, "entry_id")
        normalized_batch = _required_text(batch_id or normalized_entry, "batch_id")
        normalized_kind = _required_text(kind, "kind")
        numeric_channel = _message_id(channel_id, "channel_id")
        numeric_card = _message_id(card_message_id, "card_message_id")
        numeric_command = _message_id(
            command_message_id,
            "command_message_id",
            optional=True,
        )
        numeric_command_channel = _message_id(
            command_channel_id if command_channel_id is not None else numeric_channel,
            "command_channel_id",
        )
        numeric_guild = (
            _message_id(guild_id, "guild_id", optional=True)
            if guild_id is not None
            else None
        )
        expiry = _timestamp(expires_at)
        current = _timestamp(now, default_now=True)
        expected = int(expected_batch_size)
        if expected <= 0:
            raise ValueError("expected_batch_size must be positive.")

        with self._transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM transient_ui_entries WHERE entry_id = ?",
                (normalized_entry,),
            ).fetchone()
            if existing is not None:
                if existing["state"] != ACTIVE:
                    raise ValueError(
                        "A terminal transient entry cannot be registered again."
                    )
                existing_metadata = (
                    str(existing["batch_id"]),
                    int(existing["guild_id"])
                    if existing["guild_id"] is not None
                    else None,
                    int(existing["channel_id"]),
                    int(existing["card_message_id"]),
                )
                requested_entry_metadata = (
                    normalized_batch,
                    numeric_guild,
                    numeric_channel,
                    numeric_card,
                )
                if existing_metadata != requested_entry_metadata:
                    raise ValueError("Transient entry message metadata changed.")

            batch = connection.execute(
                "SELECT * FROM transient_ui_batches WHERE batch_id = ?",
                (normalized_batch,),
            ).fetchone()
            if batch is None:
                connection.execute(
                    """
                    INSERT INTO transient_ui_batches (
                        batch_id, guild_id, command_channel_id, command_message_id,
                        expected_count, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_batch,
                        numeric_guild,
                        numeric_command_channel,
                        numeric_command,
                        expected,
                        current,
                        current,
                    ),
                )
            else:
                metadata = (
                    int(batch["guild_id"]) if batch["guild_id"] is not None else None,
                    int(batch["command_channel_id"]),
                    int(batch["command_message_id"])
                    if batch["command_message_id"] is not None
                    else None,
                    int(batch["expected_count"]),
                )
                requested_metadata = (
                    numeric_guild,
                    numeric_command_channel,
                    numeric_command,
                    expected,
                )
                if metadata != requested_metadata:
                    raise ValueError("Batch command metadata or expected size changed.")
                if batch["command_state"] in {BATCH_DELETE_CLAIMED, BATCH_DELETED}:
                    raise ValueError("A terminal batch cannot accept another card.")
                registered_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM transient_ui_entries WHERE batch_id = ?",
                        (normalized_batch,),
                    ).fetchone()[0]
                )
                if existing is None and registered_count >= expected:
                    raise ValueError("Batch already contains its expected card count.")

            try:
                connection.execute(
                    """
                    INSERT INTO transient_ui_entries (
                        entry_id, batch_id, kind, guild_id, channel_id,
                        card_message_id, expires_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entry_id) DO UPDATE SET
                        kind = excluded.kind,
                        expires_at = excluded.expires_at,
                        claim_token = NULL,
                        claimed_at = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_entry,
                        normalized_batch,
                        normalized_kind,
                        numeric_guild,
                        numeric_channel,
                        numeric_card,
                        expiry,
                        current,
                        current,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "That Discord card already has an active transient entry."
                ) from exc
            row = connection.execute(
                """
                SELECT entry.*, batch.command_channel_id, batch.command_message_id
                FROM transient_ui_entries AS entry
                JOIN transient_ui_batches AS batch USING (batch_id)
                WHERE entry.entry_id = ?
                """,
                (normalized_entry,),
            ).fetchone()

        return _record(row)

    def reset(
        self,
        entry_id: str,
        *,
        expires_at: float,
        now: float | None = None,
    ) -> bool:
        normalized_entry = _required_text(entry_id, "entry_id")
        expiry = _timestamp(expires_at)
        current = _timestamp(now, default_now=True)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE transient_ui_entries
                SET expires_at = ?, claim_token = NULL, claimed_at = NULL,
                    updated_at = ?
                WHERE entry_id = ? AND state = 'active' AND claim_token IS NULL
                """,
                (expiry, current, normalized_entry),
            )
        return cursor.rowcount == 1

    def get(self, entry_id: str) -> TransientUIRecord | None:
        normalized_entry = _required_text(entry_id, "entry_id")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT entry.*, batch.command_channel_id, batch.command_message_id
                FROM transient_ui_entries AS entry
                JOIN transient_ui_batches AS batch USING (batch_id)
                WHERE entry.entry_id = ?
                """,
                (normalized_entry,),
            ).fetchone()
        return _record(row) if row is not None else None

    def get_for_card(
        self,
        *,
        channel_id: int,
        card_message_id: int,
        active_only: bool = False,
    ) -> TransientUIRecord | None:
        """Return the active or most recent lifecycle for one Discord card."""
        numeric_channel = _message_id(channel_id, "channel_id")
        numeric_card = _message_id(card_message_id, "card_message_id")
        active_clause = "AND entry.state = 'active'" if active_only else ""
        with self._transaction() as connection:
            row = connection.execute(
                f"""
                SELECT entry.*, batch.command_channel_id, batch.command_message_id
                FROM transient_ui_entries AS entry
                JOIN transient_ui_batches AS batch USING (batch_id)
                WHERE entry.channel_id = ? AND entry.card_message_id = ?
                {active_clause}
                ORDER BY
                    CASE WHEN entry.state = 'active' THEN 0 ELSE 1 END,
                    entry.created_at DESC,
                    entry.entry_id DESC
                LIMIT 1
                """,
                (numeric_channel, numeric_card),
            ).fetchone()
        return _record(row) if row is not None else None

    def reset_card(
        self,
        *,
        channel_id: int,
        card_message_id: int,
        expires_at: float,
        now: float | None = None,
    ) -> bool:
        """Extend whichever active lifecycle currently owns a Discord card."""
        numeric_channel = _message_id(channel_id, "channel_id")
        numeric_card = _message_id(card_message_id, "card_message_id")
        expiry = _timestamp(expires_at)
        current = _timestamp(now, default_now=True)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE transient_ui_entries
                SET expires_at = ?, claim_token = NULL, claimed_at = NULL,
                    updated_at = ?
                WHERE channel_id = ? AND card_message_id = ?
                  AND state = 'active' AND claim_token IS NULL
                """,
                (expiry, current, numeric_channel, numeric_card),
            )
        return cursor.rowcount == 1

    def transition_card(
        self,
        *,
        channel_id: int,
        card_message_id: int,
        kind: str,
        expires_at: float,
        now: float | None = None,
    ) -> bool:
        """Move an active card to another actionable stage and renew it.

        The entry and batch identity stay unchanged. This is important for
        public Discord messages that are edited in place as their workflow
        advances (for example, rating search -> saved-rating actions).
        """
        numeric_channel = _message_id(channel_id, "channel_id")
        numeric_card = _message_id(card_message_id, "card_message_id")
        normalized_kind = _required_text(kind, "kind")
        expiry = _timestamp(expires_at)
        current = _timestamp(now, default_now=True)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE transient_ui_entries
                SET kind = ?, expires_at = ?, claim_token = NULL,
                    claimed_at = NULL, updated_at = ?
                WHERE channel_id = ? AND card_message_id = ?
                  AND state = 'active' AND claim_token IS NULL
                """,
                (
                    normalized_kind,
                    expiry,
                    current,
                    numeric_channel,
                    numeric_card,
                ),
            )
        return cursor.rowcount == 1

    def list_expired(
        self,
        *,
        now: float | None = None,
        limit: int = 100,
        lease_seconds: float = 60,
    ) -> list[TransientUIRecord]:
        current = _timestamp(now, default_now=True)
        claim_before = current - max(1.0, float(lease_seconds))
        requested_limit = max(1, min(int(limit), 1000))
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT entry.*, batch.command_channel_id, batch.command_message_id
                FROM transient_ui_entries AS entry
                JOIN transient_ui_batches AS batch USING (batch_id)
                WHERE entry.state = 'active'
                  AND entry.expires_at <= ?
                  AND (
                      entry.claim_token IS NULL
                      OR entry.claimed_at IS NULL
                      OR entry.claimed_at <= ?
                  )
                ORDER BY entry.expires_at ASC, entry.entry_id ASC
                LIMIT ?
                """,
                (current, claim_before, requested_limit),
            ).fetchall()
        return [_record(row) for row in rows]

    def claim_expired(
        self,
        claim_token: str,
        *,
        now: float | None = None,
        limit: int = 100,
        lease_seconds: float = 60,
    ) -> list[TransientUIRecord]:
        token = _required_text(claim_token, "claim_token")
        current = _timestamp(now, default_now=True)
        claim_before = current - max(1.0, float(lease_seconds))
        requested_limit = max(1, min(int(limit), 1000))

        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT entry_id
                FROM transient_ui_entries
                WHERE state = 'active'
                  AND expires_at <= ?
                  AND (
                      claim_token IS NULL OR claimed_at IS NULL OR claimed_at <= ?
                  )
                ORDER BY expires_at ASC, entry_id ASC
                LIMIT ?
                """,
                (current, claim_before, requested_limit),
            ).fetchall()
            entry_ids = [str(row["entry_id"]) for row in rows]
            if not entry_ids:
                return []

            placeholders = ",".join("?" for _ in entry_ids)
            connection.execute(
                f"""
                UPDATE transient_ui_entries
                SET claim_token = ?, claimed_at = ?, updated_at = ?
                WHERE entry_id IN ({placeholders}) AND state = 'active'
                """,
                (token, current, current, *entry_ids),
            )
            claimed = connection.execute(
                f"""
                SELECT entry.*, batch.command_channel_id, batch.command_message_id
                FROM transient_ui_entries AS entry
                JOIN transient_ui_batches AS batch USING (batch_id)
                WHERE entry.entry_id IN ({placeholders})
                  AND entry.state = 'active' AND entry.claim_token = ?
                ORDER BY entry.expires_at ASC, entry.entry_id ASC
                """,
                (*entry_ids, token),
            ).fetchall()

        return [_record(row) for row in claimed]

    def release_claim(
        self,
        entry_id: str,
        claim_token: str,
        *,
        retry_at: float | None = None,
        now: float | None = None,
    ) -> bool:
        normalized_entry = _required_text(entry_id, "entry_id")
        token = _required_text(claim_token, "claim_token")
        current = _timestamp(now, default_now=True)
        retry = _timestamp(retry_at) if retry_at is not None else None
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE transient_ui_entries
                SET expires_at = COALESCE(?, expires_at), claim_token = NULL,
                    claimed_at = NULL, updated_at = ?
                WHERE entry_id = ? AND state = 'active' AND claim_token = ?
                """,
                (retry, current, normalized_entry, token),
            )
        return cursor.rowcount == 1

    def mark_terminal(
        self,
        entry_id: str,
        state: str,
        *,
        claim_token: str | None = None,
        now: float | None = None,
    ) -> bool:
        normalized_entry = _required_text(entry_id, "entry_id")
        normalized_state = str(state).casefold().strip()
        if normalized_state not in TERMINAL_STATES:
            raise ValueError(f"Unknown terminal transient state: {state!r}")
        token = _required_text(claim_token, "claim_token") if claim_token else None
        current = _timestamp(now, default_now=True)

        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state, claim_token, batch_id FROM transient_ui_entries "
                "WHERE entry_id = ?",
                (normalized_entry,),
            ).fetchone()
            if row is None:
                return False
            if row["state"] == normalized_state:
                return True
            if row["state"] != ACTIVE:
                return False
            if token is not None and row["claim_token"] != token:
                return False

            connection.execute(
                """
                UPDATE transient_ui_entries
                SET state = ?, claim_token = NULL, claimed_at = NULL,
                    terminal_at = ?, updated_at = ?
                WHERE entry_id = ? AND state = 'active'
                """,
                (normalized_state, current, current, normalized_entry),
            )
            if normalized_state in PRESERVE_STATES:
                connection.execute(
                    """
                    UPDATE transient_ui_batches
                    SET command_state = 'preserved', command_claim_token = NULL,
                        command_claimed_at = NULL, terminal_at = COALESCE(terminal_at, ?),
                        updated_at = ?
                    WHERE batch_id = ? AND command_state != 'deleted'
                    """,
                    (current, current, str(row["batch_id"])),
                )
        return True

    def claim_batch_command_deletion(
        self,
        batch_id: str,
        claim_token: str,
        *,
        now: float | None = None,
        lease_seconds: float = 60,
    ) -> BatchCommandClaim | None:
        normalized_batch = _required_text(batch_id, "batch_id")
        token = _required_text(claim_token, "claim_token")
        current = _timestamp(now, default_now=True)
        claim_before = current - max(1.0, float(lease_seconds))

        with self._transaction(immediate=True) as connection:
            batch = connection.execute(
                "SELECT * FROM transient_ui_batches WHERE batch_id = ?",
                (normalized_batch,),
            ).fetchone()
            if batch is None or batch["command_state"] in {
                BATCH_DELETED,
                BATCH_PRESERVED,
            }:
                return None
            if (
                batch["command_state"] == BATCH_DELETE_CLAIMED
                and batch["command_claim_token"] != token
                and batch["command_claimed_at"] is not None
                and float(batch["command_claimed_at"]) > claim_before
            ):
                return None

            counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN state IN ('dismissed', 'expired') THEN 1 ELSE 0 END)
                           AS disposable,
                       SUM(CASE WHEN state IN ('accepted', 'kept') THEN 1 ELSE 0 END)
                           AS preserved
                FROM transient_ui_entries
                WHERE batch_id = ?
                """,
                (normalized_batch,),
            ).fetchone()
            total = int(counts["total"] or 0)
            disposable = int(counts["disposable"] or 0)
            preserved = int(counts["preserved"] or 0)
            if (
                total != int(batch["expected_count"])
                or disposable != total
                or preserved != 0
            ):
                return None

            if batch["command_message_id"] is None:
                connection.execute(
                    """
                    UPDATE transient_ui_batches
                    SET command_state = 'deleted', command_claim_token = NULL,
                        command_claimed_at = NULL, terminal_at = ?, updated_at = ?
                    WHERE batch_id = ?
                    """,
                    (current, current, normalized_batch),
                )
                return None

            connection.execute(
                """
                UPDATE transient_ui_batches
                SET command_state = 'delete_claimed', command_claim_token = ?,
                    command_claimed_at = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (token, current, current, normalized_batch),
            )
            return BatchCommandClaim(
                batch_id=normalized_batch,
                guild_id=(
                    int(batch["guild_id"]) if batch["guild_id"] is not None else None
                ),
                channel_id=int(batch["command_channel_id"]),
                command_message_id=int(batch["command_message_id"]),
                claim_token=token,
            )

    def claim_deletable_batch_commands(
        self,
        claim_token: str,
        *,
        now: float | None = None,
        limit: int = 100,
        lease_seconds: float = 60,
    ) -> list[BatchCommandClaim]:
        """Claim fully disposable command messages, including stale retries.

        This scan is independent of entry expiry. It lets a later cleanup
        cycle recover when command deletion failed, or when the process died
        after deleting a Discord message but before recording the result.
        """
        token = _required_text(claim_token, "claim_token")
        current = _timestamp(now, default_now=True)
        claim_before = current - max(1.0, float(lease_seconds))
        requested_limit = max(1, min(int(limit), 1000))

        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT batch.*
                FROM transient_ui_batches AS batch
                JOIN transient_ui_entries AS entry USING (batch_id)
                WHERE (
                    batch.command_state = 'pending'
                    OR (
                        batch.command_state = 'delete_claimed'
                        AND (
                            batch.command_claimed_at IS NULL
                            OR batch.command_claimed_at <= ?
                        )
                    )
                )
                GROUP BY batch.batch_id
                HAVING COUNT(entry.entry_id) = batch.expected_count
                   AND SUM(
                       CASE WHEN entry.state IN ('dismissed', 'expired')
                            THEN 1 ELSE 0 END
                   ) = COUNT(entry.entry_id)
                   AND SUM(
                       CASE WHEN entry.state IN ('accepted', 'kept')
                            THEN 1 ELSE 0 END
                   ) = 0
                ORDER BY batch.updated_at ASC, batch.batch_id ASC
                LIMIT ?
                """,
                (claim_before, requested_limit),
            ).fetchall()

            claims = []
            for row in rows:
                batch_id = str(row["batch_id"])
                if row["command_message_id"] is None:
                    connection.execute(
                        """
                        UPDATE transient_ui_batches
                        SET command_state = 'deleted', command_claim_token = NULL,
                            command_claimed_at = NULL, terminal_at = ?, updated_at = ?
                        WHERE batch_id = ?
                        """,
                        (current, current, batch_id),
                    )
                    continue

                connection.execute(
                    """
                    UPDATE transient_ui_batches
                    SET command_state = 'delete_claimed', command_claim_token = ?,
                        command_claimed_at = ?, updated_at = ?
                    WHERE batch_id = ?
                    """,
                    (token, current, current, batch_id),
                )
                claims.append(
                    BatchCommandClaim(
                        batch_id=batch_id,
                        guild_id=(
                            int(row["guild_id"])
                            if row["guild_id"] is not None
                            else None
                        ),
                        channel_id=int(row["command_channel_id"]),
                        command_message_id=int(row["command_message_id"]),
                        claim_token=token,
                    )
                )

        return claims

    def mark_batch_command_deleted(
        self,
        batch_id: str,
        claim_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        normalized_batch = _required_text(batch_id, "batch_id")
        token = _required_text(claim_token, "claim_token")
        current = _timestamp(now, default_now=True)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE transient_ui_batches
                SET command_state = 'deleted', command_claim_token = NULL,
                    command_claimed_at = NULL, terminal_at = ?, updated_at = ?
                WHERE batch_id = ? AND command_state = 'delete_claimed'
                  AND command_claim_token = ?
                """,
                (current, current, normalized_batch, token),
            )
        return cursor.rowcount == 1

    def release_batch_command_claim(
        self,
        batch_id: str,
        claim_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        normalized_batch = _required_text(batch_id, "batch_id")
        token = _required_text(claim_token, "claim_token")
        current = _timestamp(now, default_now=True)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE transient_ui_batches
                SET command_state = 'pending', command_claim_token = NULL,
                    command_claimed_at = NULL, updated_at = ?
                WHERE batch_id = ? AND command_state = 'delete_claimed'
                  AND command_claim_token = ?
                """,
                (current, normalized_batch, token),
            )
        return cursor.rowcount == 1

    def purge_terminal(
        self,
        *,
        before: float,
        limit: int = 1000,
    ) -> int:
        cutoff = _timestamp(before)
        requested_limit = max(1, min(int(limit), 10000))
        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT batch.batch_id
                FROM transient_ui_batches AS batch
                WHERE batch.command_state IN ('deleted', 'preserved')
                  AND batch.updated_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM transient_ui_entries AS entry
                      WHERE entry.batch_id = batch.batch_id
                        AND (entry.state = 'active' OR entry.updated_at > ?)
                  )
                ORDER BY batch.updated_at ASC, batch.batch_id ASC
                LIMIT ?
                """,
                (cutoff, cutoff, requested_limit),
            ).fetchall()
            batch_ids = [str(row["batch_id"]) for row in rows]
            if not batch_ids:
                return 0
            placeholders = ",".join("?" for _ in batch_ids)
            connection.execute(
                f"DELETE FROM transient_ui_batches WHERE batch_id IN ({placeholders})",
                batch_ids,
            )
        return len(batch_ids)
