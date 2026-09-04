"""Durable SQLite storage for MediaBot events.

This module deliberately does not import Discord or any media provider.  It
owns the event schema and the transactions that must remain correct when two
people click at nearly the same time.  The command/UI layer belongs above the
``EventService`` in :mod:`mediabot.services.events`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


EVENT_SCHEMA_VERSION = 2


class EventStoreError(RuntimeError):
    """Base class for durable event-state failures."""


class EventNotFoundError(EventStoreError):
    """Raised when an event does not exist in the requested guild."""


class EventConflictError(EventStoreError):
    """Raised when a durable uniqueness rule rejects an operation."""


class OpenEventExistsError(EventConflictError):
    """Only one event may accept nominations in a guild at a time."""

    def __init__(self, existing_event_id: int):
        self.existing_event_id = int(existing_event_id)
        super().__init__(
            f"Guild already has open event #{self.existing_event_id}."
        )


class EventStateError(EventConflictError):
    """Raised when an operation is invalid for the event's current state."""


class StaleEventRevisionError(EventConflictError):
    """The event changed after a UI rendered its confirmation preview."""


class VoteLimitExceededError(EventConflictError):
    """Raised when a multi-choice ballot has no remaining selections."""

    def __init__(self, vote_limit: int):
        self.vote_limit = int(vote_limit)
        super().__init__(f"This event allows {self.vote_limit} vote(s) per person.")


class NominationNotFoundError(EventNotFoundError):
    """Raised when a nomination is missing or belongs to another event."""


class TimeOptionNotFoundError(EventNotFoundError):
    """Raised when a candidate time is missing or belongs to another event."""


