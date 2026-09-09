"""Durable, provider-agnostic capture of raw life-admin thoughts."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class LifeCaptureError(ValueError):
    """Raised when a capture cannot safely be stored."""


@dataclass(frozen=True)
class LifeCapture:
    capture_id: str
    created_at: str
    path: Path
    title: str


class LifeCaptureService:
    """Write one immutable Markdown inbox item using an atomic rename."""

    MAX_TEXT_LENGTH = 8_000

    def __init__(self, inbox_path: str | os.PathLike[str]):
        normalized = str(inbox_path or "").strip()
        if not normalized:
            raise LifeCaptureError("Life capture inbox path is blank.")
        self.inbox_path = Path(normalized)

    def capture(
        self,
        text: str,
        *,
        discord_user_id: int,
        discord_channel_id: int | None,
        discord_guild_id: int | None,
        now: datetime | None = None,
        capture_id: str | None = None,
    ) -> LifeCapture:
        raw_text = str(text or "").strip()
        if not raw_text:
            raise LifeCaptureError("Capture text is blank.")
        if len(raw_text) > self.MAX_TEXT_LENGTH:
            raise LifeCaptureError(
                f"Capture text exceeds {self.MAX_TEXT_LENGTH} characters."
            )

        created = now or datetime.now(timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created = created.astimezone(timezone.utc).replace(microsecond=0)
        created_at = created.isoformat().replace("+00:00", "Z")
        stable_id = str(uuid.UUID(capture_id)) if capture_id else str(uuid.uuid4())
        title = self._title_for(raw_text)
        filename = f"{created:%Y-%m-%dT%H%M%SZ}-{stable_id}.md"

        self.inbox_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        final_path = self.inbox_path / filename
        temporary_path = self.inbox_path / f".{filename}.{uuid.uuid4().hex}.tmp"
        payload = self._render(
            capture_id=stable_id,
            created_at=created_at,
            title=title,
            raw_text=raw_text,
            discord_user_id=discord_user_id,
            discord_channel_id=discord_channel_id,
            discord_guild_id=discord_guild_id,
        )

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(temporary_path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Linking within the inbox gives us an atomic, no-overwrite publish:
            # an existing capture can never be silently replaced on an ID collision.
            os.link(temporary_path, final_path)
            self._fsync_directory(self.inbox_path)
            temporary_path.unlink()
            self._fsync_directory(self.inbox_path)
        except FileExistsError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise LifeCaptureError(
                "Capture ID collision; the existing capture was preserved."
            ) from exc
        except Exception:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        return LifeCapture(
            capture_id=stable_id,
            created_at=created_at,
            path=final_path,
            title=title,
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist directory-entry changes where the platform supports it."""

        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _title_for(text: str) -> str:
        collapsed = re.sub(r"\s+", " ", text).strip()
        if len(collapsed) <= 80:
            return collapsed
        return f"{collapsed[:77].rstrip()}..."

    @staticmethod
    def _yaml_string(value: object) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    @classmethod
    def _render(
        cls,
        *,
        capture_id: str,
        created_at: str,
        title: str,
        raw_text: str,
        discord_user_id: int,
        discord_channel_id: int | None,
        discord_guild_id: int | None,
    ) -> str:
        optional_ids = {
            "channel_id": discord_channel_id,
            "guild_id": discord_guild_id,
        }
        source_lines = [
            f"  user_id: {cls._yaml_string(discord_user_id)}",
            *(
                f"  {key}: {cls._yaml_string(value)}"
                for key, value in optional_ids.items()
                if value is not None
            ),
        ]
        return "\n".join(
            [
                "---",
                f"id: {cls._yaml_string(capture_id)}",
                'type: "capture"',
                'status: "inbox"',
                f"title: {cls._yaml_string(title)}",
                f"created: {cls._yaml_string(created_at)}",
                'source: "discord"',
                "discord:",
                *source_lines,
                "---",
                "",
                raw_text,
                "",
            ]
        )
