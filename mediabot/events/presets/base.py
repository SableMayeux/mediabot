"""Typed snapshots produced by event presets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from mediabot.core.event_store import utc_text


@dataclass(frozen=True)
class EventPresetSnapshot:
    """Immutable inputs copied into an event when a preset is selected.

    Preset code may change after deployment.  ``stored_rules`` includes the
    generated UTC slot list so an in-progress event never changes underneath
    its voters merely because MediaBot was upgraded.
    """

    key: str
    version: str
    name: str
    timezone_name: str
    vote_limit: int = 1
    rules: Mapping[str, Any] = field(default_factory=dict)
    slot_times_utc: tuple[datetime, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise ValueError("A preset needs a stable key.")
        if not str(self.version).strip():
            raise ValueError("A preset needs a version.")
        if not str(self.name).strip():
            raise ValueError("A preset needs a display name.")
        if int(self.vote_limit) < 1:
            raise ValueError("A preset must allow at least one vote.")
        for value in self.slot_times_utc:
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError("Preset slots must be timezone-aware datetimes.")

    def stored_rules(self) -> dict[str, Any]:
        payload = dict(self.rules)
        payload["slot_times_utc"] = [
            utc_text(value)
            for value in self.slot_times_utc
        ]
        return payload
