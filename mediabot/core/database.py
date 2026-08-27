import json
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any


DB_PATH = os.environ.get(
    "DB_PATH",
    "/app/data/mediabot.db"
)


@contextmanager
def connection():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=10.0,
    )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_tracking_db():

    with connection() as conn:

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS request_messages (
                seerr_request_id INTEGER PRIMARY KEY,

                media_type TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,

                title TEXT NOT NULL,
                year TEXT,

                requester_discord_id INTEGER,

                discord_guild_id INTEGER,
                discord_channel_id INTEGER NOT NULL,
                discord_message_id INTEGER NOT NULL,

                request_status TEXT,

                requested_seasons TEXT NOT NULL DEFAULT '',
                requested_episode_counts TEXT NOT NULL DEFAULT '{}',
                requested_episode_numbers TEXT NOT NULL DEFAULT '{}',

                jellyfin_item_id TEXT,
                jellyfin_available INTEGER NOT NULL DEFAULT 0,

                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                available_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_ratings (
                discord_user_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,
                jellyfin_item_id TEXT,
                title TEXT NOT NULL,
                year TEXT,
                genres TEXT NOT NULL DEFAULT '',
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 10),
                trakt_synced INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (discord_user_id, media_type, tmdb_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS music_requests (
                local_request_id TEXT PRIMARY KEY,
                soulsync_request_id TEXT,
                normalized_query TEXT NOT NULL,
                display_query TEXT NOT NULL,
                requester_discord_id INTEGER,
                discord_guild_id INTEGER,
                discord_channel_id INTEGER,
                discord_message_id INTEGER,
                request_status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_music_requests_query
            ON music_requests (normalized_query, updated_at DESC)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_media_ratings_user
            ON media_ratings (discord_user_id, rating DESC, updated_at DESC)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_reports (
                report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_key TEXT NOT NULL,
                jellyfin_item_id TEXT NOT NULL,
                jellyfin_series_id TEXT,
                media_type TEXT NOT NULL CHECK (
                    media_type IN ('movie', 'series', 'episode')
                ),
                title TEXT NOT NULL,
                year TEXT NOT NULL DEFAULT '',
                season_number INTEGER,
                episode_number INTEGER,
                episode_title TEXT,
                category TEXT NOT NULL CHECK (
                    category IN (
                        'wont_play', 'wrong_audio', 'bad_subtitles',
                        'bad_quality', 'wrong_episode', 'other'
                    )
                ),
                details TEXT NOT NULL DEFAULT '',
                reporter_discord_id INTEGER NOT NULL,
                discord_guild_id INTEGER NOT NULL,
                discord_channel_id INTEGER NOT NULL,
                discord_message_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (
                    status IN ('open', 'in_progress', 'resolved', 'dismissed')
                ),
                handler_discord_id INTEGER,
                resolution_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                acknowledged_at TEXT,
                resolved_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_media_reports_active_reporter
            ON media_reports (
                discord_guild_id,
                reporter_discord_id,
                target_key,
                category
            )
            WHERE status IN ('open', 'in_progress')
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_media_reports_queue
            ON media_reports (discord_guild_id, status, created_at, report_id)
            """
        )

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(media_ratings)")
        }

        if "genres" not in columns:
            conn.execute(
                "ALTER TABLE media_ratings "
                "ADD COLUMN genres TEXT NOT NULL DEFAULT ''"
            )

        request_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(request_messages)")
        }

        if "requested_seasons" not in request_columns:
            conn.execute(
                "ALTER TABLE request_messages "
                "ADD COLUMN requested_seasons TEXT NOT NULL DEFAULT ''"
            )

        if "requested_episode_counts" not in request_columns:
            conn.execute(
                "ALTER TABLE request_messages "
                "ADD COLUMN requested_episode_counts TEXT NOT NULL DEFAULT '{}'"
            )

        if "requested_episode_numbers" not in request_columns:
            conn.execute(
                "ALTER TABLE request_messages "
                "ADD COLUMN requested_episode_numbers TEXT NOT NULL DEFAULT '{}'"
            )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_request_messages_pending
            ON request_messages (
                jellyfin_available,
                media_type,
                tmdb_id
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_request_intents (
                intent_id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (
                    state IN ('prepared', 'accepted', 'tracked', 'failed')
                ),
                media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
                tmdb_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                year TEXT NOT NULL DEFAULT '',
                requester_discord_id INTEGER NOT NULL,
                discord_guild_id INTEGER NOT NULL,
                discord_channel_id INTEGER NOT NULL,
                discord_message_id INTEGER NOT NULL,
                direct_tracking_id INTEGER NOT NULL,
                requested_seasons TEXT NOT NULL DEFAULT '[]',
                requested_episode_counts TEXT NOT NULL DEFAULT '{}',
                requested_episode_numbers TEXT NOT NULL DEFAULT '{}',
                seerr_request_id INTEGER,
                accepted_seasons TEXT NOT NULL DEFAULT '[]',
                accepted_episode_counts TEXT NOT NULL DEFAULT '{}',
                accepted_episode_numbers TEXT NOT NULL DEFAULT '{}',
                request_status TEXT NOT NULL DEFAULT 'Preparing request',
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                tracked_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_media_request_intents_recovery
            ON media_request_intents (state, updated_at, intent_id)
            """
        )


def _positive_ints(values):
    return sorted({int(value) for value in values or () if int(value) > 0})


def _episode_map(values):
    return {
        str(int(season)): _positive_ints(episodes)
        for season, episodes in (values or {}).items()
        if int(season) > 0
    }


def _episode_count_map(values):
    return {
        str(int(season)): max(0, int(count))
        for season, count in (values or {}).items()
        if int(season) > 0
    }


def begin_media_request_intent(
    *,
    intent_id: str,
    media_type: str,
    tmdb_id: int,
    title: str,
    year: str | None,
    requester_discord_id: int,
    discord_guild_id: int,
    discord_channel_id: int,
    discord_message_id: int,
    direct_tracking_id: int,
    requested_seasons=(),
    requested_episode_counts=None,
    requested_episode_numbers=None,
) -> None:
    """Persist submission intent before any provider side effect occurs."""

    normalized_type = str(media_type).casefold()
    if normalized_type not in {"movie", "tv"}:
        raise ValueError("Media request type must be movie or tv.")
    if int(direct_tracking_id) >= 0:
        raise ValueError("Direct tracking IDs must be negative.")

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO media_request_intents (
                intent_id, state, media_type, tmdb_id, title, year,
                requester_discord_id, discord_guild_id, discord_channel_id,
                discord_message_id, direct_tracking_id, requested_seasons,
                requested_episode_counts, requested_episode_numbers
            )
            VALUES (?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(intent_id),
                normalized_type,
                int(tmdb_id),
                str(title),
                str(year or ""),
                int(requester_discord_id),
                int(discord_guild_id),
                int(discord_channel_id),
                int(discord_message_id),
                int(direct_tracking_id),
                json.dumps(_positive_ints(requested_seasons)),
                json.dumps(
                    _episode_count_map(requested_episode_counts), sort_keys=True
                ),
                json.dumps(_episode_map(requested_episode_numbers), sort_keys=True),
            ),
        )


def record_media_request_acceptance(
    *,
    intent_id: str,
    seerr_request_id: int | None,
    accepted_seasons=(),
    accepted_episode_counts=None,
    accepted_episode_numbers=None,
    request_status: str,
) -> None:
    """Durably record provider acceptance before editing Discord or tracking."""

    with connection() as conn:
        cursor = conn.execute(
            """
            UPDATE media_request_intents
            SET state = 'accepted',
                seerr_request_id = COALESCE(?, seerr_request_id),
                accepted_seasons = ?,
                accepted_episode_counts = ?,
                accepted_episode_numbers = ?,
                request_status = ?,
                error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE intent_id = ? AND state IN ('prepared', 'accepted')
            """,
            (
                int(seerr_request_id) if seerr_request_id is not None else None,
                json.dumps(_positive_ints(accepted_seasons)),
                json.dumps(
                    _episode_count_map(accepted_episode_counts), sort_keys=True
                ),
                json.dumps(_episode_map(accepted_episode_numbers), sort_keys=True),
                str(request_status),
                str(intent_id),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Request intent {intent_id!r} is not recoverable.")


def fail_media_request_intent(*, intent_id: str, error: str) -> None:
    """Close a prepared intent only when no provider accepted an action."""

    with connection() as conn:
        conn.execute(
            """
            UPDATE media_request_intents
            SET state = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE intent_id = ? AND state = 'prepared'
            """,
            (str(error)[:500], str(intent_id)),
        )


def mark_media_request_intent_tracked(intent_id: str) -> None:
    with connection() as conn:
        cursor = conn.execute(
            """
            UPDATE media_request_intents
            SET state = 'tracked', tracked_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE intent_id = ? AND state = 'accepted'
            """,
            (str(intent_id),),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Request intent {intent_id!r} was not accepted.")


def recoverable_media_request_intents(limit: int = 100) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM media_request_intents
            WHERE state = 'accepted'
            ORDER BY updated_at ASC, intent_id ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def media_request_intent_stats(
    *,
    discord_guild_id: int | None = None,
) -> dict[str, int]:
    guild_clause = ""
    parameters: tuple[Any, ...] = ()
    if discord_guild_id is not None:
        guild_clause = "WHERE discord_guild_id = ?"
        parameters = (int(discord_guild_id),)
    with connection() as conn:
        rows = conn.execute(
            f"""
            SELECT state, COUNT(*) AS count
            FROM media_request_intents
            {guild_clause}
            GROUP BY state
            """,
            parameters,
        ).fetchall()
    counts = {str(row["state"]): int(row["count"]) for row in rows}
    return {
        "prepared": counts.get("prepared", 0),
        "accepted": counts.get("accepted", 0),
        "tracked": counts.get("tracked", 0),
        "failed": counts.get("failed", 0),
    }


def track_request(
    *,
    seerr_request_id,
    media_type,
    tmdb_id,
    title,
    year,
    requester_discord_id,
    discord_guild_id,
    discord_channel_id,
    discord_message_id,
    request_status,
    requested_seasons=(),
    requested_episode_counts=None,
    requested_episode_numbers=None,
):

    normalized_seasons = sorted({
        int(season)
        for season in requested_seasons or ()
        if int(season) > 0
    })
    normalized_counts = {
        str(int(season)): max(0, int(count))
        for season, count in (requested_episode_counts or {}).items()
        if int(season) > 0
    }
    normalized_numbers = {
        str(int(season)): sorted({
            int(episode)
            for episode in episodes
            if int(episode) > 0
        })
        for season, episodes in (requested_episode_numbers or {}).items()
        if int(season) > 0
    }

    with connection() as conn:

        conn.execute(
            """
            INSERT INTO request_messages (
                seerr_request_id,
                media_type,
                tmdb_id,
                title,
                year,
                requester_discord_id,
                discord_guild_id,
                discord_channel_id,
                discord_message_id,
                request_status,
                requested_seasons,
                requested_episode_counts,
                requested_episode_numbers
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(seerr_request_id)
            DO UPDATE SET
                media_type = excluded.media_type,
                tmdb_id = excluded.tmdb_id,
                title = excluded.title,
                year = excluded.year,
                requester_discord_id = excluded.requester_discord_id,
                discord_guild_id = excluded.discord_guild_id,
                discord_channel_id = excluded.discord_channel_id,
                discord_message_id = excluded.discord_message_id,
                request_status = excluded.request_status,
                requested_seasons = excluded.requested_seasons,
                requested_episode_counts = excluded.requested_episode_counts,
                requested_episode_numbers = excluded.requested_episode_numbers,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(seerr_request_id),
                str(media_type),
                int(tmdb_id),
                str(title),
                str(year or ""),
                int(requester_discord_id)
                    if requester_discord_id
                    else None,
                int(discord_guild_id)
                    if discord_guild_id
                    else None,
                int(discord_channel_id),
                int(discord_message_id),
                str(request_status),
                ",".join(str(season) for season in normalized_seasons),
                json.dumps(normalized_counts, sort_keys=True),
                json.dumps(normalized_numbers, sort_keys=True),
            )
        )


def normalize_music_query(value: str) -> str:
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split()
    )


def begin_music_request(
    *,
    local_request_id: str,
    display_query: str,
    requester_discord_id: int,
    discord_guild_id: int | None,
    discord_channel_id: int | None,
    discord_message_id: int | None,
) -> None:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO music_requests (
                local_request_id,
                normalized_query,
                display_query,
                requester_discord_id,
                discord_guild_id,
                discord_channel_id,
                discord_message_id,
                request_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'submitting')
            """,
            (
                str(local_request_id),
                normalize_music_query(display_query),
                str(display_query),
                int(requester_discord_id),
                int(discord_guild_id) if discord_guild_id else None,
                int(discord_channel_id) if discord_channel_id else None,
                int(discord_message_id) if discord_message_id else None,
            ),
        )


def update_music_request(
    *,
    local_request_id: str,
    request_status: str,
    soulsync_request_id: str | None = None,
    error: str | None = None,
) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE music_requests
            SET soulsync_request_id = COALESCE(?, soulsync_request_id),
                request_status = ?,
                error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE local_request_id = ?
            """,
            (
                str(soulsync_request_id) if soulsync_request_id else None,
                str(request_status),
                str(error)[:500] if error else None,
                str(local_request_id),
            ),
        )


def recent_music_request(
    display_query: str,
    *,
    max_age_minutes: int = 15,
    discord_guild_id: int | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_music_query(display_query)
    age = max(1, int(max_age_minutes))
    guild_clause = ""
    parameters: list[Any] = [normalized, f"-{age} minutes"]
    if discord_guild_id is not None:
        guild_clause = "AND discord_guild_id = ?"
        parameters.append(int(discord_guild_id))

    with connection() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM music_requests
            WHERE normalized_query = ?
              AND created_at >= datetime('now', ?)
              {guild_clause}
              AND request_status IN (
                  'submitting', 'ambiguous', 'queued', 'searching',
                  'downloading', 'downloaded', 'completed'
              )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()

    return dict(row) if row else None


def latest_music_request(
    query: str,
    *,
    requester_discord_id: int | None = None,
    discord_guild_id: int | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_music_query(query)
    contains = f"%{normalized}%"
    parameters: list[Any] = [normalized, contains, contains]
    user_clause = ""

    if requester_discord_id is not None:
        user_clause = "AND requester_discord_id = ?"
        parameters.append(int(requester_discord_id))

    guild_clause = ""
    if discord_guild_id is not None:
        guild_clause = "AND discord_guild_id = ?"
        parameters.append(int(discord_guild_id))

    with connection() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM music_requests
            WHERE (
                normalized_query = ?
                OR normalized_query LIKE ?
                OR ? LIKE '%' || normalized_query || '%'
            )
            {user_clause}
            {guild_clause}
            ORDER BY
                CASE WHEN normalized_query = ? THEN 0 ELSE 1 END,
                updated_at DESC
            LIMIT 1
            """,
            (*parameters, normalized),
        ).fetchone()

    return dict(row) if row else None


def pending_requests(
    limit=100
):

    with connection() as conn:

        return conn.execute(
            """
            SELECT *
            FROM request_messages
            WHERE jellyfin_available = 0
              AND lower(request_status) NOT IN ('declined', 'failed')
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (
                int(limit),
            )
        ).fetchall()


def request_by_id(
    seerr_request_id: int,
    *,
    discord_guild_id: int | None = None,
) -> dict[str, Any] | None:
    """Return one locally tracked Seerr request, including completed rows."""
    guild_clause = "AND discord_guild_id = ?" if discord_guild_id is not None else ""
    parameters = [int(seerr_request_id)]
    if discord_guild_id is not None:
        parameters.append(int(discord_guild_id))
    with connection() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM request_messages
            WHERE seerr_request_id = ?
            {guild_clause}
            LIMIT 1
            """,
            parameters,
        ).fetchone()

    return dict(row) if row else None


def update_tracked_request_status(
    *,
    seerr_request_id: int,
    request_status: str,
) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE request_messages
            SET request_status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE seerr_request_id = ?
            """,
            (str(request_status), int(seerr_request_id)),
        )


def latest_media_request(
    query: str,
    *,
    requester_discord_id: int | None = None,
    discord_guild_id: int | None = None,
) -> dict[str, Any] | None:
    """Find the newest tracked video request by a forgiving title match."""
    normalized = normalize_music_query(query)
    if not normalized:
        return None

    contains = f"%{normalized}%"
    parameters: list[Any] = [normalized, contains, contains]
    user_clause = ""

    if requester_discord_id is not None:
        user_clause = "AND requester_discord_id = ?"
        parameters.append(int(requester_discord_id))
    guild_clause = ""
    if discord_guild_id is not None:
        guild_clause = "AND discord_guild_id = ?"
        parameters.append(int(discord_guild_id))

    with connection() as conn:
        row = conn.execute(
            f"""
            SELECT *,
                   lower(trim(replace(replace(replace(title, '-', ' '), ':', ' '),
                                      '  ', ' '))) AS searchable_title
            FROM request_messages
            WHERE (
                lower(trim(replace(replace(replace(title, '-', ' '), ':', ' '),
                                   '  ', ' '))) = ?
                OR lower(trim(replace(replace(replace(title, '-', ' '), ':', ' '),
                                      '  ', ' '))) LIKE ?
                OR ? LIKE '%' || lower(trim(replace(replace(replace(
                                      title, '-', ' '), ':', ' '), '  ', ' '))) || '%'
            )
            {user_clause}
            {guild_clause}
            ORDER BY
                CASE WHEN lower(trim(replace(replace(replace(
                     title, '-', ' '), ':', ' '), '  ', ' '))) = ? THEN 0 ELSE 1 END,
                updated_at DESC
            LIMIT 1
            """,
            (*parameters, normalized),
        ).fetchone()

    return dict(row) if row else None


def mark_available(
    *,
    seerr_request_id,
    jellyfin_item_id
):

    with connection() as conn:

        conn.execute(
            """
            UPDATE request_messages

            SET
                jellyfin_item_id = ?,
                jellyfin_available = 1,
                available_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP

            WHERE seerr_request_id = ?
            """,
            (
                str(jellyfin_item_id),
                int(seerr_request_id),
            )
        )


def tracking_stats(*, discord_guild_id: int | None = None):

    guild_clause = ""
    parameters: tuple[Any, ...] = ()
    if discord_guild_id is not None:
        guild_clause = "WHERE discord_guild_id = ?"
        parameters = (int(discord_guild_id),)

    with connection() as conn:

        total = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM request_messages
            {guild_clause}
            """,
            parameters,
        ).fetchone()[0]

        pending_prefix = "AND" if guild_clause else "WHERE"
        pending = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM request_messages
            {guild_clause}
              {pending_prefix} jellyfin_available = 0
              AND lower(request_status) NOT IN ('declined', 'failed')
            """,
            parameters,
        ).fetchone()[0]

        available_prefix = "AND" if guild_clause else "WHERE"
        available = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM request_messages
            {guild_clause}
              {available_prefix} jellyfin_available = 1
            """,
            parameters,
        ).fetchone()[0]

    return {
        "total": total,
        "pending": pending,
        "available": available,
    }


def save_rating(
    *,
    discord_user_id: int,
    media_type: str,
    tmdb_id: int,
    title: str,
    year: str | None,
    rating: int,
    genres: list[str] | tuple[str, ...] = (),
    jellyfin_item_id: str | None = None,
    trakt_synced: bool = False,
) -> None:
    normalized_type = str(media_type).casefold()

    if normalized_type not in ("movie", "tv"):
        raise ValueError("Rating media type must be movie or tv.")

    numeric_rating = int(rating)

    if not 1 <= numeric_rating <= 10:
        raise ValueError("Rating must be between 1 and 10.")

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO media_ratings (
                discord_user_id,
                media_type,
                tmdb_id,
                jellyfin_item_id,
                title,
                year,
                genres,
                rating,
                trakt_synced
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_user_id, media_type, tmdb_id)
            DO UPDATE SET
                jellyfin_item_id = excluded.jellyfin_item_id,
                title = excluded.title,
                year = excluded.year,
                genres = excluded.genres,
                rating = excluded.rating,
                trakt_synced = excluded.trakt_synced,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(discord_user_id),
                normalized_type,
                int(tmdb_id),
                str(jellyfin_item_id) if jellyfin_item_id else None,
                str(title),
                str(year or ""),
                ",".join(
                    str(genre).strip()
                    for genre in genres
                    if str(genre).strip()
                ),
                numeric_rating,
                1 if trakt_synced else 0,
            ),
        )


