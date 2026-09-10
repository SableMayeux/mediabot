"""Narrow client for the internal, fail-closed torrent intake gateway."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp


class TorrentIntakeError(RuntimeError):
    """Raised when a magnet cannot be safely accepted by the intake gateway."""


class TorrentInputError(ValueError):
    """Raised for invalid owner-supplied torrent input."""


@dataclass(frozen=True)
class MagnetReference:
    info_hash: str


@dataclass(frozen=True)
class TorrentIntakeResult:
    info_hash: str
    category: str
    duplicate: bool


_CATEGORY_ALIASES = {
    "app": "applications",
    "application": "applications",
    "applications": "applications",
    "apps": "applications",
    "album": "music",
    "albums": "music",
    "audio": "music",
    "film": "movies",
    "game": "games",
    "games": "games",
    "misc": "other",
    "movie": "movies",
    "movies": "movies",
    "music": "music",
    "other": "other",
    "program": "applications",
    "programs": "applications",
    "song": "music",
    "songs": "music",
    "software": "applications",
    "show": "tv",
    "shows": "tv",
    "series": "tv",
    "tv": "tv",
}

_CATEGORY_LABELS = {
    "applications": "application",
    "games": "game",
    "movies": "movie",
    "music": "music",
    "other": "other",
    "tv": "TV",
}

_MANUAL_REVIEW_CATEGORIES = frozenset({"applications", "games", "other"})


def normalize_torrent_category(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    try:
        return _CATEGORY_ALIASES[normalized]
    except KeyError as exc:
        raise TorrentInputError(
            "Choose `movie`, `tv`, `music`, `game`, `app`, or `other` "
            "before the magnet link."
        ) from exc


def torrent_category_label(category: str) -> str:
    """Return a safe display label for one canonical gateway category."""

    try:
        return _CATEGORY_LABELS[str(category)]
    except KeyError as exc:
        raise TorrentInputError("The torrent gateway returned an unknown category.") from exc


def torrent_category_requires_manual_review(category: str) -> bool:
    """Whether intake must remain stopped in isolated manual quarantine."""

    return str(category) in _MANUAL_REVIEW_CATEGORIES


def parse_magnet_reference(value: str) -> MagnetReference:
    magnet = str(value or "").strip()
    if not magnet:
        raise TorrentInputError("The magnet link is blank.")
    if len(magnet) > 8_192:
        raise TorrentInputError("The magnet link is too long.")
    if any(character.isspace() or ord(character) < 32 for character in magnet):
        raise TorrentInputError("The magnet link contains whitespace or control characters.")

    parsed = urlsplit(magnet)
    if parsed.scheme.casefold() != "magnet" or parsed.netloc or parsed.path or parsed.fragment:
        raise TorrentInputError("That is not a canonical `magnet:?xt=...` link.")

    try:
        query = parse_qs(
            parsed.query,
            keep_blank_values=False,
            strict_parsing=False,
            max_num_fields=100,
        )
    except ValueError as exc:
        raise TorrentInputError("The magnet query is malformed.") from exc

    btih_hashes: set[str] = set()
    for exact_topic in query.get("xt", []):
        normalized = exact_topic.casefold()
        if normalized.startswith("urn:btih:"):
            digest = exact_topic[9:]
            if re.fullmatch(r"[0-9a-fA-F]{40}", digest):
                btih_hashes.add(digest.casefold())
                continue
            if re.fullmatch(r"[A-Za-z2-7]{32}", digest):
                try:
                    decoded = base64.b32decode(digest.upper(), casefold=True)
                except binascii.Error as exc:
                    raise TorrentInputError("The magnet BTIH digest is malformed.") from exc
                btih_hashes.add(decoded.hex())
                continue
            raise TorrentInputError("The magnet BTIH digest is malformed.")

        if normalized.startswith("urn:btmh:1220"):
            digest = exact_topic[13:]
            if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise TorrentInputError("The magnet BTMH digest is malformed.")

    if not btih_hashes:
        raise TorrentInputError(
            "That magnet has no BTIH identifier. BitTorrent v2-only magnets are not "
            "accepted yet."
        )
    if len(btih_hashes) != 1:
        raise TorrentInputError("The magnet contains conflicting BTIH identifiers.")
    return MagnetReference(next(iter(btih_hashes)))


class TorrentIntakeService:
    """Submit one validated magnet without exposing qBittorrent credentials."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token_path: str | os.PathLike[str] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = str(
            base_url if base_url is not None else os.environ.get("TORRENT_INTAKE_URL", "")
        ).strip().rstrip("/")
        self.token_path = Path(
            token_path
            if token_path is not None
            else os.environ.get(
                "TORRENT_INTAKE_TOKEN_PATH",
                "/run/secrets/torrent_intake_token",
            )
        )
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    async def start(self) -> None:
        if not self.enabled or (self.session and not self.session.closed):
            return
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TorrentIntakeError("Torrent intake credential is unavailable.") from exc
        if len(token) < 43 or any(character.isspace() for character in token):
            raise TorrentIntakeError("Torrent intake credential is malformed.")
        self.session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=self.timeout,
        )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            raise TorrentIntakeError("Torrent intake is not configured.")
        if not self.session or self.session.closed:
            raise TorrentIntakeError("Torrent intake session is not initialized.")
        try:
            async with self.session.get(
                f"{self.base_url}/health",
                allow_redirects=False,
            ) as response:
                try:
                    payload: Any = await response.json(content_type=None)
                except Exception:
                    payload = None
                if response.status != 200:
                    raise TorrentIntakeError("The torrent intake gateway is unhealthy.")
        except TorrentIntakeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TorrentIntakeError("The torrent intake gateway is unavailable.") from exc
        if payload != {"ok": True}:
            raise TorrentIntakeError("Torrent intake returned an invalid health receipt.")

        # /health is intentionally unauthenticated so Docker can monitor the
        # gateway without receiving the intake credential. Prove that this
        # client's mounted token matches the token loaded by the live gateway
        # with an authenticated request that cannot enqueue anything: an empty
        # object is rejected before magnet parsing or qBittorrent I/O.
        try:
            async with self.session.post(
                f"{self.base_url}/v1/torrents",
                json={},
                allow_redirects=False,
            ) as response:
                try:
                    auth_payload: Any = await response.json(content_type=None)
                except Exception:
                    auth_payload = None
                if response.status != 400 or auth_payload != {"error": "invalid_request"}:
                    raise TorrentIntakeError(
                        "Torrent intake returned an invalid authentication receipt."
                    )
        except TorrentIntakeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TorrentIntakeError("The torrent intake gateway is unavailable.") from exc
        return payload

    async def submit(self, category: str, magnet: str) -> TorrentIntakeResult:
        normalized_category = normalize_torrent_category(category)
        reference = parse_magnet_reference(magnet)
        if not self.enabled:
            raise TorrentIntakeError("Torrent intake is not configured.")
        if not self.session or self.session.closed:
            raise TorrentIntakeError("Torrent intake session is not initialized.")

        try:
            async with self.session.post(
                f"{self.base_url}/v1/torrents",
                json={"category": normalized_category, "magnet": magnet},
                allow_redirects=False,
            ) as response:
                response_status = response.status
                try:
                    payload: Any = await response.json(content_type=None)
                except Exception:
                    payload = None
                if response_status not in {200, 201}:
                    code = payload.get("error") if isinstance(payload, dict) else None
                    messages = {
                        "duplicate_conflict": (
                            "That torrent already exists under a different media category."
                        ),
                        "invalid_category": "The torrent category was rejected.",
                        "invalid_magnet": "The magnet link was rejected by the security gate.",
                        "upstream_unavailable": "qBittorrent is not accepting secure intake right now.",
                    }
                    raise TorrentIntakeError(
                        messages.get(str(code), "The torrent intake gateway rejected the request.")
                    )
        except TorrentIntakeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TorrentIntakeError("The torrent intake gateway is unavailable.") from exc

        if (
            not isinstance(payload, dict)
            or set(payload) != {"ok", "duplicate", "info_hash", "category"}
            or payload.get("ok") is not True
        ):
            raise TorrentIntakeError("Torrent intake returned an invalid receipt.")
        info_hash = str(payload.get("info_hash", "")).casefold()
        if info_hash != reference.info_hash:
            raise TorrentIntakeError("Torrent intake returned a mismatched receipt.")
        if payload.get("category") != normalized_category:
            raise TorrentIntakeError("Torrent intake returned a mismatched category receipt.")
        duplicate = payload.get("duplicate")
        if type(duplicate) is not bool:
            raise TorrentIntakeError("Torrent intake returned an invalid duplicate receipt.")
        if duplicate is not (response_status == 200):
            raise TorrentIntakeError("Torrent intake returned a mismatched status receipt.")
        return TorrentIntakeResult(
            info_hash=info_hash,
            category=normalized_category,
            duplicate=duplicate,
        )
