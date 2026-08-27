"""Domain logic for library playback/problem reports.

Discord rendering and SQLite persistence deliberately stay outside this module.
That keeps report parsing and Jellyfin target resolution independently testable
while the command layer follows the bot's existing view and database patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReportUsageError(ValueError):
    """Raised when a report target or category cannot be used safely."""


class ReportCategory(str, Enum):
    WONT_PLAY = "wont_play"
    WRONG_AUDIO = "wrong_audio"
    BAD_SUBTITLES = "bad_subtitles"
    BAD_QUALITY = "bad_quality"
    WRONG_EPISODE = "wrong_episode"
    OTHER = "other"


REPORT_CATEGORY_LABELS = {
    ReportCategory.WONT_PLAY: "Won't Play",
    ReportCategory.WRONG_AUDIO: "Wrong Audio",
    ReportCategory.BAD_SUBTITLES: "Bad Subtitles",
    ReportCategory.BAD_QUALITY: "Bad Quality",
    ReportCategory.WRONG_EPISODE: "Wrong Episode",
    ReportCategory.OTHER: "Other",
}


_CATEGORY_ALIASES = {
    "wont play": ReportCategory.WONT_PLAY,
    "won t play": ReportCategory.WONT_PLAY,
    "will not play": ReportCategory.WONT_PLAY,
    "wrong audio": ReportCategory.WRONG_AUDIO,
    "bad audio": ReportCategory.WRONG_AUDIO,
    "bad subtitles": ReportCategory.BAD_SUBTITLES,
    "wrong subtitles": ReportCategory.BAD_SUBTITLES,
    "bad subs": ReportCategory.BAD_SUBTITLES,
    "bad quality": ReportCategory.BAD_QUALITY,
    "wrong episode": ReportCategory.WRONG_EPISODE,
    "other": ReportCategory.OTHER,
}


# Only a trailing token is consumed. A title containing an unrelated number or
# the letter S is therefore not silently changed into an episode report.
_EPISODE_SUFFIXES = (
    re.compile(
        r"(?ix)^(?P<title>.+?)\s+S0*(?P<season>\d{1,3})\s*E0*(?P<episode>\d{1,4})$"
    ),
    re.compile(
        r"(?ix)^(?P<title>.+?)\s+(?P<season>\d{1,3})\s*x\s*0*(?P<episode>\d{1,4})$"
    ),
    re.compile(
        r"(?ix)^(?P<title>.+?)\s+season\s+0*(?P<season>\d{1,3})\s+episode\s+0*(?P<episode>\d{1,4})$"
    ),
)


@dataclass(frozen=True)
class ReportQuery:
    search_query: str
    season_number: int | None = None
    episode_number: int | None = None

    @property
    def is_episode(self) -> bool:
        return self.season_number is not None

    @property
    def episode_label(self) -> str | None:
        if not self.is_episode:
            return None
        return f"S{self.season_number:02d}E{self.episode_number:02d}"


@dataclass(frozen=True)
class ResolvedReportTarget:
    """An exact Jellyfin object suitable for durable report storage."""

    jellyfin_item_id: str
    media_type: str
    title: str
    year: str
    jellyfin_series_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_title: str | None = None

    @property
    def target_key(self) -> str:
        # Jellyfin item IDs are the authoritative identity. Episode targets use
        # the episode ID, not just title/season/episode text.
        return f"{self.media_type}:{self.jellyfin_item_id}"

    @property
    def display_name(self) -> str:
        if self.media_type != "episode":
            return f"{self.title} ({self.year})" if self.year else self.title

        label = f"S{self.season_number:02d}E{self.episode_number:02d}"
        if self.episode_title:
            label += f" - {self.episode_title}"
        return f"{self.title} - {label}"


def _normalized_words(value: str) -> str:
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split()
    )


def parse_report_query(value: str) -> ReportQuery:
    """Parse a library title and an optional trailing episode selector."""

    query = " ".join(str(value or "").split())
    if not query:
        raise ReportUsageError(
            "Tell me what has the problem. Example: $report Breaking Bad S02E07"
        )

    for pattern in _EPISODE_SUFFIXES:
        match = pattern.fullmatch(query)
        if not match:
            continue

        title = " ".join(match.group("title").split()).strip()
        season = int(match.group("season"))
        episode = int(match.group("episode"))

        if not title or season < 1 or episode < 1:
            raise ReportUsageError(
                "Use a real season and episode, such as S02E07."
            )

        return ReportQuery(title, season, episode)

    return ReportQuery(query)


def normalize_report_category(
    value: str | ReportCategory,
    *,
    details: str = "",
) -> ReportCategory:
    """Normalize UI/command input and enforce useful `Other` reports."""

    if isinstance(value, ReportCategory):
        category = value
    else:
        normalized = _normalized_words(value)
        try:
            category = _CATEGORY_ALIASES[normalized]
        except KeyError as exc:
            labels = ", ".join(REPORT_CATEGORY_LABELS.values())
            raise ReportUsageError(f"Choose one of: {labels}.") from exc

    if category is ReportCategory.OTHER and not str(details).strip():
        raise ReportUsageError("Tell the administrator what is wrong.")

    return category


class ReportService:
    """Search Jellyfin and resolve a selected movie/show/episode exactly."""

    def __init__(self, jellyfin_provider):
        self.jellyfin = jellyfin_provider

    async def search(self, query: ReportQuery, *, limit: int = 25) -> list[dict]:
        items = await self.jellyfin.search(
            query.search_query,
            limit=max(1, min(int(limit), 50)),
        )
        allowed_types = {"Series"} if query.is_episode else {"Movie", "Series"}
        return [item for item in items if item.get("Type") in allowed_types]

    async def resolve(
        self,
        item: dict[str, Any],
        query: ReportQuery,
    ) -> ResolvedReportTarget:
        item_id = str(item.get("Id") or "").strip()
        title = str(item.get("Name") or "").strip()
        item_type = str(item.get("Type") or "")
        year = str(item.get("ProductionYear") or "").strip()

        if not item_id or not title or item_type not in {"Movie", "Series"}:
            raise ReportUsageError("That Jellyfin result cannot be reported safely.")

        if not query.is_episode:
            return ResolvedReportTarget(
                jellyfin_item_id=item_id,
                media_type=item_type.casefold(),
                title=title,
                year=year,
            )

        if item_type != "Series":
            raise ReportUsageError("An episode report must point to a TV series.")

        episode = await self.jellyfin.series_episode(
            item_id,
            season_number=query.season_number,
            episode_number=query.episode_number,
        )
        if not episode:
            raise ReportUsageError(
                f"{query.episode_label} is not indexed in Jellyfin for {title}."
            )

        episode_id = str(episode.get("Id") or "").strip()
        if not episode_id:
            raise ReportUsageError("Jellyfin found that episode without a usable ID.")

        return ResolvedReportTarget(
            jellyfin_item_id=episode_id,
            jellyfin_series_id=item_id,
            media_type="episode",
            title=title,
            year=year,
            season_number=query.season_number,
            episode_number=query.episode_number,
            episode_title=str(episode.get("Name") or "").strip() or None,
        )
