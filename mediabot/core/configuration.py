"""Small runtime configuration checks shared by portable installations."""

from __future__ import annotations

import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def configured_timezone(override_name: str, *, environment=None) -> str:
    values = os.environ if environment is None else environment
    value = (values.get(override_name, "").strip() or values.get("TZ", "").strip() or "UTC")
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"{override_name} or TZ must name an installed IANA timezone.") from exc
    return value


def validate_core_configuration(*, discord_token, seerr_url, seerr_api_key, guild_ids):
    missing = [
        name for name, value in (
            ("DISCORD_TOKEN", discord_token),
            ("SEERR_URL", seerr_url),
            ("SEERR_API_KEY", seerr_api_key),
            ("ALLOWED_GUILD_IDS", guild_ids),
        ) if not value or (isinstance(value, str) and not value.strip())
    ]
    if missing:
        raise RuntimeError("Missing required configuration: " + ", ".join(missing) + ". Run scripts/setup.py.")
    try:
        parsed = urlsplit(seerr_url)
        invalid = (parsed.scheme not in {"http", "https"} or not parsed.hostname
                   or parsed.username is not None or parsed.password is not None
                   or parsed.query or parsed.fragment or any(c.isspace() for c in seerr_url))
        parsed.port  # Reject malformed ports without printing the supplied URL.
        if invalid:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("SEERR_URL must be an HTTP(S) URL without credentials, query, or fragment.") from exc
