import os
import sqlite3
from contextlib import contextmanager


DB_PATH = os.environ.get(
    "DB_PATH",
    "/app/data/mediabot.db"
)


@contextmanager
def connection():
    conn = sqlite3.connect(
        DB_PATH
    )

    conn.row_factory = sqlite3.Row

    try:
        yield conn
        conn.commit()

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
            CREATE INDEX IF NOT EXISTS
                idx_request_messages_pending
            ON request_messages (
                jellyfin_available,
                media_type,
                tmdb_id
            )
            """
        )


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
    request_status
):

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
                request_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

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
            )
        )


def pending_requests(
    limit=100
):

    with connection() as conn:

        return conn.execute(
            """
            SELECT *
            FROM request_messages
            WHERE jellyfin_available = 0
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (
                int(limit),
            )
        ).fetchall()


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


def tracking_stats():

    with connection() as conn:

        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM request_messages
            """
        ).fetchone()[0]

        pending = conn.execute(
            """
            SELECT COUNT(*)
            FROM request_messages
            WHERE jellyfin_available = 0
            """
        ).fetchone()[0]

        available = conn.execute(
            """
            SELECT COUNT(*)
            FROM request_messages
            WHERE jellyfin_available = 1
            """
        ).fetchone()[0]

    return {
        "total": total,
        "pending": pending,
        "available": available,
    }
