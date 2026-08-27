"""Provider-independent domain service for generic media events.

Discord views resolve exact media candidates, then call this service.  The
service owns validation, lifecycle rules, voting, scheduling, and local-night
lookups; :mod:`mediabot.core.event_store` owns SQLite transactions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mediabot.core.event_store import (
    EventConflictError,
    EventNotFoundError,
    EventStateError,
    EventStore,
    NominationNotFoundError,
    OpenEventExistsError,
    StaleEventRevisionError,
    VoteLimitExceededError,
    parse_utc_text,
    utc_text,
)
from mediabot.events.presets.base import EventPresetSnapshot


DEFAULT_EVENT_TIMEZONE = "America/Denver"
MAX_EVENT_SCHEDULE_SLOTS = 31
SCHEDULE_INPUT_EXAMPLE = "2026-10-03 19:00, 2026-10-10 19:00"
_LOCAL_SCHEDULE_VALUE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2})$"
)


class EventUsageError(ValueError):
    """Raised for actionable organizer/voter input errors."""


class EventStatus(str, Enum):
    OPEN = "open"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


def _optional_time(value: Any) -> datetime | None:
    return parse_utc_text(str(value)) if value else None


@dataclass(frozen=True)
class EventRecord:
    event_id: int
    discord_guild_id: int
    discord_channel_id: int
    dashboard_message_id: int | None
    name: str
    created_by_discord_id: int
    status: EventStatus
    timezone_name: str
    vote_limit: int
    preset_key: str | None
    preset_version: str | None
    rules: Mapping[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime
    scheduled_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "EventRecord":
        return cls(
            event_id=int(row["event_id"]),
            discord_guild_id=int(row["discord_guild_id"]),
            discord_channel_id=int(row["discord_channel_id"]),
            dashboard_message_id=(
                int(row["dashboard_message_id"])
                if row.get("dashboard_message_id") is not None
                else None
            ),
            name=str(row["name"]),
            created_by_discord_id=int(row["created_by_discord_id"]),
            status=EventStatus(str(row["status"])),
            timezone_name=str(row["timezone_name"]),
            vote_limit=int(row["vote_limit"]),
            preset_key=str(row["preset_key"]) if row.get("preset_key") else None,
            preset_version=(
                str(row["preset_version"]) if row.get("preset_version") else None
            ),
            rules=json.loads(str(row.get("rules_json") or "{}")),
            revision=int(row["revision"]),
            created_at=parse_utc_text(str(row["created_at"])),
            updated_at=parse_utc_text(str(row["updated_at"])),
            scheduled_at=_optional_time(row.get("scheduled_at")),
            completed_at=_optional_time(row.get("completed_at")),
            cancelled_at=_optional_time(row.get("cancelled_at")),
        )


@dataclass(frozen=True)
class NominationRecord:
    nomination_id: int
    event_id: int
    media_type: str
    tmdb_id: int
    jellyfin_item_id: str | None
    title: str
    year: str
    poster_path: str | None
    nominated_by_discord_id: int
    status: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "NominationRecord":
        return cls(
            nomination_id=int(row["nomination_id"]),
            event_id=int(row["event_id"]),
            media_type=str(row["media_type"]),
            tmdb_id=int(row["tmdb_id"]),
            jellyfin_item_id=(
                str(row["jellyfin_item_id"])
                if row.get("jellyfin_item_id")
                else None
            ),
            title=str(row["title"]),
            year=str(row.get("year") or ""),
            poster_path=str(row["poster_path"]) if row.get("poster_path") else None,
            nominated_by_discord_id=int(row["nominated_by_discord_id"]),
            status=str(row["status"]),
            created_at=parse_utc_text(str(row["created_at"])),
        )


@dataclass(frozen=True)
class NominationResult:
    nomination: NominationRecord
    created: bool


@dataclass(frozen=True)
class RankedNomination:
    nomination: NominationRecord
    vote_count: int


@dataclass(frozen=True)
class VoteResult:
    nomination_id: int
    selected: bool
    selected_nomination_ids: tuple[int, ...]
    event_revision: int


@dataclass(frozen=True)
class ScheduleAssignment:
    starts_at: datetime
    nomination_id: int | None


@dataclass(frozen=True)
class SchedulePlan:
    event_id: int
    event_revision: int
    assignments: tuple[ScheduleAssignment, ...]
    tied_vote_counts: tuple[int, ...]


@dataclass(frozen=True)
class EventSlotRecord:
    slot_id: int
    event_id: int
    starts_at: datetime
    slot_status: str
    discord_guild_id: int
    discord_channel_id: int
    dashboard_message_id: int | None
    event_name: str
    event_status: EventStatus
    timezone_name: str
    preset_key: str | None
    nomination_id: int | None
    media_type: str | None
    tmdb_id: int | None
    jellyfin_item_id: str | None
    title: str | None
    year: str
    poster_path: str | None
    vote_count: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "EventSlotRecord":
        return cls(
            slot_id=int(row["slot_id"]),
            event_id=int(row["event_id"]),
            starts_at=parse_utc_text(str(row["starts_at_utc"])),
            slot_status=str(row["slot_status"]),
            discord_guild_id=int(row["discord_guild_id"]),
            discord_channel_id=int(row["discord_channel_id"]),
            dashboard_message_id=(
                int(row["dashboard_message_id"])
                if row.get("dashboard_message_id") is not None
                else None
            ),
            event_name=str(row["event_name"]),
            event_status=EventStatus(str(row["event_status"])),
            timezone_name=str(row["timezone_name"]),
            preset_key=str(row["preset_key"]) if row.get("preset_key") else None,
            nomination_id=(
                int(row["nomination_id"])
                if row.get("nomination_id") is not None
                else None
            ),
            media_type=str(row["media_type"]) if row.get("media_type") else None,
            tmdb_id=int(row["tmdb_id"]) if row.get("tmdb_id") is not None else None,
            jellyfin_item_id=(
                str(row["jellyfin_item_id"])
                if row.get("jellyfin_item_id")
                else None
            ),
            title=str(row["title"]) if row.get("title") else None,
            year=str(row.get("year") or ""),
            poster_path=str(row["poster_path"]) if row.get("poster_path") else None,
            vote_count=int(row.get("vote_count") or 0),
        )


def validate_timezone(timezone_name: str) -> str:
    normalized = str(timezone_name or "").strip()
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise EventUsageError(f"Unknown event timezone: {normalized}") from exc
    return normalized


def normalize_media_type(media_type: str) -> str:
    normalized = " ".join(str(media_type).casefold().split())
    aliases = {
        "movie": "movie",
        "film": "movie",
        "tv": "tv",
        "show": "tv",
        "series": "tv",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise EventUsageError("An event nomination must be a movie or TV show.") from exc


def local_day_utc_bounds(
    reference: datetime | None = None,
    *,
    timezone_name: str = DEFAULT_EVENT_TIMEZONE,
) -> tuple[datetime, datetime]:
    """Return the UTC half-open range for one local calendar day."""

    normalized_zone = validate_timezone(timezone_name)
    local_zone = ZoneInfo(normalized_zone)
    if reference is None:
        local_reference = datetime.now(timezone.utc).astimezone(local_zone)
    elif reference.tzinfo is None:
        local_reference = reference.replace(tzinfo=local_zone)
    else:
        local_reference = reference.astimezone(local_zone)

    local_date = local_reference.date()
    start_local = datetime.combine(local_date, time.min, tzinfo=local_zone)
    end_local = datetime.combine(
        local_date + timedelta(days=1),
        time.min,
        tzinfo=local_zone,
    )
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
    )


def parse_schedule_input(
    value: str,
    *,
    timezone_name: str = DEFAULT_EVENT_TIMEZONE,
) -> tuple[datetime, ...]:
    """Parse comma-separated local event slots without guessing at DST.

    Accepted syntax is ``YYYY-MM-DD HH:MM`` for each value.  Returned
    datetimes retain the event's IANA timezone and input order.  Ambiguous
    fall-back times and nonexistent spring-forward times are rejected rather
    than silently choosing one interpretation.
    """

    normalized_zone = validate_timezone(timezone_name)
    local_zone = ZoneInfo(normalized_zone)
    raw = str(value or "").strip()
    usage = (
        "Use local `YYYY-MM-DD HH:MM` values separated by commas. "
        f"Example: `{SCHEDULE_INPUT_EXAMPLE}`."
    )
    if not raw:
        raise EventUsageError(f"Tell me when to schedule it. {usage}")

    pieces = tuple(piece.strip() for piece in raw.split(","))
    if any(not piece for piece in pieces):
        raise EventUsageError(f"One of the scheduled dates is empty. {usage}")
    if len(pieces) > MAX_EVENT_SCHEDULE_SLOTS:
        raise EventUsageError(
            f"An event can have at most {MAX_EVENT_SCHEDULE_SLOTS} scheduled slots."
        )

    parsed: list[datetime] = []
    seen_utc: set[str] = set()
    for piece in pieces:
        match = _LOCAL_SCHEDULE_VALUE.fullmatch(piece)
        if match is None:
            raise EventUsageError(f"`{piece}` is not a valid local date and time. {usage}")

        try:
            naive = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour")),
                int(match.group("minute")),
            )
        except ValueError as exc:
            raise EventUsageError(
                f"`{piece}` is not a real calendar date and time. {usage}"
            ) from exc

        valid_candidates: list[datetime] = []
        candidate_offsets = set()
        for fold in (0, 1):
            candidate = naive.replace(tzinfo=local_zone, fold=fold)
            round_trip = (
                candidate.astimezone(timezone.utc)
                .astimezone(local_zone)
                .replace(tzinfo=None)
            )
            if round_trip == naive:
                valid_candidates.append(candidate)
                candidate_offsets.add(candidate.utcoffset())

        if not valid_candidates:
            raise EventUsageError(
                f"`{piece}` does not exist in {normalized_zone} because the "
                "clocks jump forward. Choose a time before or after the clock change."
            )
        if len(candidate_offsets) > 1:
            raise EventUsageError(
                f"`{piece}` happens twice in {normalized_zone} because the "
                "clocks fall back. Choose an unambiguous time before or after "
                "the clock change."
            )

        aware = valid_candidates[0]
        identity = utc_text(aware)
        if identity in seen_utc:
            raise EventUsageError(f"`{piece}` appears more than once in the schedule.")
        seen_utc.add(identity)
        parsed.append(aware)

    return tuple(parsed)


class EventService:
    """Durable generic event lifecycle suitable for Discord command adapters."""

    def __init__(self, store: EventStore):
        self.store = store

    def initialize(self) -> None:
        self.store.init_schema()

    def create_event(
        self,
        *,
        discord_guild_id: int,
        discord_channel_id: int,
        created_by_discord_id: int,
        name: str | None = None,
        timezone_name: str = DEFAULT_EVENT_TIMEZONE,
        vote_limit: int = 1,
        preset: EventPresetSnapshot | None = None,
    ) -> EventRecord:
        if preset is not None:
            event_name = str(name or preset.name).strip()
            normalized_zone = validate_timezone(preset.timezone_name)
            normalized_limit = int(preset.vote_limit)
            preset_key = preset.key
            preset_version = preset.version
            rules = preset.stored_rules()
        else:
            event_name = str(name or "").strip()
            normalized_zone = validate_timezone(timezone_name)
            normalized_limit = int(vote_limit)
            preset_key = None
            preset_version = None
            rules = {}

        if not event_name:
            raise EventUsageError("Give the event a short name.")
        if len(event_name) > 100:
            raise EventUsageError("Event names must be 100 characters or fewer.")
        if normalized_limit < 1 or normalized_limit > 25:
            raise EventUsageError("Vote limit must be between 1 and 25.")

        row = self.store.create_event(
            discord_guild_id=int(discord_guild_id),
            discord_channel_id=int(discord_channel_id),
            name=event_name,
            created_by_discord_id=int(created_by_discord_id),
            timezone_name=normalized_zone,
            vote_limit=normalized_limit,
            preset_key=preset_key,
            preset_version=preset_version,
            rules=rules,
        )
        return EventRecord.from_row(row)

    def event(
        self,
        event_id: int,
        *,
        discord_guild_id: int | None = None,
    ) -> EventRecord:
        row = self.store.event_by_id(
            int(event_id),
            discord_guild_id=(
                int(discord_guild_id) if discord_guild_id is not None else None
            ),
        )
        if row is None:
            raise EventNotFoundError(f"Event #{int(event_id)} does not exist here.")
        return EventRecord.from_row(row)

    def current_event(self, discord_guild_id: int) -> EventRecord | None:
        row = self.store.open_event_for_guild(int(discord_guild_id))
        return EventRecord.from_row(row) if row else None

    def list_events(
        self,
        *,
        discord_guild_id: int,
        statuses: Sequence[str | EventStatus] | None = None,
        limit: int = 100,
    ) -> tuple[EventRecord, ...]:
        normalized = (
            tuple(
                value.value if isinstance(value, EventStatus) else str(value)
                for value in statuses
            )
            if statuses is not None
            else None
        )
        return tuple(
            EventRecord.from_row(row)
            for row in self.store.list_events(
                discord_guild_id=int(discord_guild_id),
                statuses=normalized,
                limit=int(limit),
            )
        )

    def set_dashboard_message(
        self,
        *,
        event_id: int,
        discord_channel_id: int,
        dashboard_message_id: int,
    ) -> EventRecord:
        return EventRecord.from_row(
            self.store.set_dashboard_message(
                event_id=int(event_id),
                discord_channel_id=int(discord_channel_id),
                dashboard_message_id=int(dashboard_message_id),
            )
        )

    def nominate(
        self,
        *,
        event_id: int,
        media_type: str,
        tmdb_id: int,
        title: str,
        year: str | None,
        nominated_by_discord_id: int,
        genres: Iterable[str] = (),
        jellyfin_item_id: str | None = None,
        poster_path: str | None = None,
    ) -> NominationResult:
        event = self.event(int(event_id))
        normalized_type = normalize_media_type(media_type)
        normalized_title = str(title or "").strip()
        if not normalized_title:
            raise EventUsageError("That media result does not have a usable title.")
        if int(tmdb_id) < 1:
            raise EventUsageError("That media result does not have a usable TMDB ID.")

        allowed_types = {
            normalize_media_type(value)
            for value in event.rules.get("media_types", ("movie", "tv"))
        }
        if normalized_type not in allowed_types:
            label = " or ".join(sorted(allowed_types))
            raise EventUsageError(f"{event.name} accepts {label} nominations only.")

        required_genres = {
            " ".join(str(value).casefold().split())
            for value in event.rules.get("required_genres", ())
        }
        actual_genres = {
            " ".join(str(value).casefold().split())
            for value in genres
            if str(value).strip()
        }
        if required_genres and not required_genres.issubset(actual_genres):
            labels = ", ".join(sorted(value.title() for value in required_genres))
            raise EventUsageError(f"{event.name} requires: {labels}.")

        row, created = self.store.add_nomination(
            event_id=event.event_id,
            media_type=normalized_type,
            tmdb_id=int(tmdb_id),
            title=normalized_title,
            year=str(year or ""),
            nominated_by_discord_id=int(nominated_by_discord_id),
            jellyfin_item_id=jellyfin_item_id,
            poster_path=poster_path,
        )
        return NominationResult(NominationRecord.from_row(row), created)

    def nominations(self, event_id: int) -> tuple[NominationRecord, ...]:
        return tuple(
            NominationRecord.from_row(row)
            for row in self.store.list_nominations(int(event_id))
        )

    def toggle_vote(
        self,
        *,
        event_id: int,
        nomination_id: int,
        discord_user_id: int,
    ) -> VoteResult:
        selected, choices, revision = self.store.toggle_vote(
            event_id=int(event_id),
            nomination_id=int(nomination_id),
            discord_user_id=int(discord_user_id),
        )
        return VoteResult(
            nomination_id=int(nomination_id),
            selected=selected,
            selected_nomination_ids=choices,
            event_revision=revision,
        )

    def user_vote_ids(
        self,
        *,
        event_id: int,
        discord_user_id: int,
    ) -> tuple[int, ...]:
        return self.store.user_vote_ids(
            event_id=int(event_id),
            discord_user_id=int(discord_user_id),
        )

    def rankings(self, event_id: int) -> tuple[RankedNomination, ...]:
        return tuple(
            RankedNomination(
                nomination=NominationRecord.from_row(row),
                vote_count=int(row["vote_count"]),
            )
            for row in self.store.ranking_rows(int(event_id))
        )

    def build_ranked_schedule(
        self,
        event_id: int,
        *,
        starts_at: Sequence[datetime] | None = None,
    ) -> SchedulePlan:
        """Build a deterministic preview; confirmation must pass its revision."""

        event = self.event(int(event_id))
        if event.status is not EventStatus.OPEN:
            raise EventStateError("Only an open event can be scheduled.")

        if starts_at is None:
            stored_times = event.rules.get("slot_times_utc", ())
            times = tuple(parse_utc_text(str(value)) for value in stored_times)
        else:
            times = tuple(_aware_datetime(value) for value in starts_at)

        if not times:
            raise EventUsageError("Choose at least one date and time to schedule.")
        if len(times) > MAX_EVENT_SCHEDULE_SLOTS:
            raise EventUsageError(
                f"An event can have at most {MAX_EVENT_SCHEDULE_SLOTS} scheduled slots."
            )

        ranking = self.rankings(event.event_id)
        if not ranking:
            raise EventUsageError("Nominate at least one title before scheduling.")

        assignments = tuple(
            ScheduleAssignment(
                starts_at=value,
                nomination_id=(
                    ranking[index].nomination.nomination_id
                    if index < len(ranking)
                    else None
                ),
            )
            for index, value in enumerate(times)
        )
        vote_counts: dict[int, int] = {}
        for result in ranking:
            vote_counts[result.vote_count] = vote_counts.get(result.vote_count, 0) + 1

        return SchedulePlan(
            event_id=event.event_id,
            event_revision=event.revision,
            assignments=assignments,
            tied_vote_counts=tuple(
                sorted(
                    (count for count, occurrences in vote_counts.items() if occurrences > 1),
                    reverse=True,
                )
            ),
        )

    def schedule_event(
        self,
        *,
        event_id: int,
        assignments: Sequence[ScheduleAssignment],
        expected_revision: int | None = None,
    ) -> tuple[EventRecord, tuple[EventSlotRecord, ...]]:
        event = self.event(int(event_id))
        normalized = tuple(
            ScheduleAssignment(
                starts_at=_aware_datetime(value.starts_at),
                nomination_id=(
                    int(value.nomination_id)
                    if value.nomination_id is not None
                    else None
                ),
            )
            for value in assignments
        )
        if not normalized:
            raise EventUsageError("Schedule at least one event slot.")
        if len(normalized) > MAX_EVENT_SCHEDULE_SLOTS:
            raise EventUsageError(
                f"An event can have at most {MAX_EVENT_SCHEDULE_SLOTS} scheduled slots."
            )
        if not any(value.nomination_id is not None for value in normalized):
            raise EventUsageError("Put at least one nominated title on the schedule.")

        starts = tuple(utc_text(value.starts_at) for value in normalized)
        if len(set(starts)) != len(starts):
            raise EventUsageError("An event cannot have two slots at the same time.")

        expected_slots = tuple(
            str(value)
            for value in event.rules.get("slot_times_utc", ())
        )
        if expected_slots and tuple(starts) != expected_slots:
            raise EventUsageError(
                "This preset's dates changed. Rebuild the schedule from the saved preset."
            )

        selected_ids = tuple(
            value.nomination_id
            for value in normalized
            if value.nomination_id is not None
        )
        if not bool(event.rules.get("allow_repeats", False)) and len(
            set(selected_ids)
        ) != len(selected_ids):
            raise EventUsageError("A title can only occupy one slot in this event.")

        event_row, _ = self.store.freeze_schedule(
            event_id=event.event_id,
            assignments=tuple(zip(starts, (value.nomination_id for value in normalized))),
            expected_revision=expected_revision,
        )
        return (
            EventRecord.from_row(event_row),
            tuple(
                EventSlotRecord.from_row(row)
                for row in self.store.slots_for_event(event.event_id)
            ),
        )

    def schedule_ranked(
        self,
        plan: SchedulePlan,
    ) -> tuple[EventRecord, tuple[EventSlotRecord, ...]]:
        return self.schedule_event(
            event_id=plan.event_id,
            assignments=plan.assignments,
            expected_revision=plan.event_revision,
        )

    def slots(self, event_id: int) -> tuple[EventSlotRecord, ...]:
        return tuple(
            EventSlotRecord.from_row(row)
            for row in self.store.slots_for_event(int(event_id))
        )

    def tonight(
        self,
        *,
        discord_guild_id: int,
        reference: datetime | None = None,
        timezone_name: str = DEFAULT_EVENT_TIMEZONE,
    ) -> tuple[EventSlotRecord, ...]:
        start, end = local_day_utc_bounds(
            reference,
            timezone_name=timezone_name,
        )
        return tuple(
            EventSlotRecord.from_row(row)
            for row in self.store.slots_between(
                discord_guild_id=int(discord_guild_id),
                start_utc=utc_text(start),
                end_utc=utc_text(end),
            )
        )

    def complete(self, event_id: int) -> EventRecord:
        return EventRecord.from_row(self.store.complete_event(int(event_id)))

    def cancel(self, event_id: int) -> EventRecord:
        return EventRecord.from_row(self.store.cancel_event(int(event_id)))


def _aware_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EventUsageError("Event dates and times must include a timezone.")
    return value


__all__ = [
    "DEFAULT_EVENT_TIMEZONE",
    "MAX_EVENT_SCHEDULE_SLOTS",
    "EventConflictError",
    "EventNotFoundError",
    "EventRecord",
    "EventService",
    "EventSlotRecord",
    "EventStateError",
    "EventStatus",
    "EventUsageError",
    "NominationNotFoundError",
    "NominationRecord",
    "NominationResult",
    "OpenEventExistsError",
    "RankedNomination",
    "ScheduleAssignment",
    "SchedulePlan",
    "StaleEventRevisionError",
    "VoteLimitExceededError",
    "VoteResult",
    "local_day_utc_bounds",
    "normalize_media_type",
    "parse_schedule_input",
    "validate_timezone",
]
