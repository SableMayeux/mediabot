import os
import sqlite3
import asyncio
import difflib
import io
import json
import logging
import re
import secrets
import signal
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from mediabot.core.database import (
    init_tracking_db,
    track_request,
    pending_requests,
    mark_available,
    tracking_stats,
    save_rating,
    ratings_for_user,
    delete_rating,
    begin_music_request,
    update_music_request,
    recent_music_request,
    latest_music_request,
    request_by_id,
    latest_media_request,
    update_tracked_request_status,
    begin_media_request_intent,
    record_media_request_acceptance,
    fail_media_request_intent,
    mark_media_request_intent_tracked,
    recoverable_media_request_intents,
    media_request_intent_stats,
    create_media_report,
    media_report_by_id,
    list_media_reports,
    update_media_report_status,
    media_report_stats,
)
from mediabot.core.transient_store import TransientUIStore
from mediabot.core.event_store import EventStore
from mediabot.core.runtime_health import (
    RuntimeHealthError,
    validate_runtime_health,
    write_runtime_health,
)

from mediabot.providers.jellyfin import JellyfinProvider
from mediabot.providers.seerr import (
    SeerrProvider,
    SeerrError,
)
from mediabot.providers.soulsync import (
    SoulSyncProvider,
    SoulSyncError,
)
from mediabot.providers.sonarr import (
    SonarrProvider,
    SonarrError,
)
from mediabot.services.library import LibraryService
from mediabot.services.discovery import (
    DiscoveryService,
    DiscoveryUsageError,
)
from mediabot.services.recommendations import RecommendationService
from mediabot.services.reports import (
    REPORT_CATEGORY_LABELS,
    ReportCategory,
    ReportQuery,
    ReportService,
    ReportUsageError,
    normalize_report_category,
    parse_report_query,
)
from mediabot.services.events import (
    DEFAULT_EVENT_TIMEZONE,
    EventNotFoundError,
    EventService,
    EventStateError,
    EventStatus,
    EventUsageError,
    OpenEventExistsError,
    ScheduleAssignment,
    StaleEventRevisionError,
    VoteLimitExceededError,
    parse_schedule_input,
)
from mediabot.events.presets import build_spooktober_preset


# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

SEERR_URL = os.environ.get(
    "SEERR_URL",
    "http://host.docker.internal:5055"
).rstrip("/")

SEERR_API_KEY = os.environ["SEERR_API_KEY"]

DB_PATH = os.environ.get(
    "DB_PATH",
    "/app/data/mediabot.db"
)

PREFIX = "$"

BOT_VERSION = "0.10.0"


def parse_allowed_guild_ids(value):
    guild_ids = set()
    for raw_id in str(value or "").split(","):
        normalized = raw_id.strip()
        if not normalized:
            continue
        if not normalized.isdigit() or int(normalized) <= 0:
            raise ValueError("ALLOWED_GUILD_IDS must contain positive Discord IDs.")
        guild_ids.add(int(normalized))
    return frozenset(guild_ids)


def parse_event_reminder_role_id(value):
    """Accept only a Discord role ID/mention; arbitrary mention text is unsafe."""

    normalized = str(value or "").strip()
    if not normalized:
        return None
    match = re.fullmatch(r"(?:([1-9][0-9]*)|<@&([1-9][0-9]*)>)", normalized)
    if match is None:
        raise ValueError(
            "EVENT_REMINDER_MENTION must be blank, a positive role ID, or <@&ROLE_ID>."
        )
    return int(match.group(1) or match.group(2))


def validate_event_reminder_role_scope(role_id, guild_ids):
    """A Discord role snowflake is meaningful in exactly one guild."""

    if role_id is not None and len(guild_ids) != 1:
        raise ValueError(
            "EVENT_REMINDER_MENTION requires exactly one ALLOWED_GUILD_ID. "
            "Leave it blank for multi-guild deployments."
        )
    return role_id


ALLOWED_GUILD_IDS = parse_allowed_guild_ids(
    os.environ.get("ALLOWED_GUILD_IDS", "")
)

RUNTIME_HEALTH_PATH = os.environ.get(
    "RUNTIME_HEALTH_PATH",
    "/app/data/runtime-health.json",
)
RUNTIME_STARTED_AT = time.time()
LAST_TRANSIENT_CLEANUP = {
    "completed_at": None,
    "claimed": 0,
    "cards": 0,
    "commands": 0,
    "preserved": 0,
    "retried": 0,
}
LAST_JELLYFIN_RECONCILIATION = {
    "completed_at": None,
    "failed": False,
}
LAST_EVENT_RECONCILIATION = {
    "completed_at": None,
    "failed": False,
    "reminders": 0,
    "native_events": 0,
    "completed": 0,
}
SEMANTIC_RESOLVER_SEMAPHORE = asyncio.Semaphore(8)
SEMANTIC_GENRE_CACHE_TTL_SECONDS = 60 * 60
SEMANTIC_GENRE_CACHE = {}
ACTIVE_MEDIA_SUBMISSIONS = set()
MEDIA_SUBMISSION_DRAIN_SECONDS = int(
    os.environ.get("MEDIA_SUBMISSION_DRAIN_SECONDS", "30")
)

JELLYFIN_TASTE_USER = os.environ.get(
    "JELLYFIN_TASTE_USER",
    ""
).strip()



# Browser-facing Seerr URL.
#
# This is intentionally separate from SEERR_URL.
# SEERR_URL may be an internal Docker address that humans
# cannot open in their browser.
SEERR_PUBLIC_URL = os.environ.get(
    "SEERR_PUBLIC_URL",
    SEERR_URL
).rstrip("/")

# Search / confirmation UI lifetime.
REQUEST_UI_TIMEOUT = int(
    os.environ.get(
        "REQUEST_UI_TIMEOUT",
        "300"
    )
)

EVENT_RECONCILE_SECONDS = max(
    30,
    int(os.environ.get("EVENT_RECONCILE_SECONDS", "60")),
)
EVENT_CYCLE_MAX_AGE_SECONDS = max(120, EVENT_RECONCILE_SECONDS * 3)
EVENT_COMPLETION_GRACE_HOURS = max(
    1,
    int(os.environ.get("EVENT_COMPLETION_GRACE_HOURS", "6")),
)
EVENT_REMINDER_ROLE_ID = validate_event_reminder_role_scope(
    parse_event_reminder_role_id(os.environ.get("EVENT_REMINDER_MENTION", "")),
    ALLOWED_GUILD_IDS,
)
EVENT_REMINDER_MENTION = (
    f"<@&{EVENT_REMINDER_ROLE_ID}>" if EVENT_REMINDER_ROLE_ID is not None else ""
)

SOULSYNC_PUBLIC_URL = os.environ.get(
    "SOULSYNC_PUBLIC_URL",
    "",
).strip().rstrip("/")


# ============================================================
# LOGGING
# ============================================================

_LOG_SECRET_VALUES = tuple(
    sorted(
        {
            str(value)
            for name, value in os.environ.items()
            if value
            and len(str(value)) >= 8
            and (
                name.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET"))
                or name.upper() in {"DISCORD_TOKEN", "TRAKT_CLIENT_ID"}
            )
        },
        key=len,
        reverse=True,
    )
)


def _redact_sensitive_text(value):
    text = str(value)
    for secret_value in _LOG_SECRET_VALUES:
        text = text.replace(secret_value, "[REDACTED]")
    text = re.sub(
        r"(?i)([A-Za-z0-9_-]{20,})\.([A-Za-z0-9_-]{5,})\.([A-Za-z0-9_-]{20,})",
        "[REDACTED_DISCORD_TOKEN]",
        text,
    )
    text = re.sub(
        r"(?i)(DISCORD_TOKEN|SEERR_API_KEY|JELLYFIN_API_KEY|SOULSYNC_API_KEY|"
        r"SONARR_API_KEY|TRAKT_CLIENT_ID|TRAKT_CLIENT_SECRET)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(X-Api-Key|Authorization)\s*[:=]\s*\S+",
        r"\1: [REDACTED]",
        text,
    )
    return text


class RedactingFormatter(logging.Formatter):
    """Redact secrets after exception tracebacks have been formatted."""

    def format(self, record):
        return _redact_sensitive_text(super().format(record))

LOG_PATH = os.environ.get(
    "LOG_PATH",
    "/app/data/mediabot.log"
)

LOG_MAX_BYTES = int(
    os.environ.get(
        "LOG_MAX_BYTES",
        str(2 * 1024 * 1024)
    )
)

LOG_BACKUP_COUNT = int(
    os.environ.get(
        "LOG_BACKUP_COUNT",
        "5"
    )
)

os.makedirs(
    os.path.dirname(LOG_PATH),
    exist_ok=True
)

logger = logging.getLogger(
    "mediabot"
)

logger.setLevel(
    logging.INFO
)

logger.propagate = False

if not logger.handlers:

    formatter = RedactingFormatter(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    )

    stream_handler = (
        logging.StreamHandler()
    )

    stream_handler.setFormatter(
        formatter
    )

    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8"
    )

    file_handler.setFormatter(
        formatter
    )

    logger.addHandler(
        stream_handler
    )

    logger.addHandler(
        file_handler
    )


def log_exception(
    context: str,
    error: Exception
):
    error_id = secrets.token_hex(3)

    logger.error(
        "ERROR_ID=%s | %s | %s: %s",
        error_id,
        context,
        type(error).__name__,
        error,
        exc_info=(
            type(error),
            error,
            error.__traceback__
        )
    )

    return error_id


def redact_log_text(
    value: str
):
    return _redact_sensitive_text(value)


class LoggedView(
    discord.ui.View
):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item
    ):
        error_id = log_exception(
            (
                "Discord component failure "
                f"view={type(self).__name__} "
                f"item={type(item).__name__}"
            ),
            error
        )

        message = (
            "MediaBot hit an internal error.\n"
            f"Error ID: `{error_id}`\n\n"
            "An administrator can run "
            "`$admin errors`."
        )

        try:
            if interaction.response.is_done():

                await interaction.followup.send(
                    message,
                    ephemeral=True
                )

            else:

                await interaction.response.send_message(
                    message,
                    ephemeral=True
                )

        except Exception as response_error:

            logger.error(
                "Could not report component "
                "error %s back to Discord: %s",
                error_id,
                response_error
            )


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    allowed_mentions=discord.AllowedMentions.none(),
)


@bot.check
async def enforce_allowed_guild(ctx):
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    if ctx.guild.id in ALLOWED_GUILD_IDS:
        return True
    logger.warning(
        "Blocked command from untrusted guild guild=%s user=%s command=%s",
        ctx.guild.id,
        ctx.author.id,
        ctx.command,
    )
    raise commands.CheckFailure("MediaBot is not configured for this server.")


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_links (
                discord_user_id INTEGER PRIMARY KEY,
                discord_username TEXT NOT NULL,
                seerr_user_id INTEGER NOT NULL,
                seerr_username TEXT NOT NULL,
                linked_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()


def get_link(discord_user_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM user_links
            WHERE discord_user_id = ?
            """,
            (discord_user_id,)
        ).fetchone()


def set_link(
    discord_user_id: int,
    discord_username: str,
    seerr_user_id: int,
    seerr_username: str
):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_links (
                discord_user_id,
                discord_username,
                seerr_user_id,
                seerr_username
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT(discord_user_id)
            DO UPDATE SET
                discord_username = excluded.discord_username,
                seerr_user_id = excluded.seerr_user_id,
                seerr_username = excluded.seerr_username,
                linked_at = CURRENT_TIMESTAMP
            """,
            (
                discord_user_id,
                discord_username,
                seerr_user_id,
                seerr_username
            )
        )

        conn.commit()


seerr = SeerrProvider()
jellyfin = JellyfinProvider()
soulsync = SoulSyncProvider()
sonarr = SonarrProvider()
library = LibraryService(jellyfin)
discovery = DiscoveryService(seerr)
recommendations = RecommendationService(discovery)
reports = ReportService(jellyfin)
transient_ui_store = TransientUIStore(DB_PATH)
event_store = EventStore(DB_PATH)
events = EventService(event_store)


# ============================================================
# MEDIA HELPERS
# ============================================================

MEDIA_STATUS = {
    1: "Unknown",
    2: "Pending",
    3: "Processing",
    4: "Partially Available",
    5: "Available",
    6: "Blocklisted",
    7: "Deleted",
}


def media_title(item):
    return (
        item.get("title")
        or item.get("name")
        or "Unknown Title"
    )


def media_year(item):
    raw = (
        item.get("releaseDate")
        or item.get("firstAirDate")
        or ""
    )

    if raw and len(raw) >= 4:
        return raw[:4]

    return "????"


def media_status(item):
    info = item.get("mediaInfo") or {}

    status = info.get("status")

    if not status:
        return "Not in library"

    return MEDIA_STATUS.get(
        status,
        f"Status {status}"
    )


def already_available(item):
    info = item.get("mediaInfo") or {}
    return info.get("status") == 5


def already_underway(item):
    info = item.get("mediaInfo") or {}

    return info.get("status") in (
        2,
        3,
        4
    )


def is_blocklisted(item):
    return (item.get("mediaInfo") or {}).get("status") == 6


def tv_season_request_options(details):
    """Return selectable seasons, blocked statuses, and episode counts."""
    catalog = {}

    for season in details.get("seasons") or []:
        try:
            season_number = int(season.get("seasonNumber") or 0)
            episode_count = max(0, int(season.get("episodeCount") or 0))
        except (TypeError, ValueError, AttributeError):
            continue

        if season_number > 0:
            catalog[season_number] = episode_count

    season_statuses = {}
    media_info = details.get("mediaInfo") or {}

    for season in media_info.get("seasons") or []:
        try:
            season_number = int(season.get("seasonNumber") or 0)
            status = int(season.get("status") or 1)
        except (TypeError, ValueError, AttributeError):
            continue

        if season_number > 0:
            season_statuses[season_number] = status

    blocked_statuses = {2, 3, 5, 6}
    selectable = [
        season_number
        for season_number in sorted(catalog)
        if catalog[season_number] > 0
        and season_statuses.get(season_number, 1) not in blocked_statuses
    ]
    blocked = {
        season_number: MEDIA_STATUS.get(status, f"Status {status}")
        for season_number, status in sorted(season_statuses.items())
        if season_number in catalog and status in blocked_statuses
    }
    blocked.update({
        season_number: "Not released"
        for season_number, episode_count in catalog.items()
        if episode_count <= 0
    })
    return selectable, blocked, catalog


def compact_episode_ranges(episode_numbers):
    numbers = sorted({int(value) for value in episode_numbers if int(value) > 0})
    if not numbers:
        return ""

    ranges = []
    start = previous = numbers[0]

    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue

        ranges.append(
            f"E{start}" if start == previous else f"E{start}-E{previous}"
        )
        start = previous = number

    ranges.append(
        f"E{start}" if start == previous else f"E{start}-E{previous}"
    )
    return ", ".join(ranges)


def compact_number_ranges(numbers, *, prefix=""):
    values = sorted({int(value) for value in numbers})
    if not values:
        return "None"

    ranges = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(
            f"{prefix}{start}"
            if start == previous
            else f"{prefix}{start}-{prefix}{previous}"
        )
        start = previous = value
    ranges.append(
        f"{prefix}{start}"
        if start == previous
        else f"{prefix}{start}-{prefix}{previous}"
    )
    return ", ".join(ranges)


def tvdb_id_from_details(details):
    external_ids = details.get("externalIds") or {}
    candidates = (
        details.get("tvdbId"),
        external_ids.get("tvdbId"),
        external_ids.get("tvdb"),
    )
    for value in candidates:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return numeric
    return None


def seerr_accepted_seasons(payload):
    accepted = set()
    for season in (payload or {}).get("seasons") or []:
        value = season.get("seasonNumber") if isinstance(season, dict) else season
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            accepted.add(number)
    return tuple(sorted(accepted))


def media_request_episode_payload(details, seasons, direct_episode_requests):
    season_numbers = tuple(sorted({int(value) for value in seasons or ()}))
    direct = {
        int(season): tuple(sorted({int(episode) for episode in episodes}))
        for season, episodes in (direct_episode_requests or {}).items()
    }
    catalog = tv_season_request_options(details)[2]
    counts = {
        season: catalog.get(season) or max(direct.get(season, (0,)))
        for season in season_numbers
    }
    numbers = {
        season: direct.get(season, range(1, counts[season] + 1))
        for season in season_numbers
    }
    return counts, numbers


def _intent_json(value, fallback):
    try:
        decoded = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return decoded


def track_accepted_media_request_intent(record):
    """Materialize one accepted provider action into normal request tracking."""

    accepted_seasons = tuple(
        sorted({
            int(value)
            for value in _intent_json(record.get("accepted_seasons"), [])
            if int(value) > 0
        })
    )
    raw_counts = _intent_json(record.get("accepted_episode_counts"), {})
    raw_numbers = _intent_json(record.get("accepted_episode_numbers"), {})
    accepted_counts = {
        int(season): max(0, int(count))
        for season, count in raw_counts.items()
        if int(season) > 0
    }
    accepted_numbers = {
        int(season): tuple(sorted({
            int(episode) for episode in episodes if int(episode) > 0
        }))
        for season, episodes in raw_numbers.items()
        if int(season) > 0
    }
    tracking_request_id = (
        int(record["seerr_request_id"])
        if record.get("seerr_request_id") is not None
        else int(record["direct_tracking_id"])
    )
    track_request(
        seerr_request_id=tracking_request_id,
        media_type=record["media_type"],
        tmdb_id=int(record["tmdb_id"]),
        title=record["title"],
        year=record.get("year"),
        requester_discord_id=int(record["requester_discord_id"]),
        discord_guild_id=int(record["discord_guild_id"]),
        discord_channel_id=int(record["discord_channel_id"]),
        discord_message_id=int(record["discord_message_id"]),
        request_status=record.get("request_status") or "Provider accepted",
        requested_seasons=accepted_seasons,
        requested_episode_counts=accepted_counts,
        requested_episode_numbers=accepted_numbers,
    )
    mark_media_request_intent_tracked(record["intent_id"])
    return tracking_request_id


def recover_accepted_media_request_intents():
    recovered = 0
    failed = 0
    for record in recoverable_media_request_intents(limit=1000):
        try:
            tracking_request_id = track_accepted_media_request_intent(record)
            recovered += 1
            logger.warning(
                "RECOVERED ACCEPTED REQUEST | intent=%s | request_id=%s | title=%s",
                record["intent_id"],
                tracking_request_id,
                record["title"],
            )
        except Exception as exc:
            failed += 1
            log_exception(
                f"Could not recover accepted request intent {record['intent_id']}",
                exc,
            )
    return {"recovered": recovered, "failed": failed}


async def tv_season_request_inventory(item, details):
    """Resolve empty, partial, and complete seasons using exact episodes."""
    selectable, blocked, catalog = tv_season_request_options(details)
    selectable = set(selectable)
    missing_episodes = {}
    seerr_statuses = {}

    for season in (details.get("mediaInfo") or {}).get("seasons") or []:
        try:
            season_number = int(season.get("seasonNumber") or 0)
            status = int(season.get("status") or 1)
        except (TypeError, ValueError, AttributeError):
            continue

        if season_number > 0:
            seerr_statuses[season_number] = status

    direct_episode_requests = {}
    tvdb_id = tvdb_id_from_details(details)

    if sonarr.enabled and tvdb_id:
        try:
            sonarr_inventory = await sonarr.series_inventory(tvdb_id)
        except Exception as exc:
            logger.info("Sonarr episode inventory unavailable: %s", exc)
            sonarr_inventory = None

        if sonarr_inventory:
            catalog.update({
                int(season): len(state.get("expected") or ())
                for season, state in sonarr_inventory["seasons"].items()
                if int(season) > 0 and state.get("expected")
            })
            selectable = set()
            blocked = {}
            missing_episodes = {}

            all_seasons = sorted(
                set(catalog) | {
                    int(season)
                    for season in sonarr_inventory["seasons"]
                    if int(season) > 0
                }
            )

            for season_number in all_seasons:
                state = sonarr_inventory["seasons"].get(season_number)
                status = seerr_statuses.get(int(season_number), 1)

                # A catalog season not yet known to Sonarr is still a normal
                # Seerr request. Direct Sonarr repair is reserved for an
                # already-approved season with some files present.
                if state is None:
                    episode_count = int(catalog.get(season_number) or 0)
                    if episode_count <= 0:
                        blocked[season_number] = "Not released"
                    elif status in {2, 3, 5, 6}:
                        blocked[season_number] = MEDIA_STATUS[status]
                    else:
                        selectable.add(season_number)
                        missing_episodes[season_number] = tuple(
                            range(1, episode_count + 1)
                        )
                    continue

                expected = set(state.get("expected") or ())
                available = set(state.get("available") or ())
                missing = set(state.get("missing") or ())
                future = set(state.get("future") or ())
                monitored = set(state.get("monitored") or ())
                queued = set(state.get("queued") or ())
                aired_missing = missing - future
                repairable = (aired_missing - queued) | (future - monitored)

                if not expected:
                    blocked[int(season_number)] = "Not released"
                elif not missing:
                    blocked[int(season_number)] = "Available"
                elif status in {2, 3}:
                    blocked[int(season_number)] = MEDIA_STATUS[status]
                elif status == 6:
                    blocked[int(season_number)] = MEDIA_STATUS[status]
                elif status in {4, 5} and not available:
                    blocked[int(season_number)] = "Needs administrator repair"
                elif status in {4, 5} and repairable:
                    selectable.add(int(season_number))
                    missing_episodes[int(season_number)] = tuple(
                        sorted(repairable)
                    )
                    direct_episode_requests[int(season_number)] = tuple(
                        sorted(repairable)
                    )
                elif status in {4, 5} and queued:
                    queue_states = sorted({
                        str((state.get("queue_status") or {}).get(episode) or "queued")
                        for episode in queued
                    })
                    label = "/".join(queue_states[:2])
                    blocked[int(season_number)] = (
                        f"Sonarr queue {compact_episode_ranges(queued)} ({label})"
                    )
                elif status in {4, 5} and future:
                    blocked[int(season_number)] = (
                        "Upcoming " + compact_episode_ranges(future) + " monitored"
                    )
                else:
                    # Unknown/deleted scope remains approval-governed even if
                    # Sonarr already knows the series.
                    selectable.add(int(season_number))
                    missing_episodes[int(season_number)] = tuple(
                        sorted(missing)
                    )

            return (
                sorted(selectable),
                blocked,
                catalog,
                missing_episodes,
                direct_episode_requests,
            )

    if jellyfin.enabled:
        try:
            jellyfin_item = await jellyfin.find_by_tmdb(
                tmdb_id=int(item["id"]),
                title=media_title(item),
                media_type="tv",
            )
            available_numbers = (
                await jellyfin.series_season_episode_numbers(jellyfin_item["Id"])
                if jellyfin_item
                else {}
            )

            for season_number, episode_count in catalog.items():
                expected = set(range(1, episode_count + 1))
                available = available_numbers.get(season_number, set())
                missing = expected - available

                if expected and not missing:
                    selectable.discard(season_number)
                    blocked[season_number] = "Available"
                elif available and missing:
                    missing_episodes[season_number] = tuple(sorted(missing))
                    status = seerr_statuses.get(season_number, 1)
                    if status not in {2, 3, 4, 5, 6}:
                        selectable.add(season_number)
                    else:
                        selectable.discard(season_number)
                        blocked[season_number] = (
                            "Partial - Sonarr repair unavailable"
                        )
        except Exception as exc:
            logger.info("Episode-level Jellyfin inventory unavailable: %s", exc)

    for season_number, status in seerr_statuses.items():
        if status == 4 and season_number in selectable:
            missing_episodes.setdefault(season_number, ())

    return (
        sorted(selectable),
        blocked,
        catalog,
        missing_episodes,
        direct_episode_requests,
    )


def discord_message_coordinates(message):
    if message is None:
        return None
    channel_id = getattr(getattr(message, "channel", None), "id", None)
    message_id = getattr(message, "id", None)
    try:
        channel_id = int(channel_id)
        message_id = int(message_id)
    except (TypeError, ValueError):
        return None
    if channel_id <= 0 or message_id <= 0:
        return None
    return channel_id, message_id


def transient_record_for_message(message, *, active_only=True):
    coordinates = discord_message_coordinates(message)
    if coordinates is None:
        return None
    try:
        return transient_ui_store.get_for_card(
            channel_id=coordinates[0],
            card_message_id=coordinates[1],
            active_only=active_only,
        )
    except Exception as exc:
        log_exception("Could not read transient UI state", exc)
        return None


def register_transient_card(
    *,
    message,
    command_message,
    kind,
    batch_id=None,
    expected_batch_size=1,
):
    coordinates = discord_message_coordinates(message)
    command_coordinates = discord_message_coordinates(command_message)
    if coordinates is None:
        return None
    entry_id = (
        f"{kind}:{coordinates[0]}:{coordinates[1]}:"
        f"{secrets.token_hex(5)}"
    )
    normalized_batch = batch_id or entry_id
    try:
        return transient_ui_store.register(
            entry_id=entry_id,
            batch_id=str(normalized_batch),
            expected_batch_size=int(expected_batch_size),
            kind=str(kind),
            guild_id=getattr(getattr(message, "guild", None), "id", None),
            channel_id=coordinates[0],
            card_message_id=coordinates[1],
            command_channel_id=(
                command_coordinates[0] if command_coordinates else coordinates[0]
            ),
            command_message_id=(
                command_coordinates[1] if command_coordinates else None
            ),
            expires_at=time.time() + REQUEST_UI_TIMEOUT,
        )
    except Exception as exc:
        log_exception(
            f"Could not register transient UI kind={kind} message={coordinates[1]}",
            exc,
        )
        return None


def transient_batch_id_for_command(command_message, *, kind):
    coordinates = discord_message_coordinates(command_message)
    if coordinates is None:
        return f"{kind}:unknown:{secrets.token_hex(8)}"
    return f"{kind}:{coordinates[0]}:{coordinates[1]}"


def touch_transient_message(message):
    coordinates = discord_message_coordinates(message)
    if coordinates is None:
        return False
    try:
        return transient_ui_store.reset_card(
            channel_id=coordinates[0],
            card_message_id=coordinates[1],
            expires_at=time.time() + REQUEST_UI_TIMEOUT,
        )
    except Exception as exc:
        log_exception("Could not extend transient UI expiry", exc)
        return False


async def renew_transient_interaction(interaction, message):
    """Renew a live card or reject a click once expiry owns the card."""

    if touch_transient_message(message):
        return True
    record = transient_record_for_message(message, active_only=False)
    if record is None:
        # Some static or test-only views are intentionally not durable.
        return True
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "That menu expired while you were using it. Run the command again.",
            ephemeral=True,
        )
    return False


def transition_transient_message(message, kind):
    coordinates = discord_message_coordinates(message)
    if coordinates is None:
        return False
    try:
        return transient_ui_store.transition_card(
            channel_id=coordinates[0],
            card_message_id=coordinates[1],
            kind=kind,
            expires_at=time.time() + REQUEST_UI_TIMEOUT,
        )
    except Exception as exc:
        log_exception(f"Could not transition transient UI to {kind}", exc)
        return False


def mark_transient_message_terminal(message, state):
    record = transient_record_for_message(message, active_only=True)
    if record is None:
        return None
    try:
        if transient_ui_store.mark_terminal(record.entry_id, state):
            return record
    except Exception as exc:
        log_exception(
            f"Could not mark transient UI {record.entry_id} as {state}",
            exc,
        )
    return None


def expired_saved_rating_embed(message):
    """Return a static rating embed without promising unavailable actions."""
    embeds = list(getattr(message, "embeds", None) or ())
    if not embeds:
        return None
    try:
        embed = embeds[0].copy()
        for index, field in enumerate(embed.fields):
            if str(field.name).casefold() == "rating":
                embed.set_field_at(
                    index,
                    name=field.name,
                    value="**Saved locally.** Rating actions expired.",
                    inline=field.inline,
                )
                break
        embed.set_footer(text="Rating saved locally - actions expired")
        return embed
    except Exception as exc:
        logger.info("Could not build static saved-rating embed: %s", exc)
        return None


def expired_saved_event_vote_embed(message):
    """Keep a committed vote receipt while removing expired controls."""
    embeds = list(getattr(message, "embeds", None) or ())
    if not embeds:
        return None
    try:
        embed = embeds[0].copy()
        embed.set_footer(text="Vote saved - controls expired; run $event vote to change it")
        return embed
    except Exception as exc:
        logger.info("Could not build static event-vote embed: %s", exc)
        return None


def expired_interactive_success_embed(message, kind):
    if kind == "rating_saved_actions":
        return expired_saved_rating_embed(message)
    if kind == "event_vote_saved_actions":
        return expired_saved_event_vote_embed(message)
    if kind == "event_time_vote_saved_actions":
        embed = expired_saved_event_vote_embed(message)
        if embed is not None:
            embed.set_footer(
                text="Availability saved - controls expired; run $event time to change it"
            )
        return embed
    return None


async def delete_message_safely(message, *, label):
    if message is None:
        return True

    try:
        await message.delete()
        return True
    except discord.NotFound:
        return True
    except Exception as exc:
        logger.warning(
            "Could not delete %s message=%s error=%s",
            label,
            getattr(message, "id", None),
            exc,
        )
        return False


async def discord_message_by_id(channel_id, message_id):
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        channel = await bot.fetch_channel(int(channel_id))
    return await channel.fetch_message(int(message_id))


async def delete_discord_message_by_id(channel_id, message_id, *, label):
    try:
        message = await discord_message_by_id(channel_id, message_id)
    except discord.NotFound:
        return True
    except Exception as exc:
        logger.warning(
            "Could not fetch %s channel=%s message=%s error=%s",
            label,
            channel_id,
            message_id,
            exc,
        )
        return False
    return await delete_message_safely(message, label=label)


async def finish_transient_batch_command(record, *, command_message=None):
    claim_token = f"command:{secrets.token_hex(10)}"
    try:
        claim = transient_ui_store.claim_batch_command_deletion(
            record.batch_id,
            claim_token,
        )
    except Exception as exc:
        log_exception("Could not claim transient command cleanup", exc)
        return False
    if claim is None:
        # The batch is incomplete or contains an accepted/kept card. Nothing
        # was deleted, and callers must not count this as a command cleanup.
        return None

    command_coordinates = discord_message_coordinates(command_message)
    if command_coordinates == (claim.channel_id, claim.command_message_id):
        deleted = await delete_message_safely(
            command_message,
            label="expired request command",
        )
    else:
        deleted = await delete_discord_message_by_id(
            claim.channel_id,
            claim.command_message_id,
            label="expired request command",
        )

    return finalize_transient_command_claim(claim, deleted)


def finalize_transient_command_claim(claim, deleted):
    """Commit or release one durable command-deletion claim."""

    try:
        if deleted:
            transient_ui_store.mark_batch_command_deleted(
                claim.batch_id,
                claim.claim_token,
            )
        else:
            transient_ui_store.release_batch_command_claim(
                claim.batch_id,
                claim.claim_token,
            )
    except Exception as exc:
        log_exception("Could not finalize transient command cleanup", exc)
        return False
    return deleted


async def cleanup_unsuccessful_request(
    *,
    origin_message,
    command_message=None,
    batch_state=None,
    batch_card_id=None,
):
    record = transient_record_for_message(origin_message, active_only=True)
    deleted = await delete_message_safely(
        origin_message,
        label="unsuccessful request card",
    )

    if record is not None:
        if deleted:
            try:
                transient_ui_store.mark_terminal(record.entry_id, "dismissed")
            except Exception as exc:
                log_exception("Could not retire dismissed transient card", exc)
            await finish_transient_batch_command(
                record,
                command_message=command_message,
            )
        return

    if batch_state is not None and batch_card_id is not None:
        await batch_state.set_state(batch_card_id, "dismissed")
        return

    await delete_message_safely(command_message, label="unsuccessful request command")


# ============================================================
# REQUEST UI HELPERS
# ============================================================

RESULTS_PER_PAGE = 5


def split_media_query_year(query: str) -> tuple[str, str | None]:
    """Accept natural title suffixes such as `Sherlock Holmes (2009)`."""
    normalized = " ".join(str(query).split())
    match = re.match(
        r"^(?P<title>.+?)(?:\s*\((?P<paren>18\d{2}|19\d{2}|20\d{2}|21\d{2})\)"
        r"|\s+(?P<plain>18\d{2}|19\d{2}|20\d{2}|21\d{2}))$",
        normalized,
    )
    if not match:
        return normalized, None

    title = " ".join(match.group("title").split()).strip()
    year = match.group("paren") or match.group("plain")
    return (title or normalized), year


def normalized_media_title(value: str) -> str:
    return "".join(
        character
        for character in str(value).casefold()
        if character.isalnum()
    )


def prioritize_search_results(items, *, query: str, year: str | None = None):
    """Put exact title/year matches first without discarding alternatives."""
    expected_title = normalized_media_title(query)
    return sorted(
        items,
        key=lambda item: (
            0 if not year or media_year(item) == str(year) else 1,
            0 if normalized_media_title(media_title(item)) == expected_title else 1,
        ),
    )


async def fetch_search_page(
    query: str,
    page: int = 1
):
    result = await seerr.request(
        "GET",
        "/search",
        params={
            "query": query,
            "page": page
        }
    )

    filtered = [
        item
        for item in result.get("results", [])
        if item.get("mediaType") in (
            "movie",
            "tv"
        )
    ]

    total_pages = int(
        result.get("totalPages")
        or 1
    )

    return filtered, total_pages


async def resolve_seerr_search_query(query: str, page: int = 1):
    """Search naturally, using a trailing year as fallback disambiguation."""
    normalized = " ".join(str(query).split())
    base_query, target_year = split_media_query_year(normalized)
    parenthesized_year = bool(
        re.search(r"\((?:18|19|20|21)\d{2}\)$", normalized)
    )

    if target_year and not parenthesized_year:
        raw_results, raw_pages = await fetch_search_page(normalized, page)
        raw_title = normalized_media_title(normalized)
        exact_numeric_title = any(
            normalized_media_title(media_title(item)) == raw_title
            for item in raw_results
        )
        if exact_numeric_title:
            return normalized, None, raw_results, raw_pages

    results, total_pages = await fetch_search_page(base_query, page)
    if not results and target_year and not parenthesized_year:
        results = raw_results
        total_pages = raw_pages
    results = prioritize_search_results(
        results,
        query=base_query,
        year=target_year,
    )
    return base_query, target_year, results, total_pages


async def fetch_media_details(
    item: dict
):
    media_type = item.get("mediaType")
    media_id = item.get("id")

    if media_type not in ("movie", "tv"):
        return item

    try:
        return await seerr.request(
            "GET",
            f"/{media_type}/{media_id}"
        )

    except Exception as exc:
        print(
            "WARNING: Could not fetch full "
            f"{media_type} details for "
            f"{media_id}: {exc}"
        )

        return item


def short_overview(
    item: dict,
    limit: int = 150
):
    overview = (
        item.get("overview")
        or item.get("Overview")
        or "No overview available."
    )

    overview = " ".join(
        str(overview).split()
    )

    if len(overview) > limit:
        return overview[:limit - 1].rstrip() + "…"

    return overview


def rating_text(item: dict):
    rating = item.get("voteAverage")
    count = item.get("voteCount")

    if rating is None:
        return "No rating"

    try:
        rating = float(rating)
    except Exception:
        return "No rating"

    if rating <= 0:
        return "No rating"

    if count:
        try:
            return (
                f"{rating:.1f}/10 "
                f"({int(count):,} votes)"
            )
        except Exception:
            pass

    return f"{rating:.1f}/10"


def runtime_text(
    item: dict,
    details: dict
):
    runtime = details.get("runtime")

    if not runtime:
        episode_runtime = (
            details.get("episodeRunTime")
            or details.get("episode_run_time")
        )

        if isinstance(
            episode_runtime,
            list
        ) and episode_runtime:
            runtime = episode_runtime[0]

    try:
        runtime = int(runtime)
    except Exception:
        return None

    if runtime <= 0:
        return None

    hours, minutes = divmod(
        runtime,
        60
    )

    if hours:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


def genres_text(details: dict):
    genres = []

    for genre in (
        details.get("genres")
        or []
    ):
        if isinstance(genre, dict):
            name = genre.get("name")

            if name:
                genres.append(name)

        elif genre:
            genres.append(str(genre))

    if not genres:
        return None

    return ", ".join(
        genres[:5]
    )


def creator_text(
    item: dict,
    details: dict
):
    media_type = item.get("mediaType")

    if media_type == "movie":
        credits = (
            details.get("credits")
            or {}
        )

        crew = (
            credits.get("crew")
            or []
        )

        directors = [
            person.get("name")
            for person in crew
            if (
                person.get("job") == "Director"
                and person.get("name")
            )
        ]

        if directors:
            return (
                "Director",
                ", ".join(directors[:3])
            )

    if media_type == "tv":
        creators = (
            details.get("createdBy")
            or details.get("created_by")
            or []
        )

        names = []

        for creator in creators:
            if isinstance(
                creator,
                dict
            ):
                name = creator.get("name")

                if name:
                    names.append(name)

        if names:
            return (
                "Created By",
                ", ".join(names[:3])
            )

    return None


def seerr_media_url(item: dict):
    return (
        f"{SEERR_PUBLIC_URL}/"
        f"{item['mediaType']}/"
        f"{item['id']}"
    )


def tmdb_media_url(item: dict):
    media_type = item.get(
        "mediaType",
        "movie"
    )

    return (
        "https://www.themoviedb.org/"
        f"{media_type}/{item['id']}"
    )


def compact_embed_field(value, limit=1024):
    """Fit arbitrary dynamic text inside Discord's embed-field ceiling."""

    text = str(value or "")
    maximum = max(32, min(1024, int(limit)))
    if len(text) <= maximum:
        return text
    suffix = "\n... more omitted"
    prefix = text[: maximum - len(suffix)]
    if "\n" in prefix:
        prefix = prefix.rsplit("\n", 1)[0]
    return prefix.rstrip() + suffix


def compact_embed_title(value, limit=256):
    """Fit user/provider supplied text inside Discord's embed-title ceiling."""

    text = " ".join(str(value or "").split()) or "MediaBot"
    maximum = max(32, min(256, int(limit)))
    if len(text) <= maximum:
        return text
    return text[: maximum - 3].rstrip() + "..."


def compact_embed_chunks(lines, *, limit=1024, max_chunks=8):
    """Pack complete display lines into bounded Discord embed fields."""

    maximum = max(64, min(1024, int(limit)))
    chunks = []
    current = []
    current_length = 0
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        if len(line) > maximum:
            line = compact_embed_field(line, maximum)
        added = len(line) + (1 if current else 0)
        if current and current_length + added > maximum:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
            if len(chunks) >= max_chunks:
                break
        current.append(line)
        current_length += len(line) + (1 if len(current) > 1 else 0)
    if current and len(chunks) < max_chunks:
        chunks.append("\n".join(current))
    return chunks


def build_media_embed(
    item: dict,
    details: dict,
    *,
    heading: str,
    state_text: str = None,
    color=None
):
    title = media_title(item)
    year = media_year(item)

    if color is None:
        color = discord.Color.blurple()

    embed = discord.Embed(
        title=compact_embed_title(f"{title} ({year})"),
        description=short_overview(
            details,
            limit=600
        ),
        color=color
    )

    embed.add_field(
        name="Type",
        value=item.get(
            "mediaType",
            "unknown"
        ).upper(),
        inline=True
    )

    embed.add_field(
        name="Rating",
        value=rating_text(
            details
        ),
        inline=True
    )

    embed.add_field(
        name="Library",
        value=media_status(item),
        inline=True
    )

    runtime = runtime_text(
        item,
        details
    )

    if runtime:
        embed.add_field(
            name="Runtime",
            value=runtime,
            inline=True
        )

    genres = genres_text(details)

    if genres:
        embed.add_field(
            name="Genres",
            value=genres,
            inline=False
        )

    creator = creator_text(
        item,
        details
    )

    if creator:
        label, value = creator

        embed.add_field(
            name=label,
            value=value,
            inline=False
        )

    if state_text:
        embed.add_field(
            name=heading,
            value=compact_embed_field(state_text),
            inline=False
        )

    poster = (
        details.get("posterPath")
        or item.get("posterPath")
    )

    if poster:
        embed.set_thumbnail(
            url=(
                "https://image.tmdb.org/"
                "t/p/w342"
                + poster
            )
        )

    return embed


class MediaLinksView(discord.ui.View):
    def __init__(
        self,
        item: dict
    ):
        # Link buttons do not require the bot to remain
        # alive, so they can safely have no timeout.
        super().__init__(
            timeout=None
        )

        public_url = (
            SEERR_PUBLIC_URL
            or ""
        )

        if (
            public_url
            and "host.docker.internal"
            not in public_url
        ):
            self.add_item(
                discord.ui.Button(
                    label="Open in Seerr",
                    style=discord.ButtonStyle.link,
                    url=seerr_media_url(item)
                )
            )

        self.add_item(
            discord.ui.Button(
                label="TMDB",
                style=discord.ButtonStyle.link,
                url=tmdb_media_url(item)
            )
        )


# ============================================================
# CONFIRMATION VIEW
# ============================================================

class ConfirmRequestView(
    LoggedView
):
    def __init__(
        self,
        *,
        requester_id: int,
        item: dict,
        details: dict,
        seerr_user_id: int,
        origin_message,
        seasons=None,
        direct_episode_requests=None,
        command_message=None,
        batch_state=None,
        batch_card_id=None,
    ):
        super().__init__(
            timeout=REQUEST_UI_TIMEOUT
        )

        self.requester_id = requester_id
        self.item = item
        self.details = details
        self.seerr_user_id = (
            seerr_user_id
        )
        self.origin_message = (
            origin_message
        )
        self.seasons = seasons
        self.direct_episode_requests = {
            int(season): tuple(sorted({int(episode) for episode in episodes}))
            for season, episodes in (direct_episode_requests or {}).items()
        }
        self.command_message = command_message
        self.batch_state = batch_state
        self.batch_card_id = batch_card_id

        self.confirm_message = None
        self.finished = False

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):
        if (
            interaction.user.id
            != self.requester_id
        ):
            await interaction.response.send_message(
                (
                    "This confirmation belongs "
                    "to someone else."
                ),
                ephemeral=True
            )

            return False

        return await renew_transient_interaction(interaction, self.origin_message)

    async def finish_confirmation_message(
        self,
        content: str
    ):
        if not self.confirm_message:
            return

        try:
            await self.confirm_message.edit(
                content=content,
                embed=None,
                view=None
            )
        except Exception:
            pass

    @discord.ui.button(
        label="REQUEST",
        style=discord.ButtonStyle.success
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        # ----------------------------------------------------
        # ACKNOWLEDGE IMMEDIATELY
        # ----------------------------------------------------
        #
        # Discord gives component interactions only a few
        # seconds for the initial response.
        #
        # Rather than 'defer(thinking=True)', immediately
        # edit the ephemeral confirmation message into a
        # submitting state.
        #
        # This:
        #   - ACKs Discord immediately
        #   - eliminates endless "thinking..."
        #   - disables double clicking
        #   - gives the user visible progress

        if getattr(
            self,
            "_submitting",
            False
        ):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "That request is already being submitted.",
                    ephemeral=True
                )

            return

        self._submitting = True

        for child in self.children:
            child.disabled = True

        title = media_title(
            self.item
        )

        year = media_year(
            self.item
        )

        media_type = self.item[
            "mediaType"
        ]

        media_id = self.item[
            "id"
        ]

        try:
            await interaction.response.edit_message(
                content=(
                    f"Submitting **{title} "
                    f"({year})**..."
                ),
                view=self
            )

        except Exception as exc:
            self._submitting = False

            error_id = log_exception(
                (
                    "Failed initial REQUEST "
                    f"interaction ACK for "
                    f"{title} ({year})"
                ),
                exc
            )

            logger.error(
                "REQUEST interaction ACK failed "
                "error_id=%s",
                error_id
            )

            return

        selected_seasons = tuple(sorted({int(value) for value in (self.seasons or ())}))
        selected_direct = {
            season: self.direct_episode_requests[season]
            for season in selected_seasons
            if season in self.direct_episode_requests
        }
        selected_seerr = tuple(
            season for season in selected_seasons if season not in selected_direct
        )
        direct_tracking_id = -int(secrets.token_hex(7), 16)
        intent_id = secrets.token_hex(16)
        requested_counts, requested_numbers = media_request_episode_payload(
            self.details,
            selected_seasons,
            selected_direct,
        )
        try:
            begin_media_request_intent(
                intent_id=intent_id,
                media_type=media_type,
                tmdb_id=media_id,
                title=title,
                year=year,
                requester_discord_id=interaction.user.id,
                discord_guild_id=interaction.guild_id,
                discord_channel_id=self.origin_message.channel.id,
                discord_message_id=self.origin_message.id,
                direct_tracking_id=direct_tracking_id,
                requested_seasons=selected_seasons,
                requested_episode_counts=requested_counts,
                requested_episode_numbers=requested_numbers,
            )
        except Exception as exc:
            self._submitting = False
            error_id = log_exception("Could not persist media request intent", exc)
            await interaction.edit_original_response(
                content=(
                    "**MediaBot did not submit this request.**\n\n"
                    f"The durable request ledger was unavailable (Error `{error_id}`). "
                    "Nothing was sent to Seerr or Sonarr."
                ),
                view=None,
            )
            return

        submission_task = asyncio.current_task()
        if submission_task is not None:
            ACTIVE_MEDIA_SUBMISSIONS.add(submission_task)
            submission_task.add_done_callback(ACTIVE_MEDIA_SUBMISSIONS.discard)
        seerr_result = None
        sonarr_result = None
        request_id = None
        accepted_seerr = ()
        accepted_direct = {}
        verification_notes = []
        direct_error_id = None

        try:
            # Brand-new movies and seasons always stay approval-governed in
            # Seerr. A genuine partial, already-approved season can then be
            # repaired through Sonarr in the same confirmation.
            if media_type != "tv" or selected_seerr:
                seerr_result = await seerr.create_request(
                    media_type=media_type,
                    media_id=media_id,
                    seerr_user_id=self.seerr_user_id,
                    seasons=(selected_seerr if media_type == "tv" else None),
                )
                if not str((seerr_result or {}).get("id") or "").isdigit():
                    message = str((seerr_result or {}).get("message") or seerr_result)
                    raise SeerrError(
                        "Seerr did not create a request: " + message[:300]
                    )
                request_id = int(seerr_result["id"])

                if media_type == "tv":
                    accepted_seerr = selected_seerr
                record_media_request_acceptance(
                    intent_id=intent_id,
                    seerr_request_id=request_id,
                    accepted_seasons=accepted_seerr,
                    accepted_episode_counts={
                        season: requested_counts.get(season, 0)
                        for season in accepted_seerr
                    },
                    accepted_episode_numbers={
                        season: requested_numbers.get(season, ())
                        for season in accepted_seerr
                    },
                    request_status="Submitted to Seerr",
                )

                if media_type == "tv":
                    verified_seasons = seerr_accepted_seasons(seerr_result)
                    accepted_seerr = verified_seasons
                    if not accepted_seerr:
                        try:
                            accepted_seerr = seerr_accepted_seasons(
                                await seerr.request_details(request_id)
                            )
                        except Exception as exc:
                            logger.warning(
                                "Seerr request #%s was created but season verification failed: %s",
                                request_id,
                                exc,
                            )
                            accepted_seerr = selected_seerr
                            verification_notes.append(
                                "Seerr accepted the request, but MediaBot could not re-check its season rows."
                            )
                    if not accepted_seerr:
                        accepted_seerr = selected_seerr
                        verification_notes.append(
                            "Seerr created the request, but returned no season details to verify."
                        )
                    record_media_request_acceptance(
                        intent_id=intent_id,
                        seerr_request_id=request_id,
                        accepted_seasons=accepted_seerr,
                        accepted_episode_counts={
                            season: requested_counts.get(season, 0)
                            for season in accepted_seerr
                        },
                        accepted_episode_numbers={
                            season: requested_numbers.get(season, ())
                            for season in accepted_seerr
                        },
                        request_status="Submitted to Seerr",
                    )

            if selected_direct:
                tvdb_id = tvdb_id_from_details(self.details)
                if not tvdb_id:
                    raise SonarrError("The selected series has no TVDB identifier.")
                try:
                    sonarr_result = await sonarr.request_missing_episodes(
                        tvdb_id=tvdb_id,
                        missing_by_season=selected_direct,
                    )
                except Exception as exc:
                    # If Seerr already accepted another selected season, this
                    # is a partial submission, not a total failure.
                    if request_id is None:
                        raise
                    direct_error_id = log_exception(
                        f"Sonarr repair failed after Seerr request #{request_id}",
                        exc,
                    )
                    verification_notes.append(
                        f"The new-season request succeeded, but episode repair failed (Error `{direct_error_id}`)."
                    )

            if sonarr_result:
                raw_accepted = sonarr_result.get("accepted_by_season") or {}
                accepted_direct = {
                    int(season): tuple(sorted({int(episode) for episode in episodes}))
                    for season, episodes in raw_accepted.items()
                    if episodes
                }
                if not raw_accepted and sonarr_result.get("monitor_succeeded", True):
                    accepted_direct = dict(selected_direct)
                unresolved = sonarr_result.get("unresolved") or {}
                if unresolved:
                    unresolved_text = "; ".join(
                        f"S{season} {compact_episode_ranges(episodes)}"
                        for season, episodes in sorted(unresolved.items())
                    )
                    verification_notes.append(
                        "Sonarr could not resolve these episode numbers: "
                        f"`{unresolved_text}`. They were not tracked as submitted."
                    )

                if accepted_direct:
                    accepted_so_far = tuple(
                        sorted(set(accepted_seerr) | set(accepted_direct))
                    )
                    accepted_counts, accepted_numbers = media_request_episode_payload(
                        self.details,
                        accepted_so_far,
                        accepted_direct,
                    )
                    record_media_request_acceptance(
                        intent_id=intent_id,
                        seerr_request_id=request_id,
                        accepted_seasons=accepted_so_far,
                        accepted_episode_counts=accepted_counts,
                        accepted_episode_numbers=accepted_numbers,
                        request_status=(
                            "Submitted to Seerr and Sonarr"
                            if request_id is not None
                            else "Exact episode repair accepted"
                        ),
                    )

            if request_id is None and not accepted_direct:
                if sonarr_result and sonarr_result.get("already_available"):
                    verification_notes.append(
                        "Those episodes arrived before confirmation finished; no repair was needed."
                    )
                    fail_media_request_intent(
                        intent_id=intent_id,
                        error="No provider action was needed; episodes are already available.",
                    )
                else:
                    raise SonarrError("No acquisition action was accepted.")

        except Exception as exc:
            fail_media_request_intent(intent_id=intent_id, error=str(exc))
            self._submitting = False
            self.finished = True
            self.stop()

            error_id = log_exception(
                (
                    "Media acquisition submission could not be verified "
                    f"title={title!r} year={year!r} "
                    f"media_type={media_type!r} media_id={media_id!r}"
                ),
                exc,
            )

            try:
                await interaction.edit_original_response(
                    content=(
                        "**MediaBot could not verify this request.**\n\n"
                        f"Error ID: `{error_id}`\n"
                        f"Check `$status {title}` before trying again; the button is "
                        "disabled to prevent an accidental duplicate."
                    ),
                    view=None,
                )
            except Exception as response_error:
                log_exception("Could not update failed request interaction", response_error)
            return

        self.finished = True
        self.stop()
        mark_transient_message_terminal(self.origin_message, "accepted")

        if self.batch_state is not None:
            await self.batch_state.set_state(self.batch_card_id, "requested")

        successful_seasons = tuple(sorted(set(accepted_seerr) | set(accepted_direct)))
        self.seasons = list(successful_seasons)
        tracking_request_id = (
            request_id
            if request_id is not None
            else direct_tracking_id
            if accepted_direct
            else None
        )
        status_parts = []

        if request_id is not None:
            request_status = (seerr_result or {}).get("status")
            status_parts.append(REQUEST_STATUS.get(request_status, "Submitted to Seerr"))

        if sonarr_result and accepted_direct:
            searched = len(sonarr_result.get("searched_episode_ids") or ())
            future = int(sonarr_result.get("future_count") or 0)
            if sonarr_result.get("search_error"):
                status_parts.append("Episodes monitored; automatic search needs retry")
                verification_notes.append(
                    "Sonarr saved the episode monitoring change, but its immediate search command failed."
                )
            elif searched and future:
                status_parts.append(
                    f"Searching {searched} aired episode(s); monitoring {future} upcoming"
                )
            elif searched:
                status_parts.append(f"Searching {searched} missing episode(s)")
            elif future:
                status_parts.append(f"Monitoring {future} upcoming episode(s)")
            else:
                status_parts.append("Exact episode repair accepted")

        if direct_error_id:
            status_parts.append("New seasons submitted; episode repair needs attention")

        if not status_parts:
            status_parts.append("Already available")

        status_text = "; ".join(status_parts)
        requested_counts, requested_numbers = media_request_episode_payload(
            self.details,
            successful_seasons,
            accepted_direct,
        )
        if tracking_request_id is not None:
            record_media_request_acceptance(
                intent_id=intent_id,
                seerr_request_id=request_id,
                accepted_seasons=successful_seasons,
                accepted_episode_counts=requested_counts,
                accepted_episode_numbers=requested_numbers,
                request_status=status_text,
            )

        logger.info(
            (
                "REQUEST SUCCESS | "
                "discord_user=%s | "
                "seerr_user=%s | "
                "title=%s | "
                "year=%s | "
                "media_type=%s | "
                "media_id=%s | "
                "request_id=%s | "
                "status=%s"
            ),
            interaction.user.id,
            self.seerr_user_id,
            title,
            year,
            media_type,
            media_id,
            request_id or tracking_request_id,
            status_text
        )

        state_lines = [f"**{status_text}**"]
        if request_id is not None:
            state_lines.append(f"Seerr request `#{request_id}`")
        if accepted_direct:
            repaired = "; ".join(
                f"S{season} {compact_episode_ranges(episodes)}"
                for season, episodes in sorted(accepted_direct.items())
            )
            state_lines.append(f"Exact Sonarr repair: `{repaired}`")
        if successful_seasons:
            state_lines.append(
                "Selected seasons: `" + ", ".join(map(str, successful_seasons)) + "`"
            )
        state_lines.extend(verification_notes)

        final_embed = build_media_embed(
            self.item,
            self.details,
            heading="Request",
            state_text="\n".join(state_lines),
            color=(
                discord.Color.gold()
                if verification_notes or direct_error_id
                else discord.Color.green()
            ),
        )

        final_embed.set_footer(
            text=(
                "MediaBot will later replace "
                "this with a Jellyfin Watch "
                "link once the item becomes "
                "available."
            )
        )

        # ----------------------------------------------------
        # PERSIST REQUEST -> DISCORD MESSAGE RELATIONSHIP
        # ----------------------------------------------------

        try:

            if tracking_request_id is not None:
                track_request(
                    seerr_request_id=(
                        int(tracking_request_id)
                    ),
                    media_type=media_type,
                    tmdb_id=media_id,
                    title=title,
                    year=year,
                    requester_discord_id=(
                        interaction.user.id
                    ),
                    discord_guild_id=(
                        interaction.guild_id
                    ),
                    discord_channel_id=(
                        self.origin_message.channel.id
                    ),
                    discord_message_id=(
                        self.origin_message.id
                    ),
                    request_status=(
                        status_text
                    ),
                    requested_seasons=successful_seasons,
                    requested_episode_counts=requested_counts,
                    requested_episode_numbers=requested_numbers,
                )
                mark_media_request_intent_tracked(intent_id)

                logger.info(
                    (
                        "REQUEST TRACKED | "
                        "request_id=%s | "
                        "channel=%s | "
                        "message=%s"
                    ),
                    request_id or tracking_request_id,
                    self.origin_message.channel.id,
                    self.origin_message.id
                )

        except Exception as exc:

            log_exception(
                (
                    "Request succeeded but "
                    "persistent request tracking failed "
                    f"request_id={request_id or tracking_request_id}"
                ),
                exc
            )


        # ----------------------------------------------------
        # UPDATE PUBLIC REQUEST CARD
        # ----------------------------------------------------

        try:
            await self.origin_message.edit(
                embed=final_embed,
                view=MediaLinksView(
                    self.item
                )
            )

        except Exception as exc:
            log_exception(
                (
                    "Request succeeded but public "
                    "request card update failed "
                    f"request_id={request_id or tracking_request_id}"
                ),
                exc
            )

        # ----------------------------------------------------
        # FINALIZE PRIVATE CONFIRMATION
        # ----------------------------------------------------

        try:
            await interaction.edit_original_response(
                content=(
                    f"Submitted **{title} "
                    f"({year})**.\n"
                    f"Status: **{status_text}**."
                    + (
                        f"\nSeerr request: `#{request_id}`"
                        if request_id is not None
                        else ""
                    )
                    + (
                        "\nSonarr is handling the exact missing episodes."
                        if accepted_direct
                        else ""
                    )
                ),
                embed=None,
                view=None
            )

        except Exception as exc:
            log_exception(
                (
                    "Request succeeded but "
                    "private confirmation cleanup "
                    f"failed request_id={request_id or tracking_request_id}"
                ),
                exc
            )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if getattr(self, "_submitting", False) or self.finished:
            await interaction.response.defer()
            return
        self._submitting = True
        self.finished = True
        self.stop()

        title = media_title(
            self.item
        )

        year = media_year(
            self.item
        )

        await interaction.response.edit_message(
            content=(
                f"Cancelled **{title} "
                f"({year})**."
            ),
            embed=None,
            view=None
        )

        await cleanup_unsuccessful_request(
            origin_message=self.origin_message,
            command_message=self.command_message,
            batch_state=self.batch_state,
            batch_card_id=self.batch_card_id,
        )

    async def on_timeout(self):
        if self.finished or getattr(self, "_submitting", False):
            return

        title = media_title(
            self.item
        )

        year = media_year(
            self.item
        )

        await cleanup_unsuccessful_request(
            origin_message=self.origin_message,
            command_message=self.command_message,
            batch_state=self.batch_state,
            batch_card_id=self.batch_card_id,
        )
        await delete_message_safely(
            self.confirm_message,
            label=f"expired confirmation for {title} ({year})",
        )


class SeasonSelectionView(LoggedView):
    PAGE_SIZE = 25

    def __init__(
        self,
        *,
        requester_id,
        item,
        details,
        seerr_user_id,
        seerr_username,
        origin_message,
        seasons,
        season_catalog=None,
        blocked_seasons=None,
        missing_episodes=None,
        direct_episode_requests=None,
        command_message=None,
        batch_state=None,
        batch_card_id=None,
    ):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.item = item
        self.details = details
        self.seerr_user_id = int(seerr_user_id)
        self.seerr_username = str(seerr_username)
        self.origin_message = origin_message
        self.seasons = tuple(sorted({int(value) for value in seasons}))
        self.selected = set()
        self.season_catalog = {
            int(season): max(0, int(count))
            for season, count in (season_catalog or {}).items()
        }
        self.blocked_seasons = dict(blocked_seasons or {})
        self.missing_episodes = {
            int(season): tuple(episodes)
            for season, episodes in (missing_episodes or {}).items()
        }
        self.direct_episode_requests = {
            int(season): tuple(episodes)
            for season, episodes in (direct_episode_requests or {}).items()
        }
        self.command_message = command_message
        self.page = 0
        self.batch_state = batch_state
        self.batch_card_id = batch_card_id
        self.finished = False
        self.submitting = False
        self.action_lock = asyncio.Lock()
        self.rebuild_controls()

    @property
    def page_count(self):
        return max(1, (len(self.seasons) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def page_seasons(self):
        start = self.page * self.PAGE_SIZE
        return self.seasons[start:start + self.PAGE_SIZE]

    def season_description(self, season):
        if self.missing_episodes.get(season):
            missing = self.missing_episodes[season]
            is_empty = (
                self.season_catalog.get(season, 0) > 0
                and len(set(missing)) >= self.season_catalog[season]
            )
            if season in self.direct_episode_requests:
                prefix = "Repair - missing "
            else:
                prefix = "Empty - request " if is_empty else "Partial - missing "
            value = prefix + compact_episode_ranges(missing)
            return value[:100]

        if season in self.missing_episodes:
            return "Partially available; request missing episodes"

        return None

    def build_embed(self):
        selected = compact_number_ranges(self.selected, prefix="S")
        embed = build_media_embed(
            self.item,
            self.details,
            heading="Choose Seasons",
            state_text=(
                f"Request as **{self.seerr_username}**\n"
                f"Selected **{len(self.selected)} of {len(self.seasons)}** "
                f"requestable season(s): `{selected}`\n"
                f"Already handled or unreleased: **{len(self.blocked_seasons)}**"
            ),
            color=discord.Color.gold(),
        )

        page_lines = []
        for season in self.page_seasons():
            detail = self.season_description(season) or "Full - request whole season"
            page_lines.append(f"**S{season}** - {detail}")
        for index, chunk in enumerate(compact_embed_chunks(page_lines), start=1):
            embed.add_field(
                name=(
                    f"Requestable on page {self.page + 1}"
                    if index == 1
                    else f"Requestable on page {self.page + 1} (continued)"
                ),
                value=chunk,
                inline=False,
            )

        blocked_by_status = {}
        for season, status in sorted(self.blocked_seasons.items()):
            blocked_by_status.setdefault(str(status), []).append(season)
        blocked_lines = [
            f"**{status} ({len(seasons)})** - {compact_number_ranges(seasons, prefix='S')}"
            for status, seasons in blocked_by_status.items()
        ]
        for index, chunk in enumerate(
            compact_embed_chunks(blocked_lines, max_chunks=4),
            start=1,
        ):
            embed.add_field(
                name="Already handled" if index == 1 else "Already handled (continued)",
                value=chunk,
                inline=False,
            )
        embed.set_footer(
            text=(
                f"Season page {self.page + 1}/{self.page_count} - "
                "Full = whole season; Empty = nothing present; Partial/Repair = "
                "only missing episodes"
            )
        )
        return embed

    def rebuild_controls(self):
        self.clear_items()
        current = self.page_seasons()
        selector = discord.ui.Select(
            placeholder=f"Seasons - page {self.page + 1}/{self.page_count}",
            min_values=0,
            max_values=len(current),
            options=[
                discord.SelectOption(
                    label=f"Season {season}",
                    value=str(season),
                    default=season in self.selected,
                    description=self.season_description(season),
                )
                for season in current
            ],
            row=0,
        )

        async def choose_page(interaction):
            async with self.action_lock:
                if self.submitting or self.finished:
                    await interaction.response.defer()
                    return
                current_set = set(current)
                self.selected.difference_update(current_set)
                self.selected.update(int(value) for value in selector.values)
                self.rebuild_controls()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

        selector.callback = choose_page
        self.add_item(selector)

        previous = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self.page == 0,
            row=1,
        )
        next_page = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.page_count - 1,
            row=1,
        )
        select_all = discord.ui.Button(
            label="Select All",
            style=discord.ButtonStyle.primary,
            row=2,
        )
        select_none = discord.ui.Button(
            label="Select None",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        proceed = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.success,
            row=3,
        )
        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            row=3,
        )

        async def go_previous(interaction):
            async with self.action_lock:
                if self.submitting or self.finished:
                    await interaction.response.defer()
                    return
                self.page -= 1
                self.rebuild_controls()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

        async def go_next(interaction):
            async with self.action_lock:
                if self.submitting or self.finished:
                    await interaction.response.defer()
                    return
                self.page += 1
                self.rebuild_controls()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

        async def choose_all(interaction):
            async with self.action_lock:
                if self.submitting or self.finished:
                    await interaction.response.defer()
                    return
                self.selected = set(self.seasons)
                self.rebuild_controls()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

        async def choose_none(interaction):
            async with self.action_lock:
                if self.submitting or self.finished:
                    await interaction.response.defer()
                    return
                self.selected.clear()
                self.rebuild_controls()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

        async def proceed_locked(interaction):
            async with self.action_lock:
                await self.continue_request(interaction)

        async def cancel_locked(interaction):
            async with self.action_lock:
                await self.cancel_request(interaction)

        previous.callback = go_previous
        next_page.callback = go_next
        select_all.callback = choose_all
        select_none.callback = choose_none
        proceed.callback = proceed_locked
        cancel.callback = cancel_locked

        for control in (previous, next_page, select_all, select_none, proceed, cancel):
            self.add_item(control)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return await renew_transient_interaction(
                interaction, self.origin_message
            )

        await interaction.response.send_message(
            "This season selector belongs to someone else.",
            ephemeral=True,
        )
        return False

    async def continue_request(self, interaction):
        if self.submitting or self.finished:
            await interaction.response.defer()
            return
        if not self.selected:
            await interaction.response.send_message(
                "Select at least one season before continuing.",
                ephemeral=True,
            )
            return

        self.submitting = True
        self.finished = True
        self.stop()
        selected = sorted(self.selected)
        confirm_view = ConfirmRequestView(
            requester_id=self.requester_id,
            item=self.item,
            details=self.details,
            seerr_user_id=self.seerr_user_id,
            origin_message=self.origin_message,
            seasons=selected,
            direct_episode_requests={
                season: self.direct_episode_requests[season]
                for season in selected
                if season in self.direct_episode_requests
            },
            command_message=self.command_message,
            batch_state=self.batch_state,
            batch_card_id=self.batch_card_id,
        )
        confirmation_embed = build_media_embed(
            self.item,
            self.details,
            heading="Request As",
            state_text=(
                f"**{self.seerr_username}**\n\n"
                f"This will submit **{len(selected)} season selection(s)**: "
                f"`{compact_number_ranges(selected, prefix='S')}`"
            ),
            color=discord.Color.gold(),
        )
        scope_lines = []
        for season in selected:
            episodes = self.direct_episode_requests.get(season)
            if episodes:
                scope_lines.append(
                    f"**S{season} exact repair** - {compact_episode_ranges(episodes)}"
                )
            else:
                detail = self.season_description(season)
                suffix = " (currently empty)" if detail and detail.startswith("Empty") else ""
                scope_lines.append(f"**S{season} whole season**{suffix}")
        for index, chunk in enumerate(compact_embed_chunks(scope_lines), start=1):
            confirmation_embed.add_field(
                name="Provider payload" if index == 1 else "Provider payload (continued)",
                value=chunk,
                inline=False,
            )
        confirm_view.confirm_message = interaction.message
        await interaction.response.edit_message(
            content="**Confirm this request.**",
            embed=confirmation_embed,
            view=confirm_view,
        )

    async def cancel_request(self, interaction):
        if self.submitting or self.finished:
            await interaction.response.defer()
            return
        self.submitting = True
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content=f"Cancelled **{media_title(self.item)} ({media_year(self.item)})**.",
            embed=None,
            view=None,
        )

        await cleanup_unsuccessful_request(
            origin_message=self.origin_message,
            command_message=self.command_message,
            batch_state=self.batch_state,
            batch_card_id=self.batch_card_id,
        )

    async def on_timeout(self):
        if self.finished or self.submitting:
            return

        await cleanup_unsuccessful_request(
            origin_message=self.origin_message,
            command_message=self.command_message,
            batch_state=self.batch_state,
            batch_card_id=self.batch_card_id,
        )


class UnavailableRatingView(LoggedView):
    def __init__(
        self,
        *,
        requester_id,
        item,
        details,
        rating,
        genres,
        command_message,
        trakt_sync_available=False,
    ):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.item = item
        self.details = details
        self.rating = int(rating)
        self.genres = tuple(genres)
        self.command_message = command_message
        self.trakt_sync_available = bool(trakt_sync_available)
        self.finished = False
        self.submitting = False
        self.message = None
        if not self.trakt_sync_available:
            self.remove_item(self.sync_to_trakt)

    def rating_embed(self, state_text=None, color=None):
        return build_media_embed(
            self.item,
            self.details,
            heading="Rating",
            state_text=(
                state_text
                or (
                    f"**{self.rating}/10 saved locally.**\n"
                    "This title is not in Jellyfin. Request it, keep the "
                    "local rating, or dismiss it."
                    + (
                        " You can also sync it directly to Trakt."
                        if self.trakt_sync_available
                        else ""
                    )
                )
            ),
            color=color or discord.Color.gold(),
        )

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return await renew_transient_interaction(
                interaction,
                self.message or getattr(interaction, "message", None),
            )

        await interaction.response.send_message(
            "This rating belongs to someone else.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Request", style=discord.ButtonStyle.success)
    async def request_media(self, interaction, button):
        if self.submitting or self.finished:
            await interaction.response.defer()
            return
        self.submitting = True
        link = get_link(interaction.user.id)
        if not link:
            self.submitting = False
            await interaction.response.send_message(
                "Your Discord account is not linked to a Seerr user. "
                "An administrator can run `$admin link @you YourSeerrUsername`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        seasons = None
        season_catalog = {}
        blocked_seasons = {}
        missing_episodes = {}
        direct_episode_requests = {}
        if self.item["mediaType"] == "tv":
            seasons, blocked_seasons, season_catalog, missing_episodes, direct_episode_requests = (
                await tv_season_request_inventory(self.item, self.details)
            )
            if not seasons:
                self.submitting = False
                await interaction.followup.send(
                    "Seerr returned no requestable seasons.",
                    ephemeral=True,
                )
                return

        selected_embed = build_media_embed(
            self.item,
            self.details,
            heading="Selection",
            state_text="**Selected from a local rating. Awaiting request confirmation.**",
            color=discord.Color.gold(),
        )
        try:
            request_card = await interaction.channel.send(
                embed=selected_embed,
                view=MediaLinksView(self.item),
            )
            lifecycle = register_transient_card(
                message=request_card,
                command_message=None,
                kind="rating_request",
            )
            if lifecycle is None:
                await delete_message_safely(
                    request_card,
                    label="unregistered rating request card",
                )
                raise RuntimeError("The request card lifecycle could not be registered.")
        except Exception as exc:
            self.submitting = False
            error_id = log_exception("Could not open rating request card", exc)
            await interaction.followup.send(
                "The rating is safe, but the request card could not be "
                f"opened. Try **Request** again. Error ID: `{error_id}`",
                ephemeral=True,
            )
            return

        common_args = {
            "requester_id": interaction.user.id,
            "item": self.item,
            "details": self.details,
            "seerr_user_id": link["seerr_user_id"],
            "origin_message": request_card,
            # The $rate command and its committed receipt are successful state;
            # a later request cancellation must never delete either one.
            "command_message": None,
        }
        if seasons is not None:
            next_view = SeasonSelectionView(
                **common_args,
                seerr_username=link["seerr_username"],
                seasons=seasons,
                season_catalog=season_catalog,
                blocked_seasons=blocked_seasons,
                missing_episodes=missing_episodes,
                direct_episode_requests=direct_episode_requests,
            )
            prompt = "**Choose the seasons to request.**"
            prompt_embed = next_view.build_embed()
        else:
            next_view = ConfirmRequestView(**common_args)
            prompt = "**Confirm this request.**"
            prompt_embed = build_media_embed(
                self.item,
                self.details,
                heading="Request As",
                state_text=f"**{link['seerr_username']}**",
                color=discord.Color.gold(),
            )

        confirmation_message = await interaction.followup.send(
            content=prompt,
            embed=prompt_embed,
            view=next_view,
            ephemeral=True,
            wait=True,
        )
        if isinstance(next_view, ConfirmRequestView):
            next_view.confirm_message = confirmation_message
        self.finished = True
        self.stop()
        mark_transient_message_terminal(interaction.message, "kept")
        await interaction.message.edit(
            embed=self.rating_embed(
                state_text=(
                    f"**{self.rating}/10 saved locally.**\n"
                    "A separate request card was opened below. Cancelling that "
                    "request will not remove this rating."
                ),
                color=discord.Color.green(),
            ),
            view=MediaLinksView(self.item),
        )

    @discord.ui.button(label="Sync to Trakt", style=discord.ButtonStyle.primary)
    async def sync_to_trakt(self, interaction, button):
        if self.submitting or self.finished:
            await interaction.response.defer()
            return
        self.submitting = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        taste_user = await configured_taste_user_for(interaction.user)
        if not taste_user:
            self.submitting = False
            await interaction.followup.send(
                "Trakt sync is restricted to the configured owner taste profile.",
                ephemeral=True,
            )
            return

        try:
            await jellyfin.trakt_rate_external(
                taste_user["Id"],
                media_type=self.item["mediaType"],
                tmdb_id=self.item["id"],
                title=media_title(self.item),
                year=media_year(self.item),
                rating=self.rating,
            )
        except Exception as exc:
            self.submitting = False
            error_id = log_exception(
                f"External Trakt rating failed tmdb_id={self.item['id']}",
                exc,
            )
            await interaction.followup.send(
                "Trakt sync failed; the local rating is untouched. "
                f"Error ID: `{error_id}`",
                ephemeral=True,
            )
            return

        save_rating(
            discord_user_id=self.requester_id,
            media_type=self.item["mediaType"],
            tmdb_id=self.item["id"],
            title=media_title(self.item),
            year=media_year(self.item),
            rating=self.rating,
            genres=self.genres,
            trakt_synced=True,
        )
        self.finished = True
        self.stop()
        mark_transient_message_terminal(interaction.message, "kept")
        await interaction.message.edit(
            embed=self.rating_embed(
                state_text=f"**{self.rating}/10 saved locally and synced to Trakt.**",
                color=discord.Color.green(),
            ),
            view=MediaLinksView(self.item),
        )
        await interaction.followup.send("Rating synced to Trakt.", ephemeral=True)

    @discord.ui.button(label="Keep Local", style=discord.ButtonStyle.secondary)
    async def keep_local(self, interaction, button):
        if self.submitting or self.finished:
            await interaction.response.defer()
            return
        self.submitting = True
        self.finished = True
        self.stop()
        mark_transient_message_terminal(interaction.message, "kept")
        await interaction.response.edit_message(
            embed=self.rating_embed(
                state_text=f"**{self.rating}/10 saved locally.**",
                color=discord.Color.blurple(),
            ),
            view=MediaLinksView(self.item),
        )

    @discord.ui.button(label="Dismiss Rating", style=discord.ButtonStyle.danger)
    async def dismiss_rating(self, interaction, button):
        if self.submitting or self.finished:
            await interaction.response.defer()
            return
        self.submitting = True
        delete_rating(
            discord_user_id=self.requester_id,
            media_type=self.item["mediaType"],
            tmdb_id=self.item["id"],
        )
        self.finished = True
        self.stop()
        await interaction.response.defer()
        await cleanup_unsuccessful_request(
            origin_message=interaction.message,
            command_message=self.command_message,
        )

    async def on_timeout(self):
        if self.finished or self.submitting:
            return
        self.finished = True
        if self.message:
            try:
                await self.message.edit(
                    embed=self.rating_embed(
                        state_text=f"**{self.rating}/10 saved locally.**",
                        color=discord.Color.blurple(),
                    ),
                    view=MediaLinksView(self.item),
                )
                mark_transient_message_terminal(self.message, "kept")
            except Exception as exc:
                logger.info("Could not retire expired rating actions: %s", exc)


# ============================================================
# SEARCH RESULT BUTTON
# ============================================================

class RankedBatchState:
    """Coordinate independent recommendation cards from one command."""

    TERMINAL_STATES = {
        "dismissed",
        "kept",
        "requested",
    }

    def __init__(self, *, origin_message, card_ids):
        self.origin_message = origin_message
        self.states = {
            card_id: "active"
            for card_id in card_ids
        }
        self.lock = asyncio.Lock()
        self.origin_deleted = False

    async def set_state(self, card_id, state):
        should_delete_origin = False

        async with self.lock:
            current = self.states.get(card_id)

            if current is None:
                return

            if current in self.TERMINAL_STATES:
                return

            self.states[card_id] = state

            if (
                not self.origin_deleted
                and self.states
                and all(
                    value == "dismissed"
                    for value in self.states.values()
                )
            ):
                self.origin_deleted = True
                should_delete_origin = True

        if not should_delete_origin:
            return

        try:
            await self.origin_message.delete()
        except Exception as exc:
            logger.warning(
                "Could not delete fully dismissed recommendation command "
                "message=%s error=%s",
                getattr(self.origin_message, "id", None),
                exc,
            )

class SearchResultButton(
    discord.ui.Button
):
    def __init__(
        self,
        slot: int
    ):
        super().__init__(
            label=str(slot + 1),
            style=(
                discord.ButtonStyle.primary
            ),
            row=0
        )

        self.slot = slot

    async def callback(
        self,
        interaction: discord.Interaction
    ):
        view = self.view

        if not isinstance(
            view,
            SearchResultsView
        ):
            return

        async with view.action_lock:
            clicked_id = (getattr(interaction, "data", None) or {}).get("custom_id")
            if clicked_id and clicked_id != view.slot_custom_id(self.slot):
                await interaction.response.send_message(
                    "That result page changed before the click completed. Choose again.",
                    ephemeral=True,
                )
                return
            await view.select_slot(
                interaction,
                self.slot
            )


# ============================================================
# SEARCH RESULTS VIEW
# ============================================================

class SearchResultsView(
    LoggedView
):
    def __init__(
        self,
        *,
        requester_id: int,
        query: str,
        results: list,
        seerr_page: int,
        total_seerr_pages: int,
        target_year: str | None = None,
        command_message=None,
        batch_state=None,
        batch_card_id=None,
    ):
        super().__init__(
            timeout=REQUEST_UI_TIMEOUT
        )

        self.requester_id = (
            requester_id
        )

        self.query = query
        self.target_year = str(target_year) if target_year else None

        self.results = []
        self.seen = set()

        self.seerr_page = (
            seerr_page
        )

        self.total_seerr_pages = (
            total_seerr_pages
        )

        self.display_page = 0
        self.view_token = secrets.token_hex(6)

        self.message = None
        self.finished = False
        self.selecting = False
        self.loading_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()
        self.command_message = command_message
        self.batch_state = batch_state
        self.batch_card_id = batch_card_id

        self._append_results(
            results
        )

        self.result_buttons = []

        for slot in range(
            RESULTS_PER_PAGE
        ):
            button = SearchResultButton(
                slot
            )

            self.result_buttons.append(
                button
            )

            self.add_item(button)

        self.prev_button = (
            discord.ui.Button(
                label="◀ Previous",
                style=(
                    discord.ButtonStyle.secondary
                ),
                row=1
            )
        )

        self.next_button = (
            discord.ui.Button(
                label="Next ▶",
                style=(
                    discord.ButtonStyle.secondary
                ),
                row=1
            )
        )

        self.cancel_button = (
            discord.ui.Button(
                label="Cancel",
                style=(
                    discord.ButtonStyle.danger
                ),
                row=1
            )
        )

        self.prev_button.callback = (
            self.previous_page
        )

        self.next_button.callback = (
            self.next_page
        )

        self.cancel_button.callback = (
            self.cancel_search
        )

        self.add_item(
            self.prev_button
        )

        self.add_item(
            self.next_button
        )

        self.add_item(
            self.cancel_button
        )

        self.refresh_controls()

    def _append_results(
        self,
        items
    ):
        for item in items:
            key = (
                item.get("mediaType"),
                item.get("id")
            )

            if key in self.seen:
                continue

            self.seen.add(key)

            self.results.append(item)

        self.results = prioritize_search_results(
            self.results,
            query=self.query,
            year=self.target_year,
        )

    def page_items(self):
        start = (
            self.display_page
            * RESULTS_PER_PAGE
        )

        end = (
            start
            + RESULTS_PER_PAGE
        )

        return self.results[
            start:end
        ]

    def slot_custom_id(self, slot):
        return f"mb:search:{self.view_token}:{self.display_page}:{int(slot)}"

    async def ensure_loaded(
        self,
        display_page: int
    ):
        async with self.loading_lock:
            needed = (
                display_page + 1
            ) * RESULTS_PER_PAGE

            while (
                len(self.results) < needed
                and self.seerr_page
                < self.total_seerr_pages
            ):
                self.seerr_page += 1

                new_items, total_pages = (
                    await fetch_search_page(
                        self.query,
                        self.seerr_page
                    )
                )

                self.total_seerr_pages = (
                    total_pages
                )

                self._append_results(
                    new_items
                )

    def has_next(self):
        current_end = (
            self.display_page + 1
        ) * RESULTS_PER_PAGE

        if current_end < len(
            self.results
        ):
            return True

        return (
            self.seerr_page
            < self.total_seerr_pages
        )

    def refresh_controls(self):
        items = self.page_items()

        for slot, button in enumerate(
            self.result_buttons
        ):
            button.custom_id = self.slot_custom_id(slot)
            button.label = str(
                slot + 1
            )

            button.disabled = (
                slot >= len(items)
            )

        self.prev_button.disabled = (
            self.display_page == 0
        )

        self.next_button.disabled = (
            not self.has_next()
        )

    def build_embed(self):
        items = self.page_items()

        embed = discord.Embed(
            title=compact_embed_title(
                f'Search results for '
                f'"{self.query}'
                + (f' ({self.target_year})' if self.target_year else '')
                + '"'
            ),
            color=discord.Color.blurple()
        )

        blocks = []

        for index, item in enumerate(
            items,
            start=1
        ):
            title = media_title(item)
            year = media_year(item)

            media_type = (
                item.get(
                    "mediaType",
                    "unknown"
                )
                .upper()
            )

            status = media_status(
                item
            )

            rating = rating_text(
                item
            )

            blocks.append(
                (
                    f"**{index}. "
                    f"{title} ({year})**\n"
                    f"{media_type} • "
                    f"{rating} • "
                    f"{status}\n"
                    f"*{short_overview(item)}*"
                )
            )

        embed.description = (
            "\n\n".join(blocks)
            if blocks
            else "No results on this page."
        )

        embed.set_footer(
            text=(
                f"Page "
                f"{self.display_page + 1} "
                f"• expires after "
                f"{REQUEST_UI_TIMEOUT // 60} "
                "minutes of inactivity "
                "• only the requester "
                "can use these buttons"
            )
        )

        return embed

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):
        if (
            interaction.user.id
            != self.requester_id
        ):
            await interaction.response.send_message(
                (
                    "These search results "
                    "belong to someone else."
                ),
                ephemeral=True
            )

            return False

        return await renew_transient_interaction(
            interaction,
            self.message or getattr(interaction, "message", None),
        )

    async def set_ranked_batch_state(self, state):
        batch_state = getattr(self, "batch_state", None)
        card_id = getattr(self, "batch_card_id", None)

        if batch_state is not None and card_id is not None:
            await batch_state.set_state(card_id, state)
        if state == "kept":
            mark_transient_message_terminal(self.message, "kept")
        elif state == "requested":
            mark_transient_message_terminal(self.message, "accepted")
        elif state == "pending":
            touch_transient_message(self.message)

    async def previous_page(
        self,
        interaction: discord.Interaction
    ):
        async with self.action_lock:
            await self._previous_page_locked(interaction)

    async def _previous_page_locked(self, interaction):
        if self.display_page <= 0:
            await interaction.response.defer()
            return

        self.display_page -= 1

        self.refresh_controls()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self
        )

    async def next_page(
        self,
        interaction: discord.Interaction
    ):
        async with self.action_lock:
            await self._next_page_locked(interaction)

    async def _next_page_locked(self, interaction):
        target = (
            self.display_page + 1
        )

        await interaction.response.defer()

        try:
            await self.ensure_loaded(
                target
            )

        except Exception as exc:
            await interaction.followup.send(
                (
                    "Couldn't fetch the next "
                    "Seerr search page:\n"
                    f"```text\n{exc}\n```"
                ),
                ephemeral=True
            )

            return

        start = (
            target
            * RESULTS_PER_PAGE
        )

        if start >= len(
            self.results
        ):
            await interaction.followup.send(
                "No more results.",
                ephemeral=True
            )

            self.refresh_controls()

            await interaction.message.edit(
                embed=self.build_embed(),
                view=self
            )

            return

        self.display_page = target

        self.refresh_controls()

        await interaction.message.edit(
            embed=self.build_embed(),
            view=self
        )

    async def cancel_search(
        self,
        interaction: discord.Interaction
    ):
        async with self.action_lock:
            await self._cancel_search_locked(interaction)

    async def _cancel_search_locked(self, interaction):
        if (
            self.finished
            or self.selecting
            or getattr(self, "submitting", False)
        ):
            await interaction.response.defer()
            return
        self.selecting = True
        self.finished = True
        self.stop()
        await interaction.response.defer()
        await cleanup_unsuccessful_request(
            origin_message=interaction.message,
            command_message=self.command_message,
            batch_state=self.batch_state,
            batch_card_id=self.batch_card_id,
        )

    async def select_slot(
        self,
        interaction: discord.Interaction,
        slot: int
    ):
        """Open one result while keeping provider/UI failures retryable."""

        try:
            await self._select_slot_once(interaction, slot)
        except Exception as exc:
            if self.finished:
                raise

            self.selecting = False
            self.refresh_controls()
            error_id = log_exception(
                f"Could not open search result query={self.query!r} slot={slot}",
                exc,
            )
            try:
                if interaction.response.is_done():
                    await interaction.message.edit(embed=self.build_embed(), view=self)
                    await interaction.followup.send(
                        "Couldn't inspect that result. The search is still open, "
                        f"so you can retry or cancel. Error ID: `{error_id}`",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "Couldn't inspect that result. The search is still open, "
                        f"so you can retry or cancel. Error ID: `{error_id}`",
                        ephemeral=True,
                    )
            except Exception as notify_exc:
                logger.info(
                    "Could not restore failed result selection UI error_id=%s: %s",
                    error_id,
                    notify_exc,
                )

    async def _select_slot_once(
        self,
        interaction: discord.Interaction,
        slot: int
    ):
        items = self.page_items()

        if self.finished or slot >= len(items):
            await interaction.response.send_message(
                "That result no longer exists.",
                ephemeral=True
            )

            return

        if self.selecting:
            await interaction.response.send_message(
                "A result from this search is already being opened.",
                ephemeral=True,
            )
            return

        self.selecting = True

        item = items[slot]

        title = media_title(item)
        year = media_year(item)

        if is_blocklisted(item):
            await interaction.response.defer(ephemeral=True)
            details = await fetch_media_details(item)
            self.finished = True
            self.stop()
            embed = build_media_embed(
                item,
                details,
                heading="Unavailable",
                state_text="**Blocklisted in Seerr.** An administrator must clear it first.",
                color=discord.Color.red(),
            )
            await interaction.message.edit(embed=embed, view=MediaLinksView(item))
            await self.set_ranked_batch_state("kept")
            try:
                await interaction.followup.send(
                    f"**{title} ({year})** is blocklisted and cannot be requested.",
                    ephemeral=True,
                )
            except Exception as exc:
                logger.info("Could not send blocklist acknowledgement: %s", exc)
            return

        # Available movies do not need a Seerr user link. TV is evaluated at
        # the season level because a series can be available while a newer
        # season remains requestable.
        if item.get("mediaType") == "movie" and already_available(item):
            await interaction.response.defer(
                ephemeral=True
            )

            details = (
                await fetch_media_details(
                    item
                )
            )

            self.finished = True
            self.stop()

            embed = build_media_embed(
                item,
                details,
                heading="Library",
                state_text=(
                    "**Already available.**"
                ),
                color=discord.Color.green()
            )

            await interaction.message.edit(
                embed=embed,
                view=MediaLinksView(item)
            )

            await self.set_ranked_batch_state("kept")
            try:
                await interaction.followup.send(
                    (
                        f"**{title} ({year})** "
                        "is already available."
                    ),
                    ephemeral=True
                )
            except Exception as exc:
                logger.info("Could not send library acknowledgement: %s", exc)

            return

        if item.get("mediaType") == "movie" and already_underway(item):
            await interaction.response.defer(
                ephemeral=True
            )

            details = (
                await fetch_media_details(
                    item
                )
            )

            self.finished = True
            self.stop()

            embed = build_media_embed(
                item,
                details,
                heading="Request",
                state_text=(
                    "**Already requested, "
                    "processing, or partially "
                    "available.**"
                ),
                color=discord.Color.gold()
            )

            await interaction.message.edit(
                embed=embed,
                view=MediaLinksView(item)
            )

            await self.set_ranked_batch_state("kept")
            try:
                await interaction.followup.send(
                    (
                        f"**{title} ({year})** "
                        "is already in progress."
                    ),
                    ephemeral=True
                )
            except Exception as exc:
                logger.info("Could not send request-status acknowledgement: %s", exc)

            return

        link = get_link(
            interaction.user.id
        )

        if not link:
            self.selecting = False
            await interaction.response.send_message(
                (
                    "Your Discord account is "
                    "not linked to a Seerr "
                    "user yet.\n\n"
                    "An administrator needs "
                    "to run:\n"
                    "`$admin link @you "
                    "YourSeerrUsername`"
                ),
                ephemeral=True
            )

            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        details = (
            await fetch_media_details(
                item
            )
        )

        seasons = None
        season_catalog = {}
        blocked_seasons = {}
        missing_episodes = {}
        direct_episode_requests = {}

        if item["mediaType"] == "tv":
            seasons, blocked_seasons, season_catalog, missing_episodes, direct_episode_requests = (
                await tv_season_request_inventory(item, details)
            )

            if not seasons:
                self.finished = True
                self.stop()
                status_text = ", ".join(
                    f"S{season} {status}"
                    for season, status in sorted(blocked_seasons.items())
                ) or "Every published season is already handled"
                handled_embed = build_media_embed(
                    item,
                    details,
                    heading="Request",
                    state_text=(
                        "**No unrequested seasons remain.**\n"
                        f"{status_text}"
                    ),
                    color=discord.Color.green(),
                )
                await interaction.message.edit(
                    embed=handled_embed,
                    view=MediaLinksView(item),
                )
                await self.set_ranked_batch_state("kept")
                try:
                    await interaction.followup.send(
                        (
                            "Every published season is already available, pending, "
                            "processing, or partially available."
                        ),
                        ephemeral=True
                    )
                except Exception as exc:
                    logger.info("Could not send season-status acknowledgement: %s", exc)
                return

        # The original public search message becomes the selected item. Keep
        # the search recoverable until Discord has also created the private
        # confirmation card; otherwise a transient send failure would strand a
        # static "awaiting confirmation" receipt with no buttons.
        selected_embed = build_media_embed(
            item,
            details,
            heading="Selection",
            state_text=(
                "**Selected. Awaiting "
                "request confirmation.**"
            ),
            color=discord.Color.gold()
        )

        selected_embed.set_footer(
            text=(
                f"Selected by "
                f"{interaction.user.display_name}"
            )
        )

        await interaction.message.edit(
            embed=selected_embed,
            view=MediaLinksView(item)
        )

        common_view_args = {
            "requester_id": interaction.user.id,
            "item": item,
            "details": details,
            "seerr_user_id": link["seerr_user_id"],
            "origin_message": interaction.message,
            "command_message": self.command_message,
            "batch_state": getattr(self, "batch_state", None),
            "batch_card_id": getattr(self, "batch_card_id", None),
        }

        if seasons is not None:
            next_view = SeasonSelectionView(
                **common_view_args,
                seerr_username=link["seerr_username"],
                seasons=seasons,
                season_catalog=season_catalog,
                blocked_seasons=blocked_seasons,
                missing_episodes=missing_episodes,
                direct_episode_requests=direct_episode_requests,
            )
            prompt = "**Choose the seasons to request.**"
            next_embed = next_view.build_embed()
        else:
            next_view = ConfirmRequestView(
                **common_view_args,
                seasons=None,
            )
            prompt = "**Confirm this request.**"
            next_embed = build_media_embed(
                item,
                details,
                heading="Request As",
                state_text=f"**{link['seerr_username']}**",
                color=discord.Color.gold(),
            )

        confirmation_message = (
            await interaction.followup.send(
                content=(
                    prompt
                ),
                embed=next_embed,
                view=next_view,
                ephemeral=True,
                wait=True
            )
        )

        if isinstance(next_view, ConfirmRequestView):
            next_view.confirm_message = confirmation_message

        self.finished = True
        self.stop()
        await self.set_ranked_batch_state("pending")

    async def on_timeout(self):
        async with self.action_lock:
            if (
                self.finished
                or self.selecting
                or getattr(self, "submitting", False)
            ):
                return
            self.finished = True
            self.stop()
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
                batch_state=self.batch_state,
                batch_card_id=self.batch_card_id,
            )


async def apply_selected_rating(
    *,
    interaction,
    item,
    numeric_rating,
    command_message,
):
    """Persist one explicitly selected search result and render its outcome."""
    details = await fetch_media_details(item)
    raw_genres = details.get("genres") or []
    genres = [
        str(genre.get("name") if isinstance(genre, dict) else genre)
        for genre in raw_genres
        if genre
    ]
    media_type = item["mediaType"]
    tmdb_id = int(item["id"])
    jellyfin_item = (
        await jellyfin.find_by_tmdb(
            tmdb_id=tmdb_id,
            title=media_title(item),
            media_type=media_type,
        )
        if jellyfin.enabled
        else None
    )
    trakt_synced = False
    sync_note = "Saved locally."
    taste_user = await configured_taste_user_for(interaction.user)

    if taste_user and jellyfin_item:
        try:
            await jellyfin.trakt_rate(
                taste_user["Id"],
                jellyfin_item["Id"],
                numeric_rating,
            )
            trakt_synced = True
            sync_note = "Saved locally and synced to Trakt."
        except Exception as exc:
            logger.info("Trakt rating sync unavailable: %s", exc)
            sync_note = "Saved locally; Trakt sync is currently unavailable."
    elif not jellyfin_item:
        sync_note = "Saved locally; choose what happens next below."

    save_rating(
        discord_user_id=interaction.user.id,
        media_type=media_type,
        tmdb_id=tmdb_id,
        jellyfin_item_id=(jellyfin_item or {}).get("Id"),
        title=media_title(item),
        year=media_year(item),
        rating=numeric_rating,
        genres=genres,
        trakt_synced=trakt_synced,
    )

    durable_rating_actions = False
    if not jellyfin_item:
        durable_rating_actions = transition_transient_message(
            interaction.message,
            "rating_saved_actions",
        )
        result_view = UnavailableRatingView(
            requester_id=interaction.user.id,
            item=item,
            details=details,
            rating=numeric_rating,
            genres=genres,
            command_message=command_message,
            trakt_sync_available=bool(taste_user),
        )
        result_view.message = interaction.message
        result_embed = result_view.rating_embed(
            state_text=(
                None
                if durable_rating_actions
                else f"**{numeric_rating}/10 saved locally.**"
            ),
            color=(
                discord.Color.gold()
                if durable_rating_actions
                else discord.Color.blurple()
            ),
        )
        result_controls = (
            result_view
            if durable_rating_actions
            else MediaLinksView(item)
        )
        if not durable_rating_actions:
            # The rating itself is committed. If durable action tracking could
            # not be established, render a safe static success card.
            mark_transient_message_terminal(interaction.message, "kept")
    else:
        mark_transient_message_terminal(interaction.message, "kept")
        result_embed = build_media_embed(
            item,
            details,
            heading="Rating",
            state_text=f"**{numeric_rating}/10.** {sync_note}",
            color=discord.Color.green(),
        )
        result_controls = JellyfinAvailableView(
            item=item,
            jellyfin_item_id=jellyfin_item["Id"],
        )

    try:
        await interaction.message.edit(
            embed=result_embed,
            view=result_controls,
        )
    except Exception as exc:
        # Persistence already succeeded. Do not bubble this into
        # RatingSearchView and resurrect live search controls.
        log_exception(
            f"Rating saved but result card update failed tmdb_id={tmdb_id}",
            exc,
        )

    try:
        await interaction.followup.send(
            f"Rated **{media_title(item)} ({media_year(item)})** "
            f"**{numeric_rating}/10**. {sync_note}",
            ephemeral=True,
        )
    except Exception as exc:
        # The follow-up is convenience notification, not part of the commit.
        logger.info("Could not send committed rating acknowledgement: %s", exc)


class RatingSearchView(SearchResultsView):
    """Paginated, explicit title selection before a rating is persisted."""

    def __init__(self, *, numeric_rating, **kwargs):
        self.numeric_rating = int(numeric_rating)
        self.submitting = False
        super().__init__(**kwargs)

    def build_embed(self):
        embed = super().build_embed()
        embed.title = f"Choose what to rate - {self.numeric_rating}/10"
        embed.color = discord.Color.gold()
        base_footer = embed.footer.text or ""
        embed.set_footer(
            text=(
                f"Nothing is saved until you choose a result - {base_footer}"
            )[:2048]
        )
        return embed

    async def select_slot(self, interaction, slot):
        items = self.page_items()
        if slot >= len(items):
            await interaction.response.send_message(
                "That result is no longer on this page.",
                ephemeral=True,
            )
            return

        if self.submitting:
            await interaction.response.send_message(
                "That rating is already being saved.",
                ephemeral=True,
            )
            return

        self.submitting = True
        for child in self.children:
            child.disabled = True

        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.message.edit(view=self)

        try:
            await apply_selected_rating(
                interaction=interaction,
                item=items[slot],
                numeric_rating=self.numeric_rating,
                command_message=self.command_message,
            )
        except Exception as exc:
            self.submitting = False
            for child in self.children:
                child.disabled = False
            self.refresh_controls()
            await interaction.message.edit(embed=self.build_embed(), view=self)
            error_id = log_exception(
                f"Rating selection failed tmdb_id={items[slot].get('id')}",
                exc,
            )
            await interaction.followup.send(
                f"Couldn't save that rating. Error ID: `{error_id}`",
                ephemeral=True,
            )
            return

        self.finished = True
        self.stop()


class RecommendationCandidateView(SearchResultsView):
    def __init__(
        self,
        *,
        requester_id: int,
        pick,
        details_by_key: dict,
        command_message=None,
        batch_state=None,
        batch_card_id=None,
    ):
        self.pick = pick
        self.details_by_key = details_by_key

        filter_text = pick.options.media_type or "movie/show"

        if pick.genre_name:
            filter_text += f" {pick.genre_name}"

        super().__init__(
            requester_id=requester_id,
            query=f"recommendation {filter_text}",
            results=list(pick.items),
            seerr_page=1,
            total_seerr_pages=1,
            command_message=command_message,
            batch_state=batch_state,
            batch_card_id=batch_card_id,
        )

        result_count = len(pick.items)

        for button in self.result_buttons[result_count:]:
            self.remove_item(button)

        self.remove_item(self.prev_button)
        self.remove_item(self.next_button)
        for index, button in enumerate(
            self.result_buttons[:result_count],
            start=1
        ):
            button.label = (
                "Request this"
                if result_count == 1
                else f"Request {index}"
            )

        self.cancel_button.row = 0

    async def on_timeout(self):
        async with self.action_lock:
            if self.finished or getattr(self, "submitting", False) or self.selecting:
                return
            self.finished = True
            self.stop()
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
                batch_state=self.batch_state,
                batch_card_id=self.batch_card_id,
            )


class RecommendationCardView(RecommendationCandidateView):
    def __init__(
        self,
        *,
        requester_id: int,
        batch,
        details_by_key: dict,
        reasons: dict | None = None,
        signals: dict | None = None,
        batch_state=None,
        batch_card_id=None,
    ):
        self.reasons = reasons or {}
        self.signals = signals or {}
        self.batch_state = batch_state
        self.batch_card_id = batch_card_id
        super().__init__(
            requester_id=requester_id,
            pick=batch,
            details_by_key=details_by_key,
            batch_state=batch_state,
            batch_card_id=batch_card_id,
        )

        if self.batch_state is not None:
            self.cancel_button.label = "Dismiss"

    def build_embed(self):
        item = self.pick.items[0]
        key = (item.get("mediaType"), item.get("id"))
        details = self.details_by_key.get(key, item)
        reason = self.reasons.get(key)
        random_text = (
            "Randomized within the ranked pool."
            if self.pick.options.randomize
            else "Highest-ranked eligible result."
        )
        state_lines = [random_text]

        if reason:
            state_lines.append(f"**Why:** {reason}")

        state_lines.append(
            "Click **Request this** to continue to the normal confirmation step."
        )
        embed = build_media_embed(
            item,
            details,
            heading="Recommendation",
            state_text="\n".join(state_lines),
            color=discord.Color.magenta(),
        )
        footer = f"{self.pick.eligible_count} unseen requestable match(es) considered"

        if self.signals:
            rating_count = int(
                self.signals.get(
                    "effective_ratings",
                    self.signals.get("ratings", 0),
                )
            )
            history_count = int(self.signals.get("jellyfin", 0))
            signal_parts = [f"{rating_count} personal rating(s)"]
            history_status = self.signals.get("jellyfin_status") or (
                "connected" if history_count else "no history returned"
            )
            signal_parts.append(
                f"{history_count} Jellyfin taste item(s) ({history_status})"
            )
            trakt_status = self.signals.get("trakt_status") or (
                "connected"
                if self.signals.get("trakt_available")
                else "not connected"
            )
            signal_parts.append(f"Trakt {trakt_status}")
            if not rating_count and not history_count and not self.signals.get("trakt_available"):
                signal_parts.append("community-ranking fallback")
            footer += " • " + " • ".join(signal_parts)

        embed.set_footer(text=footer)
        return embed

    async def cancel_search(self, interaction: discord.Interaction):
        if self.batch_state is None:
            await super().cancel_search(interaction)
            return
        async with self.action_lock:
            if self.finished or self.selecting:
                await interaction.response.defer()
                return
            self.selecting = True
            self.finished = True
            self.stop()
            await interaction.response.defer()
            await cleanup_unsuccessful_request(
                origin_message=interaction.message,
                command_message=self.command_message,
                batch_state=self.batch_state,
                batch_card_id=self.batch_card_id,
            )

    async def on_timeout(self):
        async with self.action_lock:
            if self.finished or self.selecting:
                return
            self.finished = True
            self.stop()
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
                batch_state=self.batch_state,
                batch_card_id=self.batch_card_id,
            )



# ============================================================
# SOULSYNC MUSIC REQUEST UI
# ============================================================

def music_artists(track):
    artists = (
        track.get("artists")
        or track.get("artist")
        or track.get("albumArtist")
        or track.get("album_artist")
        or []
    )

    if isinstance(artists, dict):
        artists = [artists]

    if isinstance(artists, str):
        return artists.strip() or "Unknown Artist"

    names = []
    for artist in artists:
        value = artist.get("name") if isinstance(artist, dict) else artist
        if value:
            names.append(str(value).strip())

    return ", ".join(name for name in names if name) or "Unknown Artist"


def music_track_matches(track, *, title, artist):
    """Require both exact title and artist before claiming a track exists."""

    candidate_title = str(track.get("name") or track.get("title") or "")
    if " ".join(candidate_title.split()).casefold() != " ".join(str(title).split()).casefold():
        return False

    requested_artist = " ".join(str(artist or "").split()).casefold()
    candidate_artist = " ".join(music_artists(track).split()).casefold()
    if not requested_artist or requested_artist == "unknown artist":
        return True
    if not candidate_artist or candidate_artist == "unknown artist":
        return False
    return candidate_artist == requested_artist


def music_request_query(track):
    return f"{music_artists(track)} - {track.get('name') or 'Unknown Track'}"


def music_duration(track):
    try:
        total_seconds = max(0, int(track.get("duration_ms") or 0) // 1000)
    except (TypeError, ValueError):
        return "Unknown length"

    if not total_seconds:
        return "Unknown length"

    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def build_music_embed(track, *, heading, state_text, color):
    embed = discord.Embed(
        title=compact_embed_title(
            f"{heading}: {track.get('name') or 'Unknown Track'}"
        ),
        description=state_text,
        color=color,
    )
    embed.add_field(name="Artist", value=music_artists(track), inline=True)
    embed.add_field(
        name="Album",
        value=str(track.get("album") or "Unknown Album"),
        inline=True,
    )
    embed.add_field(name="Length", value=music_duration(track), inline=True)
    embed.add_field(
        name="Released",
        value=str(track.get("release_date") or "Unknown"),
        inline=True,
    )

    image_url = str(track.get("image_url") or "").strip()
    if image_url.startswith(("http://", "https://")):
        embed.set_thumbnail(url=image_url)

    return embed


class MusicLinksView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        if SOULSYNC_PUBLIC_URL:
            self.add_item(
                discord.ui.Button(
                    label="Open SoulSync",
                    style=discord.ButtonStyle.link,
                    url=SOULSYNC_PUBLIC_URL,
                )
            )


class ConfirmMusicRequestView(LoggedView):
    def __init__(self, *, requester_id, track, origin_message, command_message):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.track = track
        self.origin_message = origin_message
        self.command_message = command_message
        self.finished = False
        self.submitting = False
        self.action_lock = asyncio.Lock()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return await renew_transient_interaction(
                interaction, self.origin_message
            )

        await interaction.response.send_message(
            "This music request belongs to someone else.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Request Track", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        if self.submitting or self.finished:
            await interaction.response.defer()
            return

        query = music_request_query(self.track)
        self.submitting = True
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            embed=build_music_embed(
                self.track,
                heading="Music Request",
                state_text="**Checking the library and preparing this request...**",
                color=discord.Color.gold(),
            ),
            view=self,
        )

        try:
            library_matches = await soulsync.library_tracks(
                title=str(self.track.get("name") or ""),
                artist=music_artists(self.track),
                limit=10,
            )
            already_downloaded = any(
                music_track_matches(
                    track,
                    title=str(self.track.get("name") or ""),
                    artist=music_artists(self.track),
                )
                for track in library_matches
            )
        except SoulSyncError as exc:
            logger.info("SoulSync pre-request library check unavailable: %s", exc)
            already_downloaded = False

        if already_downloaded:
            existing = latest_music_request(
                query,
                requester_discord_id=interaction.user.id,
                discord_guild_id=interaction.guild_id,
            )
            if existing:
                update_music_request(
                    local_request_id=existing["local_request_id"],
                    request_status="downloaded",
                    soulsync_request_id=existing.get("soulsync_request_id"),
                )
            else:
                local_request_id = secrets.token_hex(12)
                begin_music_request(
                    local_request_id=local_request_id,
                    display_query=query,
                    requester_discord_id=interaction.user.id,
                    discord_guild_id=interaction.guild_id,
                    discord_channel_id=interaction.channel_id,
                    discord_message_id=interaction.message.id,
                )
                update_music_request(
                    local_request_id=local_request_id,
                    request_status="downloaded",
                )

            self.finished = True
            self.stop()
            mark_transient_message_terminal(self.origin_message, "kept")
            embed = build_music_embed(
                self.track,
                heading="Music Library",
                state_text="**Already downloaded and indexed.**",
                color=discord.Color.green(),
            )
            await interaction.edit_original_response(
                embed=embed,
                view=MusicLinksView(),
            )
            return

        duplicate = recent_music_request(
            query,
            max_age_minutes=1440,
            discord_guild_id=interaction.guild_id,
        )

        if duplicate:
            self.finished = True
            self.stop()
            await interaction.followup.send(
                (
                    f"**{query}** already has a recent music request "
                    f"with status **{duplicate['request_status']}**."
                ),
                ephemeral=True,
            )
            await cleanup_unsuccessful_request(
                origin_message=self.origin_message,
                command_message=self.command_message,
            )
            return

        local_request_id = secrets.token_hex(12)
        begin_music_request(
            local_request_id=local_request_id,
            display_query=query,
            requester_discord_id=interaction.user.id,
            discord_guild_id=interaction.guild_id,
            discord_channel_id=interaction.channel_id,
            discord_message_id=interaction.message.id,
        )

        try:
            result = await soulsync.create_music_request(
                query,
                metadata={
                    "discord_user_id": str(interaction.user.id),
                    "discord_guild_id": str(interaction.guild_id or ""),
                    "discord_channel_id": str(interaction.channel_id or ""),
                    "artist": music_artists(self.track),
                    "title": str(self.track.get("name") or ""),
                    "album": str(self.track.get("album") or "Singles"),
                    "expected_duration_ms": self.track.get("duration_ms"),
                    "external_id": str(self.track.get("id") or ""),
                    "metadata_source": str(
                        self.track.get("_metadata_source") or ""
                    ),
                },
            )
        except SoulSyncError as exc:
            if not exc.definitive:
                error_id = log_exception(
                    f"SoulSync music request was ambiguous query={query!r}",
                    exc,
                )
                update_music_request(
                    local_request_id=local_request_id,
                    request_status="ambiguous",
                    error=f"{type(exc).__name__}: {exc}",
                )
                self.finished = True
                self.stop()
                mark_transient_message_terminal(self.origin_message, "accepted")
                embed = build_music_embed(
                    self.track,
                    heading="Music Request",
                    state_text=(
                        "**SoulSync returned an ambiguous response.**\n"
                        "The request may still have been queued, so MediaBot has "
                        "blocked automatic retries to prevent duplicate downloads.\n\n"
                        f"Check with `$status {self.track.get('name') or query}`."
                    ),
                    color=discord.Color.gold(),
                )
                await interaction.message.edit(embed=embed, view=MusicLinksView())
                await interaction.followup.send(
                    "SoulSync's response was ambiguous; do not retry yet.\n"
                    f"Error ID: `{error_id}`",
                    ephemeral=True,
                )
                return
            error_id = log_exception(
                f"SoulSync rejected music request query={query!r}",
                exc,
            )
            update_music_request(
                local_request_id=local_request_id,
                request_status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            self.finished = True
            self.stop()
            await cleanup_unsuccessful_request(
                origin_message=self.origin_message,
                command_message=self.command_message,
            )
            await interaction.followup.send(
                "SoulSync rejected that request, so nothing was queued and you "
                f"can safely retry. Error ID: `{error_id}`",
                ephemeral=True,
            )
            return
        except Exception as exc:
            error_id = log_exception(
                f"SoulSync music request failed query={query!r}",
                exc,
            )
            update_music_request(
                local_request_id=local_request_id,
                request_status="ambiguous",
                error=f"{type(exc).__name__}: {exc}",
            )
            self.finished = True
            self.stop()
            mark_transient_message_terminal(self.origin_message, "accepted")
            embed = build_music_embed(
                self.track,
                heading="Music Request",
                state_text=(
                    "**SoulSync returned an ambiguous response.**\n"
                    "The request may still have been queued, so MediaBot has "
                    "blocked automatic retries to prevent duplicate downloads.\n\n"
                    f"Check with `$status {self.track.get('name') or query}`."
                ),
                color=discord.Color.gold(),
            )
            await interaction.message.edit(embed=embed, view=MusicLinksView())
            await interaction.followup.send(
                "SoulSync's response was ambiguous; do not retry yet.\n"
                f"Error ID: `{error_id}`",
                ephemeral=True,
            )
            return

        self.finished = True
        self.stop()
        mark_transient_message_terminal(self.origin_message, "accepted")
        request_id = str(result.get("request_id") or "unknown")
        status = str(result.get("status") or "queued").replace("_", " ").title()
        update_music_request(
            local_request_id=local_request_id,
            request_status=str(result.get("status") or "queued"),
            soulsync_request_id=request_id,
        )
        embed = build_music_embed(
            self.track,
            heading="Music Request",
            state_text=(
                f"**{status} in SoulSync.**\n"
                f"Request `{request_id}`\n\n"
                "SoulSync now owns matching, acquisition, organization, and "
                "the Navidrome library refresh."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.message.edit(embed=embed, view=MusicLinksView())

        logger.info(
            "MUSIC REQUEST SUCCESS | discord_user=%s | query=%s | request_id=%s",
            interaction.user.id,
            query,
            request_id,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        if self.submitting or self.finished:
            await interaction.response.defer()
            return
        self.submitting = True
        self.finished = True
        self.stop()
        await interaction.response.defer()
        await cleanup_unsuccessful_request(
            origin_message=self.origin_message,
            command_message=self.command_message,
        )

    async def on_timeout(self):
        if self.finished or self.submitting:
            return
        self.submitting = True
        self.finished = True
        self.stop()
        await cleanup_unsuccessful_request(
            origin_message=self.origin_message,
            command_message=self.command_message,
        )


class MusicResultButton(discord.ui.Button):
    def __init__(self, slot):
        super().__init__(
            label=str(slot + 1),
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.slot = slot

    async def callback(self, interaction):
        async with self.view.action_lock:
            clicked_id = (getattr(interaction, "data", None) or {}).get("custom_id")
            if clicked_id and clicked_id != self.view.slot_custom_id(self.slot):
                await interaction.response.send_message(
                    "That music page changed before the click completed. Choose again.",
                    ephemeral=True,
                )
                return
            await self.view.select_track(interaction, self.slot)


class MusicSearchView(LoggedView):
    def __init__(self, *, requester_id, query, tracks, source, command_message):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.query = query
        self.tracks = list(tracks[:25])
        self.source = str(source or "metadata provider")
        self.command_message = command_message
        self.message = None
        self.finished = False
        self.display_page = 0
        self.view_token = secrets.token_hex(6)
        self.action_lock = asyncio.Lock()
        self.result_buttons = []

        for slot in range(RESULTS_PER_PAGE):
            button = MusicResultButton(slot)
            self.result_buttons.append(button)
            self.add_item(button)

        self.previous_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        self.next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            row=1,
        )

        dismiss = discord.ui.Button(
            label="Dismiss",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        self.previous_button.callback = self.previous_page
        self.next_button.callback = self.next_page
        dismiss.callback = self.dismiss
        self.add_item(self.previous_button)
        self.add_item(self.next_button)
        self.add_item(dismiss)
        self.refresh_controls()

    @property
    def page_count(self):
        return max(
            1,
            (len(self.tracks) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE,
        )

    def page_tracks(self):
        start = self.display_page * RESULTS_PER_PAGE
        return self.tracks[start:start + RESULTS_PER_PAGE]

    def slot_custom_id(self, slot):
        return f"mb:music:{self.view_token}:{self.display_page}:{int(slot)}"

    def refresh_controls(self):
        tracks = self.page_tracks()
        for slot, button in enumerate(self.result_buttons):
            button.custom_id = self.slot_custom_id(slot)
            button.label = str(slot + 1)
            button.disabled = slot >= len(tracks)

        self.previous_button.disabled = self.display_page == 0
        self.next_button.disabled = self.display_page >= self.page_count - 1

    def build_embed(self):
        blocks = []
        for index, track in enumerate(self.page_tracks(), start=1):
            blocks.append(
                f"**{index}. {track.get('name') or 'Unknown Track'}**\n"
                f"{music_artists(track)} - {track.get('album') or 'Unknown Album'} "
                f"- {music_duration(track)}"
            )

        embed = discord.Embed(
            title=compact_embed_title(f'Music results for "{self.query}"'),
            description="\n\n".join(blocks),
            color=discord.Color.purple(),
        )
        embed.set_footer(
            text=(
                f"Page {self.display_page + 1}/{self.page_count} - "
                f"{len(self.tracks)} result(s) from {self.source} - "
                "choose the exact track"
            )
        )
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return await renew_transient_interaction(
                interaction,
                self.message or getattr(interaction, "message", None),
            )

        await interaction.response.send_message(
            "These music results belong to someone else.",
            ephemeral=True,
        )
        return False

    async def previous_page(self, interaction):
        async with self.action_lock:
            if self.display_page > 0:
                self.display_page -= 1
            self.refresh_controls()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def next_page(self, interaction):
        async with self.action_lock:
            if self.display_page < self.page_count - 1:
                self.display_page += 1
            self.refresh_controls()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def select_track(self, interaction, slot):
        tracks = self.page_tracks()
        if slot >= len(tracks):
            await interaction.response.send_message(
                "That track is no longer in these results.",
                ephemeral=True,
            )
            return

        self.finished = True
        self.stop()
        track = tracks[slot]
        view = ConfirmMusicRequestView(
            requester_id=self.requester_id,
            track=track,
            origin_message=interaction.message,
            command_message=self.command_message,
        )
        embed = build_music_embed(
            track,
            heading="Confirm Music Request",
            state_text=(
                "Confirm the exact track below. SoulSync will choose the best "
                "eligible download according to its configured quality profile."
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def dismiss(self, interaction):
        async with self.action_lock:
            if self.finished:
                await interaction.response.defer()
                return
            self.finished = True
            self.stop()
            await interaction.response.defer()
            await cleanup_unsuccessful_request(
                origin_message=interaction.message,
                command_message=self.command_message,
            )

    async def on_timeout(self):
        async with self.action_lock:
            if self.finished:
                return
            self.finished = True
            self.stop()
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
            )


# ============================================================
# JELLYFIN UI
# ============================================================

class JellyfinAvailableView(
    discord.ui.View
):
    def __init__(
        self,
        *,
        item,
        jellyfin_item_id
    ):
        super().__init__(
            timeout=None
        )

        self.add_item(
            discord.ui.Button(
                label="▶ Watch in Jellyfin",
                style=discord.ButtonStyle.link,
                url=jellyfin.watch_url(
                    jellyfin_item_id
                )
            )
        )

        public_url = (
            SEERR_PUBLIC_URL
            or ""
        )

        if (
            public_url
            and "host.docker.internal"
            not in public_url
        ):

            self.add_item(
                discord.ui.Button(
                    label="Open in Seerr",
                    style=discord.ButtonStyle.link,
                    url=seerr_media_url(
                        item
                    )
                )
            )

        self.add_item(
            discord.ui.Button(
                label="TMDB",
                style=discord.ButtonStyle.link,
                url=tmdb_media_url(
                    item
                )
            )
        )


def jellyfin_item_year(
    item
):
    value = item.get(
        "ProductionYear"
    )

    return str(
        value
        or "????"
    )


def jellyfin_item_summary(
    item
):
    title = item.get(
        "Name",
        "Unknown"
    )

    year = jellyfin_item_year(
        item
    )

    kind = item.get(
        "Type",
        "Unknown"
    )

    genres = (
        item.get("Genres")
        or []
    )

    rating = item.get(
        "CommunityRating"
    )

    parts = [
        f"**{title} ({year})**",
        kind,
    ]

    if rating:

        try:
            parts.append(
                f"{float(rating):.1f}/10"
            )

        except Exception:
            pass

    if genres:
        parts.append(
            ", ".join(
                genres[:4]
            )
        )

    return " • ".join(parts)


# ============================================================
# PLAYBACK / MEDIA PROBLEM REPORT UI
# ============================================================

REPORT_STATUS_LABELS = {
    "open": "Open",
    "in_progress": "In Progress",
    "resolved": "Resolved",
    "dismissed": "Dismissed",
}


def report_category_label(value):
    try:
        return REPORT_CATEGORY_LABELS[ReportCategory(str(value))]
    except (KeyError, ValueError):
        return str(value or "Unknown").replace("_", " ").title()


def report_record_display_name(record):
    title = str(record.get("title") or "Unknown title")
    if str(record.get("media_type")) == "episode":
        season = int(record.get("season_number") or 0)
        episode = int(record.get("episode_number") or 0)
        label = f"S{season:02d}E{episode:02d}"
        episode_title = str(record.get("episode_title") or "").strip()
        if episode_title:
            label += f" - {episode_title}"
        return f"{title} - {label}"

    year = str(record.get("year") or "").strip()
    return f"{title} ({year})" if year else title


def build_report_ticket_embed(record, *, duplicate=False):
    status = str(record.get("status") or "open")
    color = {
        "open": discord.Color.gold(),
        "in_progress": discord.Color.blurple(),
        "resolved": discord.Color.green(),
        "dismissed": discord.Color.dark_grey(),
    }.get(status, discord.Color.gold())
    prefix = "Existing report" if duplicate else "Media report"
    embed = discord.Embed(
        title=(
            f"{prefix} #{record.get('report_id')}: "
            f"{report_record_display_name(record)}"
        ),
        description=(
            "An administrator can now track this without needing you to "
            "repeat the problem."
            if status in {"open", "in_progress"}
            else "This report has been closed."
        ),
        color=color,
    )
    embed.add_field(
        name="Problem",
        value=report_category_label(record.get("category")),
        inline=True,
    )
    embed.add_field(
        name="Status",
        value=f"**{REPORT_STATUS_LABELS.get(status, status.title())}**",
        inline=True,
    )
    details = str(record.get("details") or "").strip()
    if details:
        embed.add_field(name="Details", value=details[:1024], inline=False)
    reporter_id = record.get("reporter_discord_id")
    if reporter_id:
        embed.add_field(name="Reported By", value=f"<@{int(reporter_id)}>", inline=True)
    handler_id = record.get("handler_discord_id")
    if handler_id:
        embed.add_field(name="Handled By", value=f"<@{int(handler_id)}>", inline=True)
    resolution_note = str(record.get("resolution_note") or "").strip()
    if resolution_note:
        embed.add_field(
            name="Administrator Note",
            value=resolution_note[:1024],
            inline=False,
        )
    created_at = str(record.get("created_at") or "").strip()
    if created_at:
        embed.set_footer(text=f"Created {created_at} UTC")
    return embed


class ReportLinksView(discord.ui.View):
    def __init__(self, jellyfin_item_id):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Open in Jellyfin",
                style=discord.ButtonStyle.link,
                url=jellyfin.watch_url(jellyfin_item_id),
            )
        )


class ReportResultButton(discord.ui.Button):
    def __init__(self, slot):
        super().__init__(
            label=str(slot + 1),
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.slot = int(slot)

    async def callback(self, interaction):
        async with self.view.action_lock:
            clicked_id = (getattr(interaction, "data", None) or {}).get("custom_id")
            if clicked_id and clicked_id != self.view.slot_custom_id(self.slot):
                await interaction.response.send_message(
                    "That report page changed before the click completed. Choose again.",
                    ephemeral=True,
                )
                return
            await self.view.select_result(interaction, self.slot)


class ReportCategoryButton(discord.ui.Button):
    def __init__(self, category, *, row):
        self.category = ReportCategory(category)
        super().__init__(
            label=REPORT_CATEGORY_LABELS[self.category],
            style=(
                discord.ButtonStyle.danger
                if self.category in {
                    ReportCategory.WONT_PLAY,
                    ReportCategory.WRONG_EPISODE,
                }
                else discord.ButtonStyle.primary
            ),
            row=row,
        )

    async def callback(self, interaction):
        await self.view.choose_category(interaction, self.category)


class ReportDetailsModal(discord.ui.Modal):
    def __init__(self, category_view):
        super().__init__(title="Describe the problem", timeout=REQUEST_UI_TIMEOUT)
        self.category_view = category_view
        self.details_input = discord.ui.TextInput(
            label="What is wrong?",
            placeholder="A short description is enough.",
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=3,
            max_length=1000,
        )
        self.add_item(self.details_input)

    async def on_submit(self, interaction):
        if self.category_view.finished:
            await interaction.response.send_message(
                "That report form expired. Run `$report` again.",
                ephemeral=True,
            )
            return
        if not await renew_transient_interaction(
            interaction, self.category_view.message
        ):
            return
        await self.category_view.submit_report(
            interaction,
            ReportCategory.OTHER,
            details=str(self.details_input.value),
        )


class ReportCategoryView(LoggedView):
    def __init__(self, *, requester_id, target, command_message):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.target = target
        self.command_message = command_message
        self.message = None
        self.finished = False
        self.submitting = False

        categories = list(REPORT_CATEGORY_LABELS)
        for index, category in enumerate(categories):
            self.add_item(
                ReportCategoryButton(
                    category,
                    row=0 if index < 5 else 1,
                )
            )
        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        cancel.callback = self.cancel
        self.add_item(cancel)

    def build_embed(self):
        embed = discord.Embed(
            title=f"Report a problem: {self.target.display_name}",
            description="Choose the closest problem. Most reports take one click.",
            color=discord.Color.gold(),
        )
        if self.target.media_type == "episode":
            embed.add_field(
                name="Exact Episode",
                value=(
                    f"S{self.target.season_number:02d}"
                    f"E{self.target.episode_number:02d}"
                ),
                inline=True,
            )
        embed.add_field(
            name="Library",
            value="Matched exactly in Jellyfin",
            inline=True,
        )
        embed.set_footer(text="Unsubmitted reports disappear after 5 minutes")
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return await renew_transient_interaction(
                interaction,
                self.message or getattr(interaction, "message", None),
            )
        await interaction.response.send_message(
            "This report belongs to someone else.",
            ephemeral=True,
        )
        return False

    async def choose_category(self, interaction, category):
        if self.finished or self.submitting:
            await interaction.response.defer()
            return
        if category is ReportCategory.OTHER:
            await interaction.response.send_modal(ReportDetailsModal(self))
            return
        await self.submit_report(interaction, category)

    async def submit_report(self, interaction, category, *, details=""):
        if self.finished or self.submitting:
            if not interaction.response.is_done():
                await interaction.response.defer()
            return

        try:
            normalized_category = normalize_report_category(category, details=details)
        except ReportUsageError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "Reports must be submitted inside the media server.",
                ephemeral=True,
            )
            return

        self.submitting = True
        for child in self.children:
            child.disabled = True
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            record, created = create_media_report(
                target_key=self.target.target_key,
                jellyfin_item_id=self.target.jellyfin_item_id,
                jellyfin_series_id=self.target.jellyfin_series_id,
                media_type=self.target.media_type,
                title=self.target.title,
                year=self.target.year,
                season_number=self.target.season_number,
                episode_number=self.target.episode_number,
                episode_title=self.target.episode_title,
                category=normalized_category.value,
                details=details,
                reporter_discord_id=interaction.user.id,
                discord_guild_id=interaction.guild_id,
                discord_channel_id=interaction.channel_id,
                discord_message_id=self.message.id,
            )
        except Exception as exc:
            self.submitting = False
            for child in self.children:
                child.disabled = False
            error_id = log_exception("Could not persist media report", exc)
            if self.message:
                await self.message.edit(embed=self.build_embed(), view=self)
            await interaction.followup.send(
                f"Couldn't save that report. Error ID: `{error_id}`",
                ephemeral=True,
            )
            return

        self.finished = True
        self.stop()
        if not created:
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
            )
            await interaction.followup.send(
                (
                    f"You already have active report **#{record['report_id']}** "
                    f"for {report_record_display_name(record)}."
                ),
                ephemeral=True,
            )
            return

        mark_transient_message_terminal(self.message, "accepted")
        await self.message.edit(
            embed=build_report_ticket_embed(record),
            view=ReportLinksView(record["jellyfin_item_id"]),
        )
        await interaction.followup.send(
            f"Report **#{record['report_id']}** submitted.",
            ephemeral=True,
        )
        logger.info(
            "MEDIA REPORT CREATED | report_id=%s | guild=%s | reporter=%s | target=%s | category=%s",
            record["report_id"],
            interaction.guild_id,
            interaction.user.id,
            record["target_key"],
            record["category"],
        )

    async def cancel(self, interaction):
        if self.finished or self.submitting:
            await interaction.response.defer()
            return
        self.submitting = True
        self.finished = True
        self.stop()
        await interaction.response.defer()
        await cleanup_unsuccessful_request(
            origin_message=self.message,
            command_message=self.command_message,
        )

    async def on_timeout(self):
        if self.finished or self.submitting:
            return
        self.finished = True
        self.stop()
        await cleanup_unsuccessful_request(
            origin_message=self.message,
            command_message=self.command_message,
        )


async def edit_original_report_ticket(record):
    """Reflect an admin transition on the user's original report card."""
    try:
        channel_id = int(record["discord_channel_id"])
        message_id = int(record["discord_message_id"])
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        await message.edit(
            embed=build_report_ticket_embed(record),
            view=ReportLinksView(record["jellyfin_item_id"]),
        )
    except (discord.NotFound, discord.Forbidden) as exc:
        logger.info(
            "Could not update original report card report_id=%s: %s",
            record.get("report_id"),
            exc,
        )
    except Exception as exc:
        log_exception(
            f"Original report card update failed report_id={record.get('report_id')}",
            exc,
        )


async def transition_media_report(
    *,
    report_id,
    guild_id,
    handler_id,
    status,
    note="",
):
    record = update_media_report_status(
        report_id=int(report_id),
        discord_guild_id=int(guild_id),
        status=status,
        handler_discord_id=int(handler_id),
        resolution_note=note,
    )
    if record:
        await edit_original_report_ticket(record)
        logger.info(
            "MEDIA REPORT UPDATED | report_id=%s | guild=%s | handler=%s | status=%s",
            record["report_id"],
            guild_id,
            handler_id,
            record["status"],
        )
    return record


class AdminReportQueueView(LoggedView):
    def __init__(
        self,
        *,
        requester_id,
        guild_id,
        records,
        command_message,
        initial_index=0,
    ):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.records = list(records)
        self.command_message = command_message
        self.message = None
        self.index = max(0, min(int(initial_index), len(self.records) - 1))
        self.finished = False
        self.submitting = False

        self.previous_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.claim_button = discord.ui.Button(
            label="Claim",
            style=discord.ButtonStyle.primary,
            row=1,
        )
        self.resolve_button = discord.ui.Button(
            label="Resolve",
            style=discord.ButtonStyle.success,
            row=1,
        )
        self.dismiss_button = discord.ui.Button(
            label="Dismiss",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        self.previous_button.callback = self.previous
        self.next_button.callback = self.next
        self.claim_button.callback = self.claim
        self.resolve_button.callback = self.resolve
        self.dismiss_button.callback = self.dismiss
        for button in (
            self.previous_button,
            self.next_button,
            self.claim_button,
            self.resolve_button,
            self.dismiss_button,
        ):
            self.add_item(button)
        self.refresh_controls()

    def current(self):
        if not self.records:
            return None
        return self.records[self.index]

    def refresh_controls(self):
        current = self.current()
        self.previous_button.disabled = self.submitting or self.index <= 0
        self.next_button.disabled = (
            self.submitting or self.index >= len(self.records) - 1
        )
        self.claim_button.disabled = (
            self.submitting
            or current is None
            or current.get("status") == "in_progress"
        )
        self.resolve_button.disabled = self.submitting or current is None
        self.dismiss_button.disabled = self.submitting or current is None

    def build_embed(self):
        record = self.current()
        if record is None:
            return discord.Embed(
                title="Media report queue",
                description="No open or claimed reports remain.",
                color=discord.Color.green(),
            )
        embed = build_report_ticket_embed(record)
        embed.title = (
            f"Admin report queue - {self.index + 1}/{len(self.records)}\n"
            f"#{record['report_id']}: {report_record_display_name(record)}"
        )
        embed.description = (
            "Claim it while investigating, then resolve or dismiss it. "
            "The user's original ticket will update automatically."
        )
        embed.set_footer(text="Queue controls expire after 5 minutes")
        return embed

    async def interaction_check(self, interaction):
        permissions = getattr(interaction.user, "guild_permissions", None)
        if (
            interaction.user.id == self.requester_id
            and interaction.guild_id == self.guild_id
            and bool(getattr(permissions, "administrator", False))
        ):
            return await renew_transient_interaction(
                interaction,
                self.message or getattr(interaction, "message", None),
            )
        await interaction.response.send_message(
            "This administrator queue belongs to someone else.",
            ephemeral=True,
        )
        return False

    async def previous(self, interaction):
        async with self.action_lock:
            if self.index > 0:
                self.index -= 1
            self.refresh_controls()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def next(self, interaction):
        async with self.action_lock:
            if self.index < len(self.records) - 1:
                self.index += 1
            self.refresh_controls()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def apply_status(self, interaction, status):
        async with self.action_lock:
            current = self.current()
            if self.submitting or current is None:
                await interaction.response.defer()
                return
            report_id = int(current["report_id"])
            self.submitting = True
            self.refresh_controls()
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
                record = await transition_media_report(
                    report_id=report_id,
                    guild_id=self.guild_id,
                    handler_id=interaction.user.id,
                    status=status,
                )
                if record is None:
                    existing = media_report_by_id(
                        report_id,
                        discord_guild_id=self.guild_id,
                    )
                    await interaction.followup.send(
                        (
                            f"Report #{report_id} is already "
                            f"**{REPORT_STATUS_LABELS.get((existing or {}).get('status'), 'closed')}**."
                        ),
                        ephemeral=True,
                    )
                    self.records = [
                        row for row in self.records
                        if int(row["report_id"]) != report_id
                    ]
                elif status == "in_progress":
                    for index, row in enumerate(self.records):
                        if int(row["report_id"]) == report_id:
                            self.records[index] = record
                            self.index = index
                            break
                    await interaction.followup.send(
                        f"Claimed report **#{record['report_id']}**.",
                        ephemeral=True,
                    )
                else:
                    self.records = [
                        row for row in self.records
                        if int(row["report_id"]) != report_id
                    ]
                    await interaction.followup.send(
                        (
                            f"Report **#{record['report_id']}** marked "
                            f"**{REPORT_STATUS_LABELS[record['status']]}**."
                        ),
                        ephemeral=True,
                    )
                self.index = max(0, min(self.index, len(self.records) - 1))
            finally:
                self.submitting = False
                self.refresh_controls()

            if not self.records:
                self.finished = True
                self.stop()
                mark_transient_message_terminal(self.message, "kept")
                await self.message.edit(embed=self.build_embed(), view=None)
            else:
                await self.message.edit(embed=self.build_embed(), view=self)

    async def claim(self, interaction):
        await self.apply_status(interaction, "in_progress")

    async def resolve(self, interaction):
        await self.apply_status(interaction, "resolved")

    async def dismiss(self, interaction):
        await self.apply_status(interaction, "dismissed")

    async def on_timeout(self):
        if self.finished:
            return
        await cleanup_unsuccessful_request(
            origin_message=self.message,
            command_message=self.command_message,
        )


class ReportSearchView(LoggedView):
    def __init__(self, *, requester_id, query, results, command_message):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.query = query
        self.results = list(results[:25])
        self.command_message = command_message
        self.message = None
        self.display_page = 0
        self.view_token = secrets.token_hex(6)
        self.finished = False
        self.selecting = False
        self.action_lock = asyncio.Lock()
        self.result_buttons = []

        for slot in range(RESULTS_PER_PAGE):
            button = ReportResultButton(slot)
            self.result_buttons.append(button)
            self.add_item(button)
        self.previous_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        self.next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        dismiss = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        self.previous_button.callback = self.previous_page
        self.next_button.callback = self.next_page
        dismiss.callback = self.cancel
        self.add_item(self.previous_button)
        self.add_item(self.next_button)
        self.add_item(dismiss)
        self.refresh_controls()

    @property
    def page_count(self):
        return max(
            1,
            (len(self.results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE,
        )

    def page_items(self):
        start = self.display_page * RESULTS_PER_PAGE
        return self.results[start:start + RESULTS_PER_PAGE]

    def slot_custom_id(self, slot):
        return f"mb:report:{self.view_token}:{self.display_page}:{int(slot)}"

    def refresh_controls(self):
        items = self.page_items()
        for slot, button in enumerate(self.result_buttons):
            button.custom_id = self.slot_custom_id(slot)
            button.disabled = slot >= len(items)
        self.previous_button.disabled = self.display_page == 0
        self.next_button.disabled = self.display_page >= self.page_count - 1

    def build_embed(self):
        blocks = []
        for index, item in enumerate(self.page_items(), start=1):
            blocks.append(
                f"**{index}. {item.get('Name') or 'Unknown'} "
                f"({jellyfin_item_year(item)})**\n"
                f"{item.get('Type') or 'Unknown'} - "
                f"{short_overview({'overview': item.get('Overview')}, 180)}"
            )
        target_suffix = f" - {self.query.episode_label}" if self.query.is_episode else ""
        embed = discord.Embed(
            title=compact_embed_title(
                f'Report results for "{self.query.search_query}"{target_suffix}'
            ),
            description="\n\n".join(blocks),
            color=discord.Color.gold(),
        )
        embed.set_footer(
            text=(
                f"Page {self.display_page + 1}/{self.page_count} - "
                "choose the exact library item - expires after 5 minutes"
            )
        )
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id:
            return await renew_transient_interaction(
                interaction,
                self.message or getattr(interaction, "message", None),
            )
        await interaction.response.send_message(
            "These report results belong to someone else.",
            ephemeral=True,
        )
        return False

    async def previous_page(self, interaction):
        async with self.action_lock:
            if self.display_page > 0:
                self.display_page -= 1
            self.refresh_controls()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def next_page(self, interaction):
        async with self.action_lock:
            if self.display_page < self.page_count - 1:
                self.display_page += 1
            self.refresh_controls()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def select_result(self, interaction, slot):
        items = self.page_items()
        if slot >= len(items):
            await interaction.response.send_message(
                "That result is no longer on this page.",
                ephemeral=True,
            )
            return
        if self.selecting:
            await interaction.response.defer()
            return

        self.selecting = True
        # This transition updates the public result card in place. A thinking
        # defer creates an ephemeral spinner that nothing later resolves.
        await interaction.response.defer()
        try:
            target = await reports.resolve(items[slot], self.query)
        except ReportUsageError as exc:
            self.selecting = False
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            self.selecting = False
            error_id = log_exception("Could not resolve report target", exc)
            await interaction.followup.send(
                f"Couldn't inspect that library item. Error ID: `{error_id}`",
                ephemeral=True,
            )
            return

        self.finished = True
        self.stop()
        next_view = ReportCategoryView(
            requester_id=self.requester_id,
            target=target,
            command_message=self.command_message,
        )
        next_view.message = interaction.message
        await interaction.message.edit(embed=next_view.build_embed(), view=next_view)

    async def cancel(self, interaction):
        async with self.action_lock:
            if self.selecting or self.finished:
                await interaction.response.defer()
                return
            self.selecting = True
            self.finished = True
            self.stop()
            await interaction.response.defer()
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
            )

    async def on_timeout(self):
        async with self.action_lock:
            if self.finished or self.selecting:
                return
            self.finished = True
            self.stop()
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
            )


# ============================================================
# EVENT UI
# ============================================================

@dataclass(frozen=True)
class EventCreateSpec:
    name: str
    vote_limit: int
    preset: object | None = None


_EVENT_VOTE_LIMIT = re.compile(r"(?:^|\s)--votes\s+(\d+)\s*$", re.IGNORECASE)
_event_dashboard_locks = {}
_registered_event_dashboard_messages = set()


def parse_event_create_input(value, *, now=None):
    """Parse the deliberately small `$event create` surface."""
    raw = " ".join(str(value or "").split()).strip()
    if not raw:
        raise EventUsageError(
            "Give the event a name. Example: `$event create Friday Movie Night`."
        )

    vote_limit = 1
    match = _EVENT_VOTE_LIMIT.search(raw)
    if match:
        vote_limit = int(match.group(1))
        raw = raw[:match.start()].strip()
    if "--votes" in raw.casefold():
        raise EventUsageError(
            "Put `--votes N` at the end. Example: "
            "`$event create Friday Movie Night --votes 2`."
        )
    if not 1 <= vote_limit <= 25:
        raise EventUsageError("Vote limit must be between 1 and 25.")
    if not raw:
        raise EventUsageError("Give the event a short name before `--votes`.")

    pieces = raw.split()
    if pieces[0].casefold() == "spooktober":
        if len(pieces) > 2:
            raise EventUsageError(
                "Use `$event create spooktober [YEAR] [--votes N]`."
            )
        if len(pieces) == 2:
            try:
                year = int(pieces[1])
            except ValueError as exc:
                raise EventUsageError("Spooktober year must be four digits.") from exc
        else:
            reference = now or datetime.now(ZoneInfo(DEFAULT_EVENT_TIMEZONE))
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=ZoneInfo(DEFAULT_EVENT_TIMEZONE))
            year = reference.astimezone(ZoneInfo(DEFAULT_EVENT_TIMEZONE)).year
        try:
            preset = build_spooktober_preset(year=year, vote_limit=vote_limit)
        except ValueError as exc:
            raise EventUsageError(str(exc)) from exc
        return EventCreateSpec(
            name=preset.name,
            vote_limit=preset.vote_limit,
            preset=preset,
        )

    return EventCreateSpec(name=raw, vote_limit=vote_limit)


def event_status_label(status):
    value = status.value if isinstance(status, EventStatus) else str(status)
    return {
        "open": "Open for nominations and voting",
        "scheduled": "Scheduled - voting is closed",
        "completed": "Completed",
        "cancelled": "Cancelled",
    }.get(value, value.title())


def event_display_title(title, year="", *, limit=120):
    rendered = f"{title} ({year})" if year else str(title)
    maximum = max(32, min(200, int(limit)))
    if len(rendered) <= maximum:
        return rendered
    return rendered[: maximum - 3].rstrip() + "..."


def compact_event_lines(lines, *, limit=1000):
    rendered = []
    used = 0
    for line in lines:
        candidate = str(line)
        cost = len(candidate) + (1 if rendered else 0)
        if used + cost > limit:
            rendered.append("...")
            break
        rendered.append(candidate)
        used += cost
    return "\n".join(rendered) or "None yet."


def event_line_chunks(lines, *, limit=1000):
    """Split complete line sets across Discord fields without hiding entries."""
    chunks = []
    current = []
    used = 0
    for raw_line in lines:
        line = str(raw_line)
        if len(line) > limit:
            raise ValueError("One event schedule line is too long for Discord.")
        cost = len(line) + (1 if current else 0)
        if current and used + cost > limit:
            chunks.append("\n".join(current))
            current = []
            used = 0
            cost = len(line)
        current.append(line)
        used += cost
    if current:
        chunks.append("\n".join(current))
    return tuple(chunks)


def add_event_line_fields(embed, name, lines):
    chunks = event_line_chunks(lines)
    if not chunks:
        embed.add_field(name=name, value="None yet.", inline=False)
        return
    for index, chunk in enumerate(chunks, start=1):
        label = name if index == 1 else f"{name} continued ({index})"
        embed.add_field(name=label, value=chunk, inline=False)


def build_event_line_embeds(*, title, description, field_name, lines):
    """Build multiple valid Discord embeds when one message cannot hold a list."""

    chunks = event_line_chunks(lines, limit=900) or ("None yet.",)
    chunks_per_embed = 4
    page_count = max(1, (len(chunks) + chunks_per_embed - 1) // chunks_per_embed)
    embeds = []
    for page in range(page_count):
        embed = discord.Embed(
            title=compact_embed_title(
                f"{title} - page {page + 1}/{page_count}"
                if page_count > 1
                else title
            ),
            description=compact_embed_field(description, limit=1000),
            color=discord.Color.blurple(),
        )
        visible = chunks[page * chunks_per_embed:(page + 1) * chunks_per_embed]
        for index, chunk in enumerate(visible, start=1):
            absolute = page * chunks_per_embed + index
            label = field_name if absolute == 1 else f"{field_name} continued ({absolute})"
            embed.add_field(name=label, value=chunk, inline=False)
        embed.set_footer(text=f"Page {page + 1}/{page_count}")
        if len(embed) > 6000 or len(embed.fields) > 25:
            raise ValueError("Event output exceeds Discord embed limits.")
        embeds.append(embed)
    return tuple(embeds)


def event_date_select_options(timezone_name, *, now=None, days=21):
    """Return Discord-safe upcoming local dates without pretending it is a calendar."""

    zone = ZoneInfo(str(timezone_name))
    reference = now or datetime.now(zone)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=zone)
    local_today = reference.astimezone(zone).date()
    options = []
    for offset in range(max(1, min(int(days), 25))):
        value = local_today + timedelta(days=offset)
        prefix = "Today" if offset == 0 else "Tomorrow" if offset == 1 else value.strftime("%A")
        options.append(
            discord.SelectOption(
                label=f"{prefix} - {value.strftime('%b %d')}",
                value=value.isoformat(),
                description=value.strftime("%A, %B %d, %Y"),
            )
        )
    return options


def event_clock_select_options():
    """Offer the common media-night window; Custom handles every other time."""

    options = []
    for minutes in range(12 * 60, 24 * 60, 30):
        hour, minute = divmod(minutes, 60)
        value = f"{hour:02d}:{minute:02d}"
        label = datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p").lstrip("0")
        options.append(discord.SelectOption(label=label, value=value))
    return options


def event_local_datetime(date_value, time_value, timezone_name):
    return parse_schedule_input(
        f"{date_value} {time_value}",
        timezone_name=timezone_name,
    )[0]


def validate_future_event_times(values, *, reference=None):
    now = reference or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    normalized = tuple(values)
    if any(value.astimezone(timezone.utc) <= now for value in normalized):
        raise EventUsageError("Choose a date and time in the future.")
    return normalized


def current_visible_event(discord_guild_id, *, reference=None):
    """Prefer an open event, then the nearest schedule that has not ended."""

    current = events.current_event(int(discord_guild_id))
    if current is not None:
        return current
    now = reference or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    scheduled = events.list_events(
        discord_guild_id=int(discord_guild_id),
        statuses=(EventStatus.SCHEDULED,),
        limit=100,
    )
    candidates = []
    for event_record in scheduled:
        slot_times = [slot.starts_at for slot in events.slots(event_record.event_id)]
        if not slot_times:
            continue
        if max(slot_times) + timedelta(hours=EVENT_COMPLETION_GRACE_HOURS) <= now:
            continue
        future = [value for value in slot_times if value >= now]
        # Upcoming events win. Once one starts, keep it visible during the grace
        # window until lifecycle rollover marks it complete.
        sort_time = min(future) if future else max(slot_times)
        candidates.append((0 if future else 1, sort_time, event_record))
    return min(candidates, key=lambda value: (value[0], value[1]))[2] if candidates else None


def build_event_dashboard_embed(event_record):
    color = {
        EventStatus.OPEN: discord.Color.blurple(),
        EventStatus.SCHEDULED: discord.Color.green(),
        EventStatus.COMPLETED: discord.Color.dark_green(),
        EventStatus.CANCELLED: discord.Color.dark_grey(),
    }.get(event_record.status, discord.Color.blurple())
    embed = discord.Embed(
        title=compact_embed_title(event_record.name),
        description=event_status_label(event_record.status),
        color=color,
    )
    details = [
        f"Event **#{event_record.event_id}**",
        f"Vote for up to **{event_record.vote_limit}** title(s)",
        f"Timezone: **{event_record.timezone_name.replace('_', ' ')}**",
    ]
    if event_record.preset_key:
        details.append(
            f"Preset: **{event_record.preset_key.title()}** "
            f"v{event_record.preset_version}"
        )
    embed.add_field(name="Details", value="\n".join(details), inline=False)

    rankings = events.rankings(event_record.event_id)
    if event_record.status is EventStatus.SCHEDULED:
        embed.add_field(
            name="Ballot",
            value=(
                f"Voting is closed with **{len(rankings)}** nominated title(s). "
                "The frozen ranked schedule is below."
            ),
            inline=False,
        )
    elif rankings:
        shown_rankings = rankings[:15]
        lines = [
            (
                f"**{index}. {event_display_title(row.nomination.title, row.nomination.year)}** "
                f"- {row.vote_count} vote{'s' if row.vote_count != 1 else ''}"
            )
            for index, row in enumerate(shown_rankings, start=1)
        ]
        add_event_line_fields(
            embed,
            f"Ballot - top {len(shown_rankings)} of {len(rankings)}",
            lines,
        )
        if len(rankings) > len(shown_rankings):
            embed.add_field(
                name="Full ballot",
                value="Run `$event vote` to review every title in five-item pages.",
                inline=False,
            )
    else:
        embed.add_field(name="Ballot", value="No nominations yet.", inline=False)

    if event_record.status is EventStatus.SCHEDULED:
        slot_lines = []
        for slot in events.slots(event_record.event_id):
            timestamp = discord.utils.format_dt(slot.starts_at, style="F")
            title = event_display_title(slot.title or "TBD", slot.year, limit=90)
            slot_lines.append(f"{timestamp} - **{title}**")
        add_event_line_fields(embed, "Schedule", slot_lines)

    if event_record.status is EventStatus.OPEN and not event_record.preset_key:
        all_time_options = events.ranked_time_options(event_record.event_id)
        time_options = events.future_time_options(
            event_record.event_id,
            ranked=True,
        )
        if time_options:
            time_lines = []
            for index, option in enumerate(time_options[:10], start=1):
                stamp = discord.utils.format_dt(option.starts_at, style="F")
                relative = discord.utils.format_dt(option.starts_at, style="R")
                time_lines.append(
                    f"**{index}.** {stamp} ({relative}) - "
                    f"{option.vote_count} available"
                )
            add_event_line_fields(
                embed,
                f"Time vote - top {min(len(time_options), 10)} of {len(time_options)}",
                time_lines,
            )
        else:
            embed.add_field(
                name="Time vote",
                value=(
                    "No future candidate times remain. An administrator can use "
                    "**Add times** below; expired vote history is retained."
                    if all_time_options
                    else "No candidate times yet. An administrator can use **Add times** below."
                ),
                inline=False,
            )

    if event_record.status is EventStatus.OPEN:
        embed.add_field(
            name="Join in",
            value=(
                "`$event nominate <title>` - add an exact title\n"
                "`$event vote` - choose your favorites\n"
                "`$event time` - propose or vote on times\n"
                "`$event tonight` - see tonight's scheduled media"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Event #{event_record.event_id} - revision {event_record.revision}")
    return embed


async def refresh_event_dashboard(event_id):
    """Best-effort edit of the one stored dashboard; never fail a command."""
    numeric_event_id = int(event_id)
    lock = _event_dashboard_locks.setdefault(numeric_event_id, asyncio.Lock())
    async with lock:
        try:
            # Load and render inside the lock. A slower revision-N edit can no
            # longer land after a concurrent revision-N+1 refresh.
            event_record = events.event(numeric_event_id)
            if not event_record.dashboard_message_id:
                return False
            message = await discord_message_by_id(
                event_record.discord_channel_id,
                event_record.dashboard_message_id,
            )
            view = (
                EventDashboardView(event_record)
                if event_record.status in {EventStatus.OPEN, EventStatus.SCHEDULED}
                else None
            )
            await message.edit(embed=build_event_dashboard_embed(event_record), view=view)
            return True
        except (discord.NotFound, discord.Forbidden) as exc:
            logger.info("Event dashboard unavailable event=%s: %s", event_id, exc)
        except Exception as exc:
            log_exception(f"Could not refresh event dashboard event={event_id}", exc)
    return False


class EventJellyfinLinksView(discord.ui.View):
    def __init__(self, jellyfin_item_id):
        super().__init__(timeout=None)
        if jellyfin_item_id:
            self.add_item(
                discord.ui.Button(
                    label="Watch in Jellyfin",
                    style=discord.ButtonStyle.link,
                    url=jellyfin.watch_url(jellyfin_item_id),
                )
            )


def build_event_nomination_receipt(event_record, nomination, genres):
    embed = discord.Embed(
        title=f"Nominated: {event_display_title(nomination.title, nomination.year)}",
        description=f"Added to **{event_record.name}**.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Type", value=nomination.media_type.upper(), inline=True)
    embed.add_field(
        name="Library",
        value="Available" if nomination.jellyfin_item_id else "Not in library",
        inline=True,
    )
    if genres:
        embed.add_field(name="Genres", value=", ".join(genres[:6]), inline=False)
    if nomination.poster_path:
        embed.set_thumbnail(
            url="https://image.tmdb.org/t/p/w342" + nomination.poster_path
        )
    embed.set_footer(text=f"Nomination #{nomination.nomination_id} - no media was requested")
    return embed


class EventNominationSearchView(SearchResultsView):
    def __init__(self, *, guild_id, event_record, **kwargs):
        self.guild_id = int(guild_id)
        self.event_id = int(event_record.event_id)
        self.event_name = event_record.name
        super().__init__(**kwargs)

    def build_embed(self):
        embed = super().build_embed()
        embed.title = compact_embed_title(
            f'Nominate for {self.event_name}: "{self.query}"'
        )
        embed.set_footer(
            text=(
                f"Page {self.display_page + 1} - choose the exact title - "
                f"expires after {REQUEST_UI_TIMEOUT // 60} minutes of inactivity"
            )
        )
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "That event belongs to another server.", ephemeral=True
            )
            return False
        return await super().interaction_check(interaction)

    async def select_slot(self, interaction, slot):
        items = self.page_items()
        if slot >= len(items):
            await interaction.response.send_message(
                "That result is no longer on this page.", ephemeral=True
            )
            return
        if self.selecting:
            await interaction.response.send_message(
                "A title from this search is already being saved.", ephemeral=True
            )
            return

        self.selecting = True
        item = items[slot]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            current = events.event(self.event_id, discord_guild_id=self.guild_id)
            if current.status is not EventStatus.OPEN:
                raise EventStateError("Nominations are closed for this event.")
            details = await fetch_media_details(item)
            merged = dict(item)
            merged.update(details or {})
            merged["mediaType"] = item.get("mediaType")
            merged["id"] = item.get("id")
            raw_genres = merged.get("genres") or []
            genres = tuple(
                str(genre.get("name") if isinstance(genre, dict) else genre).strip()
                for genre in raw_genres
                if str(genre.get("name") if isinstance(genre, dict) else genre).strip()
            )
            jellyfin_item = None
            if jellyfin.enabled:
                try:
                    jellyfin_item = await jellyfin.find_by_tmdb(
                        tmdb_id=int(item["id"]),
                        title=media_title(merged),
                        media_type=str(item["mediaType"]),
                    )
                except Exception as exc:
                    logger.info("Event nomination Jellyfin lookup unavailable: %s", exc)
            result = events.nominate(
                event_id=self.event_id,
                media_type=str(item["mediaType"]),
                tmdb_id=int(item["id"]),
                title=media_title(merged),
                year=media_year(merged),
                nominated_by_discord_id=interaction.user.id,
                genres=genres,
                jellyfin_item_id=(jellyfin_item or {}).get("Id"),
                poster_path=merged.get("posterPath") or item.get("posterPath"),
            )
        except (EventUsageError, EventStateError) as exc:
            self.selecting = False
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            self.selecting = False
            error_id = log_exception("Could not save event nomination", exc)
            await interaction.followup.send(
                f"Couldn't save that nomination. Error ID: `{error_id}`",
                ephemeral=True,
            )
            return

        if not result.created:
            self.finished = True
            self.stop()
            await cleanup_unsuccessful_request(
                origin_message=self.message or interaction.message,
                command_message=self.command_message,
            )
            await interaction.followup.send(
                (
                    f"**{event_display_title(result.nomination.title, result.nomination.year)}** "
                    "is already nominated."
                ),
                ephemeral=True,
            )
            return

        self.finished = True
        self.stop()
        mark_transient_message_terminal(self.message or interaction.message, "accepted")
        await interaction.message.edit(
            embed=build_event_nomination_receipt(current, result.nomination, genres),
            view=(
                EventJellyfinLinksView(result.nomination.jellyfin_item_id)
                if result.nomination.jellyfin_item_id
                else None
            ),
        )
        await refresh_event_dashboard(self.event_id)
        await interaction.followup.send("Nomination saved.", ephemeral=True)


class EventVoteButton(discord.ui.Button):
    def __init__(self, slot):
        super().__init__(label=str(int(slot) + 1), row=0)
        self.slot = int(slot)

    async def callback(self, interaction):
        clicked_id = (getattr(interaction, "data", None) or {}).get("custom_id")
        await self.view.toggle_slot(interaction, self.slot, clicked_id=clicked_id)


class EventVoteView(LoggedView):
    def __init__(
        self,
        *,
        requester_id,
        guild_id,
        event_record,
        rankings,
        selected_ids,
        command_message,
        transient=True,
    ):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.event_id = int(event_record.event_id)
        self.event_name = event_record.name
        self.vote_limit = int(event_record.vote_limit)
        self.rankings = list(rankings)
        self.selected_ids = set(int(value) for value in selected_ids)
        self.command_message = command_message
        self.transient = bool(transient)
        self.message = None
        self.display_page = 0
        self.view_token = secrets.token_hex(6)
        self.finished = False
        self.saved = False
        self.durable = False
        self.action_lock = asyncio.Lock()
        self.result_buttons = []
        for slot in range(RESULTS_PER_PAGE):
            button = EventVoteButton(slot)
            self.result_buttons.append(button)
            self.add_item(button)
        self.previous_button = discord.ui.Button(
            label="Previous", style=discord.ButtonStyle.secondary, row=1
        )
        self.next_button = discord.ui.Button(
            label="Next", style=discord.ButtonStyle.secondary, row=1
        )
        self.done_button = discord.ui.Button(
            label="Done", style=discord.ButtonStyle.success, row=1
        )
        self.previous_button.callback = self.previous_page
        self.next_button.callback = self.next_page
        self.done_button.callback = self.done
        self.add_item(self.previous_button)
        self.add_item(self.next_button)
        self.add_item(self.done_button)
        self.refresh_controls()

    @property
    def page_count(self):
        return max(1, (len(self.rankings) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)

    def page_items(self):
        start = self.display_page * RESULTS_PER_PAGE
        return self.rankings[start:start + RESULTS_PER_PAGE]

    def slot_custom_id(self, slot, nomination_id=None):
        if nomination_id is None:
            items = self.page_items()
            nomination_id = (
                items[int(slot)].nomination.nomination_id
                if 0 <= int(slot) < len(items)
                else 0
            )
        return (
            f"mb:vote:{self.view_token}:{self.display_page}:"
            f"{int(slot)}:{int(nomination_id)}"
        )

    def refresh_controls(self):
        items = self.page_items()
        for slot, button in enumerate(self.result_buttons):
            nomination_id = (
                items[slot].nomination.nomination_id if slot < len(items) else 0
            )
            button.custom_id = self.slot_custom_id(slot, nomination_id)
            button.disabled = slot >= len(items)
            selected = (
                slot < len(items)
                and items[slot].nomination.nomination_id in self.selected_ids
            )
            button.style = (
                discord.ButtonStyle.success if selected else discord.ButtonStyle.primary
            )
        self.previous_button.disabled = self.display_page == 0
        self.next_button.disabled = self.display_page >= self.page_count - 1

    def build_embed(self, *, expired=False):
        blocks = []
        for index, row in enumerate(self.page_items(), start=1):
            nomination = row.nomination
            chosen = nomination.nomination_id in self.selected_ids
            blocks.append(
                (
                    f"**{'Selected - ' if chosen else ''}{index}. "
                    f"{event_display_title(nomination.title, nomination.year)}**\n"
                    f"{nomination.media_type.upper()} - {row.vote_count} "
                    f"vote{'s' if row.vote_count != 1 else ''}"
                )
            )
        embed = discord.Embed(
            title=compact_embed_title(f"Vote: {self.event_name}"),
            description="\n\n".join(blocks) or "No nominations are available.",
            color=discord.Color.green() if self.saved else discord.Color.blurple(),
        )
        selected_rows = self.rankings if expired else self.page_items()
        selected_names = [
            event_display_title(row.nomination.title, row.nomination.year)
            for row in selected_rows
            if row.nomination.nomination_id in self.selected_ids
        ]
        add_event_line_fields(
            embed,
            (
                f"Saved choices ({len(selected_names)})"
                if expired
                else f"Selected on this page ({len(selected_names)})"
            ),
            selected_names,
        )
        embed.add_field(
            name="Saved total",
            value=(
                f"**{len(self.selected_ids)}/{self.vote_limit}** choice(s). "
                + (
                    " Every saved title is listed above."
                    if expired
                    else " Use Previous/Next to review every selected title."
                )
            ),
            inline=False,
        )
        if expired:
            embed.set_footer(text="Vote saved - run $event vote to change it")
        else:
            embed.set_footer(
                text=(
                    f"Page {self.display_page + 1}/{self.page_count} - click a number "
                    f"to toggle - controls expire after {REQUEST_UI_TIMEOUT // 60} "
                    "minutes of inactivity"
                )
            )
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "That ballot belongs to another server.", ephemeral=True
            )
            return False
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "This vote card belongs to someone else. Run `$event vote` for your own.",
                ephemeral=True,
            )
            return False
        return await renew_transient_interaction(
            interaction,
            self.message or getattr(interaction, "message", None),
        )

    async def previous_page(self, interaction):
        async with self.action_lock:
            if self.display_page > 0:
                self.display_page -= 1
            self.refresh_controls()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def next_page(self, interaction):
        async with self.action_lock:
            if self.display_page < self.page_count - 1:
                self.display_page += 1
            self.refresh_controls()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def toggle_slot(self, interaction, slot, *, clicked_id=None):
        async with self.action_lock:
            items = self.page_items()
            if self.finished or slot >= len(items):
                await interaction.response.send_message(
                    "That ballot changed. Run `$event vote` again.", ephemeral=True
                )
                return
            nomination_id = items[slot].nomination.nomination_id
            if clicked_id and clicked_id != self.slot_custom_id(slot, nomination_id):
                await interaction.response.send_message(
                    "That ballot changed before the click completed. Choose again.",
                    ephemeral=True,
                )
                return
            try:
                result = events.toggle_vote(
                    event_id=self.event_id,
                    nomination_id=nomination_id,
                    discord_user_id=self.requester_id,
                )
            except VoteLimitExceededError as exc:
                await interaction.response.send_message(
                    f"{exc} Deselect one choice first.", ephemeral=True
                )
                return
            except (EventStateError, EventNotFoundError) as exc:
                self.finished = True
                self.stop()
                await interaction.response.edit_message(content=str(exc), embed=None, view=None)
                mark_transient_message_terminal(self.message or interaction.message, "kept")
                return
            except Exception as exc:
                error_id = log_exception("Could not save event vote", exc)
                await interaction.response.send_message(
                    f"Couldn't save that vote. Error ID: `{error_id}`", ephemeral=True
                )
                return

            self.saved = True
            self.selected_ids = set(result.selected_nomination_ids)
            self.rankings = list(events.rankings(self.event_id))
            self.display_page = min(self.display_page, self.page_count - 1)
            if not self.transient:
                self.durable = True
            elif not self.durable:
                self.durable = transition_transient_message(
                    self.message or interaction.message,
                    "event_vote_saved_actions",
                )
                if not self.durable:
                    self.finished = True
                    self.stop()
                    mark_transient_message_terminal(
                        self.message or interaction.message, "kept"
                    )
            else:
                touch_transient_message(self.message or interaction.message)
            self.refresh_controls()
            await interaction.response.edit_message(
                embed=self.build_embed(expired=not self.durable),
                view=self if self.durable else None,
            )
            await refresh_event_dashboard(self.event_id)

    async def done(self, interaction):
        async with self.action_lock:
            self.finished = True
            self.stop()
            if not self.saved:
                if not self.transient:
                    await interaction.response.edit_message(
                        content="No title votes changed.", embed=None, view=None
                    )
                    return
                await interaction.response.defer()
                await cleanup_unsuccessful_request(
                    origin_message=self.message or interaction.message,
                    command_message=self.command_message,
                )
                return
            mark_transient_message_terminal(self.message or interaction.message, "kept")
            await interaction.response.edit_message(
                embed=self.build_embed(expired=True), view=None
            )

    async def on_timeout(self):
        async with self.action_lock:
            if self.finished:
                return
            self.finished = True
            self.stop()
            if not self.transient:
                try:
                    if self.message is not None:
                        await self.message.edit(
                            embed=self.build_embed(expired=self.saved), view=None
                        )
                except Exception as exc:
                    logger.info("Could not retire private event vote controls: %s", exc)
                return
            if self.saved:
                mark_transient_message_terminal(self.message, "kept")
                try:
                    await self.message.edit(embed=self.build_embed(expired=True), view=None)
                except Exception as exc:
                    logger.info("Could not retire expired event vote controls: %s", exc)
                return
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
            )


def event_time_option_label(option, timezone_name):
    local = option.starts_at.astimezone(ZoneInfo(timezone_name))
    return local.strftime("%a %b %d at %I:%M %p").replace(" 0", " ")


def build_event_time_picker_embed(event_record, selected_at=None):
    embed = discord.Embed(
        title=compact_embed_title(f"Add times: {event_record.name}"),
        description=(
            "Choose a date and time, then click **Add candidate**. Add every time "
            "that could work; everyone can vote for all the times they are available."
        ),
        color=discord.Color.blurple(),
    )
    if selected_at is not None:
        embed.add_field(
            name="Ready to add",
            value=(
                f"{discord.utils.format_dt(selected_at, style='F')} "
                f"({discord.utils.format_dt(selected_at, style='R')})"
            ),
            inline=False,
        )
    all_options = events.time_options(event_record.event_id)
    options = events.future_time_options(event_record.event_id)
    if options:
        lines = [
            (
                f"{discord.utils.format_dt(option.starts_at, style='F')} - "
                f"{option.vote_count} available"
            )
            for option in options
        ]
        add_event_line_fields(embed, "Candidate times", lines)
    else:
        embed.add_field(
            name="Candidate times",
            value=(
                "No future times remain. Expired candidates and their votes are kept in history."
                if all_options
                else "None yet."
            ),
            inline=False,
        )
    embed.set_footer(
        text="Times are shown in each reader's Discord timezone; Custom accepts Denver local time"
    )
    return embed


class EventCustomTimeModal(discord.ui.Modal):
    def __init__(self, parent):
        super().__init__(title="Add a custom event time", timeout=REQUEST_UI_TIMEOUT)
        self.parent_view = parent
        self.local_value = discord.ui.TextInput(
            label="Local date and time",
            placeholder="2026-09-12 19:30",
            min_length=16,
            max_length=16,
        )
        self.add_item(self.local_value)

    async def on_submit(self, interaction):
        parent = self.parent_view
        try:
            starts_at = parse_schedule_input(
                str(self.local_value.value),
                timezone_name=parent.timezone_name,
            )[0]
            validate_future_event_times((starts_at,))
            events.add_time_options(
                parent.event_id,
                (starts_at,),
                parent.requester_id,
            )
        except (EventUsageError, EventStateError, EventNotFoundError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception as exc:
            error_id = log_exception("Could not add custom event time", exc)
            await interaction.response.send_message(
                f"Couldn't add that time. Error ID: `{error_id}`", ephemeral=True
            )
            return
        parent.saved = True
        parent.selected_at = starts_at
        await interaction.response.send_message(
            f"Added {discord.utils.format_dt(starts_at, style='F')}.", ephemeral=True
        )
        await refresh_event_dashboard(parent.event_id)
        if parent.message is not None:
            try:
                await parent.message.edit(
                    embed=build_event_time_picker_embed(
                        events.event(parent.event_id), starts_at
                    ),
                    view=parent,
                )
            except Exception as exc:
                logger.info("Could not refresh event time picker after modal: %s", exc)


class EventTimePickerView(LoggedView):
    def __init__(self, *, requester_id, guild_id, event_record, command_message=None):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.event_id = int(event_record.event_id)
        self.timezone_name = event_record.timezone_name
        self.command_message = command_message
        self.message = None
        self.saved = False
        self.finished = False
        local_now = datetime.now(ZoneInfo(self.timezone_name))
        date_options = event_date_select_options(self.timezone_name, now=local_now)
        default_date_index = 1 if local_now.hour >= 19 and len(date_options) > 1 else 0
        self.selected_date = date_options[default_date_index].value
        self.selected_time = "19:00"
        self.selected_at = event_local_datetime(
            self.selected_date, self.selected_time, self.timezone_name
        )

        date_options[default_date_index].default = True
        clock_options = event_clock_select_options()
        for option in clock_options:
            option.default = option.value == self.selected_time
        self.date_select = discord.ui.Select(
            placeholder="Choose a date",
            options=date_options,
            min_values=1,
            max_values=1,
            row=0,
        )
        self.time_select = discord.ui.Select(
            placeholder="Choose a time",
            options=clock_options,
            min_values=1,
            max_values=1,
            row=1,
        )
        self.date_select.callback = self.choose_date
        self.time_select.callback = self.choose_time
        self.add_item(self.date_select)
        self.add_item(self.time_select)

        add_button = discord.ui.Button(
            label="Add candidate", style=discord.ButtonStyle.success, row=2
        )
        custom_button = discord.ui.Button(
            label="Custom", style=discord.ButtonStyle.secondary, row=2
        )
        done_button = discord.ui.Button(
            label="Done", style=discord.ButtonStyle.primary, row=2
        )
        reset_button = discord.ui.Button(
            label="Reset times", style=discord.ButtonStyle.danger, row=2
        )
        add_button.callback = self.add_candidate
        custom_button.callback = self.custom_time
        done_button.callback = self.done
        reset_button.callback = self.reset_times
        self.add_item(add_button)
        self.add_item(custom_button)
        self.add_item(done_button)
        self.add_item(reset_button)
        self.refresh_select_defaults()

    def refresh_select_defaults(self):
        for option in self.date_select.options:
            option.default = option.value == self.selected_date
        for option in self.time_select.options:
            option.default = option.value == self.selected_time

    def build_embed(self):
        return build_event_time_picker_embed(
            events.event(self.event_id), self.selected_at
        )

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the administrator who opened this picker can change it.",
                ephemeral=True,
            )
            return False
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not getattr(permissions, "administrator", False):
            await interaction.response.send_message(
                "An administrator must propose event times.", ephemeral=True
            )
            return False
        return await renew_transient_interaction(
            interaction, self.message or getattr(interaction, "message", None)
        )

    async def choose_date(self, interaction):
        self.selected_date = self.date_select.values[0]
        self.selected_at = event_local_datetime(
            self.selected_date, self.selected_time, self.timezone_name
        )
        self.refresh_select_defaults()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def choose_time(self, interaction):
        self.selected_time = self.time_select.values[0]
        self.selected_at = event_local_datetime(
            self.selected_date, self.selected_time, self.timezone_name
        )
        self.refresh_select_defaults()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def add_candidate(self, interaction):
        try:
            validate_future_event_times((self.selected_at,))
            events.add_time_options(
                self.event_id,
                (self.selected_at,),
                self.requester_id,
            )
        except (EventUsageError, EventStateError, EventNotFoundError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.saved = True
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await refresh_event_dashboard(self.event_id)

    async def reset_times(self, interaction):
        try:
            events.replace_time_options(self.event_id, (), self.requester_id)
        except (EventUsageError, EventStateError, EventNotFoundError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.saved = True
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await refresh_event_dashboard(self.event_id)

    async def custom_time(self, interaction):
        await interaction.response.send_modal(EventCustomTimeModal(self))

    async def done(self, interaction):
        self.finished = True
        self.stop()
        await interaction.response.defer()
        if self.command_message is not None:
            await cleanup_unsuccessful_request(
                origin_message=self.message or interaction.message,
                command_message=self.command_message,
            )
        else:
            await interaction.edit_original_response(
                content="Candidate times saved." if self.saved else "No times changed.",
                embed=None,
                view=None,
            )

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        self.stop()
        if self.command_message is not None:
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
            )
        elif self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception as exc:
                logger.info("Could not retire event time picker: %s", exc)


class EventRescheduleCustomTimeModal(discord.ui.Modal):
    def __init__(self, parent):
        super().__init__(title="Choose a custom event time", timeout=REQUEST_UI_TIMEOUT)
        self.parent_view = parent
        self.local_value = discord.ui.TextInput(
            label="Local date and time",
            placeholder="2026-09-12 19:30",
            min_length=16,
            max_length=16,
        )
        self.add_item(self.local_value)

    async def on_submit(self, interaction):
        parent = self.parent_view
        try:
            starts_at = parse_schedule_input(
                str(self.local_value.value),
                timezone_name=parent.timezone_name,
            )[0]
            validate_future_event_times((starts_at,))
            event_record, slots = parent.reschedule_to(starts_at)
        except StaleEventRevisionError as exc:
            parent.finished = True
            parent.stop()
            await interaction.response.edit_message(
                content=f"{exc} Run `$event reschedule {parent.event_id}` again.",
                embed=None,
                view=None,
            )
            return
        except (EventUsageError, EventStateError, EventNotFoundError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception as exc:
            error_id = log_exception("Could not save custom event reschedule", exc)
            await interaction.response.send_message(
                f"Couldn't reschedule that event. Error ID: `{error_id}`", ephemeral=True
            )
            return
        await parent.finish_reschedule(interaction, event_record, slots)


class EventReschedulePickerView(EventTimePickerView):
    def __init__(self, *, requester_id, guild_id, event_record, slot, command_message):
        super().__init__(
            requester_id=requester_id,
            guild_id=guild_id,
            event_record=event_record,
            command_message=command_message,
        )
        self.slot = slot
        self.event_revision = int(event_record.revision)
        local = slot.starts_at.astimezone(ZoneInfo(self.timezone_name))
        self.selected_date = local.date().isoformat()
        self.selected_time = local.strftime("%H:%M")
        self.selected_at = local
        if not any(
            option.value == self.selected_date for option in self.date_select.options
        ):
            self.date_select.append_option(
                discord.SelectOption(
                    label=f"Current - {local.strftime('%b %d')}",
                    value=self.selected_date,
                    description=local.strftime("%A, %B %d, %Y"),
                )
            )
        if not any(
            option.value == self.selected_time for option in self.time_select.options
        ):
            self.time_select.append_option(
                discord.SelectOption(
                    label=local.strftime("%I:%M %p").lstrip("0"),
                    value=self.selected_time,
                )
            )
        self.refresh_select_defaults()
        for child in tuple(self.children):
            if getattr(child, "label", None) == "Add candidate":
                child.label = "Save new time"
            elif getattr(child, "label", None) == "Done":
                child.label = "Cancel"
            elif getattr(child, "label", None) == "Reset times":
                self.remove_item(child)

    def build_embed(self):
        embed = discord.Embed(
            title=compact_embed_title(f"Reschedule: {events.event(self.event_id).name}"),
            description=(
                "Choose the new date and time. The title votes and winning title stay "
                "intact, and the existing Discord event will be moved instead of duplicated."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Current",
            value=discord.utils.format_dt(self.slot.starts_at, style="F"),
            inline=False,
        )
        embed.add_field(
            name="New",
            value=(
                f"{discord.utils.format_dt(self.selected_at, style='F')} "
                f"({discord.utils.format_dt(self.selected_at, style='R')})"
            ),
            inline=False,
        )
        return embed

    def reschedule_to(self, starts_at):
        return events.reschedule_event(
            self.event_id,
            (ScheduleAssignment(starts_at, self.slot.nomination_id),),
            expected_revision=self.event_revision,
        )

    async def finish_reschedule(self, interaction, event_record, slots):
        self.saved = True
        self.finished = True
        self.stop()
        mark_transient_message_terminal(self.message or interaction.message, "accepted")
        await interaction.response.edit_message(
            embed=build_event_schedule_receipt(event_record, slots), view=None
        )
        await refresh_event_dashboard(self.event_id)

    async def add_candidate(self, interaction):
        try:
            validate_future_event_times((self.selected_at,))
            event_record, slots = self.reschedule_to(self.selected_at)
        except StaleEventRevisionError as exc:
            self.finished = True
            self.stop()
            await interaction.response.edit_message(
                content=f"{exc} Run `$event reschedule {self.event_id}` again.",
                embed=None,
                view=None,
            )
            return
        except (EventUsageError, EventStateError, EventNotFoundError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.finish_reschedule(interaction, event_record, slots)

    async def custom_time(self, interaction):
        await interaction.response.send_modal(EventRescheduleCustomTimeModal(self))

    async def done(self, interaction):
        self.finished = True
        self.stop()
        await interaction.response.defer()
        await cleanup_unsuccessful_request(
            origin_message=self.message or interaction.message,
            command_message=self.command_message,
        )


class EventTimeVoteSelect(discord.ui.Select):
    def __init__(self, parent, options):
        rendered = []
        for option in options:
            rendered.append(
                discord.SelectOption(
                    label=event_time_option_label(option, parent.timezone_name)[:100],
                    value=str(option.time_option_id),
                    description=(
                        f"{option.vote_count} member{'s' if option.vote_count != 1 else ''} available"
                    )[:100],
                    default=option.time_option_id in parent.selected_ids,
                )
            )
        super().__init__(
            placeholder="Select every time you can attend",
            options=rendered,
            min_values=0,
            max_values=len(rendered),
            row=0,
        )

    async def callback(self, interaction):
        await self.view.save_values(interaction, self.values)


class EventTimeVoteView(LoggedView):
    def __init__(
        self,
        *,
        requester_id,
        guild_id,
        event_record,
        options,
        selected_ids,
        command_message=None,
    ):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.event_id = int(event_record.event_id)
        self.event_name = event_record.name
        self.timezone_name = event_record.timezone_name
        self.options = list(options)
        option_ids = {option.time_option_id for option in self.options}
        self.selected_ids = {
            int(value) for value in selected_ids if int(value) in option_ids
        }
        self.command_message = command_message
        self.message = None
        self.saved = False
        self.finished = False
        self.durable = False
        self.add_item(EventTimeVoteSelect(self, self.options))
        clear_button = discord.ui.Button(
            label="None work", style=discord.ButtonStyle.danger, row=1
        )
        done_button = discord.ui.Button(
            label="Done", style=discord.ButtonStyle.success, row=1
        )
        clear_button.callback = self.clear
        done_button.callback = self.done
        self.add_item(clear_button)
        self.add_item(done_button)

    def build_embed(self, *, expired=False):
        lines = []
        for option in self.options:
            selected = option.time_option_id in self.selected_ids
            lines.append(
                f"{'**Available** - ' if selected else ''}"
                f"{discord.utils.format_dt(option.starts_at, style='F')} "
                f"({discord.utils.format_dt(option.starts_at, style='R')}) - "
                f"{option.vote_count} vote{'s' if option.vote_count != 1 else ''}"
            )
        embed = discord.Embed(
            title=compact_embed_title(f"When can you make {self.event_name}?"),
            description="Select every time that works for you. This is availability, not a one-choice ballot.",
            color=discord.Color.green() if self.saved else discord.Color.blurple(),
        )
        add_event_line_fields(embed, "Candidate times", lines)
        embed.set_footer(
            text=(
                "Availability saved - run $event time to change it"
                if expired
                else f"{len(self.selected_ids)} selected - controls expire after {REQUEST_UI_TIMEOUT // 60} minutes"
            )
        )
        return embed

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "This availability card belongs to someone else. Use `$event time` for your own.",
                ephemeral=True,
            )
            return False
        return await renew_transient_interaction(
            interaction, self.message or getattr(interaction, "message", None)
        )

    async def save_values(self, interaction, values):
        try:
            result = events.replace_time_votes(
                self.event_id,
                self.requester_id,
                tuple(int(value) for value in values),
            )
        except (EventUsageError, EventStateError, EventNotFoundError) as exc:
            self.finished = True
            self.stop()
            await interaction.response.edit_message(content=str(exc), embed=None, view=None)
            return
        self.saved = True
        self.selected_ids = set(result.selected_time_option_ids)
        self.options = list(events.future_time_options(self.event_id))
        option_ids = {option.time_option_id for option in self.options}
        self.selected_ids.intersection_update(option_ids)
        if not self.options:
            self.finished = True
            self.stop()
            await interaction.response.edit_message(
                content=(
                    "The candidate times changed while this card was open. "
                    "Run `$event time` again."
                ),
                embed=None,
                view=None,
            )
            await refresh_event_dashboard(self.event_id)
            return
        if self.command_message is None:
            self.durable = True
        elif not self.durable:
            self.durable = transition_transient_message(
                self.message or interaction.message,
                "event_time_vote_saved_actions",
            )
            if not self.durable:
                self.finished = True
                self.stop()
                mark_transient_message_terminal(
                    self.message or interaction.message,
                    "kept",
                )
        else:
            touch_transient_message(self.message or interaction.message)
        self.clear_items()
        self.add_item(EventTimeVoteSelect(self, self.options))
        clear_button = discord.ui.Button(label="None work", style=discord.ButtonStyle.danger, row=1)
        done_button = discord.ui.Button(label="Done", style=discord.ButtonStyle.success, row=1)
        clear_button.callback = self.clear
        done_button.callback = self.done
        self.add_item(clear_button)
        self.add_item(done_button)
        await interaction.response.edit_message(
            embed=self.build_embed(expired=not self.durable),
            view=self if self.durable else None,
        )
        await refresh_event_dashboard(self.event_id)

    async def clear(self, interaction):
        await self.save_values(interaction, ())

    async def done(self, interaction):
        self.finished = True
        self.stop()
        if self.saved:
            mark_transient_message_terminal(self.message or interaction.message, "kept")
            await interaction.response.edit_message(
                embed=self.build_embed(expired=True), view=None
            )
        elif self.command_message is not None:
            await interaction.response.defer()
            await cleanup_unsuccessful_request(
                origin_message=self.message or interaction.message,
                command_message=self.command_message,
            )
        else:
            await interaction.response.edit_message(
                content="No availability changed.", embed=None, view=None
            )

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        self.stop()
        if self.saved:
            mark_transient_message_terminal(self.message, "kept")
            if self.message is not None:
                try:
                    await self.message.edit(embed=self.build_embed(expired=True), view=None)
                except Exception as exc:
                    logger.info("Could not retire event time vote controls: %s", exc)
        elif self.command_message is not None:
            await cleanup_unsuccessful_request(
                origin_message=self.message,
                command_message=self.command_message,
            )
        elif self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception as exc:
                logger.info("Could not retire private time vote controls: %s", exc)


class EventDashboardView(LoggedView):
    """Restart-safe controls attached to the durable dashboard message."""

    def __init__(self, event_record, *, persistent=True, command_message=None):
        super().__init__(timeout=None if persistent else REQUEST_UI_TIMEOUT)
        self.persistent = bool(persistent)
        self.command_message = command_message
        self.message = None
        self.event_id = int(event_record.event_id)
        self.guild_id = int(event_record.discord_guild_id)
        if event_record.status is EventStatus.OPEN:
            title_vote = discord.ui.Button(
                label="Vote titles",
                style=discord.ButtonStyle.primary,
                custom_id=f"mb:event:{self.event_id}:titles",
            )
            time_vote = discord.ui.Button(
                label="Vote times",
                style=discord.ButtonStyle.primary,
                custom_id=f"mb:event:{self.event_id}:times",
            )
            add_times = discord.ui.Button(
                label="Add times",
                style=discord.ButtonStyle.secondary,
                custom_id=f"mb:event:{self.event_id}:add-times",
            )
            title_vote.callback = self.vote_titles
            time_vote.callback = self.vote_times
            add_times.callback = self.add_times
            self.add_item(title_vote)
            self.add_item(time_vote)
            if not event_record.preset_key:
                self.add_item(add_times)
        if event_record.status is EventStatus.SCHEDULED:
            manage = discord.ui.Button(
                label="Manage",
                style=discord.ButtonStyle.secondary,
                custom_id=f"mb:event:{self.event_id}:manage",
            )
            manage.callback = self.manage
            self.add_item(manage)

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "That event belongs to another server.", ephemeral=True
            )
            return False
        if self.persistent:
            return True
        return await renew_transient_interaction(
            interaction,
            self.message or getattr(interaction, "message", None),
        )

    async def on_timeout(self):
        if self.persistent:
            return
        await cleanup_unsuccessful_request(
            origin_message=self.message,
            command_message=self.command_message,
        )

    async def vote_titles(self, interaction):
        event_record = events.event(self.event_id, discord_guild_id=self.guild_id)
        rankings = events.rankings(self.event_id)
        if not rankings:
            await interaction.response.send_message(
                "Nothing has been nominated yet. Use `$event nominate <title>`.",
                ephemeral=True,
            )
            return
        selected = events.user_vote_ids(
            event_id=self.event_id, discord_user_id=interaction.user.id
        )
        view = EventVoteView(
            requester_id=interaction.user.id,
            guild_id=self.guild_id,
            event_record=event_record,
            rankings=rankings,
            selected_ids=selected,
            command_message=None,
            transient=False,
        )
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def vote_times(self, interaction):
        event_record = events.event(self.event_id, discord_guild_id=self.guild_id)
        options = events.future_time_options(self.event_id)
        if not options:
            await interaction.response.send_message(
                "No future candidate times remain. Ask an administrator to use **Add times**.",
                ephemeral=True,
            )
            return
        selected = events.user_time_vote_ids(
            self.event_id, interaction.user.id
        )
        view = EventTimeVoteView(
            requester_id=interaction.user.id,
            guild_id=self.guild_id,
            event_record=event_record,
            options=options,
            selected_ids=selected,
        )
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def add_times(self, interaction):
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not getattr(permissions, "administrator", False):
            await interaction.response.send_message(
                "An administrator must propose candidate times.", ephemeral=True
            )
            return
        event_record = events.event(self.event_id, discord_guild_id=self.guild_id)
        view = EventTimePickerView(
            requester_id=interaction.user.id,
            guild_id=self.guild_id,
            event_record=event_record,
        )
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def manage(self, interaction):
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not getattr(permissions, "administrator", False):
            await interaction.response.send_message(
                "An administrator must manage a scheduled event.", ephemeral=True
            )
            return
        event_record = events.event(self.event_id, discord_guild_id=self.guild_id)
        slots = events.slots(self.event_id)
        lines = [
            f"{discord.utils.format_dt(slot.starts_at, style='F')} - **{slot.title or 'TBD'}**"
            for slot in slots
        ]
        embed = discord.Embed(
            title=compact_embed_title(f"Manage: {event_record.name}"),
            description=(
                "The schedule is editable. Use `$event reschedule #` to move it, "
                "`$event reopen #` to resume voting, or complete/cancel it below."
            ),
            color=discord.Color.blurple(),
        )
        add_event_line_fields(embed, "Current schedule", lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)


def schedule_assignment_lines(plan):
    nominations = {
        nomination.nomination_id: nomination
        for nomination in events.nominations(plan.event_id)
    }
    lines = []
    for assignment in plan.assignments:
        timestamp = discord.utils.format_dt(assignment.starts_at, style="F")
        nomination = nominations.get(assignment.nomination_id)
        title = (
            event_display_title(nomination.title, nomination.year)
            if nomination
            else "TBD"
        )
        lines.append(f"{timestamp} - **{title}**")
    return lines


def build_event_schedule_preview(event_record, plan):
    embed = discord.Embed(
        title=f"Schedule {event_record.name}?",
        description=(
            "This publishes the current winners and closes nominations and voting. "
            "The schedule can still be moved or reopened later. Nothing will be "
            "requested automatically."
        ),
        color=discord.Color.gold(),
    )
    add_event_line_fields(embed, "Ranked schedule", schedule_assignment_lines(plan))
    if plan.tied_vote_counts:
        values = ", ".join(str(value) for value in plan.tied_vote_counts)
        embed.add_field(
            name="Tie warning",
            value=(
                f"Vote count(s) **{values}** are tied. The preview uses nomination "
                "order as the stable tie-breaker; confirm only if this order is okay."
            ),
            inline=False,
        )
    embed.set_footer(text=f"Preview from event revision {plan.event_revision}")
    return embed


def build_event_schedule_receipt(event_record, slots):
    embed = discord.Embed(
        title=f"Scheduled: {event_record.name}",
        description=(
            "Voting is closed for now. MediaBot will publish Discord events and "
            "send reminders; administrators can still reschedule or reopen voting."
        ),
        color=discord.Color.green(),
    )
    lines = []
    for slot in slots:
        timestamp = discord.utils.format_dt(slot.starts_at, style="F")
        title = event_display_title(slot.title or "TBD", slot.year)
        lines.append(f"{timestamp} - **{title}**")
    add_event_line_fields(embed, "Schedule", lines)
    embed.set_footer(text=f"Event #{event_record.event_id}")
    return embed


_NATIVE_EVENT_MARKER_RE = re.compile(
    r"(?<![0-9A-Za-z])\[mediabot:event=([1-9][0-9]*);"
    r"slot=([1-9][0-9]*)\](?![0-9A-Za-z])"
)
_LEGACY_NATIVE_EVENT_MARKER_RE = re.compile(
    r"MediaBot event #([1-9][0-9]*) / slot #([1-9][0-9]*)(?=[.\s]|$)"
)


class EventNativeSyncError(RuntimeError):
    """Native Discord event publication cannot currently make progress."""


def native_event_marker(event_id, slot_id):
    return f"[mediabot:event={int(event_id)};slot={int(slot_id)}]"


def native_event_marker_ids(description):
    text = str(description or "")
    matches = {
        (int(match.group(1)), int(match.group(2)))
        for pattern in (_NATIVE_EVENT_MARKER_RE, _LEGACY_NATIVE_EVENT_MARKER_RE)
        for match in pattern.finditer(text)
    }
    return matches


def native_event_is_bot_owned(remote, guild):
    bot_member_id = getattr(getattr(guild, "me", None), "id", None)
    if bot_member_id is None:
        bot_member_id = getattr(getattr(bot, "user", None), "id", None)
    creator_id = getattr(remote, "creator_id", None)
    return (
        bot_member_id is not None
        and creator_id is not None
        and int(creator_id) == int(bot_member_id)
    )


def native_event_location():
    configured = str(jellyfin.public_url or "").strip()
    if configured and len(configured) <= 100:
        return configured
    return "Jellyfin" if configured else "Discord"


def native_event_datetime_matches(actual, expected):
    if actual is None:
        return False
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    return abs((actual.astimezone(timezone.utc) - expected.astimezone(timezone.utc)).total_seconds()) <= 1


def native_event_description(event_record, slot):
    marker = native_event_marker(event_record.event_id, slot.slot_id)
    title = event_display_title(slot.title or "Media night", slot.year)
    return (
        f"{title}\n\nPlanned by Dogginator MediaBot. {marker}. "
        "The MediaBot schedule remains authoritative."
    )


def current_native_event_slot(expected_event, expected_slot):
    """Return a fresh matching scheduled slot, or None after any local change."""

    try:
        current_event = events.event(expected_event.event_id)
        current_slot = next(
            (
                value
                for value in events.slots(expected_event.event_id)
                if int(value.slot_id) == int(expected_slot.slot_id)
            ),
            None,
        )
    except (EventNotFoundError, EventStateError):
        return None
    if current_slot is None or current_event.status is not EventStatus.SCHEDULED:
        return None
    if current_slot.slot_status != "planned":
        return None
    expected_identity = (
        int(expected_event.revision),
        int(expected_slot.event_id),
        expected_slot.starts_at,
        expected_slot.nomination_id,
        expected_slot.media_type,
        expected_slot.tmdb_id,
        expected_slot.title,
        expected_slot.year,
        expected_slot.native_scheduled_event_id,
    )
    current_identity = (
        int(current_event.revision),
        int(current_slot.event_id),
        current_slot.starts_at,
        current_slot.nomination_id,
        current_slot.media_type,
        current_slot.tmdb_id,
        current_slot.title,
        current_slot.year,
        current_slot.native_scheduled_event_id,
    )
    return (current_event, current_slot) if current_identity == expected_identity else None


async def discard_stale_native_event(remote, guild, remote_events, marker):
    """Compensate an external mutation that lost its local identity race."""

    if remote is None:
        return
    if not native_event_is_bot_owned(remote, guild):
        raise EventNativeSyncError(
            f"Refusing to delete a non-MediaBot Discord event after stale sync {marker}."
        )
    try:
        await remote.delete(reason=f"{marker} discarded after local schedule changed")
    except discord.NotFound:
        pass
    except (discord.Forbidden, discord.HTTPException) as exc:
        raise EventNativeSyncError(
            f"Could not discard stale Discord event {remote.id}: {exc}"
        ) from exc
    if remote_events is not None:
        remote_events[:] = [
            value for value in remote_events if int(value.id) != int(remote.id)
        ]


async def sync_native_event_slot(event_record, slot, *, remote_events=None):
    """Idempotently create or edit one Discord Scheduled Event for one slot."""

    guild = bot.get_guild(int(event_record.discord_guild_id))
    if slot.starts_at <= datetime.now(timezone.utc):
        return False
    if guild is None:
        raise EventNativeSyncError(
            f"Discord guild {event_record.discord_guild_id} is not available."
        )
    me = getattr(guild, "me", None)
    permissions = getattr(me, "guild_permissions", None)
    if not (
        getattr(permissions, "manage_events", False)
        or getattr(permissions, "create_events", False)
        or getattr(permissions, "administrator", False)
    ):
        raise EventNativeSyncError(
            "MediaBot needs Create Events or Manage Events to publish the schedule."
        )

    current = current_native_event_slot(event_record, slot)
    if current is None:
        return False
    event_record, slot = current

    known = (
        remote_events
        if remote_events is not None
        else list(await guild.fetch_scheduled_events(with_counts=False))
    )
    current = current_native_event_slot(event_record, slot)
    if current is None:
        return False
    event_record, slot = current
    remote = next(
        (
            candidate
            for candidate in known
            if slot.native_scheduled_event_id
            and int(candidate.id) == int(slot.native_scheduled_event_id)
        ),
        None,
    )
    marker = native_event_marker(event_record.event_id, slot.slot_id)
    if remote is None:
        remote = next(
            (
                candidate
                for candidate in known
                if native_event_is_bot_owned(candidate, guild)
                and (event_record.event_id, slot.slot_id)
                in native_event_marker_ids(getattr(candidate, "description", ""))
            ),
            None,
        )

    title = compact_embed_title(
        f"{event_record.name}: {event_display_title(slot.title or 'Media night', slot.year)}",
        limit=100,
    )
    description = native_event_description(event_record, slot)
    end_time = slot.starts_at + timedelta(hours=4)
    location = native_event_location()
    changed = False
    remote_status = getattr(remote, "status", None) if remote is not None else None
    if remote is not None and remote_status not in (None, discord.EventStatus.scheduled):
        await remote.delete(reason=f"{marker} replaced after an invalid status transition")
        known[:] = [value for value in known if int(value.id) != int(remote.id)]
        remote = None
        current = current_native_event_slot(event_record, slot)
        if current is None:
            return False
        event_record, slot = current
    if remote is None:
        remote = await guild.create_scheduled_event(
            name=title,
            start_time=slot.starts_at,
            end_time=end_time,
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location=location,
            description=description,
            reason=marker,
        )
        if remote_events is not None and not any(
            int(candidate.id) == int(remote.id) for candidate in remote_events
        ):
            remote_events.append(remote)
        changed = True
    else:
        remote_start = getattr(remote, "start_time", None)
        remote_end = getattr(remote, "end_time", None)
        remote_entity_type = getattr(remote, "entity_type", None)
        if (
            remote.name != title
            or not native_event_datetime_matches(remote_start, slot.starts_at)
            or not native_event_datetime_matches(remote_end, end_time)
            or str(getattr(remote, "description", "") or "") != description
            or str(getattr(remote, "location", "") or "") != location
            or remote_entity_type not in (None, discord.EntityType.external)
        ):
            remote = await remote.edit(
                name=title,
                description=description,
                start_time=slot.starts_at,
                end_time=end_time,
                entity_type=discord.EntityType.external,
                location=location,
                reason=marker,
            )
            changed = True
    current = current_native_event_slot(event_record, slot)
    if current is None:
        await discard_stale_native_event(remote, guild, known, marker)
        return False
    current_event, current_slot = current
    if int(current_slot.native_scheduled_event_id or 0) != int(remote.id):
        try:
            events.set_native_scheduled_event_id(
                current_slot.slot_id,
                int(remote.id),
                expected_event=current_event,
                expected_slot=current_slot,
            )
        except (
            EventNotFoundError,
            EventStateError,
            StaleEventRevisionError,
        ):
            await discard_stale_native_event(remote, guild, known, marker)
            return False
        changed = True
    return changed


async def remove_native_events(
    event_record,
    *,
    remote_events=None,
    expected_slot_ids=None,
    expected_native_event_ids=None,
):
    guild = bot.get_guild(int(event_record.discord_guild_id))
    if guild is None:
        return 0
    if remote_events is None:
        try:
            remote_events = await guild.fetch_scheduled_events(with_counts=False)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info("Could not list native events for event=%s: %s", event_record.event_id, exc)
            return 0
    removed = 0
    scoped_slot_ids = (
        None
        if expected_slot_ids is None
        else {int(slot_id) for slot_id in expected_slot_ids}
    )
    if expected_native_event_ids is None:
        remote_ids = {
            int(slot.native_scheduled_event_id)
            for slot in events.slots(event_record.event_id)
            if slot.native_scheduled_event_id
            and (scoped_slot_ids is None or slot.slot_id in scoped_slot_ids)
        }
    else:
        remote_ids = {
            int(remote_id)
            for remote_id in expected_native_event_ids
            if remote_id is not None
        }
    for remote in remote_events:
        description = str(getattr(remote, "description", "") or "")
        marker_owned = (
            native_event_is_bot_owned(remote, guild)
            and any(
                marker_event_id == int(event_record.event_id)
                and (scoped_slot_ids is None or marker_slot_id in scoped_slot_ids)
                for marker_event_id, marker_slot_id in native_event_marker_ids(description)
            )
        )
        if (
            int(remote.id) not in remote_ids
            and not marker_owned
        ):
            continue
        try:
            await remote.delete(reason=f"MediaBot event #{event_record.event_id} retired")
            removed += 1
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            logger.info("Could not retire Discord event %s: %s", remote.id, exc)
    return removed


async def prune_native_event_orphans(guild, remote_events, active_slots):
    """Retire bot-owned orphan/duplicate events using exact durable slot markers."""

    grouped = {}
    retire = []
    for remote in tuple(remote_events):
        if not native_event_is_bot_owned(remote, guild):
            continue
        markers = native_event_marker_ids(getattr(remote, "description", ""))
        active_markers = [marker for marker in markers if marker in active_slots]
        if len(markers) != 1 or len(active_markers) != 1:
            if markers:
                retire.append(remote)
            continue
        grouped.setdefault(active_markers[0], []).append(remote)

    for marker, matches in grouped.items():
        if len(matches) <= 1:
            continue
        persisted_id = int(active_slots[marker].native_scheduled_event_id or 0)
        keep = next(
            (remote for remote in matches if int(remote.id) == persisted_id),
            min(matches, key=lambda remote: int(remote.id)),
        )
        retire.extend(remote for remote in matches if remote is not keep)

    removed_ids = set()
    failed = False
    for remote in retire:
        if int(remote.id) in removed_ids:
            continue
        try:
            await remote.delete(reason="MediaBot retired an orphaned or duplicate event")
            removed_ids.add(int(remote.id))
        except discord.NotFound:
            removed_ids.add(int(remote.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            failed = True
            logger.warning("Could not retire orphaned Discord event %s: %s", remote.id, exc)
    if removed_ids:
        remote_events[:] = [
            remote for remote in remote_events if int(remote.id) not in removed_ids
        ]
    return len(removed_ids), failed


def native_event_reconciliation_batch(
    event_record,
    slots,
    *,
    guild,
    remote_events,
    reference,
    limit=4,
):
    """Select a bounded, restart-stable batch without starving synced slots.

    New slots retain priority so a freshly published long event converges
    quickly.  Any spare capacity rotates through already-published slots using
    the absolute reconciliation interval, so every remote event is eventually
    rechecked for deletion or drift even after a process restart.
    """

    now = reference.astimezone(timezone.utc)
    future = sorted(
        (slot for slot in slots if slot.starts_at > now),
        key=lambda value: (value.starts_at, value.slot_id),
    )
    remote_ids = {int(remote.id) for remote in remote_events}
    owned_markers = {
        marker
        for remote in remote_events
        if native_event_is_bot_owned(remote, guild)
        for marker in native_event_marker_ids(getattr(remote, "description", ""))
    }

    def has_remote_identity(slot):
        return (
            slot.native_scheduled_event_id is not None
            and int(slot.native_scheduled_event_id) in remote_ids
        ) or (int(event_record.event_id), int(slot.slot_id)) in owned_markers

    missing = [slot for slot in future if not has_remote_identity(slot)]
    selected = missing[:limit]
    remaining = max(0, int(limit) - len(selected))
    synced = [slot for slot in future if has_remote_identity(slot)]
    if remaining and synced:
        cycle = int(now.timestamp() // EVENT_RECONCILE_SECONDS)
        start = (cycle * remaining) % len(synced)
        selected.extend(
            synced[(start + offset) % len(synced)]
            for offset in range(min(remaining, len(synced)))
        )
    return tuple(selected)


class EventScheduleTimeTieSelect(discord.ui.Select):
    def __init__(self, parent, options):
        super().__init__(
            placeholder="Choose the winning time",
            options=[
                discord.SelectOption(
                    label=event_time_option_label(option, parent.timezone_name)[:100],
                    value=str(option.time_option_id),
                    description=f"{option.vote_count} availability votes",
                )
                for option in options
            ],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction):
        await self.view.choose(interaction, int(self.values[0]))


class EventScheduleTimeTieView(LoggedView):
    def __init__(
        self,
        *,
        requester_id,
        guild_id,
        event_record,
        options,
        command_message,
    ):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.event_record = event_record
        self.event_id = int(event_record.event_id)
        self.timezone_name = event_record.timezone_name
        self.options = {option.time_option_id: option for option in options}
        self.command_message = command_message
        self.message = None
        self.finished = False
        self.add_item(EventScheduleTimeTieSelect(self, options))

    def build_embed(self):
        embed = discord.Embed(
            title=compact_embed_title(f"Break the time tie: {self.event_record.name}"),
            description=(
                "These times have the same availability score. MediaBot will not "
                "silently pick one; choose the time you actually want to publish."
            ),
            color=discord.Color.gold(),
        )
        add_event_line_fields(
            embed,
            "Tied times",
            [
                f"{discord.utils.format_dt(option.starts_at, style='F')} - **{option.vote_count}** available"
                for option in self.options.values()
            ],
        )
        return embed

    async def interaction_check(self, interaction):
        permissions = getattr(interaction.user, "guild_permissions", None)
        if (
            interaction.guild_id != self.guild_id
            or interaction.user.id != self.requester_id
            or not getattr(permissions, "administrator", False)
        ):
            await interaction.response.send_message(
                "Only the administrator who opened this preview can break the tie.",
                ephemeral=True,
            )
            return False
        return await renew_transient_interaction(
            interaction, self.message or getattr(interaction, "message", None)
        )

    async def choose(self, interaction, time_option_id):
        option = self.options.get(int(time_option_id))
        if option is None:
            await interaction.response.send_message(
                "That time is no longer in this tie.", ephemeral=True
            )
            return
        try:
            current = events.event(self.event_id, discord_guild_id=self.guild_id)
            if current.revision != self.event_record.revision:
                raise StaleEventRevisionError(
                    "The event changed after this time tie was displayed."
                )
            validate_future_event_times((option.starts_at,))
            plan = events.build_ranked_schedule(
                self.event_id,
                starts_at=(option.starts_at,),
            )
        except (
            EventUsageError,
            EventStateError,
            EventNotFoundError,
            StaleEventRevisionError,
        ) as exc:
            self.finished = True
            self.stop()
            await interaction.response.edit_message(content=str(exc), embed=None, view=None)
            return
        self.finished = True
        self.stop()
        view = EventScheduleView(
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            event_record=events.event(self.event_id),
            plan=plan,
            command_message=self.command_message,
        )
        view.message = self.message or interaction.message
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        self.stop()
        await cleanup_unsuccessful_request(
            origin_message=self.message,
            command_message=self.command_message,
        )


class EventScheduleView(LoggedView):
    def __init__(self, *, requester_id, guild_id, event_record, plan, command_message):
        super().__init__(timeout=REQUEST_UI_TIMEOUT)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.event_record = event_record
        self.plan = plan
        self.command_message = command_message
        self.message = None
        self.finished = False
        self.submitting = False
        confirm = discord.ui.Button(
            label="Confirm schedule", style=discord.ButtonStyle.success
        )
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm.callback = self.confirm
        cancel.callback = self.cancel
        self.add_item(confirm)
        self.add_item(cancel)

    def build_embed(self):
        return build_event_schedule_preview(self.event_record, self.plan)

    async def interaction_check(self, interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "That schedule belongs to another server.", ephemeral=True
            )
            return False
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the administrator who opened this preview can confirm it.",
                ephemeral=True,
            )
            return False
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not getattr(permissions, "administrator", False):
            await interaction.response.send_message(
                "An administrator must confirm the schedule.", ephemeral=True
            )
            return False
        return await renew_transient_interaction(
            interaction,
            self.message or getattr(interaction, "message", None),
        )

    async def confirm(self, interaction):
        if self.finished or self.submitting:
            await interaction.response.defer()
            return
        self.submitting = True
        for child in self.children:
            child.disabled = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            validate_future_event_times(
                tuple(assignment.starts_at for assignment in self.plan.assignments)
            )
            event_record, slots = events.schedule_ranked(self.plan)
        except StaleEventRevisionError as exc:
            self.finished = True
            self.stop()
            await cleanup_unsuccessful_request(
                origin_message=self.message or interaction.message,
                command_message=self.command_message,
            )
            await interaction.followup.send(
                f"{exc} Run `$event schedule` again to refresh.", ephemeral=True
            )
            return
        except (EventUsageError, EventStateError, EventNotFoundError) as exc:
            self.finished = True
            self.stop()
            await cleanup_unsuccessful_request(
                origin_message=self.message or interaction.message,
                command_message=self.command_message,
            )
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            self.submitting = False
            for child in self.children:
                child.disabled = False
            error_id = log_exception("Could not freeze event schedule", exc)
            await interaction.message.edit(embed=self.build_embed(), view=self)
            await interaction.followup.send(
                f"Couldn't save that schedule. Error ID: `{error_id}`", ephemeral=True
            )
            return

        self.finished = True
        self.stop()
        mark_transient_message_terminal(self.message or interaction.message, "accepted")
        await interaction.message.edit(
            embed=build_event_schedule_receipt(event_record, slots), view=None
        )
        await refresh_event_dashboard(event_record.event_id)
        await interaction.followup.send(
            "Schedule saved. Discord event publishing and reminders are queued.",
            ephemeral=True,
        )

    async def cancel(self, interaction):
        if self.finished or self.submitting:
            await interaction.response.defer()
            return
        self.submitting = True
        self.finished = True
        self.stop()
        await interaction.response.defer()
        await cleanup_unsuccessful_request(
            origin_message=self.message or interaction.message,
            command_message=self.command_message,
        )

    async def on_timeout(self):
        if self.finished or self.submitting:
            return
        self.finished = True
        self.stop()
        await cleanup_unsuccessful_request(
            origin_message=self.message,
            command_message=self.command_message,
        )



# ============================================================
# JELLYFIN REQUEST AVAILABILITY WATCHER
# ============================================================

JELLYFIN_POLL_SECONDS = int(
    os.environ.get(
        "JELLYFIN_POLL_SECONDS",
        "60"
    )
)


def tracked_request_seasons(record):
    raw = str(record["requested_seasons"] or "")
    seasons = set()

    for value in raw.split(","):
        try:
            season = int(value)
        except (TypeError, ValueError):
            continue

        if season > 0:
            seasons.add(season)

    return tuple(sorted(seasons))


def tracked_episode_counts(record):
    try:
        raw = json.loads(record["requested_episode_counts"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}

    counts = {}

    for season, count in raw.items():
        try:
            counts[int(season)] = max(0, int(count))
        except (TypeError, ValueError):
            continue

    return counts


def tracked_episode_numbers(record):
    try:
        raw = json.loads(record["requested_episode_numbers"] or "{}")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError):
        raw = {}

    numbers = {}
    for season, episodes in raw.items():
        try:
            season_number = int(season)
            episode_numbers = {
                int(episode)
                for episode in episodes
                if int(episode) > 0
            }
        except (TypeError, ValueError):
            continue

        if season_number > 0 and episode_numbers:
            numbers[season_number] = episode_numbers

    # Backward-compatible rows recorded by v0.6.0 only stored counts.
    if not numbers:
        numbers = {
            season: set(range(1, count + 1))
            for season, count in tracked_episode_counts(record).items()
            if count > 0
        }

    return numbers


async def tracked_seasons_are_available(record, jellyfin_item):
    seasons = tracked_request_seasons(record)

    if record["media_type"] != "tv" or not seasons:
        return True

    actual_numbers = await jellyfin.series_season_episode_numbers(
        jellyfin_item["Id"]
    )
    expected_numbers = tracked_episode_numbers(record)

    return all(
        expected_numbers.get(season, {1}).issubset(
            actual_numbers.get(season, set())
        )
        for season in seasons
    )


async def run_jellyfin_availability_once():

    if not jellyfin.enabled:
        return

    records = pending_requests(
        limit=1000
    )

    for record in records:

        try:
            if int(record["seerr_request_id"]) > 0:
                try:
                    live_request = await seerr.request_details(
                        int(record["seerr_request_id"])
                    )
                    live_status = REQUEST_STATUS.get(live_request.get("status"))
                    if live_status:
                        update_tracked_request_status(
                            seerr_request_id=record["seerr_request_id"],
                            request_status=live_status,
                        )
                        if live_request.get("status") in {3, 4}:
                            continue
                except Exception as exc:
                    logger.info(
                        "Seerr request reconciliation unavailable request=%s: %s",
                        record["seerr_request_id"],
                        exc,
                    )

            jf_item = await jellyfin.find_by_tmdb(
                tmdb_id=record["tmdb_id"],
                title=record["title"],
                media_type=record["media_type"]
            )

            if not jf_item:
                continue

            if not await tracked_seasons_are_available(record, jf_item):
                continue

            # Library truth must not depend on whether an old Discord message
            # still exists or remains editable.
            mark_available(
                seerr_request_id=record["seerr_request_id"],
                jellyfin_item_id=jf_item["Id"],
            )

            channel = bot.get_channel(
                int(
                    record[
                        "discord_channel_id"
                    ]
                )
            )

            if channel is None:

                channel = (
                    await bot.fetch_channel(
                        int(
                            record[
                                "discord_channel_id"
                            ]
                        )
                    )
                )

            message = await channel.fetch_message(
                int(
                    record[
                        "discord_message_id"
                    ]
                )
            )

            media_type = record[
                "media_type"
            ]

            tmdb_id = int(
                record["tmdb_id"]
            )

            try:

                details = await seerr.request(
                    "GET",
                    f"/{media_type}/{tmdb_id}"
                )

            except Exception:

                details = {
                    "id": tmdb_id,
                    "mediaType":
                        media_type,

                    "title":
                        record["title"],

                    "name":
                        record["title"],

                    "overview":
                        "Available in Jellyfin.",
                }

            details[
                "mediaType"
            ] = media_type

            item = dict(details)

            direct_repair = int(record["seerr_request_id"]) < 0
            exact_arrivals = tracked_episode_numbers(record) if direct_repair else {}
            exact_arrival_text = "; ".join(
                f"S{season} {compact_episode_ranges(episodes)}"
                for season, episodes in sorted(exact_arrivals.items())
            )

            final_embed = build_media_embed(
                item,
                details,
                heading="Availability",
                state_text=(
                    (
                        "**Requested episodes arrived in Jellyfin**"
                        if direct_repair
                        else "**Available in Jellyfin**"
                    )
                    + (
                        "\nEpisodes received: `"
                        + exact_arrival_text
                        + "`"
                        if direct_repair and exact_arrival_text
                        else "\nRequested seasons complete: `"
                        + ", ".join(map(str, tracked_request_seasons(record)))
                        + "`"
                        if tracked_request_seasons(record)
                        else ""
                    )
                ),
                color=discord.Color.green()
            )

            final_embed.set_footer(
                text=(
                    (
                        f"Originally requested as Seerr request "
                        f"#{record['seerr_request_id']}"
                    )
                    if int(record["seerr_request_id"]) > 0
                    else "Originally requested as an exact Sonarr episode repair"
                )
            )

            await message.edit(
                embed=final_embed,
                view=JellyfinAvailableView(
                    item=item,
                    jellyfin_item_id=(
                        jf_item["Id"]
                    )
                )
            )

            logger.info(
                (
                    "JELLYFIN AVAILABLE | "
                    "request_id=%s | "
                    "title=%s | "
                    "jellyfin_item=%s"
                ),
                record[
                    "seerr_request_id"
                ],
                record["title"],
                jf_item["Id"]
            )

        except Exception as exc:

            log_exception(
                (
                    "Jellyfin availability "
                    f"reconciliation failed "
                    f"request_id="
                    f"{record['seerr_request_id']}"
                ),
                exc
            )


@tasks.loop(seconds=JELLYFIN_POLL_SECONDS)
async def jellyfin_availability_watcher():
    """Keep reconciliation failures isolated to one polling cycle."""
    global LAST_JELLYFIN_RECONCILIATION
    try:
        await run_jellyfin_availability_once()
    except Exception as exc:
        log_exception("Jellyfin availability watcher cycle failed", exc)
        LAST_JELLYFIN_RECONCILIATION = {
            "completed_at": time.time(),
            "failed": True,
        }
        return
    LAST_JELLYFIN_RECONCILIATION = {
        "completed_at": time.time(),
        "failed": False,
    }


@jellyfin_availability_watcher.before_loop
async def before_jellyfin_watcher():

    await bot.wait_until_ready()


async def run_transient_ui_cleanup_once(*, now=None, claim_token=None):
    """Delete expired cards and atomically retire disposable commands."""
    current = float(now if now is not None else time.time())
    worker_token = claim_token or f"expiry:{secrets.token_hex(10)}"
    stats = {
        "claimed": 0,
        "cards": 0,
        "commands": 0,
        "preserved": 0,
        "retried": 0,
        "failed": False,
    }

    # Retry commands independently from card expiry. Without this pass, a
    # transient Discord failure after the card became terminal would never be
    # revisited because there is no longer an active entry to wake it up.
    try:
        command_claims = transient_ui_store.claim_deletable_batch_commands(
            f"{worker_token}:commands",
            now=current,
            limit=100,
            lease_seconds=90,
        )
    except Exception as exc:
        log_exception("Could not claim pending transient commands", exc)
        stats["failed"] = True
        command_claims = []

    for claim in command_claims:
        deleted = await delete_discord_message_by_id(
            claim.channel_id,
            claim.command_message_id,
            label="expired request command retry",
        )
        finalized = finalize_transient_command_claim(claim, deleted)
        if finalized:
            stats["commands"] += 1
        else:
            stats["retried"] += 1

    try:
        records = transient_ui_store.claim_expired(
            worker_token,
            now=current,
            limit=100,
            lease_seconds=90,
        )
    except Exception as exc:
        log_exception("Could not claim expired transient UIs", exc)
        stats["failed"] = True
        return stats

    stats["claimed"] = len(records)
    for record in records:
        retired_on_discord = False
        preserve_interactive_success = record.kind in {
            "rating_saved_actions",
            "event_vote_saved_actions",
            "event_time_vote_saved_actions",
        }
        try:
            message = await discord_message_by_id(
                record.channel_id,
                record.card_message_id,
            )
            latest = transient_ui_store.get(record.entry_id)
            if (
                latest is None
                or latest.state != "active"
                or latest.claim_token != worker_token
            ):
                continue
            if preserve_interactive_success:
                edit_kwargs = {"view": None}
                static_embed = expired_interactive_success_embed(
                    message,
                    record.kind,
                )
                if static_embed is not None:
                    edit_kwargs["embed"] = static_embed
                await message.edit(**edit_kwargs)
                retired_on_discord = True
            else:
                retired_on_discord = await delete_message_safely(
                    message,
                    label="expired transient card",
                )
        except discord.NotFound:
            retired_on_discord = True
        except Exception as exc:
            logger.warning(
                "Could not retire expired transient card entry=%s error=%s",
                record.entry_id,
                exc,
            )

        if not retired_on_discord:
            try:
                transient_ui_store.release_claim(
                    record.entry_id,
                    worker_token,
                    retry_at=current + 30,
                    now=current,
                )
                stats["retried"] += 1
            except Exception as exc:
                log_exception("Could not release transient UI expiry claim", exc)
            continue

        try:
            retired = transient_ui_store.mark_terminal(
                record.entry_id,
                "kept" if preserve_interactive_success else "expired",
                claim_token=worker_token,
                now=current,
            )
        except Exception as exc:
            log_exception("Could not finalize expired transient card", exc)
            retired = False
        if not retired:
            continue

        stats["cards"] += 1
        if preserve_interactive_success:
            stats["preserved"] += 1
            continue
        command_deleted = await finish_transient_batch_command(record)
        if command_deleted is True:
            stats["commands"] += 1
        elif command_deleted is False:
            stats["retried"] += 1

    try:
        transient_ui_store.purge_terminal(
            before=current - (7 * 24 * 60 * 60),
            limit=500,
        )
    except Exception as exc:
        log_exception("Could not purge retired transient UI records", exc)
        stats["failed"] = True
    return stats


@tasks.loop(seconds=30)
async def transient_ui_cleanup_watcher():
    global LAST_TRANSIENT_CLEANUP
    try:
        stats = await run_transient_ui_cleanup_once()
    except Exception as exc:
        log_exception("Transient UI cleanup watcher cycle failed", exc)
        LAST_TRANSIENT_CLEANUP = {
            **LAST_TRANSIENT_CLEANUP,
            "completed_at": time.time(),
            "failed": True,
        }
        return
    LAST_TRANSIENT_CLEANUP = {
        **stats,
        "completed_at": time.time(),
    }
    if (
        stats["cards"]
        or stats["commands"]
        or stats["preserved"]
        or stats["retried"]
    ):
        logger.info(
            "TRANSIENT UI CLEANUP | claimed=%s cards=%s commands=%s "
            "preserved=%s retried=%s",
            stats["claimed"],
            stats["cards"],
            stats["commands"],
            stats["preserved"],
            stats["retried"],
        )


@transient_ui_cleanup_watcher.before_loop
async def before_transient_ui_cleanup_watcher():
    await bot.wait_until_ready()


def build_event_reminder_embed(reminder):
    stage_title = {
        1: "Tomorrow",
        2: "Starting soon",
        3: "Starting now",
    }.get(int(reminder.stage), "Upcoming event")
    title = event_display_title(reminder.title or "Media night", reminder.year)
    embed = discord.Embed(
        title=compact_embed_title(f"{stage_title}: {reminder.event_name}"),
        description=(
            f"**{title}**\n"
            f"{discord.utils.format_dt(reminder.starts_at, style='F')} "
            f"({discord.utils.format_dt(reminder.starts_at, style='R')})"
        ),
        color=discord.Color.green() if int(reminder.stage) == 3 else discord.Color.blurple(),
    )
    if reminder.jellyfin_item_id:
        embed.add_field(
            name="Watch",
            value=f"[Open in Jellyfin]({jellyfin.watch_url(reminder.jellyfin_item_id)})",
            inline=False,
        )
    embed.set_footer(text=f"Event #{reminder.event_id} - reminder {reminder.stage}/3")
    return embed


def event_reminder_allowed_mentions():
    if EVENT_REMINDER_ROLE_ID is None:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=EVENT_REMINDER_ROLE_ID)],
        replied_user=False,
    )


def event_reminder_role_delivery_error(reminder):
    """Return why a configured role cannot be pinged, before claiming delivery."""

    if EVENT_REMINDER_ROLE_ID is None:
        return None
    guild = bot.get_guild(int(reminder.discord_guild_id))
    if guild is None:
        return f"Discord guild {reminder.discord_guild_id} is unavailable"
    role = guild.get_role(int(EVENT_REMINDER_ROLE_ID))
    if role is None:
        return f"configured reminder role {EVENT_REMINDER_ROLE_ID} does not exist"
    is_default = getattr(role, "is_default", None)
    if int(getattr(role, "id", 0)) == int(guild.id) or (
        callable(is_default) and is_default()
    ):
        return "the @everyone role cannot be used for event reminders"
    permissions = getattr(getattr(guild, "me", None), "guild_permissions", None)
    if not getattr(role, "mentionable", False) and not getattr(
        permissions, "mention_everyone", False
    ):
        return (
            f"reminder role {EVENT_REMINDER_ROLE_ID} is not mentionable and MediaBot "
            "lacks Mention Everyone permission"
        )
    return None


async def run_event_reconciliation_once(*, reference=None):
    """Reconcile event rollover, native Discord events, and one-shot reminders."""

    now = reference or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stats = {"reminders": 0, "native_events": 0, "completed": 0, "failed": False}

    for guild_id in sorted(ALLOWED_GUILD_IDS):
        scheduled = events.list_events(
            discord_guild_id=guild_id,
            statuses=(EventStatus.SCHEDULED,),
            limit=500,
        )
        scheduled_with_slots = tuple(
            (event_record, events.slots(event_record.event_id))
            for event_record in scheduled
        )
        active_slots = {
            (event_record.event_id, slot.slot_id): slot
            for event_record, slots in scheduled_with_slots
            for slot in slots
        }
        has_event_history = bool(scheduled) or bool(
            events.list_events(
                discord_guild_id=guild_id,
                limit=1,
                include_archived=True,
            )
        )
        guild = bot.get_guild(guild_id)
        remote_events = None
        if has_event_history:
            if guild is None:
                stats["failed"] = True
                logger.warning("Native event guild unavailable guild=%s", guild_id)
            else:
                try:
                    remote_events = list(
                        await guild.fetch_scheduled_events(with_counts=False)
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    stats["failed"] = True
                    logger.warning(
                        "Native event listing unavailable guild=%s: %s", guild_id, exc
                    )
        if guild is not None and remote_events is not None:
            removed, prune_failed = await prune_native_event_orphans(
                guild,
                remote_events,
                active_slots,
            )
            stats["native_events"] += removed
            stats["failed"] = stats["failed"] or prune_failed
        for event_record, slots in scheduled_with_slots:
            if slots and max(slot.starts_at for slot in slots) + timedelta(
                hours=EVENT_COMPLETION_GRACE_HOURS
            ) <= now:
                try:
                    completed = events.complete(
                        event_record.event_id,
                        expected_revision=event_record.revision,
                    )
                    await refresh_event_dashboard(completed.event_id)
                    if remote_events is not None:
                        await remove_native_events(
                            completed,
                            remote_events=remote_events,
                        )
                    stats["completed"] += 1
                except StaleEventRevisionError:
                    logger.info(
                        "Skipped stale event rollover event=%s",
                        event_record.event_id,
                    )
                except Exception as exc:
                    stats["failed"] = True
                    log_exception(
                        f"Could not roll over past event {event_record.event_id}", exc
                    )
                continue

            if remote_events is None:
                continue
            native_batch = native_event_reconciliation_batch(
                event_record,
                slots,
                guild=guild,
                remote_events=remote_events,
                reference=now,
            )
            for slot in native_batch:
                try:
                    if await sync_native_event_slot(
                        event_record, slot, remote_events=remote_events
                    ):
                        stats["native_events"] += 1
                except (
                    EventNativeSyncError,
                    discord.Forbidden,
                    discord.HTTPException,
                ) as exc:
                    stats["failed"] = True
                    logger.warning(
                        "Native event sync failed event=%s slot=%s error=%s",
                        event_record.event_id,
                        slot.slot_id,
                        exc,
                    )
                except Exception as exc:
                    stats["failed"] = True
                    log_exception(
                        f"Native event sync failed event={event_record.event_id} slot={slot.slot_id}",
                        exc,
                    )

    for reminder in events.due_reminders(reference=now):
        if int(reminder.stage) == 3 and now > reminder.starts_at + timedelta(minutes=15):
            # Never wake a server with a stale "starting now" ping after downtime.
            events.advance_reminder(reminder.slot_id, reminder.stage)
            continue
        role_error = event_reminder_role_delivery_error(reminder)
        if role_error:
            stats["failed"] = True
            logger.warning(
                "Reminder role unavailable event=%s slot=%s: %s",
                reminder.event_id,
                reminder.slot_id,
                role_error,
            )
            continue
        channel = bot.get_channel(int(reminder.discord_channel_id))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(reminder.discord_channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                stats["failed"] = True
                logger.warning(
                    "Reminder channel unavailable event=%s channel=%s error=%s",
                    reminder.event_id,
                    reminder.discord_channel_id,
                    exc,
                )
                continue
        try:
            if not events.claim_reminder(
                reminder.slot_id,
                reminder.stage,
                event_id=reminder.event_id,
                starts_at=reminder.starts_at,
            ):
                continue
            await channel.send(
                content=EVENT_REMINDER_MENTION or None,
                embed=build_event_reminder_embed(reminder),
                allowed_mentions=event_reminder_allowed_mentions(),
            )
            stats["reminders"] += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            stats["failed"] = True
            logger.warning(
                "Reminder delivery failed event=%s slot=%s error=%s",
                reminder.event_id,
                reminder.slot_id,
                exc,
            )
        except Exception as exc:
            stats["failed"] = True
            log_exception(
                f"Reminder delivery failed event={reminder.event_id} slot={reminder.slot_id}",
                exc,
            )
    return stats


@tasks.loop(seconds=EVENT_RECONCILE_SECONDS)
async def event_lifecycle_watcher():
    global LAST_EVENT_RECONCILIATION
    try:
        stats = await run_event_reconciliation_once()
    except Exception as exc:
        log_exception("Event lifecycle watcher cycle failed", exc)
        stats = {"reminders": 0, "native_events": 0, "completed": 0, "failed": True}
    LAST_EVENT_RECONCILIATION = {**stats, "completed_at": time.time()}


@event_lifecycle_watcher.before_loop
async def before_event_lifecycle_watcher():
    await bot.wait_until_ready()


def register_persistent_event_dashboards():
    registered = 0
    for guild_id in sorted(ALLOWED_GUILD_IDS):
        records = events.list_events(
            discord_guild_id=guild_id,
            statuses=(EventStatus.OPEN, EventStatus.SCHEDULED),
            limit=100,
        )
        for event_record in records:
            message_id = event_record.dashboard_message_id
            if not message_id or int(message_id) in _registered_event_dashboard_messages:
                continue
            bot.add_view(EventDashboardView(event_record), message_id=int(message_id))
            _registered_event_dashboard_messages.add(int(message_id))
            registered += 1
    return registered


def runtime_worker_snapshot():
    return {
        "transient_ui_cleanup": transient_ui_cleanup_watcher.is_running(),
        "event_lifecycle": event_lifecycle_watcher.is_running(),
        "jellyfin_availability": (
            not jellyfin.enabled or jellyfin_availability_watcher.is_running()
        ),
    }


def runtime_metric_snapshot(now=None):
    current = float(time.time() if now is None else now)
    intent_stats = media_request_intent_stats()

    def age(value):
        return None if value is None else max(0, int(current - float(value)))

    return {
        "uptime_seconds": max(0, int(current - RUNTIME_STARTED_AT)),
        "guilds": len(bot.guilds),
        "cleanup_last_run_age_seconds": age(
            LAST_TRANSIENT_CLEANUP.get("completed_at")
        ),
        "cleanup_retried": int(LAST_TRANSIENT_CLEANUP.get("retried") or 0),
        "cleanup_failed": bool(LAST_TRANSIENT_CLEANUP.get("failed", False)),
        "jellyfin_last_run_age_seconds": age(
            LAST_JELLYFIN_RECONCILIATION.get("completed_at")
        ),
        "jellyfin_cycle_failed": bool(
            LAST_JELLYFIN_RECONCILIATION.get("failed", False)
        ),
        "event_last_run_age_seconds": age(
            LAST_EVENT_RECONCILIATION.get("completed_at")
        ),
        "event_cycle_max_age_seconds": EVENT_CYCLE_MAX_AGE_SECONDS,
        "event_cycle_failed": bool(
            LAST_EVENT_RECONCILIATION.get("failed", False)
        ),
        "event_reminders_sent": int(
            LAST_EVENT_RECONCILIATION.get("reminders") or 0
        ),
        "request_intents_prepared": intent_stats["prepared"],
        "request_intents_accepted": intent_stats["accepted"],
    }


def write_runtime_health_snapshot(state=None):
    ready = bool(bot.is_ready())
    return write_runtime_health(
        RUNTIME_HEALTH_PATH,
        version=BOT_VERSION,
        state=state or ("ready" if ready else "reconnecting"),
        discord_ready=ready,
        workers=runtime_worker_snapshot(),
        metrics=runtime_metric_snapshot(),
    )


@tasks.loop(seconds=30)
async def runtime_health_watcher():
    try:
        write_runtime_health_snapshot()
    except Exception as exc:
        log_exception("Could not write runtime health heartbeat", exc)


@runtime_health_watcher.before_loop
async def before_runtime_health_watcher():
    await bot.wait_until_ready()


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_interaction(interaction):
    """Turn orphaned component clicks into a useful expiry message."""
    if interaction.type != discord.InteractionType.component:
        return

    message = interaction.message
    if (
        message is not None
        and bot.user is not None
        and message.author.id != bot.user.id
    ):
        return

    # Registered views renew their durable lease from their authorized
    # interaction_check. Do not renew here: orphan clicks and clicks from a
    # different user must not extend an abandoned menu.
    active_record = transient_record_for_message(message, active_only=True)
    lifecycle_record = active_record or transient_record_for_message(
        message,
        active_only=False,
    )

    # Registered view callbacks are dispatched just before this event. Give
    # them time to acknowledge normally, then handle only an untouched
    # interaction left behind by a restart or expired in-memory view.
    await asyncio.sleep(2.0)
    if interaction.response.is_done():
        return

    try:
        await interaction.response.send_message(
            "That menu expired or MediaBot restarted. Run the command again "
            "to get a fresh one.",
            ephemeral=True,
        )
    except discord.InteractionResponded:
        pass
    except discord.HTTPException as exc:
        logger.info("Could not answer an orphaned component interaction: %s", exc)

    if (
        active_record is not None
        and active_record.kind in {
            "rating_saved_actions",
            "event_vote_saved_actions",
            "event_time_vote_saved_actions",
        }
    ):
        try:
            edit_kwargs = {"view": None}
            static_embed = expired_interactive_success_embed(
                message,
                active_record.kind,
            )
            if static_embed is not None:
                edit_kwargs["embed"] = static_embed
            await message.edit(**edit_kwargs)
            transient_ui_store.mark_terminal(active_record.entry_id, "kept")
        except Exception as exc:
            logger.info("Could not retire orphaned saved actions: %s", exc)
    elif active_record is not None:
        await cleanup_unsuccessful_request(origin_message=message)
    elif (
        lifecycle_record is not None
        and lifecycle_record.state in {"accepted", "kept"}
        and message is not None
    ):
        # A crash can occur after the external action and durable terminal mark
        # but before the public edit removes buttons. Preserve the successful
        # card while making those stale components inert.
        try:
            await message.edit(view=None)
        except Exception as exc:
            logger.info("Could not strip orphaned terminal controls: %s", exc)


@bot.event
async def on_ready():

    for guild in tuple(bot.guilds):
        if guild.id in ALLOWED_GUILD_IDS:
            continue
        logger.warning("Leaving untrusted Discord guild guild=%s name=%r", guild.id, guild.name)
        try:
            await guild.leave()
        except Exception as exc:
            log_exception(f"Could not leave untrusted guild {guild.id}", exc)

    if (
        jellyfin.enabled
        and not jellyfin_availability_watcher.is_running()
    ):
        jellyfin_availability_watcher.start()
    if not transient_ui_cleanup_watcher.is_running():
        transient_ui_cleanup_watcher.start()
    if not event_lifecycle_watcher.is_running():
        event_lifecycle_watcher.start()
    if not runtime_health_watcher.is_running():
        runtime_health_watcher.start()
    try:
        registered = register_persistent_event_dashboards()
        if registered:
            logger.info("Registered %s persistent event dashboard(s)", registered)
    except Exception as exc:
        log_exception("Could not register persistent event dashboards", exc)
    try:
        write_runtime_health_snapshot("ready")
    except Exception as exc:
        log_exception("Could not mark runtime ready", exc)
    logger.info(
        "MediaBot online | discord_user=%s | discord_id=%s | prefix=%s | seerr=%s",
        bot.user,
        bot.user.id,
        PREFIX,
        SEERR_URL
    )

    print("=" * 60)
    print("MediaBot online")
    print(f"Discord user: {bot.user}")
    print(f"Discord ID:   {bot.user.id}")
    print(f"Prefix:       {PREFIX}")
    print(f"Seerr:        {SEERR_URL}")
    print("=" * 60)


@bot.event
async def on_disconnect():
    try:
        write_runtime_health_snapshot("reconnecting")
    except Exception as exc:
        logger.warning("Could not mark runtime reconnecting: %s", exc)


@bot.event
async def on_guild_join(guild):
    if guild.id in ALLOWED_GUILD_IDS:
        return
    logger.warning("Rejecting untrusted Discord guild guild=%s name=%r", guild.id, guild.name)
    try:
        await guild.leave()
    except Exception as exc:
        log_exception(f"Could not reject untrusted guild {guild.id}", exc)


# ============================================================
# BASIC COMMANDS
# ============================================================

# ============================================================
# AUTOMATIC HELP
# ============================================================

HELP_FALLBACKS = {
    "help": (
        "Show the complete current command tree or "
        "detailed help for a command."
    ),
    "ping": (
        "Check whether MediaBot is online and show "
        "Discord latency."
    ),
    "version": (
        "Show the currently running MediaBot version."
    ),
    "whoami": (
        "Show your linked Discord and Seerr identity."
    ),
    "request": (
        "Search for a movie or show, choose the exact result, and request it."
    ),
    "report": (
        "Report a playback, audio, subtitle, quality, or wrong-episode problem."
    ),
    "event": (
        "Plan shared media nights with nominations, voting, scheduling, and a "
        "read-only tonight list."
    ),
    "admin": (
        "Administrative commands for MediaBot."
    ),
    "health": (
        "Check MediaBot and Seerr connectivity."
    ),
    "users": (
        "List Seerr users available for account linking."
    ),
    "link": (
        "Link a Discord member to a Seerr account."
    ),
    "logs": (
        "Show recent MediaBot application logs."
    ),
    "errors": (
        "Show recent warnings, errors, and error IDs."
    ),
}

COMMAND_USAGE = {
    "help": "help [command]",
    "request": "request <title> [year]",
    "report": "report <title> [SxxExx]",
    "event": "event",
    "event create": "event create <name> [--votes N]",
    "event nominate": "event nominate <title> [year]",
    "event vote": "event vote",
    "event time": "event time [YYYY-MM-DD HH:MM, ...]",
    "event schedule": "event schedule [YYYY-MM-DD HH:MM, ...]",
    "event reschedule": "event reschedule <event id> [YYYY-MM-DD HH:MM, ...]",
    "event reopen": "event reopen <event id>",
    "event tonight": "event tonight",
    "event history": "event history",
    "event archive": "event archive <event id>",
    "event clear": "event clear",
    "event complete": "event complete <event id>",
    "event cancel": "event cancel <event id>",
    "music": "music <artist and track>",
    "discover": "discover [movie|show] [genres] [--count N] [--top N] [--random]",
    "recommend": "recommend [movie|show] [genres] [--count N] [--top N] [--random]",
    "rate": "rate [title and year] [1-10]",
    "ratings": "ratings [page]",
    "status": "status <title or #request>",
    "new": "new [count]",
    "admin reports": "admin reports [page]",
    "admin reports claim": "admin reports claim <id>",
    "admin reports resolve": "admin reports resolve <id> [note]",
    "admin reports dismiss": "admin reports dismiss <id> [note]",
}


def command_description(command):
    return (
        command.help
        or HELP_FALLBACKS.get(
            command.name
        )
        or "No description yet."
    )


def command_usage(
    command,
    prefix="$"
):
    custom = COMMAND_USAGE.get(command.qualified_name)
    if custom:
        return f"{prefix}{custom}"

    usage = (
        f"{prefix}"
        f"{command.qualified_name}"
    )

    if command.signature:
        usage += (
            f" {command.signature}"
        )

    return usage


def walk_command_tree(
    command,
    *,
    prefix="$",
    depth=0
):
    indent = "    " * depth

    lines = [
        (
            f"{indent}`"
            f"{command_usage(command, prefix)}"
            f"`\n"
            f"{indent}{command_description(command)}"
        )
    ]

    if isinstance(
        command,
        commands.Group
    ):
        children = sorted(
            command.commands,
            key=lambda child: child.name
        )

        for child in children:
            lines.extend(
                walk_command_tree(
                    child,
                    prefix=prefix,
                    depth=depth + 1
                )
            )

    return lines


@bot.command(
    name="help",
    aliases=["h", "?"],
    help=(
        "Show the complete current command tree or "
        "detailed help for one command."
    )
)
async def mediabot_help(
    ctx,
    *,
    topic: str = None
):
    prefix = ctx.clean_prefix
    normalized_topic = " ".join((topic or "").split()).casefold()
    permissions = getattr(ctx.author, "guild_permissions", None)
    is_administrator = bool(getattr(permissions, "administrator", False))

    if normalized_topic and normalized_topic not in {"all", "advanced"}:
        topic = " ".join(
            topic.split()
        )

        command = bot.get_command(
            topic
        )

        if command is None:
            await ctx.reply(
                (
                    f"No command named `{topic}` exists.\n\n"
                    f"Run `{prefix}help` for the current "
                    "command tree."
                )
            )

            return

        if (
            command.qualified_name.split()[0] == "admin"
            and not is_administrator
        ):
            await ctx.reply("Administrator commands are not available to this account.")
            return

        requested_name = normalized_topic or command.qualified_name
        embed = discord.Embed(
            title=compact_embed_title(f"Help: {prefix}{requested_name}"),
            description=(
                command_description(command)
            ),
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="Usage",
            value=(
                f"`{command_usage(command, prefix)}`"
            ),
            inline=False
        )

        if isinstance(
            command,
            commands.Group
        ):
            children = sorted(
                command.commands,
                key=lambda child: child.name
            )

            if children:
                child_lines = []

                for child in children:
                    child_lines.append(
                        (
                            f"`{command_usage(child, prefix)}`\n"
                            f"{command_description(child)}"
                        )
                    )

                for index, chunk in enumerate(
                    compact_embed_chunks(child_lines, limit=1024, max_chunks=8),
                    start=1,
                ):
                    embed.add_field(
                        name="Subcommands" if index == 1 else "Subcommands (continued)",
                        value=chunk,
                        inline=False,
                    )

        if command.aliases:
            alias_lines = [f"`{prefix}{alias}`" for alias in command.aliases]
            behavior = None
            if requested_name in {"random", "randomrequest", "rr"}:
                behavior = "This legacy alias automatically adds `--random`."
            elif requested_name == "ratings":
                behavior = "This alias lists your ratings. Use `$ratings 2` for page 2."
            embed.add_field(
                name="Aliases",
                value=", ".join(alias_lines) + (f"\n{behavior}" if behavior else ""),
                inline=False,
            )

        await ctx.reply(
            embed=embed
        )

        return

    embed = discord.Embed(
        title="Dogginator Media",
        description=(
            f"MediaBot **v{BOT_VERSION}**\n\n"
            "Pick what you want to do. You never need quotes around a title."
        ),
        color=discord.Color.blurple()
    )

    if normalized_topic == "advanced":
        utility_lines = [
            f"`{prefix}new` - recently added media",
            f"`{prefix}whoami` - linked request identity",
            f"`{prefix}ping` - connection latency",
            f"`{prefix}version` - running version",
        ]
        if is_administrator:
            utility_lines.append(f"`{prefix}admin` - administrator tools")
        embed.add_field(
            name="Utilities",
            value="\n".join(utility_lines),
            inline=False,
        )
        embed.set_footer(text=f"Use {prefix}help all for the complete command tree")
        await ctx.reply(embed=embed)
        return

    if normalized_topic != "all":
        embed.add_field(
            name="Movies and shows",
            value=(
                f"`{prefix}request <title>` - request something specific\n"
                f"`{prefix}recommend [genres]` - find something new for you\n"
                f"`{prefix}discover [genres]` - browse what is playable now\n"
                f"`{prefix}new` - see what was recently added\n"
                f"`{prefix}report <title>` - report a playback problem"
            ),
            inline=False,
        )
        embed.add_field(
            name="Music, ratings, and progress",
            value=(
                f"`{prefix}music <artist and track>` - request a song\n"
                f"`{prefix}rate <title> <1-10>` - choose and rate the exact title\n"
                f"`{prefix}ratings [page]` - show your ratings\n"
                f"`{prefix}status <title or #request>` - check any request"
            ),
            inline=False,
        )
        embed.add_field(
            name="Shared media nights",
            value=(
                f"`{prefix}event` - current event dashboard\n"
                f"`{prefix}event nominate <title>` - add an exact title\n"
                f"`{prefix}event vote` - choose your favorites\n"
                f"`{prefix}event time` - vote on when everyone is available\n"
                f"`{prefix}event tonight` - see tonight's schedule"
            ),
            inline=False,
        )
        embed.add_field(
            name="Discover versus recommend",
            value=(
                "**Discover** only shows media already in Jellyfin. "
                "**Recommend** shows unseen media that can be requested.\n"
                "Add `--random` to either one and `--count 3` for several."
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"Use {prefix}help <command> for details or "
                f"{prefix}help advanced for utilities"
            )
        )
        await ctx.reply(embed=embed)
        return

    top_level_commands = sorted(
        [
            command
            for command in bot.commands
            if (
                not command.hidden
                and command.name
                not in command.aliases
                and (command.name != "admin" or is_administrator)
            )
        ],
        key=lambda command: command.name
    )

    rendered = []

    for command in top_level_commands:
        rendered.extend(
            walk_command_tree(
                command,
                prefix=prefix
            )
        )

    # Discord fields cap at 1024 characters, so split
    # without losing the generated command tree.
    chunks = []
    current = ""

    for block in rendered:
        candidate = (
            current
            + ("\n\n" if current else "")
            + block
        )

        if len(candidate) > 1000:
            if current:
                chunks.append(
                    current
                )

            current = block

        else:
            current = candidate

    if current:
        chunks.append(
            current
        )

    for index, chunk in enumerate(
        chunks,
        start=1
    ):
        title = (
            "Commands"
            if index == 1
            else f"Commands continued ({index})"
        )

        embed.add_field(
            name=title,
            value=chunk,
            inline=False
        )

    embed.set_footer(
        text=(
            f"Use {prefix}help <command> "
            "for details."
        )
    )

    await ctx.reply(
        embed=embed
    )


@bot.command(
    name="version",
    help=(
        "Show the currently running MediaBot version."
    )
)
async def version(
    ctx
):
    await ctx.reply(
        f"Dogginator MediaBot **v{BOT_VERSION}**"
    )


@bot.command()
async def ping(ctx):
    await ctx.reply(
        f"Pong. `{round(bot.latency * 1000)} ms`"
    )


async def send_private_output(
    ctx,
    *,
    content=None,
    file=None,
    public_success="I sent that to you privately.",
    committed=False,
):
    """Deliver sensitive output by DM without leaking it when DMs are closed."""

    try:
        kwargs = {}
        if content is not None:
            kwargs["content"] = content
        if file is not None:
            kwargs["file"] = file
        await ctx.author.send(**kwargs)
    except Exception as exc:
        logger.info(
            "Private Discord delivery failed user=%s type=%s",
            getattr(ctx.author, "id", None),
            type(exc).__name__,
        )
        if ctx.guild is not None:
            await ctx.reply(
                (
                    "The change was saved, but I couldn't DM the private details. "
                    "Enable direct messages and run the read-only command again."
                    if committed
                    else "I couldn't DM those private details. Enable direct messages "
                    "for this server and try again. Nothing sensitive was posted here."
                ),
                delete_after=30,
            )
        return False

    if ctx.guild is not None and public_success:
        await ctx.reply(public_success, delete_after=15)
    return True


@bot.command()
async def whoami(ctx):
    link = get_link(ctx.author.id)

    if not link:
        await send_private_output(
            ctx,
            content=(
                "Discord account: "
                f"**{ctx.author.display_name}**\n"
                "Seerr account: **NOT LINKED**"
            ),
            public_success="I sent your account status privately.",
        )
        return

    await send_private_output(
        ctx,
        content=(
            f"Discord: **{ctx.author.display_name}**\n"
            f"Seerr: **{link['seerr_username']}**\n"
            f"Seerr user ID: `{link['seerr_user_id']}`"
        ),
        public_success="I sent your linked request identity privately.",
    )


@bot.command(
    name="music",
    help=(
        "Search for a track, page through the matches, and request the exact one."
    ),
)
async def music_request(ctx, *, query: str = ""):
    query = " ".join(query.split())

    if not query:
        await ctx.reply(
            "Usage: `$music <artist and track>`\n"
            "Example: `$music Chappell Roan Pink Pony Club`"
        )
        return

    if not soulsync.enabled:
        await ctx.reply("SoulSync music requests are not configured yet.")
        return

    try:
        async with ctx.typing():
            result = await soulsync.search_tracks(query, limit=25)
    except SoulSyncError as exc:
        error_id = log_exception(f"SoulSync search failed query={query!r}", exc)
        await ctx.reply(f"SoulSync search failed. Error ID: `{error_id}`")
        return

    metadata_source = str(result.get("source") or "")
    tracks = [
        {**track, "_metadata_source": metadata_source}
        if isinstance(track, dict)
        else track
        for track in (result.get("tracks") or [])
    ]
    if not tracks:
        await ctx.reply(f'Nothing found for **"{query}"**.')
        return

    view = MusicSearchView(
        requester_id=ctx.author.id,
        query=query,
        tracks=tracks,
        source=result.get("source"),
        command_message=ctx.message,
    )
    message = await ctx.reply(embed=view.build_embed(), view=view)
    view.message = message
    register_transient_card(
        message=message,
        command_message=ctx.message,
        kind="music_search",
    )


# ============================================================
# REQUEST
# ============================================================

@bot.command(
    help=(
        "Search for a movie or show, choose the exact result, then choose seasons "
        "when it is a series."
    )
)
async def request(
    ctx,
    *,
    query: str = ""
):
    # Human-friendly normalization:
    # quotes are NOT required and repeated whitespace
    # should not affect searches.
    query = " ".join(
        query.split()
    )

    if not query:
        await ctx.reply(
            (
                "Usage: "
                "`$request <movie or TV show>`\n"
                "Example: "
                "`$request Interstellar`"
            )
        )

        return

    search_query = query
    target_year = None
    async with ctx.typing():
        try:
            search_query, target_year, results, total_pages = (
                await resolve_seerr_search_query(query, 1)
            )

        except SeerrError as exc:
            error_id = log_exception(
                f"Seerr search failed query={search_query!r}",
                exc,
            )
            await ctx.reply(f"Seerr search failed. Error ID: `{error_id}`")

            return

    if not results:
        suffix = f" ({target_year})" if target_year else ""
        await ctx.reply(
            f'Nothing found for **"{search_query}{suffix}"**.'
        )

        return

    view = SearchResultsView(
        requester_id=ctx.author.id,
        query=search_query,
        target_year=target_year,
        results=results,
        seerr_page=1,
        command_message=ctx.message,
        total_seerr_pages=(
            total_pages
        )
    )

    message = await ctx.reply(
        embed=view.build_embed(),
        view=view
    )

    view.message = message
    register_transient_card(
        message=message,
        command_message=ctx.message,
        kind="media_search",
    )


async def send_recommendation_batch(ctx, batch, *, reasons=None, signals=None):
    details_by_key = {}
    details = await asyncio.gather(
        *(fetch_media_details(item) for item in batch.items)
    )

    for item, item_details in zip(batch.items, details):
        details_by_key[(item.get("mediaType"), item.get("id"))] = item_details

    batch_state = RankedBatchState(
        origin_message=ctx.message,
        card_ids=(
            (item.get("mediaType"), item.get("id"))
            for item in batch.items
        ),
    )
    durable_batch_id = transient_batch_id_for_command(
        ctx.message,
        kind="recommendation",
    )

    for item in batch.items:
        card_id = (item.get("mediaType"), item.get("id"))
        single_batch = replace(batch, items=(item,))
        view = RecommendationCardView(
            requester_id=ctx.author.id,
            batch=single_batch,
            details_by_key=details_by_key,
            reasons=reasons,
            signals=signals,
            batch_state=batch_state,
            batch_card_id=card_id,
        )
        message = await ctx.reply(embed=view.build_embed(), view=view)
        view.message = message
        register_transient_card(
            message=message,
            command_message=ctx.message,
            kind="recommendation",
            batch_id=durable_batch_id,
            expected_batch_size=len(batch.items),
        )


async def available_semantic_genres(item, semaphore=None):
    if item.get("Type") != "Series":
        return set()

    tmdb_id = jellyfin._tmdb_id(item)

    if not tmdb_id:
        return set()

    cache_key = str(tmdb_id)
    cached = SEMANTIC_GENRE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] <= SEMANTIC_GENRE_CACHE_TTL_SECONDS:
        return set(cached[1])

    limiter = semaphore or SEMANTIC_RESOLVER_SEMAPHORE
    async with limiter:
        details = await asyncio.wait_for(
            seerr.request("GET", f"/tv/{tmdb_id}"),
            timeout=5,
        )

    romance_keyword_ids = set(DiscoveryService.TV_ROMANCE_KEYWORD_IDS)

    keywords = details.get("keywords") or []

    if isinstance(keywords, dict):
        keywords = keywords.get("results") or []

    for keyword in keywords:
        try:
            keyword_id = int(keyword.get("id") or 0)
        except (TypeError, ValueError, AttributeError):
            keyword_id = 0

        keyword_name = "".join(
            character
            for character in str(
                keyword.get("name", "") if isinstance(keyword, dict) else keyword
            ).casefold()
            if character.isalnum()
        )

        if keyword_id in romance_keyword_ids or keyword_name in {
            "romance",
            "love",
            "courtship",
        }:
            result = {"Romance"}
            SEMANTIC_GENRE_CACHE[cache_key] = (now, frozenset(result))
            return result

    SEMANTIC_GENRE_CACHE[cache_key] = (now, frozenset())
    return set()


def available_media_shape(jellyfin_item):
    media_type = "movie" if jellyfin_item.get("Type") == "Movie" else "tv"
    raw_tmdb_id = jellyfin._tmdb_id(jellyfin_item)

    try:
        tmdb_id = int(raw_tmdb_id) if raw_tmdb_id else 0
    except (TypeError, ValueError):
        tmdb_id = 0

    year = jellyfin_item.get("ProductionYear")
    date_key = "releaseDate" if media_type == "movie" else "firstAirDate"
    runtime_ticks = jellyfin_item.get("RunTimeTicks")

    try:
        runtime = round(int(runtime_ticks) / 600_000_000) if runtime_ticks else None
    except (TypeError, ValueError):
        runtime = None

    shaped = {
        "id": tmdb_id,
        "mediaType": media_type,
        "title": jellyfin_item.get("Name"),
        "name": jellyfin_item.get("Name"),
        date_key: f"{year}-01-01" if year else "",
        "overview": jellyfin_item.get("Overview") or "No overview available.",
        "voteAverage": jellyfin_item.get("CommunityRating"),
        "mediaInfo": {"status": 5},
    }
    details = dict(shaped)
    details["genres"] = [
        {"name": genre} for genre in jellyfin_item.get("Genres") or []
    ]

    if runtime:
        details["runtime"] = runtime

    return shaped, details


async def send_available_discovery_batch(ctx, batch):
    for index, jellyfin_item in enumerate(batch.items, start=1):
        item, fallback_details = available_media_shape(jellyfin_item)
        details = fallback_details

        if item["id"]:
            remote_details = await fetch_media_details(item)
            details = {**fallback_details, **remote_details}

            if not remote_details.get("genres"):
                details["genres"] = fallback_details["genres"]

        embed = build_media_embed(
            item,
            details,
            heading="Discover",
            state_text=(
                "**Available in Jellyfin right now.**\n"
                f"Library rank **#{index}** in this result batch."
            ),
            color=discord.Color.green(),
        )
        filter_text = (
            f" matching {batch.genre_filter}"
            if batch.genre_filter
            else ""
        )
        embed.set_footer(
            text=(
                f"{batch.eligible_count} available Jellyfin title"
                f"{'s' if batch.eligible_count != 1 else ''}{filter_text} • "
                f"selected from the top {min(batch.options.pool_size, batch.eligible_count)}"
            )
        )

        if item["id"]:
            view = JellyfinAvailableView(
                item=item,
                jellyfin_item_id=jellyfin_item["Id"],
            )
        else:
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(
                label="▶ Watch in Jellyfin",
                style=discord.ButtonStyle.link,
                url=jellyfin.watch_url(jellyfin_item["Id"]),
            ))

        await ctx.reply(embed=embed, view=view)


@bot.command(
    name="discover",
    aliases=["disc", "random"],
    help=(
        "Browse media playable right now with optional genres, quantity, ranking, "
        "and randomization. Genres support implicit AND, plaintext "
        "`and`/`or`, `&&`/`||`, and parentheses. Usage: $discover "
        "[movie|show] [expression] "
        "[--count N] [--top N] [--random]"
    ),
)
@commands.cooldown(2, 30, commands.BucketType.user)
async def discover_media(ctx, *, filters: str = ""):
    normalized = " ".join(filters.split())
    if str(ctx.invoked_with).casefold() == "random" and "--random" not in normalized:
        normalized = (normalized + " --random").strip()
    batch = None

    if not jellyfin.enabled:
        await ctx.reply("Jellyfin integration is not configured.")
        return

    try:
        async with ctx.typing():
            batch = await library.discover(
                normalized,
                expression_service=discovery,
                semantic_genre_resolver=available_semantic_genres,
            )

            if batch:
                await send_available_discovery_batch(ctx, batch)
    except DiscoveryUsageError as exc:
        await ctx.reply(
            f"{exc}\n\n"
            "Usage: `$discover [movie|show] [genre expression] [--count N] "
            "[--top N] [--random]`"
        )
        return
    except Exception as exc:
        error_id = log_exception(
            f"Jellyfin $discover failed filters={normalized!r}",
            exc,
        )
        await ctx.reply(f"Couldn't load discovery.\nError ID: `{error_id}`")
        return

    if not batch:
        await ctx.reply("Jellyfin has no available titles matching those filters.")


async def configured_taste_user_for(discord_user):
    if not JELLYFIN_TASTE_USER or not jellyfin.enabled:
        return None

    if not await bot.is_owner(discord_user):
        return None

    return await jellyfin.resolve_user(JELLYFIN_TASTE_USER)


async def configured_taste_user(ctx):
    return await configured_taste_user_for(ctx.author)


@bot.command(
    name="recommend",
    aliases=["recs", "randomrequest", "rr"],
    help=(
        "Rank unseen, requestable titles from your ratings and any connected "
        "server taste sources. Genres support implicit AND, plaintext `and`/`or`, "
        "`&&`/`||`, and parentheses. Usage: $recommend [movie|show] [expression] "
        "[--count N] [--top N] [--random]"
    ),
)
async def recommend_media(ctx, *, filters: str = ""):
    normalized = " ".join(filters.split())
    if (
        str(ctx.invoked_with).casefold() in {"randomrequest", "rr"}
        and "--random" not in normalized
    ):
        normalized = (normalized + " --random").strip()
    local_ratings = ratings_for_user(ctx.author.id)
    jellyfin_items = []
    trakt_items = []
    trakt_ratings = []
    trakt_available = False
    trakt_status = "not connected for this account"
    jellyfin_status = "not connected for this account"

    try:
        async with ctx.typing():
            taste_user = None
            try:
                taste_user = await configured_taste_user(ctx)
            except Exception as exc:
                logger.info("Configured Jellyfin taste user unavailable: %s", exc)
                jellyfin_status = "unavailable"
                trakt_status = "unavailable"

            if taste_user:
                try:
                    jellyfin_items = await jellyfin.user_taste_items(taste_user["Id"])
                    jellyfin_status = "connected"
                except Exception as exc:
                    logger.info("Jellyfin taste history unavailable: %s", exc)
                    jellyfin_status = "unavailable"

                try:
                    parsed = discovery.parse_discover(normalized)
                    trakt_types = (
                        (parsed.media_type,)
                        if parsed.media_type
                        else ("movie", "tv")
                    )

                    for trakt_type in trakt_types:
                        type_recommendations = (
                            await jellyfin.trakt_recommendations(
                                taste_user["Id"], trakt_type
                            )
                        )

                        for item in type_recommendations:
                            item.setdefault("mediaType", trakt_type)

                        trakt_items.extend(type_recommendations)

                        trakt_ratings.extend(
                            await jellyfin.trakt_ratings(
                                taste_user["Id"], trakt_type
                            )
                        )
                    trakt_available = True
                    trakt_status = "connected"
                except Exception as exc:
                    logger.info("Trakt recommendations unavailable: %s", exc)
                    trakt_status = "unavailable (plugin authorization or server error)"

            batch = await recommendations.recommend(
                normalized,
                ratings=local_ratings,
                jellyfin_items=jellyfin_items,
                trakt_items=trakt_items,
                trakt_ratings=trakt_ratings,
                trakt_available=trakt_available,
            )

            if batch:
                display_signals = {
                    **batch.signals,
                    "jellyfin_status": jellyfin_status,
                    "trakt_status": trakt_status,
                }
                await send_recommendation_batch(
                    ctx,
                    batch,
                    reasons=batch.reasons,
                    signals=display_signals,
                )
    except DiscoveryUsageError as exc:
        await ctx.reply(
            f"{exc}\n\n"
            "Usage: `$recommend [movie|show] [genre expression] [--count N] "
            "[--top N] [--random]`"
        )
        return
    except Exception as exc:
        error_id = log_exception(
            f"$recommend failed filters={normalized!r}",
            exc,
        )
        await ctx.reply(f"Couldn't build recommendations.\nError ID: `{error_id}`")
        return

    if not batch:
        await ctx.reply("No unseen, unrated, requestable recommendations matched.")


@bot.command(
    name="rate",
    aliases=["ratings"],
    help=(
        "Rate a movie or show from 1-10 after choosing the exact search result. "
        "Run $rate with no title to see your ratings. "
        "Usage: $rate <title> <1-10>"
    ),
)
async def rate_media(ctx, *, value: str = ""):
    if str(ctx.invoked_with).casefold() == "ratings":
        try:
            page = int(value.strip() or "1")
        except ValueError:
            await ctx.reply("Usage: `$ratings [page]`")
            return
        await send_ratings(ctx, page=page)
        return
    if not value.strip():
        await send_ratings(ctx, page=1)
        return

    try:
        query, raw_rating = value.rsplit(maxsplit=1)
        numeric_rating = int(raw_rating)
    except (ValueError, TypeError):
        await ctx.reply("Usage: `$rate <movie or show title> <1-10>`")
        return

    query = " ".join(query.split())

    if not query or not 1 <= numeric_rating <= 10:
        await ctx.reply("Rating must be a whole number from 1 through 10.")
        return

    search_query = query
    target_year = None
    try:
        async with ctx.typing():
            search_query, target_year, results, total_pages = (
                await resolve_seerr_search_query(query, 1)
            )
    except Exception as exc:
        error_id = log_exception(f"$rate failed query={query!r}", exc)
        await ctx.reply(f"Couldn't search for that title.\nError ID: `{error_id}`")
        return

    if not results:
        suffix = f" ({target_year})" if target_year else ""
        await ctx.reply(f'Nothing found for **"{search_query}{suffix}"**.')
        return

    view = RatingSearchView(
        requester_id=ctx.author.id,
        query=search_query,
        target_year=target_year,
        numeric_rating=numeric_rating,
        results=results,
        seerr_page=1,
        total_seerr_pages=total_pages,
        command_message=ctx.message,
    )
    message = await ctx.reply(embed=view.build_embed(), view=view)
    view.message = message
    register_transient_card(
        message=message,
        command_message=ctx.message,
        kind="rating_search",
    )


async def send_ratings(ctx, *, page=1):
    rows = ratings_for_user(ctx.author.id)

    if not rows:
        await ctx.reply("You haven't rated anything yet. Try `$rate <title> <1-10>`. ")
        return

    page_size = 15
    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(1, min(int(page), page_count))
    start = (page - 1) * page_size
    visible = rows[start:start + page_size]
    lines = [
        (
            f"**{row['title']} ({row['year'] or '????'})** — "
            f"{row['rating']}/10"
            + (" • Trakt synced" if row["trakt_synced"] else "")
        )
        for row in visible
    ]
    await ctx.reply(
        f"**Your MediaBot ratings - page {page}/{page_count} "
        f"({len(rows)} total)**\n"
        + "\n".join(lines)
        + (f"\n\nNext: `$ratings {page + 1}`" if page < page_count else "")
    )


@bot.command(
    name="report",
    help=(
        "Report a problem with playable Jellyfin media. Choose the exact title, "
        "then click the closest problem. Usage: $report <title> [SxxExx]"
    ),
)
async def report_media(ctx, *, value: str = ""):
    if ctx.guild is None:
        await ctx.reply("Reports must be submitted inside the media server.")
        return
    if not jellyfin.enabled:
        await ctx.reply("Jellyfin reporting is not configured yet.")
        return

    try:
        parsed = parse_report_query(value)
    except ReportUsageError as exc:
        await ctx.reply(
            f"{exc}\n\nUsage: `$report <title> [SxxExx]`"
        )
        return

    report_query = parsed
    search_title, target_year = split_media_query_year(parsed.search_query)

    try:
        async with ctx.typing():
            raw_results = await reports.search(parsed, limit=25)
            exact_numeric_title = any(
                normalized_media_title(item.get("Name"))
                == normalized_media_title(parsed.search_query)
                for item in raw_results
            )
            if target_year and not exact_numeric_title:
                report_query = ReportQuery(
                    search_query=search_title,
                    season_number=parsed.season_number,
                    episode_number=parsed.episode_number,
                )
                results = await reports.search(report_query, limit=25)
            else:
                target_year = None
                results = raw_results
    except Exception as exc:
        error_id = log_exception(
            f"Jellyfin report search failed query={parsed.search_query!r}",
            exc,
        )
        await ctx.reply(
            f"Couldn't search Jellyfin for that report. Error ID: `{error_id}`"
        )
        return

    if target_year:
        results = sorted(
            results,
            key=lambda item: (
                0 if jellyfin_item_year(item) == target_year else 1,
                0
                if normalized_media_title(item.get("Name"))
                == normalized_media_title(report_query.search_query)
                else 1,
            ),
        )

    if not results:
        suffix = f" {parsed.episode_label}" if parsed.is_episode else ""
        await ctx.reply(
            f"**{parsed.search_query}{suffix}** is not currently indexed in "
            "Jellyfin. Reports are for playable library media; use `$request` "
            "if the title or season is missing."
        )
        return

    view = ReportSearchView(
        requester_id=ctx.author.id,
        query=report_query,
        results=results,
        command_message=ctx.message,
    )
    message = await ctx.reply(embed=view.build_embed(), view=view)
    view.message = message
    register_transient_card(
        message=message,
        command_message=ctx.message,
        kind="report_search",
    )


# ============================================================
# EVENTS
# ============================================================

@bot.group(
    name="event",
    invoke_without_command=True,
    help=(
        "Plan a shared media night: nominate exact titles, vote, schedule the "
        "winners, or see tonight's lineup. Run $event for the current dashboard."
    ),
)
@commands.guild_only()
async def event_group(ctx):
    event_record = current_visible_event(ctx.guild.id)
    if event_record is not None:
        view = EventDashboardView(
            event_record,
            persistent=False,
            command_message=ctx.message,
        )
        message = await ctx.reply(
            embed=build_event_dashboard_embed(event_record),
            view=view,
        )
        view.message = message
        register_transient_card(
            message=message,
            command_message=ctx.message,
            kind="event_dashboard_snapshot",
        )
        return

    embed = discord.Embed(
        title="Media events",
        description="There is no open or scheduled event in this server.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Start here",
        value=(
            "Administrator: `$event create Friday Movie Night`\n"
            "Everyone: `$event nominate <title>`, `$event vote`, then `$event time`\n"
            "Tonight: `$event tonight`"
        ),
        inline=False,
    )
    embed.set_footer(text="Use $help event for every event command")
    await ctx.reply(embed=embed)


@event_group.command(
    name="create",
    help=(
        "Administrator: create one open event for this server. Add trailing "
        "--votes N for multiple choices, or use spooktober [YEAR]."
    ),
)
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def event_create(ctx, *, value: str = ""):
    try:
        spec = parse_event_create_input(value)
        event_record = events.create_event(
            discord_guild_id=ctx.guild.id,
            discord_channel_id=ctx.channel.id,
            created_by_discord_id=ctx.author.id,
            name=spec.name,
            timezone_name=DEFAULT_EVENT_TIMEZONE,
            vote_limit=spec.vote_limit,
            preset=spec.preset,
        )
    except OpenEventExistsError as exc:
        await ctx.reply(
            f"Event **#{exc.existing_event_id}** is already open. "
            "Schedule or cancel it before creating another."
        )
        return
    except EventUsageError as exc:
        await ctx.reply(str(exc))
        return
    except Exception as exc:
        error_id = log_exception("Could not create event", exc)
        await ctx.reply(f"Couldn't create that event. Error ID: `{error_id}`")
        return

    dashboard = await ctx.reply(
        embed=build_event_dashboard_embed(event_record),
        view=EventDashboardView(event_record),
    )
    try:
        event_record = events.set_dashboard_message(
            event_id=event_record.event_id,
            discord_channel_id=dashboard.channel.id,
            dashboard_message_id=dashboard.id,
        )
    except Exception as exc:
        log_exception(
            f"Event created but dashboard coordinates were not saved event={event_record.event_id}",
            exc,
        )


@event_group.command(
    name="nominate",
    help=(
        "Search Seerr and add one exact movie or show to the current event ballot. "
        "This never requests the media."
    ),
)
@commands.guild_only()
async def event_nominate(ctx, *, query: str = ""):
    event_record = events.current_event(ctx.guild.id)
    if event_record is None:
        await ctx.reply("There is no open event. Ask an administrator to create one.")
        return
    query = " ".join(str(query or "").split())
    if not query:
        await ctx.reply("Usage: `$event nominate <movie or show title> [year]`")
        return

    try:
        async with ctx.typing():
            search_query, target_year, results, total_pages = (
                await resolve_seerr_search_query(query, 1)
            )
    except Exception as exc:
        error_id = log_exception(f"Event nomination search failed query={query!r}", exc)
        await ctx.reply(f"Couldn't search for that title. Error ID: `{error_id}`")
        return
    if not results:
        suffix = f" ({target_year})" if target_year else ""
        await ctx.reply(f'Nothing found for **"{search_query}{suffix}"**.')
        return

    view = EventNominationSearchView(
        guild_id=ctx.guild.id,
        event_record=event_record,
        requester_id=ctx.author.id,
        query=search_query,
        target_year=target_year,
        results=results,
        seerr_page=1,
        total_seerr_pages=total_pages,
        command_message=ctx.message,
    )
    message = await ctx.reply(embed=view.build_embed(), view=view)
    view.message = message
    register_transient_card(
        message=message,
        command_message=ctx.message,
        kind="event_nomination_search",
    )


@event_group.command(
    name="time",
    help=(
        "Vote for every candidate time you can attend. Administrators get a date/time "
        "picker when no candidates exist, or may append local YYYY-MM-DD HH:MM values."
    ),
)
@commands.guild_only()
async def event_time(ctx, *, value: str = ""):
    event_record = events.current_event(ctx.guild.id)
    if event_record is None:
        await ctx.reply("There is no open event to choose a time for.")
        return
    raw = str(value or "").strip()
    options = events.future_time_options(event_record.event_id)
    permissions = getattr(ctx.author, "guild_permissions", None)
    is_admin = bool(getattr(permissions, "administrator", False))

    if raw:
        if not is_admin:
            await ctx.reply("An administrator must propose candidate times.")
            return
        try:
            if raw.casefold() in {"clear", "reset"}:
                events.replace_time_options(
                    event_record.event_id,
                    (),
                    ctx.author.id,
                )
            else:
                starts_at = parse_schedule_input(
                    raw, timezone_name=event_record.timezone_name
                )
                validate_future_event_times(starts_at)
                events.add_time_options(
                    event_record.event_id,
                    starts_at,
                    ctx.author.id,
                )
            options = events.future_time_options(event_record.event_id)
        except (EventUsageError, EventStateError, EventNotFoundError) as exc:
            await ctx.reply(str(exc))
            return
        await refresh_event_dashboard(event_record.event_id)

    if not options:
        if not is_admin:
            await ctx.reply(
                "No future candidate times remain. Ask an administrator to run `$event time`."
            )
            return
        view = EventTimePickerView(
            requester_id=ctx.author.id,
            guild_id=ctx.guild.id,
            event_record=event_record,
            command_message=ctx.message,
        )
        message = await ctx.reply(embed=view.build_embed(), view=view)
        view.message = message
        register_transient_card(
            message=message,
            command_message=ctx.message,
            kind="event_time_picker",
        )
        return

    selected = events.user_time_vote_ids(event_record.event_id, ctx.author.id)
    view = EventTimeVoteView(
        requester_id=ctx.author.id,
        guild_id=ctx.guild.id,
        event_record=event_record,
        options=options,
        selected_ids=selected,
        command_message=ctx.message,
    )
    message = await ctx.reply(embed=view.build_embed(), view=view)
    view.message = message
    register_transient_card(
        message=message,
        command_message=ctx.message,
        kind="event_time_vote",
    )


@event_group.command(
    name="vote",
    help=(
        "Open your private-to-control ballot for the current event. Number buttons "
        "toggle choices; a one-vote ballot moves your vote in one click."
    ),
)
@commands.guild_only()
async def event_vote(ctx):
    event_record = events.current_event(ctx.guild.id)
    if event_record is None:
        await ctx.reply("There is no open event to vote in.")
        return
    rankings = events.rankings(event_record.event_id)
    if not rankings:
        await ctx.reply("Nothing has been nominated yet. Try `$event nominate <title>`.")
        return
    selected_ids = events.user_vote_ids(
        event_id=event_record.event_id,
        discord_user_id=ctx.author.id,
    )
    view = EventVoteView(
        requester_id=ctx.author.id,
        guild_id=ctx.guild.id,
        event_record=event_record,
        rankings=rankings,
        selected_ids=selected_ids,
        command_message=ctx.message,
    )
    message = await ctx.reply(embed=view.build_embed(), view=view)
    view.message = message
    register_transient_card(
        message=message,
        command_message=ctx.message,
        kind="event_vote",
    )


@event_group.command(
    name="schedule",
    help=(
        "Administrator: preview the title and time winners, then publish. Bare "
        "schedule uses the availability winner; local YYYY-MM-DD HH:MM remains a fallback."
    ),
)
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def event_schedule(ctx, *, value: str = ""):
    event_record = events.current_event(ctx.guild.id)
    if event_record is None:
        await ctx.reply("There is no open event to schedule.")
        return
    try:
        if event_record.preset_key:
            if value.strip():
                raise EventUsageError(
                    "This preset already saved its dates. Run `$event schedule` with no dates."
                )
            plan = events.build_ranked_schedule(event_record.event_id)
        elif value.strip():
            starts_at = parse_schedule_input(
                value,
                timezone_name=event_record.timezone_name,
            )
            validate_future_event_times(starts_at)
            plan = events.build_ranked_schedule(
                event_record.event_id,
                starts_at=starts_at,
            )
        elif events.future_time_options(event_record.event_id):
            ranked_times = events.future_time_options(
                event_record.event_id,
                ranked=True,
            )
            tied_times = tuple(
                option
                for option in ranked_times
                if option.vote_count == ranked_times[0].vote_count
            )
            if len(tied_times) > 1:
                view = EventScheduleTimeTieView(
                    requester_id=ctx.author.id,
                    guild_id=ctx.guild.id,
                    event_record=event_record,
                    options=tied_times,
                    command_message=ctx.message,
                )
                message = await ctx.reply(embed=view.build_embed(), view=view)
                view.message = message
                register_transient_card(
                    message=message,
                    command_message=ctx.message,
                    kind="event_schedule_preview",
                )
                return
            plan = events.build_ranked_schedule(event_record.event_id)
        else:
            view = EventTimePickerView(
                requester_id=ctx.author.id,
                guild_id=ctx.guild.id,
                event_record=event_record,
                command_message=ctx.message,
            )
            message = await ctx.reply(embed=view.build_embed(), view=view)
            view.message = message
            register_transient_card(
                message=message,
                command_message=ctx.message,
                kind="event_time_picker",
            )
            return
        validate_future_event_times(
            tuple(assignment.starts_at for assignment in plan.assignments)
        )
    except (EventUsageError, EventStateError, EventNotFoundError) as exc:
        await ctx.reply(str(exc))
        return
    except Exception as exc:
        error_id = log_exception("Could not build event schedule preview", exc)
        await ctx.reply(f"Couldn't preview that schedule. Error ID: `{error_id}`")
        return

    view = EventScheduleView(
        requester_id=ctx.author.id,
        guild_id=ctx.guild.id,
        event_record=event_record,
        plan=plan,
        command_message=ctx.message,
    )
    message = await ctx.reply(embed=view.build_embed(), view=view)
    view.message = message
    register_transient_card(
        message=message,
        command_message=ctx.message,
        kind="event_schedule_preview",
    )


@event_group.command(
    name="reschedule",
    help=(
        "Administrator: move a scheduled event without losing votes. With one slot, "
        "omit dates for the picker; otherwise supply comma-separated local times."
    ),
)
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def event_reschedule(ctx, event_id: int, *, value: str = ""):
    try:
        event_record = events.event(event_id, discord_guild_id=ctx.guild.id)
        if event_record.status is not EventStatus.SCHEDULED:
            raise EventStateError("Only a scheduled event can be rescheduled.")
        slots = events.slots(event_record.event_id)
        if not value.strip():
            if len(slots) != 1:
                raise EventUsageError(
                    "This event has multiple slots. Supply one comma-separated local time per slot."
                )
            view = EventReschedulePickerView(
                requester_id=ctx.author.id,
                guild_id=ctx.guild.id,
                event_record=event_record,
                slot=slots[0],
                command_message=ctx.message,
            )
            message = await ctx.reply(embed=view.build_embed(), view=view)
            view.message = message
            register_transient_card(
                message=message,
                command_message=ctx.message,
                kind="event_reschedule_picker",
            )
            return
        starts_at = parse_schedule_input(value, timezone_name=event_record.timezone_name)
        validate_future_event_times(starts_at)
        if len(starts_at) != len(slots):
            raise EventUsageError(
                f"Give exactly {len(slots)} date/time value(s), one for each existing slot."
            )
        assignments = tuple(
            ScheduleAssignment(starts, slot.nomination_id)
            for starts, slot in zip(starts_at, slots)
        )
        event_record, slots = events.reschedule_event(
            event_record.event_id,
            assignments,
            expected_revision=event_record.revision,
        )
    except (
        EventUsageError,
        EventStateError,
        EventNotFoundError,
        StaleEventRevisionError,
    ) as exc:
        await ctx.reply(str(exc))
        return
    except Exception as exc:
        error_id = log_exception(f"Could not reschedule event {event_id}", exc)
        await ctx.reply(f"Couldn't reschedule that event. Error ID: `{error_id}`")
        return
    await refresh_event_dashboard(event_record.event_id)
    await ctx.reply(embed=build_event_schedule_receipt(event_record, slots))


@event_group.command(
    name="reopen",
    help="Administrator: reopen a scheduled event while retaining nominations and votes.",
)
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def event_reopen(ctx, event_id: int):
    try:
        existing = events.event(event_id, discord_guild_id=ctx.guild.id)
        retired_slots = events.slots(existing.event_id)
        updated = events.reopen(existing.event_id)
    except (EventNotFoundError, EventStateError, OpenEventExistsError) as exc:
        await ctx.reply(str(exc))
        return
    await remove_native_events(
        existing,
        expected_slot_ids={slot.slot_id for slot in retired_slots},
        expected_native_event_ids={
            slot.native_scheduled_event_id
            for slot in retired_slots
            if slot.native_scheduled_event_id
        },
    )
    await refresh_event_dashboard(updated.event_id)
    await ctx.reply(
        f"**Reopened: {updated.name}**\nNominations, title votes, and time votes are live again."
    )


@event_group.command(
    name="history",
    help="Show recent active and archived events for this server.",
)
@commands.guild_only()
async def event_history(ctx):
    records = events.list_events(
        discord_guild_id=ctx.guild.id,
        limit=20,
        include_archived=True,
    )
    if not records:
        await ctx.reply("This server does not have any event history yet.")
        return
    lines = []
    for record in records:
        archived = " - archived" if record.archived_at else ""
        lines.append(
            f"**#{record.event_id} {record.name}** - {record.status.value}{archived}"
        )
    embeds = build_event_line_embeds(
        title="Event history",
        description="Recent event state is retained even when old dashboard cards are cleared.",
        field_name="Events",
        lines=lines,
    )
    for index, embed in enumerate(embeds):
        if index == 0:
            await ctx.reply(embed=embed)
        else:
            await ctx.send(embed=embed)


@event_group.command(
    name="archive",
    help="Administrator: hide one completed or cancelled event without deleting its history.",
)
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def event_archive(ctx, event_id: int):
    try:
        existing = events.event(event_id, discord_guild_id=ctx.guild.id)
        updated = events.archive(existing.event_id)
    except (EventNotFoundError, EventStateError) as exc:
        await ctx.reply(str(exc))
        return
    if existing.dashboard_message_id:
        await delete_discord_message_by_id(
            existing.discord_channel_id,
            existing.dashboard_message_id,
            label="archived event dashboard",
        )
    await ctx.reply(
        f"Archived **#{updated.event_id} {updated.name}**. Its history is still available."
    )


@event_group.command(
    name="clear",
    help="Administrator: archive old terminal or fully elapsed events and remove stale dashboards.",
)
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def event_clear(ctx):
    now = datetime.now(timezone.utc)
    before = events.list_events(
        discord_guild_id=ctx.guild.id,
        limit=500,
        include_archived=False,
    )
    eligible = {
        record.event_id: record
        for record in before
        if record.status in {EventStatus.COMPLETED, EventStatus.CANCELLED}
        or (
            record.status is EventStatus.SCHEDULED
            and events.slots(record.event_id)
            and max(slot.starts_at for slot in events.slots(record.event_id))
            + timedelta(hours=EVENT_COMPLETION_GRACE_HOURS)
            < now
        )
    }
    archived = events.clear_old(
        ctx.guild.id,
        reference=now - timedelta(hours=EVENT_COMPLETION_GRACE_HOURS),
    )
    for record in archived:
        old = eligible.get(record.event_id)
        if old and old.dashboard_message_id:
            await delete_discord_message_by_id(
                old.discord_channel_id,
                old.dashboard_message_id,
                label="cleared event dashboard",
            )
        await remove_native_events(old or record)
    await ctx.reply(
        f"Cleared **{len(archived)}** old event{'s' if len(archived) != 1 else ''}. "
        "Nothing was hard-deleted; `$event history` still shows them."
    )


@event_group.command(
    name="tonight",
    help="Show today's scheduled event slots in Denver time. Read-only; no request is made.",
)
@commands.guild_only()
async def event_tonight(ctx):
    try:
        slots = events.tonight(
            discord_guild_id=ctx.guild.id,
            timezone_name=DEFAULT_EVENT_TIMEZONE,
        )
    except Exception as exc:
        error_id = log_exception("Could not load tonight's event schedule", exc)
        await ctx.reply(f"Couldn't load tonight's schedule. Error ID: `{error_id}`")
        return
    if not slots:
        await ctx.reply("Nothing is scheduled tonight in Denver time.")
        return

    lines = []
    for slot in slots:
        timestamp = discord.utils.format_dt(slot.starts_at, style="t")
        relative = discord.utils.format_dt(slot.starts_at, style="R")
        title = event_display_title(slot.title or "TBD", slot.year)
        if slot.jellyfin_item_id:
            title = f"[{title}]({jellyfin.watch_url(slot.jellyfin_item_id)})"
            availability = "Available in Jellyfin"
        else:
            availability = "Not linked to Jellyfin"
        lines.append(
            f"{timestamp} ({relative}) - **{title}**\n"
            f"{slot.event_name} - {availability}"
        )
    embeds = build_event_line_embeds(
        title="Tonight's media",
        description=f"**{len(lines)} scheduled title(s) in Denver local time.**",
        field_name="Lineup",
        lines=lines,
    )
    for index, embed in enumerate(embeds):
        embed.set_footer(
            text=f"Denver local day - read-only schedule - page {index + 1}/{len(embeds)}"
        )
        if index == 0:
            await ctx.reply(embed=embed)
        else:
            await ctx.send(embed=embed)


async def transition_event_command(ctx, event_id, target):
    try:
        existing = events.event(event_id, discord_guild_id=ctx.guild.id)
        updated = (
            events.complete(existing.event_id)
            if target == "complete"
            else events.cancel(existing.event_id)
        )
    except (EventNotFoundError, EventStateError) as exc:
        await ctx.reply(str(exc))
        return
    except Exception as exc:
        error_id = log_exception(f"Could not {target} event {event_id}", exc)
        await ctx.reply(f"Couldn't {target} that event. Error ID: `{error_id}`")
        return

    await refresh_event_dashboard(updated.event_id)
    await remove_native_events(existing)
    verb = "Completed" if target == "complete" else "Cancelled"
    await ctx.reply(
        f"**{verb}: {updated.name}**\nEvent **#{updated.event_id}** is now "
        f"**{updated.status.value}**."
    )


@event_group.command(
    name="complete",
    help="Administrator: mark a scheduled event complete. Usage: $event complete <event id>",
)
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def event_complete(ctx, event_id: int):
    await transition_event_command(ctx, event_id, "complete")


@event_group.command(
    name="cancel",
    help="Administrator: cancel an open or scheduled event. Usage: $event cancel <event id>",
)
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def event_cancel(ctx, event_id: int):
    await transition_event_command(ctx, event_id, "cancel")



# ============================================================
# JELLYFIN COMMANDS
# ============================================================

async def reply_with_music_status(ctx, query):
    guild_id = getattr(getattr(ctx, "guild", None), "id", None)
    record = latest_music_request(
        query,
        requester_discord_id=ctx.author.id,
        discord_guild_id=guild_id,
    ) or latest_music_request(
        query,
        discord_guild_id=guild_id,
    )
    if not record:
        return False

    display_query = record["display_query"]
    artist, separator, title = display_query.partition(" - ")
    if not separator:
        title = display_query
        artist = ""

    status_value = record["request_status"]
    error = record.get("error")

    if soulsync.enabled:
        try:
            library_matches = await soulsync.library_tracks(
                title=title,
                artist=artist,
                limit=10,
            )
            exact_library_match = next(
                (
                    track
                    for track in library_matches
                    if music_track_matches(track, title=title, artist=artist)
                ),
                None,
            )

            if exact_library_match:
                status_value = "downloaded"
                update_music_request(
                    local_request_id=record["local_request_id"],
                    request_status=status_value,
                    soulsync_request_id=record.get("soulsync_request_id"),
                )
            elif record.get("soulsync_request_id"):
                live = await soulsync.request_status(record["soulsync_request_id"])
                status_value = str(live.get("status") or status_value)
                error = live.get("error") or error
                update_music_request(
                    local_request_id=record["local_request_id"],
                    request_status=status_value,
                    soulsync_request_id=record.get("soulsync_request_id"),
                    error=error,
                )
        except SoulSyncError as exc:
            logger.info("SoulSync status refresh unavailable: %s", exc)

    status_text = {
        "submitting": "Submitting",
        "ambiguous": "Request may be queued; duplicate retry blocked",
        "queued": "Requested - queued",
        "searching": "Requested - searching",
        "downloading": "Downloading",
        "downloaded": "Downloaded and indexed in the music library",
        "completed": "Downloaded",
        "not_found": "Not found",
        "failed": "Failed",
    }.get(status_value, status_value.replace("_", " ").title())

    embed = discord.Embed(
        title=compact_embed_title(display_query),
        color=(
            discord.Color.green()
            if status_value in {"downloaded", "completed"}
            else discord.Color.gold()
        ),
    )
    embed.add_field(name="Type", value="Music track", inline=True)
    embed.add_field(name="Status", value=f"**{status_text}**", inline=False)
    if error and status_value in {"ambiguous", "failed", "not_found"}:
        embed.add_field(name="Last error", value=str(error)[:900], inline=False)
    embed.set_footer(text="Music status is reconciled against SoulSync's library")
    await ctx.reply(embed=embed, view=MusicLinksView())
    return True


REQUEST_STATUS = {
    1: "Pending approval",
    2: "Approved",
    3: "Declined",
    4: "Failed",
    5: "Completed",
}


async def reply_with_request_id_status(ctx, request_id):
    guild_id = getattr(getattr(ctx, "guild", None), "id", None)
    record = request_by_id(
        request_id,
        discord_guild_id=guild_id,
    )
    if not record:
        return False
    live = None

    if int(request_id) > 0:
        try:
            live = await seerr.request_details(int(request_id))
        except Exception as exc:
            logger.info("Seerr request #%s status refresh unavailable: %s", request_id, exc)

    title = record.get("title") or media_title((live or {}).get("media") or {})
    year = record.get("year") or "????"
    raw_status = (live or {}).get("status")
    status_text = REQUEST_STATUS.get(raw_status) or record.get(
        "request_status",
        "Unknown",
    )
    terminal_failure = str(status_text).casefold() in {"declined", "failed"}
    embed = discord.Embed(
        title=f"{title} ({year})",
        color=(
            discord.Color.green()
            if record.get("jellyfin_available")
            else discord.Color.red()
            if terminal_failure
            else discord.Color.gold()
        ),
    )
    embed.add_field(
        name="Request",
        value=f"#{int(request_id)}" if int(request_id) > 0 else "MediaBot episode repair",
        inline=True,
    )
    embed.add_field(name="Status", value=f"**{status_text}**", inline=True)
    seasons = str(record.get("requested_seasons") or "").strip()
    if seasons:
        embed.add_field(name="Seasons", value=seasons, inline=True)
    if record and int(request_id) < 0:
        exact = tracked_episode_numbers(record)
        if exact:
            embed.add_field(
                name="Episodes",
                value="; ".join(
                    f"S{season} {compact_episode_ranges(episodes)}"
                    for season, episodes in sorted(exact.items())
                ),
                inline=False,
            )
    embed.add_field(
        name="Library",
        value=(
            "Available in Jellyfin"
            if record.get("jellyfin_available")
            else "No download is pending because this request was declined or failed"
            if terminal_failure
            else "Waiting for the requested media"
        ),
        inline=False,
    )
    embed.set_footer(
        text=(
            "Exact episode state is reconciled through Jellyfin and Sonarr"
            if int(request_id) < 0
            else "Request status refreshed from Seerr when available"
        )
    )
    await ctx.reply(embed=embed)
    return True


async def add_series_operational_status(embed, jellyfin_item):
    tmdb_id = jellyfin._tmdb_id(jellyfin_item)
    if not tmdb_id or not sonarr.enabled:
        return False

    try:
        details = await seerr.tv_details(int(tmdb_id))
        tvdb_id = tvdb_id_from_details(details)
        inventory = await sonarr.series_inventory(tvdb_id) if tvdb_id else None
    except Exception as exc:
        logger.info("Series operational status unavailable: %s", exc)
        return False

    if not inventory:
        return False

    complete = 0
    issue_lines = []
    for season_number, state in sorted(inventory["seasons"].items()):
        expected = set(state.get("expected") or ())
        available = set(state.get("available") or ())
        missing = set(state.get("missing") or ())
        future = set(state.get("future") or ())
        monitored = set(state.get("monitored") or ())
        queued = set(state.get("queued") or ())
        if expected and not missing:
            complete += 1
            continue
        if not expected:
            continue

        parts = [f"{len(available)}/{len(expected)}"]
        if queued:
            grouped_queue = {}
            for episode in queued:
                label = str(
                    (state.get("queue_status") or {}).get(episode) or "queued"
                )
                grouped_queue.setdefault(label, set()).add(episode)
            for label, episodes in sorted(grouped_queue.items()):
                parts.append(f"{compact_episode_ranges(episodes)} {label}")
        not_queued_aired = (missing - future) - queued
        if not_queued_aired:
            parts.append(f"missing {compact_episode_ranges(not_queued_aired)}")
        monitored_future = future & monitored
        unmonitored_future = future - monitored
        if monitored_future:
            parts.append(
                f"upcoming {compact_episode_ranges(monitored_future)} monitored"
            )
        if unmonitored_future:
            parts.append(f"upcoming {compact_episode_ranges(unmonitored_future)}")
        issue_lines.append(f"**S{season_number}:** " + " - ".join(parts))

    if issue_lines:
        shown = issue_lines[:12]
        if len(issue_lines) > len(shown):
            shown.append(f"...and {len(issue_lines) - len(shown)} more incomplete seasons")
        embed.add_field(
            name="Episode state",
            value="\n".join(shown)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="Episode state",
            value="Every known Sonarr season is complete.",
            inline=False,
        )
    embed.set_footer(
        text=(
            f"{complete} complete season(s) - playback: Jellyfin - downloads: Sonarr"
        )
    )
    return True

@bot.command(
    help=(
        "Check a movie, show, track, or request number and see what is available, "
        "requested, downloading, missing, or upcoming."
    )
)
async def status(
    ctx,
    *,
    query: str = ""
):
    query = " ".join(
        query.split()
    )
    guild_id = getattr(getattr(ctx, "guild", None), "id", None)

    if not query:

        await ctx.reply(
            (
                "Usage: `$status <movie, TV show, music track, or #request>`\n"
                "Examples: `$status Interstellar`, `$status Pink Pony Club`, "
                "`$status #272`"
            )
        )

        return

    request_number = query[1:] if query.startswith("#") else query
    explicit_request_number = query.startswith("#") and bool(
        re.fullmatch(r"-?\d+", request_number)
    )
    known_bare_request_number = bool(
        re.fullmatch(r"-?\d+", query)
        and request_by_id(int(query), discord_guild_id=guild_id)
    )
    if explicit_request_number or known_bare_request_number:
        if await reply_with_request_id_status(ctx, int(request_number)):
            return
        await ctx.reply(f"No tracked request `#{request_number}` was found.")
        return

    search_query, target_year = split_media_query_year(query)
    parenthesized_year = bool(
        re.search(r"\((?:18|19|20|21)\d{2}\)$", query)
    )
    jellyfin_warning_id = None

    # --------------------------------------------------------
    # FIRST: ACTUAL JELLYFIN LIBRARY
    # --------------------------------------------------------

    if jellyfin.enabled:

        try:

            if target_year and not parenthesized_year:
                raw_results = await library.search(query, limit=5)
                raw_title = normalized_media_title(query)
                exact_numeric_title = any(
                    normalized_media_title(item.get("Name")) == raw_title
                    for item in raw_results
                )
                if exact_numeric_title:
                    results = raw_results
                    search_query = query
                    target_year = None
                else:
                    results = await library.search(search_query, limit=10)
            else:
                results = await library.search(search_query, limit=10)

        except Exception as exc:

            error_id = log_exception(
                (
                    "Jellyfin status search failed "
                    f"query={query!r}"
                ),
                exc
            )

            jellyfin_warning_id = error_id
            results = []

        if results:
            normalized_query = normalized_media_title(search_query)
            results = sorted(
                results,
                key=lambda candidate: (
                    0
                    if (
                        normalized_media_title(candidate.get("Name")) == normalized_query
                        and (
                            not target_year
                            or jellyfin_item_year(candidate) == target_year
                        )
                    )
                    else 1,
                    0
                    if normalized_media_title(candidate.get("Name")) == normalized_query
                    else 1,
                    0 if not target_year or jellyfin_item_year(candidate) == target_year else 1,
                    -difflib.SequenceMatcher(
                        None,
                        normalized_query,
                        normalized_media_title(candidate.get("Name")),
                    ).ratio(),
                ),
            )

            exact = [
                candidate
                for candidate in results
                if normalized_media_title(candidate.get("Name")) == normalized_query
                and (
                    not target_year
                    or jellyfin_item_year(candidate) == target_year
                )
            ]
            if len(exact) == 1:
                item = exact[0]
            elif len(results) == 1 and not target_year:
                item = results[0]
            else:
                choices = "\n".join(
                    f"- **{candidate.get('Name') or 'Unknown'} "
                    f"({jellyfin_item_year(candidate)})** - {candidate.get('Type') or 'Media'}"
                    for candidate in results[:5]
                )
                await ctx.reply(
                    "**More than one library title could match. I won't guess.**\n"
                    f"{choices}\n\nRun `$status <exact title> <year>`, for example "
                    "`$status Sherlock Holmes 2009`."
                )
                return

            embed = discord.Embed(
                title=compact_embed_title(
                    f"{item.get('Name', 'Unknown')} "
                    f"({jellyfin_item_year(item)})"
                ),
                description=short_overview(item, limit=1000),
                color=discord.Color.green()
            )

            embed.add_field(
                name="Status",
                value=(
                    "**Series indexed in Jellyfin**"
                    if item.get("Type") == "Series"
                    else "**Available in Jellyfin**"
                ),
                inline=False
            )

            genres = (
                item.get("Genres")
                or []
            )

            if genres:

                embed.add_field(
                    name="Genres",
                    value=", ".join(
                        genres[:5]
                    ),
                    inline=False
                )

            rating = item.get(
                "CommunityRating"
            )

            if rating:

                embed.add_field(
                    name="Rating",
                    value=(
                        f"{float(rating):.1f}/10"
                    ),
                    inline=True
                )

            if item.get("Type") == "Series":
                await add_series_operational_status(embed, item)

                tracked = latest_media_request(
                    item.get("Name") or search_query,
                    requester_discord_id=ctx.author.id,
                    discord_guild_id=guild_id,
                ) or latest_media_request(
                    item.get("Name") or search_query,
                    discord_guild_id=guild_id,
                )
                if tracked and not tracked.get("jellyfin_available"):
                    tracked_id = int(tracked["seerr_request_id"])
                    request_label = (
                        f"#{tracked_id}"
                        if tracked_id > 0
                        else "Exact episode repair"
                    )
                    embed.add_field(
                        name="Latest request",
                        value=(
                            f"**{request_label}** - "
                            f"{tracked.get('request_status') or 'In progress'}"
                        ),
                        inline=False,
                    )

            view = discord.ui.View(
                timeout=None
            )

            view.add_item(
                discord.ui.Button(
                    label="▶ Watch in Jellyfin",
                    style=discord.ButtonStyle.link,
                    url=jellyfin.watch_url(
                        item["Id"]
                    )
                )
            )

            await ctx.reply(
                embed=embed,
                view=view
            )

            return

    tracked = latest_media_request(
        search_query,
        requester_discord_id=ctx.author.id,
        discord_guild_id=guild_id,
    ) or latest_media_request(
        search_query,
        discord_guild_id=guild_id,
    )
    if tracked and await reply_with_request_id_status(
        ctx,
        tracked["seerr_request_id"],
    ):
        return

    if await reply_with_music_status(ctx, query):
        return

    # --------------------------------------------------------
    # SECOND: SEERR
    # --------------------------------------------------------

    try:

        search_query, target_year, results, _ = (
            await resolve_seerr_search_query(query, 1)
        )

    except Exception as exc:

        error_id = log_exception(
            (
                "Seerr status search failed "
                f"query={query!r}"
            ),
            exc
        )

        if jellyfin_warning_id:
            await ctx.reply(
                "Jellyfin and Seerr lookups both failed.\n"
                f"Error IDs: `{jellyfin_warning_id}`, `{error_id}`"
            )
        else:
            await ctx.reply(
                "Seerr lookup failed.\n"
                f"Error ID: `{error_id}`"
            )

        return

    if not results:

        await ctx.reply(
            (
                f'Nothing found for **"{query}"**.'
                + (
                    f"\nJellyfin was unavailable (Error `{jellyfin_warning_id}`)."
                    if jellyfin_warning_id
                    else ""
                )
            )
        )

        return

    ranked_results = prioritize_search_results(
        results,
        query=search_query,
        year=target_year,
    )
    normalized_query = normalized_media_title(search_query)
    exact = [
        candidate
        for candidate in ranked_results
        if normalized_media_title(media_title(candidate)) == normalized_query
        and (not target_year or media_year(candidate) == target_year)
    ]
    if len(exact) == 1:
        item = exact[0]
    elif len(ranked_results) == 1 and not target_year:
        item = ranked_results[0]
    else:
        choices = "\n".join(
            f"- **{media_title(candidate)} ({media_year(candidate)})** - "
            f"{str(candidate.get('mediaType') or 'media').upper()} - {media_status(candidate)}"
            for candidate in ranked_results[:5]
        )
        await ctx.reply(
            "**More than one catalog title could match. I won't guess.**\n"
            f"{choices}\n\nRun `$status <exact title> <year>`."
        )
        return

    await ctx.reply(
        (
            f"**{media_title(item)} "
            f"({media_year(item)})**\n"
            f"Seerr status: "
            f"**{media_status(item)}**"
            + (
                f"\nJellyfin was unavailable (Error `{jellyfin_warning_id}`), "
                "so this result is from Seerr."
                if jellyfin_warning_id
                else ""
            )
        )
    )


@bot.command(
    name="new",
    help=(
        "Show recently added movies and "
        "shows from Jellyfin."
    )
)
async def newly_added(
    ctx,
    limit: int = 10
):
    limit = max(
        1,
        min(
            int(limit),
            15
        )
    )

    if not jellyfin.enabled:

        await ctx.reply(
            (
                "Jellyfin integration is "
                "not configured."
            )
        )

        return

    try:

        items = await library.latest(
            limit=limit
        )

    except Exception as exc:

        error_id = log_exception(
            "Jellyfin $new failed",
            exc
        )

        await ctx.reply(
            (
                "Couldn't load recent media.\n"
                f"Error ID: `{error_id}`"
            )
        )

        return

    if not items:

        await ctx.reply(
            "Jellyfin returned no recent media."
        )

        return

    lines = []

    for index, item in enumerate(
        items,
        start=1
    ):

        title = item.get(
            "Name",
            "Unknown"
        )

        year = jellyfin_item_year(
            item
        )

        kind = item.get(
            "Type",
            "Unknown"
        )

        url = jellyfin.watch_url(
            item["Id"]
        )

        lines.append(
            (
                f"**{index}. "
                f"[{title} ({year})]({url})**\n"
                f"{kind}"
            )
        )

    embed = discord.Embed(
        title="Recently Added to Jellyfin",
        description="\n\n".join(
            lines
        ),
        color=discord.Color.green()
    )

    embed.set_footer(
        text=(
            f"Showing {len(items)} "
            "most recently created library items"
        )
    )

    await ctx.reply(
        embed=embed
    )


# ============================================================
# ADMIN
# ============================================================

@bot.group(
    name="admin",
    invoke_without_command=True,
    help=(
        "Administrative MediaBot commands for health, "
        "user linking, logs, and diagnostics."
    )
)
@commands.has_guild_permissions(
    administrator=True
)
async def admin(ctx):
    await ctx.reply(
        (
            "**MediaBot admin commands**\n"
            "`$admin integrations` (or `health`) - service and tracking health\n"
            "`$admin users` - list Seerr users\n"
            "`$admin link @user SeerrUsername` - link accounts\n"
            "`$admin reports` - playback/problem report queue\n"
            "`$admin logs [lines]` - recent bot logs\n"
            "`$admin errors [lines]` - warnings/errors\n"
            "\n"
            "Use `$help admin` for generated command help."
        )
    )


@admin.command(
    name="users"
)
@commands.is_owner()
@commands.has_guild_permissions(
    administrator=True
)
async def admin_users(ctx):
    try:
        users = await seerr.users()

    except SeerrError as exc:
        error_id = log_exception("Could not read Seerr users", exc)
        await ctx.reply(f"Couldn't read Seerr users. Error ID: `{error_id}`")

        return

    if not users:
        await ctx.reply(
            "Seerr returned no users."
        )

        return

    lines = []

    for user in users:
        username = (
            user.get("username")
            or user.get("plexUsername")
            or user.get("email")
            or "UNKNOWN"
        )

        lines.append(
            f"`{user.get('id')}` - {username}"
        )

    text = "\n".join(lines)

    if len(text) > 1800:
        text = text[:1800] + "\n..."

    await send_private_output(
        ctx,
        content=f"**Seerr Users**\n{text}",
        public_success="I sent the Seerr user list to you privately.",
    )


@admin.command(
    name="link"
)
@commands.is_owner()
@commands.has_guild_permissions(
    administrator=True
)
async def admin_link(
    ctx,
    discord_user: str,
    *,
    seerr_username: str
):
    """
    Link a Discord user to a Seerr user.

    Accepts:
      - actual Discord mention
      - Discord user ID
      - username
      - display name

    Seerr matching accepts:
      - username
      - email
      - Plex username
      - numeric Seerr user ID
    """

    # --------------------------------------------------------
    # Resolve Discord member
    # --------------------------------------------------------

    member = None

    # Best case: Discord actually parsed a mention somewhere
    # in the message. This also survives things like bolding.
    if ctx.message.mentions:
        member = ctx.message.mentions[0]

    # Otherwise clean up whatever token was supplied.
    token = discord_user.strip()

    if member is None:
        cleaned = (
            token
            .replace("<@", "")
            .replace("!>", "")
            .replace(">", "")
            .replace("*", "")
            .strip()
        )

        # User ID / raw mention ID
        if cleaned.isdigit():
            user_id = int(cleaned)

            member = ctx.guild.get_member(user_id)

            if member is None:
                try:
                    member = await ctx.guild.fetch_member(user_id)
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    pass

    # Name/display-name fallback
    if member is None:
        wanted_discord = (
            token
            .replace("*", "")
            .lstrip("@")
            .strip()
            .casefold()
        )

        matches = []

        for candidate in ctx.guild.members:
            candidate_names = {
                candidate.name.casefold(),
                candidate.display_name.casefold(),
            }

            global_name = getattr(
                candidate,
                "global_name",
                None
            )

            if global_name:
                candidate_names.add(
                    global_name.casefold()
                )

            if wanted_discord in candidate_names:
                matches.append(candidate)

        if len(matches) == 1:
            member = matches[0]

        elif len(matches) > 1:
            await ctx.reply(
                (
                    "More than one Discord member matched "
                    f'**"{token}"**.\n'
                    "Use an actual @mention or Discord user ID."
                )
            )
            return

    if member is None:
        await ctx.reply(
            (
                "Couldn't identify that Discord user.\n\n"
                "Use one of:\n"
                "`$admin link @user SeerrUser`\n"
                "`$admin link DiscordUserID SeerrUser`"
            )
        )
        return

    # --------------------------------------------------------
    # Get Seerr users
    # --------------------------------------------------------

    try:
        users = await seerr.users()

    except SeerrError as exc:
        error_id = log_exception("Could not read Seerr users for account link", exc)
        await ctx.reply(f"Couldn't read Seerr users. Error ID: `{error_id}`")
        return

    wanted = seerr_username.strip().casefold()

    matches = []

    for user in users:

        # Allow exact Seerr numeric ID too.
        if wanted == str(user.get("id", "")).casefold():
            matches.append(user)
            continue

        possible_names = [
            user.get("username"),
            user.get("email"),
            user.get("plexUsername"),
        ]

        normalized = {
            str(value).strip().casefold()
            for value in possible_names
            if value
        }

        if wanted in normalized:
            matches.append(user)

    if len(matches) == 0:
        await ctx.reply(
            (
                f'No exact Seerr user match for '
                f'**"{seerr_username}"**.\n\n'
                "I checked:\n"
                "- username\n"
                "- email\n"
                "- Plex username\n"
                "- Seerr user ID\n\n"
                "Run `$admin users` to see available accounts."
            )
        )
        return

    if len(matches) > 1:
        await ctx.reply(
            (
                "More than one Seerr user matched that value. "
                "I refuse to guess.\n"
                "Use the numeric Seerr user ID shown by "
                "`$admin users`."
            )
        )
        return

    user = matches[0]

    canonical_name = (
        user.get("username")
        or user.get("email")
        or user.get("plexUsername")
        or str(user["id"])
    )

    set_link(
        member.id,
        str(member),
        int(user["id"]),
        canonical_name
    )

    await send_private_output(
        ctx,
        content=(
            f"**Account linked.**\n\n"
            f"Discord: {member}\n"
            f"Seerr: **{canonical_name}**\n"
            f"Seerr ID: `{user['id']}`"
        ),
        public_success="**Account linked.** I sent the identity details privately.",
        committed=True,
    )


@admin.group(
    name="reports",
    invoke_without_command=True,
    help=(
        "Review unresolved playback and media problem reports. "
        "Usage: $admin reports [page]"
    ),
)
@commands.has_guild_permissions(administrator=True)
async def admin_reports(ctx, page: int = 1):
    records = list_media_reports(
        discord_guild_id=ctx.guild.id,
        statuses=("open", "in_progress"),
        limit=500,
    )
    if not records:
        await ctx.reply("**Media report queue is empty.**")
        return

    initial_index = max(0, min(int(page) - 1, len(records) - 1))
    view = AdminReportQueueView(
        requester_id=ctx.author.id,
        guild_id=ctx.guild.id,
        records=records,
        command_message=ctx.message,
        initial_index=initial_index,
    )
    message = await ctx.reply(embed=view.build_embed(), view=view)
    view.message = message
    register_transient_card(
        message=message,
        command_message=ctx.message,
        kind="admin_report_queue",
    )


async def run_admin_report_transition(ctx, report_id, status, note=""):
    existing = media_report_by_id(
        int(report_id),
        discord_guild_id=ctx.guild.id,
    )
    if existing is None:
        await ctx.reply(f"No report **#{int(report_id)}** exists in this server.")
        return
    if existing["status"] not in {"open", "in_progress"}:
        await ctx.reply(
            f"Report **#{int(report_id)}** is already "
            f"**{REPORT_STATUS_LABELS.get(existing['status'], existing['status'])}**."
        )
        return

    record = await transition_media_report(
        report_id=int(report_id),
        guild_id=ctx.guild.id,
        handler_id=ctx.author.id,
        status=status,
        note=note,
    )
    if record is None:
        await ctx.reply(
            "That report changed while this command was running. "
            "Run `$admin reports` to refresh the queue."
        )
        return
    await ctx.reply(
        embed=build_report_ticket_embed(record),
        view=ReportLinksView(record["jellyfin_item_id"]),
    )


@admin_reports.command(
    name="claim",
    help="Mark one report as actively being investigated.",
)
@commands.has_guild_permissions(administrator=True)
async def admin_reports_claim(ctx, report_id: int):
    await run_admin_report_transition(ctx, report_id, "in_progress")


@admin_reports.command(
    name="resolve",
    help="Resolve one report and optionally save an administrator note.",
)
@commands.has_guild_permissions(administrator=True)
async def admin_reports_resolve(ctx, report_id: int, *, note: str = ""):
    await run_admin_report_transition(ctx, report_id, "resolved", note)


@admin_reports.command(
    name="dismiss",
    help="Dismiss one report and optionally save an administrator note.",
)
@commands.has_guild_permissions(administrator=True)
async def admin_reports_dismiss(ctx, report_id: int, *, note: str = ""):
    await run_admin_report_transition(ctx, report_id, "dismissed", note)




@admin.command(
    name="integrations",
    aliases=["health"],
    help=(
        "Show MediaBot integration and "
        "request-tracking health."
    )
)
@commands.is_owner()
@commands.has_guild_permissions(
    administrator=True
)
async def admin_integrations(
    ctx
):
    lines = [f"MediaBot: **v{BOT_VERSION}**", "Discord: **OK**"]

    try:
        heartbeat, heartbeat_age = validate_runtime_health(
            RUNTIME_HEALTH_PATH,
            expected_version=BOT_VERSION,
            max_age_seconds=120,
        )
        metrics = heartbeat.get("metrics") or {}
        lines.append(
            "Runtime: **HEALTHY** "
            f"(heartbeat {int(heartbeat_age)}s ago, "
            f"uptime {int(metrics.get('uptime_seconds') or 0)}s)"
        )
        lines.append(
            "Transient cleanup: **OK** "
            f"(last cycle {metrics.get('cleanup_last_run_age_seconds', 'pending')}s ago, "
            f"retries {int(metrics.get('cleanup_retried') or 0)})"
        )
    except RuntimeHealthError as exc:
        lines.append(f"Runtime: **FAILED** (`{exc}`)")

    try:
        with db() as conn:
            quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        database_size = os.path.getsize(DB_PATH)
        if quick_check != "ok":
            raise RuntimeError(str(quick_check))
        lines.append(
            "SQLite: **OK** "
            f"({journal_mode}, busy {busy_timeout}ms, {database_size} bytes)"
        )
    except Exception as exc:
        lines.append(f"SQLite: **FAILED** (`{type(exc).__name__}`)")

    try:

        await seerr.health()

        lines.append(
            "Seerr: **OK**"
        )

    except Exception as exc:

        lines.append(
            f"Seerr: **FAILED** (`{type(exc).__name__}`)"
        )

    if jellyfin.enabled:

        try:

            info = await jellyfin.health()

            version = (
                info.get("Version")
                or "unknown"
            )

            lines.append(
                f"Jellyfin: **OK** "
                f"(v{version})"
            )

        except Exception as exc:

            lines.append(
                f"Jellyfin: **FAILED** "
                f"(`{type(exc).__name__}`)"
            )

    else:

        lines.append(
            "Jellyfin: **NOT CONFIGURED**"
        )

    if soulsync.enabled:
        try:
            health = await soulsync.health()
            services = health.get("services") or {}
            lines.append(
                "SoulSync: **OK** "
                f"(Soulseek {'OK' if services.get('soulseek') else 'FAILED'})"
            )
        except Exception as exc:
            lines.append(
                f"SoulSync: **FAILED** (`{type(exc).__name__}`)"
            )
    else:
        lines.append("SoulSync: **NOT CONFIGURED**")

    if sonarr.enabled:
        try:
            health = await sonarr.health()
            lines.append(
                f"Sonarr: **OK** (v{health.get('version') or 'unknown'})"
            )
        except Exception as exc:
            lines.append(f"Sonarr: **FAILED** (`{type(exc).__name__}`)")
    else:
        lines.append("Sonarr: **NOT CONFIGURED**")

    stats = tracking_stats(discord_guild_id=ctx.guild.id)
    intent_stats = media_request_intent_stats(discord_guild_id=ctx.guild.id)

    lines.append(
        (
            "Tracked video requests: "
            f"**{stats['total']}**"
        )
    )

    lines.append(
        (
            "Waiting to be indexed in Jellyfin: "
            f"**{stats['pending']}**"
        )
    )

    lines.append(
        (
            "Matched to Jellyfin: "
            f"**{stats['available']}**"
        )
    )

    lines.append(
        "Durable request ledger: "
        f"**{intent_stats['prepared']} preparing**, "
        f"**{intent_stats['accepted']} awaiting recovery**"
    )

    report_counts = media_report_stats(ctx.guild.id)
    lines.append(
        "Open/claimed media reports: "
        f"**{report_counts['active']}**"
    )

    try:
        event_records = events.list_events(
            discord_guild_id=ctx.guild.id,
            limit=500,
        )
        open_events = sum(
            record.status is EventStatus.OPEN for record in event_records
        )
        scheduled_events = sum(
            record.status is EventStatus.SCHEDULED for record in event_records
        )
        lines.append(
            "Events: **OK** "
            f"({open_events} open, {scheduled_events} scheduled, "
            f"{len(event_records)} total)"
        )
    except Exception as exc:
        lines.append(f"Events: **FAILED** (`{type(exc).__name__}`)")

    await send_private_output(
        ctx,
        content="\n".join(lines),
        public_success="I sent the integration health report privately.",
    )


# ============================================================
# ADMIN LOGGING
# ============================================================

def count_log_lines():
    try:
        with open(
            LOG_PATH,
            "r",
            encoding="utf-8",
            errors="replace"
        ) as handle:
            return sum(
                1
                for _ in handle
            )

    except FileNotFoundError:
        return 0


def read_log_tail(
    line_count: int = 80
):
    line_count = max(
        1,
        min(
            int(line_count),
            500
        )
    )

    try:
        with open(
            LOG_PATH,
            "r",
            encoding="utf-8",
            errors="replace"
        ) as handle:
            lines = handle.readlines()

    except FileNotFoundError:
        return (
            "MediaBot log file does not "
            "exist yet."
        )

    return redact_log_text(
        "".join(
            lines[-line_count:]
        )
    )


async def send_log_output(
    ctx,
    text: str,
    *,
    filename: str
):
    if not text.strip():
        text = "(no matching log entries)"

    # Discord code block comfortably under the
    # message-size ceiling.
    if len(text) <= 1750:

        await send_private_output(
            ctx,
            content=f"```text\n{text}\n```",
            public_success="I sent the requested diagnostics privately.",
        )
        return

    payload = io.BytesIO(
        text.encode(
            "utf-8",
            errors="replace"
        )
    )

    await send_private_output(
        ctx,
        file=discord.File(
            payload,
            filename=filename
        ),
        public_success="I sent the requested diagnostics privately.",
    )


@admin.command(
    name="logs",
    help=(
        "Show recent MediaBot logs. "
        "Usage: $admin logs [lines]"
    )
)
@commands.is_owner()
@commands.has_guild_permissions(
    administrator=True
)
async def admin_logs(
    ctx,
    lines: int = 80
):
    lines = max(
        1,
        min(
            lines,
            500
        )
    )

    available = count_log_lines()

    output = read_log_tail(
        lines
    )

    shown = min(
        lines,
        available
    )

    header = (
        "MediaBot Logs\n"
        f"Requested: {lines} line(s)\n"
        f"Showing:   {shown} of {available} available line(s)\n"
        + ("-" * 60)
        + "\n"
    )

    await send_log_output(
        ctx,
        header + output,
        filename="mediabot-logs.txt"
    )


@admin.command(
    name="errors",
    help=(
        "Show recent MediaBot warnings, "
        "errors and exception IDs."
    )
)
@commands.is_owner()
@commands.has_guild_permissions(
    administrator=True
)
async def admin_errors(
    ctx,
    lines: int = 120
):
    # Read a broader window, then filter it.
    raw = read_log_tail(
        max(
            lines * 5,
            250
        )
    )

    interesting = []

    for line in raw.splitlines():

        upper = line.upper()

        if any(
            token in upper
            for token in (
                "| ERROR |",
                "| WARNING |",
                "ERROR_ID=",
                "TRACEBACK",
                "EXCEPTION"
            )
        ):
            interesting.append(line)

    selected = interesting[
        -lines:
    ]

    output = "\n".join(
        selected
    )

    header = (
        "MediaBot Warnings / Errors\n"
        f"Requested: {lines} entrie(s)\n"
        f"Showing:   {len(selected)} matching entrie(s)\n"
        + ("-" * 60)
        + "\n"
    )

    if not output.strip():
        output = "(no matching log entries)"

    await send_log_output(
        ctx,
        header + output,
        filename="mediabot-errors.txt"
    )


# ============================================================
# ERRORS
# ============================================================

@bot.event
async def on_command_error(
    ctx,
    error
):
    # discord.py often wraps the useful exception.
    original = getattr(
        error,
        "original",
        error
    )

    if isinstance(
        error,
        commands.CommandNotFound
    ):
        attempted = str(getattr(ctx, "invoked_with", "") or "").casefold()
        candidates = sorted({
            command.name
            for command in bot.commands
            if not command.hidden
        })
        matches = difflib.get_close_matches(attempted, candidates, n=1, cutoff=0.55)
        suggestion = (
            f" Did you mean `{ctx.clean_prefix}{matches[0]}`?"
            if matches
            else ""
        )
        await ctx.reply(
            f"I don't know `{ctx.clean_prefix}{attempted}`.{suggestion}\n"
            f"Run `{ctx.clean_prefix}help` for the short command list."
        )
        return

    if isinstance(original, commands.CommandOnCooldown):
        await ctx.reply(
            "That command is cooling down. Try again in "
            f"**{max(1, round(original.retry_after))} second(s)**."
        )
        return

    if isinstance(original, commands.MaxConcurrencyReached):
        await ctx.reply(
            "That command is already running here. Let the current search finish "
            "and try again."
        )
        return

    if isinstance(original, commands.NoPrivateMessage):
        await ctx.reply("That command only works inside the Media Discord server.")
        return

    if isinstance(original, commands.NotOwner):
        await ctx.reply("That command is restricted to the MediaBot owner.")
        return

    if isinstance(original, commands.CheckFailure):
        await ctx.reply(str(original) or "That command is not available here.")
        return

    if isinstance(
        error,
        commands.MissingPermissions
    ):
        await ctx.reply(
            (
                "You do not have permission "
                "to use that command."
            )
        )

        return

    if isinstance(
        error,
        commands.MemberNotFound
    ):
        await ctx.reply(
            (
                "Couldn't identify that "
                "Discord user."
            )
        )

        return

    if isinstance(
        error,
        commands.BadArgument
    ):
        await ctx.reply(
            (
                "I couldn't understand one of "
                "those arguments.\n"
                f"Try `$help {ctx.command}`."
            )
        )

        return

    if isinstance(error, commands.UserInputError):
        usage = (
            command_usage(ctx.command, ctx.clean_prefix)
            if ctx.command is not None
            else f"{ctx.clean_prefix}help"
        )
        await ctx.reply(
            f"I couldn't use those arguments.\nUsage: `{usage}`"
        )
        return

    error_id = log_exception(
        (
            "Command failure "
            f"command={ctx.command} "
            f"user={ctx.author.id} "
            f"guild={getattr(ctx.guild, 'id', None)}"
        ),
        original
    )

    try:
        await ctx.reply(
            (
                "**MediaBot hit an internal error.**\n"
                f"Error ID: `{error_id}`\n\n"
                "An administrator can inspect it "
                "with `$admin errors`."
            )
        )

    except Exception as response_error:
        log_exception(
            (
                "Could not report command "
                f"error_id={error_id}"
            ),
            response_error
        )


# ============================================================
# MAIN
# ============================================================

async def run_client_until_shutdown(client, token, shutdown_requested):
    """Run Discord until it exits or a Unix shutdown request arrives."""
    client_task = asyncio.create_task(client.start(token))
    shutdown_task = asyncio.create_task(shutdown_requested.wait())
    try:
        done, _ = await asyncio.wait(
            {client_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done and not client_task.done():
            active = {
                task for task in ACTIVE_MEDIA_SUBMISSIONS if not task.done()
            }
            if active:
                logger.warning(
                    "Shutdown waiting for %s active media submission(s)",
                    len(active),
                )
                _, pending = await asyncio.wait(
                    active,
                    timeout=max(1, MEDIA_SUBMISSION_DRAIN_SECONDS),
                )
                if pending:
                    logger.error(
                        "Shutdown drain expired with %s media submission(s); "
                        "durable intents will reconcile after restart",
                        len(pending),
                    )
            await client.close()
        await client_task
    finally:
        if not shutdown_task.done():
            shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)


async def main():
    if not ALLOWED_GUILD_IDS:
        raise RuntimeError("ALLOWED_GUILD_IDS must name at least one trusted server.")
    init_db()
    init_tracking_db()
    recovery = recover_accepted_media_request_intents()
    if recovery["failed"]:
        raise RuntimeError(
            "Could not recover accepted media request intents; refusing startup."
        )
    events.initialize()
    write_runtime_health_snapshot("starting")
    shutdown_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(shutdown_signal, shutdown_requested.set)
            installed_signals.append(shutdown_signal)
        except (NotImplementedError, RuntimeError):
            # Windows test/dev loops do not implement Unix signal handlers.
            pass
    try:
        await seerr.start()
        await jellyfin.start()
        await soulsync.start()
        await sonarr.start()

        try:
            await seerr.health()

            print(
                f"Seerr API reachable at {SEERR_URL}"
            )

        except Exception as exc:
            print(
                f"WARNING: Initial Seerr test failed: {exc}"
            )

        if not shutdown_requested.is_set():
            await run_client_until_shutdown(
                bot,
                DISCORD_TOKEN,
                shutdown_requested,
            )

    finally:
        for shutdown_signal in installed_signals:
            loop.remove_signal_handler(shutdown_signal)
        try:
            write_runtime_health_snapshot("stopping")
        except Exception as exc:
            logger.warning("Could not mark runtime stopping: %s", exc)
        watcher_tasks = []
        for watcher in (
            runtime_health_watcher,
            transient_ui_cleanup_watcher,
            jellyfin_availability_watcher,
            event_lifecycle_watcher,
        ):
            if watcher.is_running():
                watcher_task = watcher.get_task()
                if watcher_task is not None:
                    watcher_tasks.append(watcher_task)
                watcher.cancel()
        if watcher_tasks:
            await asyncio.gather(*watcher_tasks, return_exceptions=True)
        await sonarr.close()
        await soulsync.close()
        await jellyfin.close()
        await seerr.close()


if __name__ == "__main__":
    asyncio.run(main())
