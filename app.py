import os
import sqlite3
import asyncio
import io
import logging
import re
import secrets
from logging.handlers import RotatingFileHandler
from urllib.parse import urlencode, quote
from yarl import URL
from typing import Optional

import aiohttp
import discord
from discord.ext import commands


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
        "180"
    )
)


# ============================================================
# LOGGING
# ============================================================

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

    formatter = logging.Formatter(
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
    # Discord bot-token-ish values
    value = re.sub(
        r'(?i)'
        r'([A-Za-z0-9_-]{20,})'
        r'\.([A-Za-z0-9_-]{5,})'
        r'\.([A-Za-z0-9_-]{20,})',
        '[REDACTED_DISCORD_TOKEN]',
        value
    )

    # Common explicit secrets
    value = re.sub(
        r'(?i)'
        r'(DISCORD_TOKEN|'
        r'SEERR_API_KEY|'
        r'JELLYFIN_API_KEY|'
        r'SOULSYNC_API_KEY)'
        r'\s*[:=]\s*\S+',
        r'\1=[REDACTED]',
        value
    )

    # HTTP API key headers
    value = re.sub(
        r'(?i)'
        r'(X-Api-Key|Authorization)'
        r'\s*[:=]\s*\S+',
        r'\1: [REDACTED]',
        value
    )

    return value


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
    help_command=None
)


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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


# ============================================================
# SEERR CLIENT
# ============================================================

class SeerrError(Exception):
    pass