def utc_text(value: datetime) -> str:
    """Return a fixed-width UTC timestamp that sorts correctly in SQLite."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Event times must be timezone-aware datetimes.")
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc_text(value: str) -> datetime:
    """Parse a timestamp emitted by :func:`utc_text` or SQLite defaults."""

    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_json(value: dict[str, Any] | None) -> str:
    """Persist a stable, human-inspectable rule snapshot."""

    return json.dumps(
        value or {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


class EventStore:
    """SQLite repository with transaction-safe event mutations."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None):
        self.db_path = str(
            db_path
            if db_path is not None
            else os.environ.get("DB_PATH", "/app/data/mediabot.db")
        )

    def _connect(self) -> sqlite3.Connection:
        parent = Path(self.db_path).parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def init_schema(self) -> None:
        """Apply the independently versioned event schema exactly once."""

        with self._transaction(immediate=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT (
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
                )
                """
            )

            applied = {
                int(row["version"])
                for row in conn.execute(
                    "SELECT version FROM event_schema_migrations"
                ).fetchall()
            }

            if 1 not in applied:
                self._apply_v1(conn)
                conn.execute(
                    """
                    INSERT INTO event_schema_migrations (version, name)
                    VALUES (1, 'generic event engine')
                    """
                )

            # The migration itself is shape-aware and cheap to repeat.  Running
            # it even with a marker present heals databases that were manually
            # restored from an intermediate v2 backup.
            self._apply_v2(conn)
            if 2 not in applied:
                conn.execute(
                    """
                    INSERT INTO event_schema_migrations (version, name)
                    VALUES (2, 'event time voting, reminders, and archiving')
                    """
                )

    @staticmethod
    def _apply_v1(conn: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_guild_id INTEGER NOT NULL,
                discord_channel_id INTEGER NOT NULL,
                dashboard_message_id INTEGER,
                name TEXT NOT NULL,
                created_by_discord_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (
                    status IN ('open', 'scheduled', 'completed', 'cancelled')
                ),
                timezone_name TEXT NOT NULL,
                vote_limit INTEGER NOT NULL DEFAULT 1 CHECK (vote_limit >= 1),
                preset_key TEXT,
                preset_version TEXT,
                rules_json TEXT NOT NULL DEFAULT '{}',
                revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                updated_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                scheduled_at TEXT,
                completed_at TEXT,
                cancelled_at TEXT
            )
            """,
            """
            CREATE UNIQUE INDEX uq_events_one_open_per_guild
            ON events (discord_guild_id)
            WHERE status = 'open'
            """,
            """
            CREATE INDEX idx_events_guild_status
            ON events (discord_guild_id, status, created_at DESC)
            """,
            """
            CREATE TABLE event_nominations (
                nomination_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
                tmdb_id INTEGER NOT NULL CHECK (tmdb_id > 0),
                jellyfin_item_id TEXT,
                title TEXT NOT NULL,
                year TEXT NOT NULL DEFAULT '',
                poster_path TEXT,
                nominated_by_discord_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active' CHECK (
                    status IN ('active', 'withdrawn')
                ),
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                UNIQUE (event_id, media_type, tmdb_id),
                UNIQUE (nomination_id, event_id),
                FOREIGN KEY (event_id) REFERENCES events(event_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_event_nominations_event
            ON event_nominations (event_id, status, created_at, nomination_id)
            """,
            """
            CREATE TABLE event_votes (
                event_id INTEGER NOT NULL,
                nomination_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                PRIMARY KEY (event_id, nomination_id, discord_user_id),
                FOREIGN KEY (event_id) REFERENCES events(event_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (nomination_id, event_id)
                    REFERENCES event_nominations(nomination_id, event_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_event_votes_user
            ON event_votes (event_id, discord_user_id, nomination_id)
            """,
            """
            CREATE TABLE event_slots (
                slot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                starts_at_utc TEXT NOT NULL,
                nomination_id INTEGER,
                status TEXT NOT NULL DEFAULT 'planned' CHECK (
                    status IN ('planned', 'completed', 'cancelled')
                ),
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                updated_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                UNIQUE (event_id, starts_at_utc),
                FOREIGN KEY (event_id) REFERENCES events(event_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (nomination_id, event_id)
                    REFERENCES event_nominations(nomination_id, event_id)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX idx_event_slots_time
            ON event_slots (starts_at_utc, status, event_id)
            """,
        )

        for statement in statements:
            conn.execute(statement)

    @staticmethod
    def _validate_v2(conn: sqlite3.Connection) -> None:
        """Fail closed when an existing v2 object has an unexpected shape.

        ``CREATE ... IF NOT EXISTS`` makes interrupted migrations retryable, but
        SQLite does not compare the existing object with the requested DDL.  A
        same-named table restored from a partial/manual repair must therefore be
        inspected explicitly before the v2 marker or runtime health can pass.
        """

        def quoted(name: str) -> str:
            return '"' + str(name).replace('"', '""') + '"'

        def normalized_default(value: Any) -> str | None:
            if value is None:
                return None
            return "".join(str(value).split())

        def table_columns(table: str) -> dict[str, tuple[str, int, int, str | None]]:
            return {
                str(row["name"]): (
                    str(row["type"]).upper(),
                    int(row["notnull"]),
                    int(row["pk"]),
                    normalized_default(row["dflt_value"]),
                )
                for row in conn.execute(
                    f"PRAGMA table_info({quoted(table)})"
                ).fetchall()
            }

        def require_columns(
            table: str,
            expected: dict[str, tuple[str, int, int, str | None]],
            *,
            exact: bool = True,
        ) -> None:
            actual = table_columns(table)
            if (exact and actual != expected) or (
                not exact
                and any(actual.get(name) != shape for name, shape in expected.items())
            ):
                raise EventStoreError(
                    f"Event schema v2 has malformed columns for {table}."
                )

        def foreign_key_shapes(table: str) -> set[tuple[Any, ...]]:
            grouped: dict[int, list[sqlite3.Row]] = {}
            for row in conn.execute(
                f"PRAGMA foreign_key_list({quoted(table)})"
            ).fetchall():
                grouped.setdefault(int(row["id"]), []).append(row)
            return {
                (
                    str(rows[0]["table"]),
                    str(rows[0]["on_update"]).upper(),
                    str(rows[0]["on_delete"]).upper(),
                    str(rows[0]["match"]).upper(),
                    tuple(
                        (str(row["from"]), str(row["to"]))
                        for row in sorted(rows, key=lambda value: int(value["seq"]))
                    ),
                )
                for rows in grouped.values()
            }

        def require_foreign_keys(
            table: str,
            expected: set[tuple[Any, ...]],
        ) -> None:
            if foreign_key_shapes(table) != expected:
                raise EventStoreError(
                    f"Event schema v2 has malformed foreign keys for {table}."
                )

        def index_shape(table: str, index_name: str) -> tuple[Any, ...] | None:
            row = next(
                (
                    value
                    for value in conn.execute(
                        f"PRAGMA index_list({quoted(table)})"
                    ).fetchall()
                    if str(value["name"]) == index_name
                ),
                None,
            )
            if row is None:
                return None
            key_rows = [
                value
                for value in conn.execute(
                    f"PRAGMA index_xinfo({quoted(index_name)})"
                ).fetchall()
                if int(value["key"]) == 1
            ]
            key_rows.sort(key=lambda value: int(value["seqno"]))
            return (
                bool(row["unique"]),
                bool(row["partial"]),
                tuple(str(value["name"]) for value in key_rows),
                tuple(bool(value["desc"]) for value in key_rows),
            )

        def require_index(
            table: str,
            name: str,
            *,
            unique: bool,
            columns: tuple[str, ...],
            descending: tuple[bool, ...] | None = None,
        ) -> None:
            expected = (
                bool(unique),
                False,
                tuple(columns),
                descending or tuple(False for _ in columns),
            )
            if index_shape(table, name) != expected:
                raise EventStoreError(
                    f"Event schema v2 has malformed index {name}."
                )

        def unique_keys(table: str) -> set[tuple[str, ...]]:
            results = set()
            for row in conn.execute(
                f"PRAGMA index_list({quoted(table)})"
            ).fetchall():
                if not bool(row["unique"]) or bool(row["partial"]):
                    continue
                key_rows = [
                    value
                    for value in conn.execute(
                        f"PRAGMA index_xinfo({quoted(str(row['name']))})"
                    ).fetchall()
                    if int(value["key"]) == 1
                ]
                key_rows.sort(key=lambda value: int(value["seqno"]))
                results.add(tuple(str(value["name"]) for value in key_rows))
            return results

        def require_unique_keys(table: str, expected: set[tuple[str, ...]]) -> None:
            if not expected.issubset(unique_keys(table)):
                raise EventStoreError(
                    f"Event schema v2 has malformed uniqueness for {table}."
                )

        timestamp_default = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        require_columns(
            "events",
            {"archived_at": ("TEXT", 0, 0, None)},
            exact=False,
        )
        require_columns(
            "event_slots",
            {"native_scheduled_event_id": ("INTEGER", 0, 0, None)},
            exact=False,
        )
        require_columns(
            "event_time_options",
            {
                "time_option_id": ("INTEGER", 0, 1, None),
                "event_id": ("INTEGER", 1, 0, None),
                "starts_at_utc": ("TEXT", 1, 0, None),
                "created_by_discord_id": ("INTEGER", 1, 0, None),
                "created_at": ("TEXT", 1, 0, timestamp_default),
            },
        )
        require_columns(
            "event_time_votes",
            {
                "event_id": ("INTEGER", 1, 1, None),
                "time_option_id": ("INTEGER", 1, 2, None),
                "discord_user_id": ("INTEGER", 1, 3, None),
                "created_at": ("TEXT", 1, 0, timestamp_default),
            },
        )
        require_columns(
            "event_reminder_state",
            {
                "slot_id": ("INTEGER", 1, 1, None),
                "event_id": ("INTEGER", 1, 0, None),
                "stage": ("TEXT", 1, 2, None),
                "advanced_at": ("TEXT", 1, 0, timestamp_default),
            },
        )

        cascade = ("NO ACTION", "CASCADE", "NONE")
        require_foreign_keys(
            "event_time_options",
            {("events", *cascade, (("event_id", "event_id"),))},
        )
        require_foreign_keys(
            "event_time_votes",
            {
                ("events", *cascade, (("event_id", "event_id"),)),
                (
                    "event_time_options",
                    *cascade,
                    (
                        ("time_option_id", "time_option_id"),
                        ("event_id", "event_id"),
                    ),
                ),
            },
        )
        require_foreign_keys(
            "event_reminder_state",
            {
                (
                    "event_slots",
                    *cascade,
                    (("slot_id", "slot_id"), ("event_id", "event_id")),
                )
            },
        )

        require_unique_keys(
            "event_time_options",
            {("event_id", "starts_at_utc"), ("time_option_id", "event_id")},
        )
        require_unique_keys(
            "event_time_votes",
            {("event_id", "time_option_id", "discord_user_id")},
        )
        require_unique_keys(
            "event_reminder_state",
            {("slot_id", "stage")},
        )
        require_index(
            "event_time_options",
            "idx_event_time_options_event",
            unique=False,
            columns=("event_id", "starts_at_utc", "time_option_id"),
        )
        require_index(
            "event_time_votes",
            "idx_event_time_votes_user",
            unique=False,
            columns=("event_id", "discord_user_id", "time_option_id"),
        )
        require_index(
            "event_slots",
            "uq_event_slots_slot_event",
            unique=True,
            columns=("slot_id", "event_id"),
        )
        require_index(
            "event_reminder_state",
            "idx_event_reminder_state_event",
            unique=False,
            columns=("event_id", "slot_id", "stage"),
        )
        require_index(
            "events",
            "idx_events_guild_archive",
            unique=False,
            columns=("discord_guild_id", "archived_at", "status", "created_at"),
            descending=(False, False, False, True),
        )

        table_sql = {
            str(row["name"]): "".join(str(row["sql"] or "").lower().split())
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('event_time_options', 'event_reminder_state')"
            ).fetchall()
        }
        if (
            "time_option_idintegerprimarykeyautoincrement"
            not in table_sql.get("event_time_options", "")
        ):
            raise EventStoreError(
                "Event schema v2 has malformed identity allocation for "
                "event_time_options."
            )
        if (
            "check(stagein('24h','1h','start'))"
            not in table_sql.get("event_reminder_state", "")
        ):
            raise EventStoreError(
                "Event schema v2 has malformed stage validation for "
                "event_reminder_state."
            )

    @staticmethod
    def _apply_v2(conn: sqlite3.Connection) -> None:
        """Add Event v2 without rebuilding or discarding any v1 tables.

        Column checks make the migration safe to retry after a manually repaired
        or partially applied database.  The migration marker is still written
        only after every statement succeeds in the surrounding transaction.
        """

        event_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(events)").fetchall()
        }
        if "archived_at" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN archived_at TEXT")
        slot_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(event_slots)").fetchall()
        }
        if "native_scheduled_event_id" not in slot_columns:
            conn.execute(
                "ALTER TABLE event_slots "
                "ADD COLUMN native_scheduled_event_id INTEGER"
            )

        statements = (
            """
            CREATE TABLE IF NOT EXISTS event_time_options (
                time_option_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                starts_at_utc TEXT NOT NULL,
                created_by_discord_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                UNIQUE (event_id, starts_at_utc),
                UNIQUE (time_option_id, event_id),
                FOREIGN KEY (event_id) REFERENCES events(event_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_event_time_options_event
            ON event_time_options (event_id, starts_at_utc, time_option_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS event_time_votes (
                event_id INTEGER NOT NULL,
                time_option_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                PRIMARY KEY (event_id, time_option_id, discord_user_id),
                FOREIGN KEY (event_id) REFERENCES events(event_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (time_option_id, event_id)
                    REFERENCES event_time_options(time_option_id, event_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_event_time_votes_user
            ON event_time_votes (event_id, discord_user_id, time_option_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_event_slots_slot_event
            ON event_slots (slot_id, event_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS event_reminder_state (
                slot_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                stage TEXT NOT NULL CHECK (stage IN ('24h', '1h', 'start')),
                advanced_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                PRIMARY KEY (slot_id, stage),
                FOREIGN KEY (slot_id, event_id)
                    REFERENCES event_slots(slot_id, event_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_event_reminder_state_event
            ON event_reminder_state (event_id, slot_id, stage)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_events_guild_archive
            ON events (discord_guild_id, archived_at, status, created_at DESC)
            """,
        )
        for statement in statements:
            conn.execute(statement)
        EventStore._validate_v2(conn)

    def schema_version(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version "
                "FROM event_schema_migrations"
            ).fetchone()
        return int(row["version"])

    def create_event(
        self,
        *,
        discord_guild_id: int,
        discord_channel_id: int,
        name: str,
        created_by_discord_id: int,
        timezone_name: str,
        vote_limit: int = 1,
        preset_key: str | None = None,
        preset_version: str | None = None,
        rules: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = (
            int(discord_guild_id),
            int(discord_channel_id),
            str(name).strip(),
            int(created_by_discord_id),
            str(timezone_name),
            int(vote_limit),
            str(preset_key) if preset_key else None,
            str(preset_version) if preset_version else None,
            canonical_json(rules),
        )

        with self._transaction(immediate=True) as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO events (
                        discord_guild_id,
                        discord_channel_id,
                        name,
                        created_by_discord_id,
                        timezone_name,
                        vote_limit,
                        preset_key,
                        preset_version,
                        rules_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                row = conn.execute(
                    """
                    SELECT event_id
                    FROM events
                    WHERE discord_guild_id = ? AND status = 'open'
                    LIMIT 1
                    """,
                    (int(discord_guild_id),),
                ).fetchone()
                if row is not None:
                    raise OpenEventExistsError(row["event_id"]) from exc
                raise

            row = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()

        return dict(row)

    def event_by_id(
        self,
        event_id: int,
        *,
        discord_guild_id: int | None = None,
    ) -> dict[str, Any] | None:
        guild_clause = ""
        parameters: list[Any] = [int(event_id)]
        if discord_guild_id is not None:
            guild_clause = "AND discord_guild_id = ?"
            parameters.append(int(discord_guild_id))

        with self._connection() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM events
                WHERE event_id = ? {guild_clause}
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return dict(row) if row else None

    def open_event_for_guild(self, discord_guild_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM events
                WHERE discord_guild_id = ? AND status = 'open'
                LIMIT 1
                """,
                (int(discord_guild_id),),
            ).fetchone()
        return dict(row) if row else None

    def list_events(
        self,
        *,
        discord_guild_id: int,
        statuses: Sequence[str] | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        normalized = tuple(dict.fromkeys(str(value).casefold() for value in statuses or ()))
        status_clause = ""
        parameters: list[Any] = [int(discord_guild_id)]
        if normalized:
            invalid = set(normalized) - {"open", "scheduled", "completed", "cancelled"}
            if invalid:
                raise ValueError("Unknown event status.")
            placeholders = ",".join("?" for _ in normalized)
            status_clause = f"AND status IN ({placeholders})"
            parameters.extend(normalized)
        archive_clause = "" if include_archived else "AND archived_at IS NULL"
        parameters.append(max(1, min(int(limit), 500)))

        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM events
                WHERE discord_guild_id = ? {status_clause} {archive_clause}
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_dashboard_message(
        self,
        *,
        event_id: int,
        discord_channel_id: int,
        dashboard_message_id: int,
    ) -> dict[str, Any]:
        with self._transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE events
                SET discord_channel_id = ?, dashboard_message_id = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE event_id = ?
                """,
                (
                    int(discord_channel_id),
                    int(dashboard_message_id),
                    int(event_id),
                ),
            )
            if cursor.rowcount == 0:
                raise EventNotFoundError(f"Event #{int(event_id)} does not exist.")
            row = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
        return dict(row)

    @staticmethod
    def _open_event_row(
        conn: sqlite3.Connection,
        event_id: int,
        *,
        activity: str,
    ) -> sqlite3.Row:
        event = conn.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (int(event_id),),
        ).fetchone()
        if event is None:
            raise EventNotFoundError(f"Event #{int(event_id)} does not exist.")
        if event["status"] != "open":
            raise EventStateError(f"{activity} is closed for this event.")
        return event

    @staticmethod
    def _time_option_rows(
        conn: sqlite3.Connection,
        event_id: int,
        *,
        ranked: bool = False,
    ) -> list[sqlite3.Row]:
        order = (
            "vote_count DESC, o.starts_at_utc ASC, o.time_option_id ASC"
            if ranked
            else "o.starts_at_utc ASC, o.time_option_id ASC"
        )
        return conn.execute(
            f"""
            SELECT o.*, COUNT(v.discord_user_id) AS vote_count
            FROM event_time_options AS o
            LEFT JOIN event_time_votes AS v
              ON v.event_id = o.event_id
             AND v.time_option_id = o.time_option_id
            WHERE o.event_id = ?
            GROUP BY o.time_option_id
            ORDER BY {order}
            """,
            (int(event_id),),
        ).fetchall()

    def add_time_options(
        self,
        *,
        event_id: int,
        starts_at_utc: Sequence[str],
        created_by_discord_id: int,
    ) -> list[dict[str, Any]]:
        """Add candidate times, leaving existing options and votes untouched."""

        normalized = tuple(dict.fromkeys(str(value) for value in starts_at_utc))
        with self._transaction(immediate=True) as conn:
            event = self._open_event_row(
                conn,
                int(event_id),
                activity="Time selection",
            )
            created = 0
            for starts_at in normalized:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO event_time_options (
                        event_id, starts_at_utc, created_by_discord_id
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        int(event_id),
                        starts_at,
                        int(created_by_discord_id),
                    ),
                )
                created += max(0, int(cursor.rowcount))
            if created:
                conn.execute(
                    """
                    UPDATE events
                    SET revision = revision + 1,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE event_id = ? AND revision = ?
                    """,
                    (int(event_id), int(event["revision"])),
                )
            rows = self._time_option_rows(conn, int(event_id))
        return [dict(row) for row in rows]

    def replace_time_options(
        self,
        *,
        event_id: int,
        starts_at_utc: Sequence[str],
        created_by_discord_id: int,
    ) -> list[dict[str, Any]]:
        """Replace candidates while retaining votes on unchanged timestamps."""

        normalized = tuple(dict.fromkeys(str(value) for value in starts_at_utc))
        desired = set(normalized)
        with self._transaction(immediate=True) as conn:
            event = self._open_event_row(
                conn,
                int(event_id),
                activity="Time selection",
            )
            current_rows = conn.execute(
                """
                SELECT starts_at_utc FROM event_time_options
                WHERE event_id = ?
                """,
                (int(event_id),),
            ).fetchall()
            current = {str(row["starts_at_utc"]) for row in current_rows}

            removed = current - desired
            if removed:
                placeholders = ",".join("?" for _ in removed)
                conn.execute(
                    f"""
                    DELETE FROM event_time_options
                    WHERE event_id = ? AND starts_at_utc IN ({placeholders})
                    """,
                    (int(event_id), *sorted(removed)),
                )
            for starts_at in normalized:
                if starts_at not in current:
                    conn.execute(
                        """
                        INSERT INTO event_time_options (
                            event_id, starts_at_utc, created_by_discord_id
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            int(event_id),
                            starts_at,
                            int(created_by_discord_id),
                        ),
                    )

            if current != desired:
                conn.execute(
                    """
                    UPDATE events
                    SET revision = revision + 1,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE event_id = ? AND revision = ?
                    """,
                    (int(event_id), int(event["revision"])),
                )
            rows = self._time_option_rows(conn, int(event_id))
        return [dict(row) for row in rows]

    def time_option_rows(
        self,
        event_id: int,
        *,
        ranked: bool = False,
    ) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = self._time_option_rows(
                conn,
                int(event_id),
                ranked=ranked,
            )
        return [dict(row) for row in rows]

    def user_time_vote_ids(
        self,
        *,
        event_id: int,
        discord_user_id: int,
    ) -> tuple[int, ...]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT time_option_id FROM event_time_votes
                WHERE event_id = ? AND discord_user_id = ?
                ORDER BY time_option_id
                """,
                (int(event_id), int(discord_user_id)),
            ).fetchall()
        return tuple(int(row["time_option_id"]) for row in rows)

    def replace_time_votes(
        self,
        *,
        event_id: int,
        discord_user_id: int,
        time_option_ids: Sequence[int],
    ) -> tuple[tuple[int, ...], int]:
        """Atomically replace one user's entire availability ballot."""

        normalized = tuple(dict.fromkeys(int(value) for value in time_option_ids))
        with self._transaction(immediate=True) as conn:
            event = self._open_event_row(
                conn,
                int(event_id),
                activity="Time voting",
            )
            if normalized:
                placeholders = ",".join("?" for _ in normalized)
                rows = conn.execute(
                    f"""
                    SELECT time_option_id FROM event_time_options
                    WHERE event_id = ? AND time_option_id IN ({placeholders})
                    """,
                    (int(event_id), *normalized),
                ).fetchall()
                found = {int(row["time_option_id"]) for row in rows}
                if found != set(normalized):
                    raise TimeOptionNotFoundError(
                        "Every selected time must be a candidate for this event."
                    )

            existing_rows = conn.execute(
                """
                SELECT time_option_id FROM event_time_votes
                WHERE event_id = ? AND discord_user_id = ?
                """,
                (int(event_id), int(discord_user_id)),
            ).fetchall()
            existing = {int(row["time_option_id"]) for row in existing_rows}
            requested = set(normalized)
            if existing != requested:
                conn.execute(
                    """
                    DELETE FROM event_time_votes
                    WHERE event_id = ? AND discord_user_id = ?
                    """,
                    (int(event_id), int(discord_user_id)),
                )
                conn.executemany(
                    """
                    INSERT INTO event_time_votes (
                        event_id, time_option_id, discord_user_id
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        (int(event_id), option_id, int(discord_user_id))
                        for option_id in normalized
                    ),
                )
                conn.execute(
                    """
                    UPDATE events
                    SET revision = revision + 1,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE event_id = ? AND revision = ?
                    """,
                    (int(event_id), int(event["revision"])),
                )
                revision = int(event["revision"]) + 1
            else:
                revision = int(event["revision"])

            rows = conn.execute(
                """
                SELECT time_option_id FROM event_time_votes
                WHERE event_id = ? AND discord_user_id = ?
                ORDER BY time_option_id
                """,
                (int(event_id), int(discord_user_id)),
            ).fetchall()
        return tuple(int(row["time_option_id"]) for row in rows), revision

    def add_nomination(
        self,
        *,
        event_id: int,
        media_type: str,
        tmdb_id: int,
        title: str,
        year: str,
        nominated_by_discord_id: int,
        jellyfin_item_id: str | None = None,
        poster_path: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        normalized_type = str(media_type).casefold()
        with self._transaction(immediate=True) as conn:
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
            if event is None:
                raise EventNotFoundError(f"Event #{int(event_id)} does not exist.")
            if event["status"] != "open":
                raise EventStateError("Nominations are closed for this event.")

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO event_nominations (
                        event_id, media_type, tmdb_id, jellyfin_item_id,
                        title, year, poster_path, nominated_by_discord_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(event_id),
                        normalized_type,
                        int(tmdb_id),
                        str(jellyfin_item_id) if jellyfin_item_id else None,
                        str(title).strip(),
                        str(year or "").strip(),
                        str(poster_path) if poster_path else None,
                        int(nominated_by_discord_id),
                    ),
                )
                nomination_id = int(cursor.lastrowid)
                created = True
                conn.execute(
                    """
                    UPDATE events
                    SET revision = revision + 1,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE event_id = ?
                    """,
                    (int(event_id),),
                )
            except sqlite3.IntegrityError as exc:
                row = conn.execute(
                    """
                    SELECT nomination_id
                    FROM event_nominations
                    WHERE event_id = ? AND media_type = ? AND tmdb_id = ?
                    LIMIT 1
                    """,
                    (int(event_id), normalized_type, int(tmdb_id)),
                ).fetchone()
                if row is None:
                    raise
                nomination_id = int(row["nomination_id"])
                created = False

            row = conn.execute(
                "SELECT * FROM event_nominations WHERE nomination_id = ?",
                (nomination_id,),
            ).fetchone()

        return dict(row), created

    def nomination_by_id(
        self,
        nomination_id: int,
        *,
        event_id: int | None = None,
    ) -> dict[str, Any] | None:
        event_clause = ""
        parameters: list[Any] = [int(nomination_id)]
        if event_id is not None:
            event_clause = "AND event_id = ?"
            parameters.append(int(event_id))
        with self._connection() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM event_nominations
                WHERE nomination_id = ? {event_clause}
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return dict(row) if row else None

    def list_nominations(
        self,
        event_id: int,
        *,
        include_withdrawn: bool = False,
    ) -> list[dict[str, Any]]:
        status_clause = "" if include_withdrawn else "AND status = 'active'"
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM event_nominations
                WHERE event_id = ? {status_clause}
                ORDER BY created_at, nomination_id
                """,
                (int(event_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def user_vote_ids(self, *, event_id: int, discord_user_id: int) -> tuple[int, ...]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT nomination_id FROM event_votes
                WHERE event_id = ? AND discord_user_id = ?
                ORDER BY nomination_id
                """,
                (int(event_id), int(discord_user_id)),
            ).fetchall()
        return tuple(int(row["nomination_id"]) for row in rows)

    def toggle_vote(
        self,
        *,
        event_id: int,
        nomination_id: int,
        discord_user_id: int,
    ) -> tuple[bool, tuple[int, ...], int]:
        """Toggle one vote and return ``(selected, choices, event_revision)``.

        A one-choice ballot moves the user's vote in one click.  Ballots with a
        larger limit require the user to deselect one of their current choices
        before adding another; silently deleting several choices would be
        surprising.
        """

        with self._transaction(immediate=True) as conn:
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
            if event is None:
                raise EventNotFoundError(f"Event #{int(event_id)} does not exist.")
            if event["status"] != "open":
                raise EventStateError("Voting is closed for this event.")

            nomination = conn.execute(
                """
                SELECT nomination_id FROM event_nominations
                WHERE nomination_id = ? AND event_id = ? AND status = 'active'
                """,
                (int(nomination_id), int(event_id)),
            ).fetchone()
            if nomination is None:
                raise NominationNotFoundError(
                    "That nomination is not active in this event."
                )

            existing = conn.execute(
                """
                SELECT 1 FROM event_votes
                WHERE event_id = ? AND nomination_id = ? AND discord_user_id = ?
                """,
                (int(event_id), int(nomination_id), int(discord_user_id)),
            ).fetchone()

            selected: bool
            if existing is not None:
                conn.execute(
                    """
                    DELETE FROM event_votes
                    WHERE event_id = ? AND nomination_id = ? AND discord_user_id = ?
                    """,
                    (int(event_id), int(nomination_id), int(discord_user_id)),
                )
                selected = False
            else:
                rows = conn.execute(
                    """
                    SELECT nomination_id FROM event_votes
                    WHERE event_id = ? AND discord_user_id = ?
                    """,
                    (int(event_id), int(discord_user_id)),
                ).fetchall()
                vote_limit = int(event["vote_limit"])
                if len(rows) >= vote_limit:
                    if vote_limit == 1:
                        conn.execute(
                            """
                            DELETE FROM event_votes
                            WHERE event_id = ? AND discord_user_id = ?
                            """,
                            (int(event_id), int(discord_user_id)),
                        )
                    else:
                        raise VoteLimitExceededError(vote_limit)

                conn.execute(
                    """
                    INSERT INTO event_votes (
                        event_id, nomination_id, discord_user_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (int(event_id), int(nomination_id), int(discord_user_id)),
                )
                selected = True

            conn.execute(
                """
                UPDATE events
                SET revision = revision + 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE event_id = ?
                """,
                (int(event_id),),
            )
            revision = int(event["revision"]) + 1
            rows = conn.execute(
                """
                SELECT nomination_id FROM event_votes
                WHERE event_id = ? AND discord_user_id = ?
                ORDER BY nomination_id
                """,
                (int(event_id), int(discord_user_id)),
            ).fetchall()

        return selected, tuple(int(row["nomination_id"]) for row in rows), revision

    def ranking_rows(self, event_id: int) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT n.*, COUNT(v.discord_user_id) AS vote_count
                FROM event_nominations AS n
                LEFT JOIN event_votes AS v
                  ON v.event_id = n.event_id
                 AND v.nomination_id = n.nomination_id
                WHERE n.event_id = ? AND n.status = 'active'
                GROUP BY n.nomination_id
                ORDER BY vote_count DESC, n.created_at ASC, n.nomination_id ASC
                """,
                (int(event_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def freeze_schedule(
        self,
        *,
        event_id: int,
        assignments: Sequence[tuple[str, int | None]],
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Atomically insert slots and freeze nominations/voting."""

        normalized = [
            (str(starts_at_utc), int(nomination_id) if nomination_id is not None else None)
            for starts_at_utc, nomination_id in assignments
        ]
        if not normalized:
            raise ValueError("Schedule at least one event slot.")
        if len({starts_at for starts_at, _ in normalized}) != len(normalized):
            raise EventConflictError("An event cannot have two slots at the same time.")

        with self._transaction(immediate=True) as conn:
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
            if event is None:
                raise EventNotFoundError(f"Event #{int(event_id)} does not exist.")
            if event["status"] != "open":
                raise EventStateError("Only an open event can be scheduled.")
            if expected_revision is not None and int(event["revision"]) != int(
                expected_revision
            ):
                raise StaleEventRevisionError(
                    "The ballot changed. Refresh the schedule preview and try again."
                )

            nomination_ids = {value for _, value in normalized if value is not None}
            if nomination_ids:
                placeholders = ",".join("?" for _ in nomination_ids)
                rows = conn.execute(
                    f"""
                    SELECT nomination_id FROM event_nominations
                    WHERE event_id = ? AND status = 'active'
                      AND nomination_id IN ({placeholders})
                    """,
                    (int(event_id), *sorted(nomination_ids)),
                ).fetchall()
                found = {int(row["nomination_id"]) for row in rows}
                if found != nomination_ids:
                    raise NominationNotFoundError(
                        "Every scheduled title must be active in this event."
                    )

            for starts_at, nomination_id in normalized:
                conn.execute(
                    """
                    INSERT INTO event_slots (
                        event_id, starts_at_utc, nomination_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (int(event_id), starts_at, nomination_id),
                )

            cursor = conn.execute(
                """
                UPDATE events
                SET status = 'scheduled',
                    scheduled_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    revision = revision + 1
                WHERE event_id = ? AND status = 'open' AND revision = ?
                """,
                (int(event_id), int(event["revision"])),
            )
            if cursor.rowcount != 1:
                raise StaleEventRevisionError(
                    "The event changed while it was being scheduled."
                )

            updated_event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
            slots = conn.execute(
                """
                SELECT * FROM event_slots
                WHERE event_id = ?
                ORDER BY starts_at_utc, slot_id
                """,
                (int(event_id),),
            ).fetchall()

        return dict(updated_event), [dict(row) for row in slots]

    @staticmethod
    def _validate_schedule_nominations(
        conn: sqlite3.Connection,
        *,
        event_id: int,
        assignments: Sequence[tuple[str, int | None]],
    ) -> None:
        nomination_ids = {
            nomination_id
            for _, nomination_id in assignments
            if nomination_id is not None
        }
        if not nomination_ids:
            return
        placeholders = ",".join("?" for _ in nomination_ids)
        rows = conn.execute(
            f"""
            SELECT nomination_id FROM event_nominations
            WHERE event_id = ? AND status = 'active'
              AND nomination_id IN ({placeholders})
            """,
            (int(event_id), *sorted(nomination_ids)),
        ).fetchall()
        found = {int(row["nomination_id"]) for row in rows}
        if found != nomination_ids:
            raise NominationNotFoundError(
                "Every scheduled title must be active in this event."
            )

    def reschedule_event(
        self,
        *,
        event_id: int,
        assignments: Sequence[tuple[str, int | None]],
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Replace a frozen schedule without losing native event identities.

        Slots are matched by their existing chronological position.  That lets
        the Discord adapter edit an existing native scheduled event instead of
        creating a duplicate.  Reminder state is retained only for a slot whose
        time and title are unchanged.
        """

        normalized = [
            (
                str(starts_at_utc),
                int(nomination_id) if nomination_id is not None else None,
            )
            for starts_at_utc, nomination_id in assignments
        ]
        if not normalized:
            raise ValueError("Schedule at least one event slot.")
        if len({starts_at for starts_at, _ in normalized}) != len(normalized):
            raise EventConflictError("An event cannot have two slots at the same time.")

        with self._transaction(immediate=True) as conn:
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
            if event is None:
                raise EventNotFoundError(f"Event #{int(event_id)} does not exist.")
            if event["status"] != "scheduled":
                raise EventStateError("Only a scheduled event can be rescheduled.")
            if expected_revision is not None and int(event["revision"]) != int(
                expected_revision
            ):
                raise StaleEventRevisionError(
                    "The schedule changed. Refresh it before choosing another time."
                )

            self._validate_schedule_nominations(
                conn,
                event_id=int(event_id),
                assignments=normalized,
            )
            existing = conn.execute(
                """
                SELECT * FROM event_slots
                WHERE event_id = ?
                ORDER BY starts_at_utc, slot_id
                """,
                (int(event_id),),
            ).fetchall()

            # Vacate every unique timestamp before putting slots in their new
            # order.  All placeholders live only inside this transaction.
            for slot in existing:
                conn.execute(
                    "UPDATE event_slots SET starts_at_utc = ? WHERE slot_id = ?",
                    (
                        f"~reschedule:{int(event_id)}:{int(slot['slot_id'])}",
                        int(slot["slot_id"]),
                    ),
                )

            shared_count = min(len(existing), len(normalized))
            for index in range(shared_count):
                slot = existing[index]
                starts_at, nomination_id = normalized[index]
                unchanged = (
                    str(slot["starts_at_utc"]) == starts_at
                    and (
                        int(slot["nomination_id"])
                        if slot["nomination_id"] is not None
                        else None
                    )
                    == nomination_id
                )
                conn.execute(
                    """
                    UPDATE event_slots
                    SET starts_at_utc = ?, nomination_id = ?, status = 'planned',
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE slot_id = ?
                    """,
                    (starts_at, nomination_id, int(slot["slot_id"])),
                )
                if not unchanged:
                    conn.execute(
                        "DELETE FROM event_reminder_state WHERE slot_id = ?",
                        (int(slot["slot_id"]),),
                    )

            for slot in existing[shared_count:]:
                conn.execute(
                    "DELETE FROM event_slots WHERE slot_id = ?",
                    (int(slot["slot_id"]),),
                )
            for starts_at, nomination_id in normalized[shared_count:]:
                conn.execute(
                    """
                    INSERT INTO event_slots (event_id, starts_at_utc, nomination_id)
                    VALUES (?, ?, ?)
                    """,
                    (int(event_id), starts_at, nomination_id),
                )

            cursor = conn.execute(
                """
                UPDATE events
                SET scheduled_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    revision = revision + 1
                WHERE event_id = ? AND status = 'scheduled' AND revision = ?
                """,
                (int(event_id), int(event["revision"])),
            )
            if cursor.rowcount != 1:
                raise StaleEventRevisionError(
                    "The schedule changed while it was being rescheduled."
                )
            updated = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
            slots = conn.execute(
                """
                SELECT * FROM event_slots
                WHERE event_id = ?
                ORDER BY starts_at_utc, slot_id
                """,
                (int(event_id),),
            ).fetchall()

        return dict(updated), [dict(row) for row in slots]

    def reopen_event(self, event_id: int) -> dict[str, Any]:
        """Return a scheduled event to voting and discard its frozen slots."""

        with self._transaction(immediate=True) as conn:
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
            if event is None:
                raise EventNotFoundError(f"Event #{int(event_id)} does not exist.")
            if event["status"] != "scheduled":
                raise EventStateError("Only a scheduled event can be reopened.")
            other = conn.execute(
                """
                SELECT event_id FROM events
                WHERE discord_guild_id = ? AND status = 'open' AND event_id != ?
                LIMIT 1
                """,
                (int(event["discord_guild_id"]), int(event_id)),
            ).fetchone()
            if other is not None:
                raise OpenEventExistsError(int(other["event_id"]))

            conn.execute("DELETE FROM event_slots WHERE event_id = ?", (int(event_id),))
            conn.execute(
                """
                UPDATE events
                SET status = 'open', scheduled_at = NULL,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    revision = revision + 1
                WHERE event_id = ?
                """,
                (int(event_id),),
            )
            updated = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
        return dict(updated)

    def _transition_event(
        self,
        *,
        event_id: int,
        target: str,
        allowed_sources: Iterable[str],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        allowed = tuple(allowed_sources)
        placeholders = ",".join("?" for _ in allowed)
        terminal_column = "completed_at" if target == "completed" else "cancelled_at"
        slot_status = "completed" if target == "completed" else "cancelled"

        with self._transaction(immediate=True) as conn:
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
            if event is None:
                raise EventNotFoundError(f"Event #{int(event_id)} does not exist.")
            if event["status"] not in allowed:
                raise EventStateError(
                    f"Event #{int(event_id)} cannot move from "
                    f"{event['status']} to {target}."
                )
            if expected_revision is not None and int(event["revision"]) != int(
                expected_revision
            ):
                raise StaleEventRevisionError(
                    "The event changed before its state transition could finish."
                )

            conn.execute(
                f"""
                UPDATE events
                SET status = ?,
                    {terminal_column} = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    revision = revision + 1
                WHERE event_id = ?
                """,
                (target, int(event_id)),
            )
            conn.execute(
                """
                UPDATE event_slots
                SET status = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE event_id = ? AND status = 'planned'
                """,
                (slot_status, int(event_id)),
            )
            updated = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()

        return dict(updated)

    def complete_event(
        self,
        event_id: int,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self._transition_event(
            event_id=int(event_id),
            target="completed",
            allowed_sources=("scheduled",),
            expected_revision=expected_revision,
        )

    def cancel_event(
        self,
        event_id: int,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self._transition_event(
            event_id=int(event_id),
            target="cancelled",
            allowed_sources=("open", "scheduled"),
            expected_revision=expected_revision,
        )

    def archive_event(self, event_id: int) -> dict[str, Any]:
        """Soft-hide a terminal event while retaining its audit history."""

        with self._transaction(immediate=True) as conn:
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
            if event is None:
                raise EventNotFoundError(f"Event #{int(event_id)} does not exist.")
            if event["status"] not in {"completed", "cancelled"}:
                raise EventStateError("Only a completed or cancelled event can be archived.")
            if event["archived_at"] is None:
                conn.execute(
                    """
                    UPDATE events
                    SET archived_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        revision = revision + 1
                    WHERE event_id = ?
                    """,
                    (int(event_id),),
                )
            updated = conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (int(event_id),),
            ).fetchone()
        return dict(updated)

    def clear_old_events(
        self,
        *,
        discord_guild_id: int,
        reference_utc: str,
    ) -> list[dict[str, Any]]:
        """Complete stale schedules and archive every terminal guild event."""

        with self._transaction(immediate=True) as conn:
            stale_rows = conn.execute(
                """
                SELECT e.event_id
                FROM events AS e
                JOIN event_slots AS s ON s.event_id = e.event_id
                WHERE e.discord_guild_id = ?
                  AND e.status = 'scheduled'
                  AND e.archived_at IS NULL
                GROUP BY e.event_id
                HAVING MAX(s.starts_at_utc) < ?
                """,
                (int(discord_guild_id), str(reference_utc)),
            ).fetchall()
            stale_ids = tuple(int(row["event_id"]) for row in stale_rows)
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                conn.execute(
                    f"""
                    UPDATE event_slots
                    SET status = 'completed',
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE event_id IN ({placeholders}) AND status = 'planned'
                    """,
                    stale_ids,
                )
                conn.execute(
                    f"""
                    UPDATE events
                    SET status = 'completed',
                        completed_at = COALESCE(
                            completed_at,
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ),
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        revision = revision + 1
                    WHERE event_id IN ({placeholders})
                    """,
                    stale_ids,
                )

            terminal_rows = conn.execute(
                """
                SELECT event_id FROM events
                WHERE discord_guild_id = ?
                  AND status IN ('completed', 'cancelled')
                  AND archived_at IS NULL
                ORDER BY created_at, event_id
                """,
                (int(discord_guild_id),),
            ).fetchall()
            terminal_ids = tuple(int(row["event_id"]) for row in terminal_rows)
            if terminal_ids:
                placeholders = ",".join("?" for _ in terminal_ids)
                conn.execute(
                    f"""
                    UPDATE events
                    SET archived_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        revision = revision + 1
                    WHERE event_id IN ({placeholders})
                    """,
                    terminal_ids,
                )
                archived = conn.execute(
                    f"""
                    SELECT * FROM events
                    WHERE event_id IN ({placeholders})
                    ORDER BY created_at, event_id
                    """,
                    terminal_ids,
                ).fetchall()
            else:
                archived = []
        return [dict(row) for row in archived]

    @staticmethod
    def _slot_query() -> str:
        return """
            SELECT
                s.slot_id,
                s.event_id,
                s.starts_at_utc,
                s.status AS slot_status,
                s.native_scheduled_event_id,
                e.discord_guild_id,
                e.discord_channel_id,
                e.dashboard_message_id,
                e.name AS event_name,
                e.status AS event_status,
                e.timezone_name,
                e.preset_key,
                n.nomination_id,
                n.media_type,
                n.tmdb_id,
                n.jellyfin_item_id,
                n.title,
                n.year,
                n.poster_path,
                COALESCE((
                    SELECT COUNT(*) FROM event_votes AS v
                    WHERE v.event_id = s.event_id
                      AND v.nomination_id = s.nomination_id
                ), 0) AS vote_count
            FROM event_slots AS s
            JOIN events AS e ON e.event_id = s.event_id
            LEFT JOIN event_nominations AS n
              ON n.event_id = s.event_id
             AND n.nomination_id = s.nomination_id
        """

    def slot_by_id(self, slot_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                self._slot_query() + " WHERE s.slot_id = ? LIMIT 1",
                (int(slot_id),),
            ).fetchone()
        return dict(row) if row else None

    def set_native_scheduled_event_id(
        self,
        *,
        slot_id: int,
        native_scheduled_event_id: int | None,
        expected_event_id: int | None = None,
        expected_event_revision: int | None = None,
        expected_starts_at_utc: str | None = None,
        expected_nomination_id: int | None = None,
        expected_native_scheduled_event_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist or clear the Discord scheduled-event ID for one slot."""

        with self._transaction(immediate=True) as conn:
            slot = conn.execute(
                """
                SELECT s.slot_id, s.event_id, s.starts_at_utc, s.nomination_id,
                       s.native_scheduled_event_id, s.status AS slot_status,
                       e.status AS event_status, e.revision AS event_revision
                FROM event_slots AS s
                JOIN events AS e ON e.event_id = s.event_id
                WHERE s.slot_id = ?
                """,
                (int(slot_id),),
            ).fetchone()
            if slot is None:
                raise EventNotFoundError(f"Event slot #{int(slot_id)} does not exist.")
            if slot["event_status"] != "scheduled" or slot["slot_status"] != "planned":
                raise EventStateError(
                    "Native scheduled-event metadata belongs to a planned event slot."
                )
            if expected_event_revision is not None:
                actual_nomination_id = (
                    int(slot["nomination_id"])
                    if slot["nomination_id"] is not None
                    else None
                )
                actual_native_id = (
                    int(slot["native_scheduled_event_id"])
                    if slot["native_scheduled_event_id"] is not None
                    else None
                )
                if (
                    int(slot["event_id"]) != int(expected_event_id)
                    or int(slot["event_revision"]) != int(expected_event_revision)
                    or str(slot["starts_at_utc"]) != str(expected_starts_at_utc)
                    or actual_nomination_id != expected_nomination_id
                    or actual_native_id != expected_native_scheduled_event_id
                ):
                    raise StaleEventRevisionError(
                        "The event slot changed while its Discord event was being synchronized."
                    )
            conn.execute(
                """
                UPDATE event_slots
                SET native_scheduled_event_id = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE slot_id = ?
                """,
                (
                    int(native_scheduled_event_id)
                    if native_scheduled_event_id is not None
                    else None,
                    int(slot_id),
                ),
            )
            row = conn.execute(
                self._slot_query() + " WHERE s.slot_id = ? LIMIT 1",
                (int(slot_id),),
            ).fetchone()
        return dict(row)

    def reminder_rows(self) -> list[dict[str, Any]]:
        """Return active slots plus their durable delivered-stage flags."""

        with self._connection() as conn:
            rows = conn.execute(
                self._slot_query()
                + """
                  WHERE e.status = 'scheduled'
                    AND e.archived_at IS NULL
                    AND s.status = 'planned'
                  ORDER BY s.starts_at_utc, s.slot_id
                """
            ).fetchall()
            results = []
            for row in rows:
                value = dict(row)
                stages = conn.execute(
                    """
                    SELECT stage FROM event_reminder_state
                    WHERE slot_id = ?
                    """,
                    (int(row["slot_id"]),),
                ).fetchall()
                value["advanced_stages"] = tuple(str(stage["stage"]) for stage in stages)
                results.append(value)
        return results

    def advance_reminder(self, *, slot_id: int, stage: str) -> tuple[str, ...]:
        """Mark a reminder and every earlier stage delivered, idempotently."""

        stages = ("24h", "1h", "start")
        try:
            through = stages.index(str(stage))
        except ValueError as exc:
            raise ValueError("Reminder stage must be 24h, 1h, or start.") from exc

        with self._transaction(immediate=True) as conn:
            slot = conn.execute(
                """
                SELECT s.slot_id, s.event_id, s.status AS slot_status,
                       e.status AS event_status, e.archived_at
                FROM event_slots AS s
                JOIN events AS e ON e.event_id = s.event_id
                WHERE s.slot_id = ?
                """,
                (int(slot_id),),
            ).fetchone()
            if slot is None:
                raise EventNotFoundError(f"Event slot #{int(slot_id)} does not exist.")
            if (
                slot["event_status"] != "scheduled"
                or slot["slot_status"] != "planned"
                or slot["archived_at"] is not None
            ):
                raise EventStateError("Reminders are closed for this event slot.")
            conn.executemany(
                """
                INSERT OR IGNORE INTO event_reminder_state (slot_id, event_id, stage)
                VALUES (?, ?, ?)
                """,
                (
                    (int(slot_id), int(slot["event_id"]), value)
                    for value in stages[: through + 1]
                ),
            )
            rows = conn.execute(
                """
                SELECT stage FROM event_reminder_state
                WHERE slot_id = ?
                """,
                (int(slot_id),),
            ).fetchall()
        present = {str(row["stage"]) for row in rows}
        return tuple(value for value in stages if value in present)

    def claim_reminder(
        self,
        *,
        slot_id: int,
        event_id: int,
        starts_at_utc: str,
        stage: str,
    ) -> bool:
        """Atomically claim one reminder stage before an external delivery attempt."""

        stages = ("24h", "1h", "start")
        try:
            through = stages.index(str(stage))
        except ValueError as exc:
            raise ValueError("Reminder stage must be 24h, 1h, or start.") from exc

        with self._transaction(immediate=True) as conn:
            slot = conn.execute(
                """
                SELECT s.slot_id, s.event_id, s.status AS slot_status,
                       e.status AS event_status, e.archived_at
                FROM event_slots AS s
                JOIN events AS e ON e.event_id = s.event_id
                WHERE s.slot_id = ?
                  AND s.event_id = ?
                  AND s.starts_at_utc = ?
                """,
                (int(slot_id), int(event_id), str(starts_at_utc)),
            ).fetchone()
            if slot is None:
                return False
            if (
                slot["event_status"] != "scheduled"
                or slot["slot_status"] != "planned"
                or slot["archived_at"] is not None
            ):
                return False

            if through:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO event_reminder_state (slot_id, event_id, stage)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (int(slot_id), int(slot["event_id"]), value)
                        for value in stages[:through]
                    ),
                )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO event_reminder_state (slot_id, event_id, stage)
                VALUES (?, ?, ?)
                """,
                (int(slot_id), int(slot["event_id"]), str(stage)),
            )
            return int(cursor.rowcount) == 1

    def slots_for_event(self, event_id: int) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                self._slot_query()
                + " WHERE s.event_id = ? ORDER BY s.starts_at_utc, s.slot_id",
                (int(event_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def slots_between(
        self,
        *,
        discord_guild_id: int,
        start_utc: str,
        end_utc: str,
    ) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                self._slot_query()
                + """
                  WHERE e.discord_guild_id = ?
                    AND e.status = 'scheduled'
                    AND s.status = 'planned'
                    AND s.starts_at_utc >= ?
                    AND s.starts_at_utc < ?
                  ORDER BY s.starts_at_utc, s.slot_id
                """,
                (int(discord_guild_id), str(start_utc), str(end_utc)),
            ).fetchall()
        return [dict(row) for row in rows]
