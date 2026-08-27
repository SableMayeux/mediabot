"""Spooktober expressed as a generic event preset."""

from __future__ import annotations

import calendar
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mediabot.events.presets.base import EventPresetSnapshot


SPOOKTOBER_PRESET_KEY = "spooktober"
SPOOKTOBER_PRESET_VERSION = "1"
DEFAULT_EVENT_TIMEZONE = "America/Denver"


def build_spooktober_preset(
    *,
    year: int,
    timezone_name: str = DEFAULT_EVENT_TIMEZONE,
    start_hour: int = 19,
    start_minute: int = 0,
    nights: Iterable[int] | None = None,
    vote_limit: int = 1,
) -> EventPresetSnapshot:
    """Create a versioned October schedule without special event tables.

    By default every October night is represented.  A deployment that only
    hosts weekend screenings can pass the desired day numbers and the exact
    resulting schedule will still be persisted in the event rule snapshot.
    """

    numeric_year = int(year)
    if not 1970 <= numeric_year <= 9999:
        raise ValueError("Spooktober year must be between 1970 and 9999.")
    if not 0 <= int(start_hour) <= 23:
        raise ValueError("Spooktober start hour must be between 0 and 23.")
    if not 0 <= int(start_minute) <= 59:
        raise ValueError("Spooktober start minute must be between 0 and 59.")
    if int(vote_limit) < 1:
        raise ValueError("Spooktober must allow at least one vote.")

    try:
        local_zone = ZoneInfo(str(timezone_name))
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown event timezone: {timezone_name}") from exc

    last_day = calendar.monthrange(numeric_year, 10)[1]
    selected_nights = tuple(
        sorted(
            {
                int(day)
                for day in (nights if nights is not None else range(1, last_day + 1))
            }
        )
    )
    if not selected_nights:
        raise ValueError("Spooktober needs at least one scheduled night.")
    if selected_nights[0] < 1 or selected_nights[-1] > last_day:
        raise ValueError("Spooktober nights must be valid October dates.")

    slots = tuple(
        datetime(
            numeric_year,
            10,
            day,
            int(start_hour),
            int(start_minute),
            tzinfo=local_zone,
        )
        for day in selected_nights
    )

    return EventPresetSnapshot(
        key=SPOOKTOBER_PRESET_KEY,
        version=SPOOKTOBER_PRESET_VERSION,
        name=f"Spooktober {numeric_year}",
        timezone_name=str(timezone_name),
        vote_limit=int(vote_limit),
        rules={
            "allow_repeats": False,
            "genre_expression": "Horror",
            "media_types": ["movie"],
            "required_genres": ["Horror"],
            "schedule_strategy": "ranked_unique",
        },
        slot_times_utc=slots,
    )