def ratings_for_user(discord_user_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM media_ratings
            WHERE discord_user_id = ?
            ORDER BY updated_at DESC
            """,
            (int(discord_user_id),),
        ).fetchall()

    return [dict(row) for row in rows]


def rating_for_media(
    *,
    discord_user_id: int,
    media_type: str,
    tmdb_id: int,
) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM media_ratings
            WHERE discord_user_id = ?
              AND media_type = ?
              AND tmdb_id = ?
            """,
            (
                int(discord_user_id),
                str(media_type).casefold(),
                int(tmdb_id),
            ),
        ).fetchone()

    return dict(row) if row else None


def delete_rating(
    *,
    discord_user_id: int,
    media_type: str,
    tmdb_id: int,
) -> bool:
    with connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM media_ratings
            WHERE discord_user_id = ?
              AND media_type = ?
              AND tmdb_id = ?
            """,
            (
                int(discord_user_id),
                str(media_type).casefold(),
                int(tmdb_id),
            ),
        )

    return cursor.rowcount > 0


REPORT_STATUSES = {
    "open",
    "in_progress",
    "resolved",
    "dismissed",
}


def create_media_report(
    *,
    target_key: str,
    jellyfin_item_id: str,
    jellyfin_series_id: str | None,
    media_type: str,
    title: str,
    year: str | None,
    season_number: int | None,
    episode_number: int | None,
    episode_title: str | None,
    category: str,
    details: str,
    reporter_discord_id: int,
    discord_guild_id: int,
    discord_channel_id: int,
    discord_message_id: int,
) -> tuple[dict[str, Any], bool]:
    """Create one report or return the reporter's matching active ticket."""
    normalized_type = str(media_type).casefold()
    normalized_category = str(category).casefold()
    if normalized_type not in {"movie", "series", "episode"}:
        raise ValueError("Report media type must be movie, series, or episode.")
    if normalized_category not in {
        "wont_play",
        "wrong_audio",
        "bad_subtitles",
        "bad_quality",
        "wrong_episode",
        "other",
    }:
        raise ValueError("Unknown report category.")

    values = (
        str(target_key),
        str(jellyfin_item_id),
        str(jellyfin_series_id) if jellyfin_series_id else None,
        normalized_type,
        str(title),
        str(year or ""),
        int(season_number) if season_number is not None else None,
        int(episode_number) if episode_number is not None else None,
        str(episode_title) if episode_title else None,
        normalized_category,
        str(details or "").strip()[:1000],
        int(reporter_discord_id),
        int(discord_guild_id),
        int(discord_channel_id),
        int(discord_message_id),
    )

    with connection() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO media_reports (
                    target_key,
                    jellyfin_item_id,
                    jellyfin_series_id,
                    media_type,
                    title,
                    year,
                    season_number,
                    episode_number,
                    episode_title,
                    category,
                    details,
                    reporter_discord_id,
                    discord_guild_id,
                    discord_channel_id,
                    discord_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            report_id = int(cursor.lastrowid)
            created = True
        except sqlite3.IntegrityError:
            row = conn.execute(
                """
                SELECT report_id
                FROM media_reports
                WHERE discord_guild_id = ?
                  AND reporter_discord_id = ?
                  AND target_key = ?
                  AND category = ?
                  AND status IN ('open', 'in_progress')
                ORDER BY report_id DESC
                LIMIT 1
                """,
                (
                    int(discord_guild_id),
                    int(reporter_discord_id),
                    str(target_key),
                    normalized_category,
                ),
            ).fetchone()
            if row is None:
                raise
            report_id = int(row["report_id"])
            created = False

        row = conn.execute(
            "SELECT * FROM media_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()

    return dict(row), created


def media_report_by_id(
    report_id: int,
    *,
    discord_guild_id: int | None = None,
) -> dict[str, Any] | None:
    parameters: list[Any] = [int(report_id)]
    guild_clause = ""
    if discord_guild_id is not None:
        guild_clause = "AND discord_guild_id = ?"
        parameters.append(int(discord_guild_id))

    with connection() as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM media_reports
            WHERE report_id = ?
            {guild_clause}
            LIMIT 1
            """,
            parameters,
        ).fetchone()

    return dict(row) if row else None


def list_media_reports(
    *,
    discord_guild_id: int,
    statuses: tuple[str, ...] = ("open", "in_progress"),
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    normalized = tuple(dict.fromkeys(str(value).casefold() for value in statuses))
    if not normalized or any(value not in REPORT_STATUSES for value in normalized):
        raise ValueError("Unknown report queue status.")
    placeholders = ",".join("?" for _ in normalized)
    parameters: list[Any] = [int(discord_guild_id), *normalized]
    parameters.extend((max(1, min(int(limit), 500)), max(0, int(offset))))

    with connection() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM media_reports
            WHERE discord_guild_id = ?
              AND status IN ({placeholders})
            ORDER BY
                CASE status WHEN 'open' THEN 0 ELSE 1 END,
                created_at ASC,
                report_id ASC
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()

    return [dict(row) for row in rows]


def update_media_report_status(
    *,
    report_id: int,
    discord_guild_id: int,
    status: str,
    handler_discord_id: int,
    resolution_note: str = "",
) -> dict[str, Any] | None:
    normalized_status = str(status).casefold()
    if normalized_status not in {"in_progress", "resolved", "dismissed"}:
        raise ValueError("Report status must be in_progress, resolved, or dismissed.")
    note = str(resolution_note or "").strip()[:1000]
    # Claim is an ownership transition, not an idempotent label update.  Only
    # an open report may be claimed; otherwise a stale queue or a second admin
    # could silently replace the first handler.  Closing transitions remain
    # valid from either active state.
    source_statuses = (
        ("open",)
        if normalized_status == "in_progress"
        else ("open", "in_progress")
    )
    source_placeholders = ",".join("?" for _ in source_statuses)

    with connection() as conn:
        cursor = conn.execute(
            f"""
            UPDATE media_reports
            SET status = ?,
                handler_discord_id = ?,
                resolution_note = ?,
                acknowledged_at = CASE
                    WHEN ? = 'in_progress'
                    THEN COALESCE(acknowledged_at, CURRENT_TIMESTAMP)
                    ELSE acknowledged_at
                END,
                resolved_at = CASE
                    WHEN ? IN ('resolved', 'dismissed')
                    THEN CURRENT_TIMESTAMP
                    ELSE NULL
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE report_id = ?
              AND discord_guild_id = ?
              AND status IN ({source_placeholders})
            """,
            (
                normalized_status,
                int(handler_discord_id),
                note,
                normalized_status,
                normalized_status,
                int(report_id),
                int(discord_guild_id),
                *source_statuses,
            ),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM media_reports WHERE report_id = ?",
            (int(report_id),),
        ).fetchone()

    return dict(row) if row else None


def media_report_stats(discord_guild_id: int | None = None) -> dict[str, int]:
    guild_clause = ""
    parameters: tuple[Any, ...] = ()
    if discord_guild_id is not None:
        guild_clause = "WHERE discord_guild_id = ?"
        parameters = (int(discord_guild_id),)

    with connection() as conn:
        rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS count
            FROM media_reports
            {guild_clause}
            GROUP BY status
            """,
            parameters,
        ).fetchall()

    result = {status: 0 for status in REPORT_STATUSES}
    for row in rows:
        result[str(row["status"])] = int(row["count"])
    result["active"] = result["open"] + result["in_progress"]
    result["total"] = sum(result[status] for status in REPORT_STATUSES)
    return result
