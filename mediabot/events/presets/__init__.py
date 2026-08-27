"""Registry for generic MediaBot event presets."""

from mediabot.events.presets.base import EventPresetSnapshot
from mediabot.events.presets.spooktober import (
    SPOOKTOBER_PRESET_KEY,
    SPOOKTOBER_PRESET_VERSION,
    build_spooktober_preset,
)


def build_event_preset(key: str, **options) -> EventPresetSnapshot:
    normalized = " ".join(str(key).casefold().split())
    if normalized == SPOOKTOBER_PRESET_KEY:
        return build_spooktober_preset(**options)
    raise ValueError(f"Unknown event preset: {key}")


__all__ = [
    "EventPresetSnapshot",
    "SPOOKTOBER_PRESET_KEY",
    "SPOOKTOBER_PRESET_VERSION",
    "build_event_preset",
    "build_spooktober_preset",
]