class SeerrClient:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self.session = aiohttp.ClientSession(
            headers={
                "X-Api-Key": SEERR_API_KEY,
                "Accept": "application/json"
            },
            timeout=aiohttp.ClientTimeout(total=20)
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def request(
        self,
        method: str,
        path: str,
        **kwargs
    ):
        if not self.session:
            raise RuntimeError(
                "Seerr HTTP session not initialized"
            )

        url = f"{SEERR_URL}/api/v1{path}"

        # Seerr's API validator follows strict URI
        # reserved-character handling.
        #
        # aiohttp normally emits spaces in mapping-style
        # query parameters as '+'. The Seerr/Overseerr
        # validator rejects '+' and expects percent encoding
        # such as '%20'.
        #
        # Build our query string ourselves using quote(),
        # not quote_plus(), so ALL MediaBot API calls get
        # RFC3986-style percent encoding.
        params = kwargs.pop(
            "params",
            None
        )

        if params:
            encoded_query = urlencode(
                params,
                doseq=True,
                quote_via=quote,
                safe=""
            )

            url = (
                f"{url}?{encoded_query}"
            )

        # IMPORTANT:
        #
        # The query string above is intentionally encoded
        # with urllib.parse.quote instead of quote_plus.
        #
        # yarl/aiohttp normally canonicalizes URLs and can
        # turn encoded reserved characters such as %27
        # back into literal apostrophes before transmission.
        #
        # Seerr's query validator rejects those literals.
        #
        # encoded=True means:
        # "this URL is already exactly how I want it sent."
        request_url = URL(
            url,
            encoded=True
        )

        async with self.session.request(
            method,
            request_url,
            **kwargs
        ) as response:

            try:
                payload = await response.json()
            except Exception:
                payload = {
                    "message": await response.text()
                }

            if response.status >= 400:
                message = (
                    payload.get("message")
                    if isinstance(payload, dict)
                    else str(payload)
                )

                raise SeerrError(
                    f"Seerr HTTP {response.status}: {message}"
                )

            return payload

    async def health(self):
        return await self.request(
            "GET",
            "/settings/public"
        )

    async def search(self, query: str):
        result = await self.request(
            "GET",
            "/search",
            params={
                "query": query,
                "page": 1
            }
        )

        return [
            item
            for item in result.get("results", [])
            if item.get("mediaType") in ("movie", "tv")
        ]

    async def users(self):
        result = await self.request(
            "GET",
            "/user"
        )

        # Handle either current array response or paginated-style
        # response gracefully.
        if isinstance(result, list):
            return result

        if isinstance(result, dict):
            return result.get("results", [])

        return []

    async def tv_details(self, media_id: int):
        return await self.request(
            "GET",
            f"/tv/{media_id}"
        )

    async def create_request(
        self,
        *,
        media_type: str,
        media_id: int,
        seerr_user_id: int,
        seasons=None
    ):
        payload = {
            "mediaType": media_type,
            "mediaId": media_id,
            "userId": seerr_user_id,
            "is4k": False
        }

        if media_type == "tv":
            payload["seasons"] = seasons or []

        return await self.request(
            "POST",
            "/request",
            json=payload
        )


seerr = SeerrClient()


# ============================================================
# MEDIA HELPERS
# ============================================================

MEDIA_STATUS = {
    1: "Unknown",
    2: "Pending",
    3: "Processing",
    4: "Partially Available",
    5: "Available",
    6: "Deleted"
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


# ============================================================
# REQUEST UI HELPERS
# ============================================================

RESULTS_PER_PAGE = 5


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
        title=f"{title} ({year})",
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
            value=state_text,
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
        seasons=None
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

        return True

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
                    f"({year})** to Seerr..."
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

        # ----------------------------------------------------
        # SUBMIT TO SEERR
        # ----------------------------------------------------

        try:
            result = await seerr.create_request(
                media_type=media_type,
                media_id=media_id,
                seerr_user_id=(
                    self.seerr_user_id
                ),
                seasons=self.seasons
            )

        except Exception as exc:

            self._submitting = False

            for child in self.children:
                child.disabled = False

            error_id = log_exception(
                (
                    "Seerr create_request failed "
                    f"title={title!r} "
                    f"year={year!r} "
                    f"media_type={media_type!r} "
                    f"media_id={media_id!r}"
                ),
                exc
            )

            try:
                await interaction.edit_original_response(
                    content=(
                        "**Request failed.**\n\n"
                        f"Error ID: `{error_id}`\n"
                        "Nothing was submitted successfully.\n\n"
                        "You may retry or ask an administrator "
                        "to run `$admin errors`."
                    ),
                    view=self
                )

            except Exception as response_error:
                log_exception(
                    (
                        "Could not update failed "
                        "request interaction"
                    ),
                    response_error
                )

            return

        # ----------------------------------------------------
        # SEERR ACCEPTED IT
        # ----------------------------------------------------

        self.finished = True
        self.stop()

        request_id = result.get(
            "id",
            "?"
        )

        request_status = result.get(
            "status"
        )

        status_text = {
            1: "Pending approval",
            2: "Approved",
            3: "Declined"
        }.get(
            request_status,
            str(request_status)
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
            request_id,
            status_text
        )

        final_embed = build_media_embed(
            self.item,
            self.details,
            heading="Request",
            state_text=(
                f"**{status_text}**\n"
                f"Seerr request `#{request_id}`"
            ),
            color=discord.Color.green()
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
                    f"request_id={request_id}"
                ),
                exc
            )

        # ----------------------------------------------------
        # FINALIZE PRIVATE CONFIRMATION
        # ----------------------------------------------------

        try:
            await interaction.edit_original_response(
                content=(
                    f"Requested **{title} "
                    f"({year})**.\n"
                    f"Status: **{status_text}**.\n"
                    f"Seerr request: `#{request_id}`"
                ),
                embed=None,
                view=None
            )

        except Exception as exc:
            log_exception(
                (
                    "Request succeeded but "
                    "private confirmation cleanup "
                    f"failed request_id={request_id}"
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
        self.finished = True
        self.stop()

        title = media_title(
            self.item
        )

        year = media_year(
            self.item
        )

        cancelled = build_media_embed(
            self.item,
            self.details,
            heading="Request",
            state_text=(
                "**Nothing was requested.**"
            ),
            color=discord.Color.dark_grey()
        )

        await self.origin_message.edit(
            embed=cancelled,
            view=MediaLinksView(
                self.item
            )
        )

        await interaction.response.edit_message(
            content=(
                f"Cancelled **{title} "
                f"({year})**."
            ),
            embed=None,
            view=None
        )

    async def on_timeout(self):
        if self.finished:
            return

        title = media_title(
            self.item
        )

        year = media_year(
            self.item
        )

        expired = build_media_embed(
            self.item,
            self.details,
            heading="Request",
            state_text=(
                "**Confirmation expired. "
                "Nothing was requested.**"
            ),
            color=discord.Color.dark_grey()
        )

        try:
            await self.origin_message.edit(
                embed=expired,
                view=MediaLinksView(
                    self.item
                )
            )
        except Exception:
            pass

        await self.finish_confirmation_message(
            (
                f"Confirmation for "
                f"**{title} ({year})** "
                "expired."
            )
        )


# ============================================================
# SEARCH RESULT BUTTON
# ============================================================

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
        total_seerr_pages: int
    ):
        super().__init__(
            timeout=REQUEST_UI_TIMEOUT
        )

        self.requester_id = (
            requester_id
        )

        self.query = query

        self.results = []
        self.seen = set()

        self.seerr_page = (
            seerr_page
        )

        self.total_seerr_pages = (
            total_seerr_pages
        )

        self.display_page = 0

        self.message = None
        self.finished = False

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

    async def ensure_loaded(
        self,
        display_page: int
    ):
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
            title=(
                f'Search results for '
                f'"{self.query}"'
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

        return True

    async def previous_page(
        self,
        interaction: discord.Interaction
    ):
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
        self.finished = True
        self.stop()

        embed = discord.Embed(
            title=(
                f'Search for '
                f'"{self.query}" cancelled'
            ),
            description=(
                "**Nothing was selected "
                "or requested.**"
            ),
            color=discord.Color.dark_grey()
        )

        await interaction.response.edit_message(
            embed=embed,
            view=None
        )

    async def select_slot(
        self,
        interaction: discord.Interaction,
        slot: int
    ):
        items = self.page_items()

        if slot >= len(items):
            await interaction.response.send_message(
                "That result no longer exists.",
                ephemeral=True
            )

            return

        item = items[slot]

        title = media_title(item)
        year = media_year(item)

        # Available media does not need a Seerr user link.
        if already_available(item):
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

            await interaction.followup.send(
                (
                    f"**{title} ({year})** "
                    "is already available."
                ),
                ephemeral=True
            )

            return

        if already_underway(item):
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

            await interaction.followup.send(
                (
                    f"**{title} ({year})** "
                    "is already in progress."
                ),
                ephemeral=True
            )

            return

        link = get_link(
            interaction.user.id
        )

        if not link:
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
        extra = ""

        if item["mediaType"] == "tv":
            season_data = (
                details.get("seasons")
                or []
            )

            seasons = sorted({
                int(
                    season[
                        "seasonNumber"
                    ]
                )
                for season in season_data
                if int(
                    season.get(
                        "seasonNumber",
                        0
                    )
                ) > 0
            })

            if not seasons:
                await interaction.followup.send(
                    (
                        "Seerr returned no "
                        "requestable seasons."
                    ),
                    ephemeral=True
                )

                return

            extra = (
                "\n\n"
                f"This will request "
                f"**{len(seasons)} season(s)**:\n"
                f"`{', '.join(map(str, seasons))}`"
            )

        # THIS is the lingering-box fix.
        #
        # The original public search message immediately
        # becomes the selected item.
        self.finished = True
        self.stop()

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

        confirm_view = ConfirmRequestView(
            requester_id=(
                interaction.user.id
            ),
            item=item,
            details=details,
            seerr_user_id=(
                link["seerr_user_id"]
            ),
            origin_message=(
                interaction.message
            ),
            seasons=seasons
        )

        confirmation_embed = (
            build_media_embed(
                item,
                details,
                heading="Request As",
                state_text=(
                    f"**{link['seerr_username']}**"
                    + extra
                ),
                color=discord.Color.gold()
            )
        )

        confirmation_message = (
            await interaction.followup.send(
                content=(
                    "**Confirm this request.**"
                ),
                embed=confirmation_embed,
                view=confirm_view,
                ephemeral=True,
                wait=True
            )
        )

        confirm_view.confirm_message = (
            confirmation_message
        )

    async def on_timeout(self):
        if self.finished:
            return

        embed = discord.Embed(
            title=(
                f'Search for '
                f'"{self.query}" expired'
            ),
            description=(
                "**Nothing was selected "
                "or requested.**\n\n"
                "Run `$request` again to "
                "start a new search."
            ),
            color=discord.Color.dark_grey()
        )

        if self.message:
            try:
                await self.message.edit(
                    embed=embed,
                    view=None
                )
            except Exception:
                pass


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():
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


# ============================================================
# BASIC COMMANDS
# ============================================================

# ============================================================
# AUTOMATIC HELP
# ============================================================

HELP_FALLBACKS = {
    "help": (
        "Show current MediaBot commands or detailed "
        "help for a specific command."
    ),
    "ping": (
        "Check whether MediaBot is alive and measure "
        "Discord latency."
    ),
    "whoami": (
        "Show your linked Discord and Seerr identity."
    ),
    "request": (
        "Search for a movie or TV show and request it "
        "through Seerr."
    ),
    "admin": (
        "Administrative MediaBot commands."
    ),
    "health": (
        "Check Discord and Seerr connectivity."
    ),
    "users": (
        "List Seerr users available for Discord linking."
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


@bot.command(
    name="help",
    help=(
        "Show all MediaBot commands or "
        "help for one command."
    )
)
async def mediabot_help(
    ctx,
    *,
    topic: str = None
):
    prefix = ctx.clean_prefix

    if topic:
        command = bot.get_command(
            topic.strip()
        )

        if command is None:
            await ctx.reply(
                (
                    f"No command named "
                    f"`{topic}` exists.\n"
                    f"Run `{prefix}help` "
                    "for the current list."
                )
            )
            return

        usage = (
            f"{prefix}"
            f"{command.qualified_name}"
        )

        if command.signature:
            usage += (
                f" {command.signature}"
            )

        description = (
            command.help
            or HELP_FALLBACKS.get(
                command.name
            )
            or "No description yet."
        )

        embed = discord.Embed(
            title=(
                f"Help: "
                f"{prefix}"
                f"{command.qualified_name}"
            ),
            description=description,
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="Usage",
            value=f"`{usage}`",
            inline=False
        )

        if isinstance(
            command,
            commands.Group
        ):
            children = sorted(
                command.commands,
                key=lambda cmd: cmd.name
            )

            if children:
                lines = []

                for child in children:
                    child_usage = (
                        f"{prefix}"
                        f"{child.qualified_name}"
                    )

                    if child.signature:
                        child_usage += (
                            f" {child.signature}"
                        )

                    description = (
                        child.help
                        or HELP_FALLBACKS.get(
                            child.name
                        )
                        or "No description yet."
                    )

                    lines.append(
                        f"`{child_usage}`\n"
                        f"{description}"
                    )

                embed.add_field(
                    name="Subcommands",
                    value="\n\n".join(
                        lines
                    ),
                    inline=False
                )

        await ctx.reply(
            embed=embed
        )

        return

    embed = discord.Embed(
        title="Dogginator Media Help",
        description=(
            "Commands are generated from "
            "MediaBot's currently registered "
            "command list."
        ),
        color=discord.Color.blurple()
    )

    command_lines = []

    for command in sorted(
        bot.commands,
        key=lambda cmd: cmd.name
    ):
        if command.hidden:
            continue

        description = (
            command.help
            or HELP_FALLBACKS.get(
                command.name
            )
            or "No description yet."
        )

        usage = (
            f"{prefix}"
            f"{command.name}"
        )

        if command.signature:
            usage += (
                f" {command.signature}"
            )

        command_lines.append(
            f"`{usage}`\n"
            f"{description}"
        )

    embed.add_field(
        name="Commands",
        value="\n\n".join(
            command_lines
        ),
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



@bot.command()
async def ping(ctx):
    await ctx.reply(
        f"Pong. `{round(bot.latency * 1000)} ms`"
    )


@bot.command()
async def whoami(ctx):
    link = get_link(ctx.author.id)

    if not link:
        await ctx.reply(
            (
                "Discord account: "
                f"**{ctx.author.display_name}**\n"
                "Seerr account: **NOT LINKED**"
            )
        )

        return

    await ctx.reply(
        (
            f"Discord: **{ctx.author.display_name}**\n"
            f"Seerr: **{link['seerr_username']}**\n"
            f"Seerr user ID: `{link['seerr_user_id']}`"
        )
    )


# ============================================================
# REQUEST
# ============================================================

@bot.command(
    help=(
        "Search for a movie or TV show, "
        "choose the correct result, and "
        "request it through Seerr."
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

    async with ctx.typing():
        try:
            results, total_pages = (
                await fetch_search_page(
                    query,
                    1
                )
            )

        except SeerrError as exc:
            logger.warning(
                "SEERR SEARCH FAILED | query=%r | error=%s",
                query,
                exc
            )

            await ctx.reply(
                (
                    "Seerr search failed:\n"
                    f"```text\n{exc}\n```"
                )
            )

            return

    if not results:
        await ctx.reply(
            f'Nothing found for **"{query}"**.'
        )

        return

    view = SearchResultsView(
        requester_id=ctx.author.id,
        query=query,
        results=results,
        seerr_page=1,
        total_seerr_pages=(
            total_pages
        )
    )

    message = await ctx.reply(
        embed=view.build_embed(),
        view=view
    )

    view.message = message


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
            "`$admin health` - service connectivity\n"
            "`$admin users` - list Seerr users\n"
            "`$admin link @user SeerrUsername` - link accounts\n"
            "`$admin logs [lines]` - recent bot logs\n"
            "`$admin errors [lines]` - warnings/errors\n"
            "\n"
            "Use `$help admin` for generated command help."
        )
    )


@admin.command(
    name="health"
)
@commands.has_guild_permissions(
    administrator=True
)
async def admin_health(ctx):
    try:
        await seerr.health()

        await ctx.reply(
            "Discord: **OK**\nSeerr API: **OK**"
        )

    except Exception as exc:
        await ctx.reply(
            f"Seerr API: **FAILED**\n```text\n{exc}\n```"
        )


@admin.command(
    name="users"
)
@commands.has_guild_permissions(
    administrator=True
)
async def admin_users(ctx):
    try:
        users = await seerr.users()

    except SeerrError as exc:
        await ctx.reply(
            f"Couldn't read Seerr users:\n```text\n{exc}\n```"
        )

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

    await ctx.reply(
        f"**Seerr Users**\n{text}"
    )


@admin.command(
    name="link"
)
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
        await ctx.reply(
            f"Couldn't read Seerr users:\n"
            f"```text\n{exc}\n```"
        )
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

    await ctx.reply(
        (
            f"**Account linked.**\n\n"
            f"Discord: {member.mention}\n"
            f"Seerr: **{canonical_name}**\n"
            f"Seerr ID: `{user['id']}`"
        )
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

        await ctx.reply(
            f"```text\n{text}\n```"
        )

        return

    payload = io.BytesIO(
        text.encode(
            "utf-8",
            errors="replace"
        )
    )

    await ctx.reply(
        file=discord.File(
            payload,
            filename=filename
        )
    )


@admin.command(
    name="logs",
    help=(
        "Show recent MediaBot logs. "
        "Usage: $admin logs [lines]"
    )
)
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

async def main():
    init_db()

    await seerr.start()

    try:
        await seerr.health()

        print(
            f"Seerr API reachable at {SEERR_URL}"
        )

    except Exception as exc:
        print(
            f"WARNING: Initial Seerr test failed: {exc}"
        )

    try:
        await bot.start(DISCORD_TOKEN)

    finally:
        await seerr.close()


if __name__ == "__main__":
    asyncio.run(main())
