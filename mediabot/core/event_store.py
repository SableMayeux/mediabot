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


EVENT_SCHEMA_VERSION = 1


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
        parameters.append(max(1, min(int(limit), 500)))

        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM events
                WHERE discord_guild_id = ? {status_clause}
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

    def _transition_event(
        self,
        *,
        event_id: int,
        target: str,
        allowed_sources: Iterable[str],
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

    def complete_event(self, event_id: int) -> dict[str, Any]:
        return self._transition_event(
            event_id=int(event_id),
            target="completed",
            allowed_sources=("scheduled",),
        )

    def cancel_event(self, event_id: int) -> dict[str, Any]:
        return self._transition_event(
            event_id=int(event_id),
            target="cancelled",
            allowed_sources=("open", "scheduled"),
        )

    @staticmethod
    def _slot_query() -> str:
        return """
            SELECT
                s.slot_id,
                s.event_id,
                s.starts_at_utc,
                s.status AS slot_status,
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
