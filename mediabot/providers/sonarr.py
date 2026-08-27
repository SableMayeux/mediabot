from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .base import Provider


class SonarrError(RuntimeError):
    """Raised when Sonarr cannot inspect or repair an existing series."""


class SonarrProvider(Provider):
    """Narrow Sonarr v3 client for exact TV episode inventory and searches."""

    name = "sonarr"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get(
                "SONARR_URL",
                "http://host.docker.internal:8989",
            )
        ).rstrip("/")
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("SONARR_API_KEY", "")
        ).strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    async def start(self) -> None:
        if self.session and not self.session.closed:
            return
        if not self.enabled:
            return

        self.session = aiohttp.ClientSession(
            headers={
                "X-Api-Key": self.api_key,
                "Accept": "application/json",
            },
            timeout=self.timeout,
        )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.enabled:
            raise SonarrError("Sonarr is not configured.")
        if not self.session or self.session.closed:
            raise SonarrError("Sonarr HTTP session is not initialized.")

        url = f"{self.base_url}/api/v3{path}"
        async with self.session.request(method, url, **kwargs) as response:
            try:
                payload = await response.json()
            except Exception:
                payload = {"message": await response.text()}

            if response.status >= 400:
                message = (
                    payload.get("message")
                    if isinstance(payload, dict)
                    else str(payload)
                )
                raise SonarrError(f"Sonarr HTTP {response.status}: {message}")

            return payload

    async def health(self) -> dict[str, Any]:
        result = await self.request("GET", "/system/status")
        return result if isinstance(result, dict) else {}

    async def series_by_tvdb(self, tvdb_id: int) -> dict[str, Any] | None:
        result = await self.request(
            "GET",
            "/series",
            params={"tvdbId": int(tvdb_id)},
        )
        rows = result if isinstance(result, list) else []
        return rows[0] if rows else None

    async def episodes(self, series_id: int) -> list[dict[str, Any]]:
        result = await self.request(
            "GET",
            "/episode",
            params={"seriesId": int(series_id)},
        )
        return list(result) if isinstance(result, list) else []

    async def queue(self) -> list[dict[str, Any]]:
        result = await self.request(
            "GET",
            "/queue",
            params={
                "page": 1,
                "pageSize": 1000,
                "includeUnknownSeriesItems": "true",
                "includeSeries": "false",
                "includeEpisode": "true",
            },
        )
        if isinstance(result, dict):
            return list(result.get("records") or [])
        return list(result) if isinstance(result, list) else []

    @staticmethod
    def _humanize_queue_value(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        text = text.replace("_", " ").replace("-", " ")
        return " ".join(text.lower().split())

    @classmethod
    def queue_detail(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Preserve Sonarr's raw queue state and add a truthful UI label."""
        status = str(row.get("status") or "queued").strip()
        tracked_status = str(row.get("trackedDownloadStatus") or "").strip()
        tracked_state = str(row.get("trackedDownloadState") or "").strip()
        status_messages = list(row.get("statusMessages") or [])

        readable_status = cls._humanize_queue_value(status) or "queued"
        readable_tracked_status = cls._humanize_queue_value(tracked_status)
        readable_tracked_state = cls._humanize_queue_value(tracked_state)

        if readable_tracked_status not in {"", "ok"}:
            label = readable_tracked_status
            if readable_tracked_state not in {
                "",
                readable_tracked_status,
                "downloading",
            }:
                label += f": {readable_tracked_state}"
        elif readable_tracked_state not in {"", "downloading"}:
            label = readable_tracked_state
        else:
            label = readable_status

        return {
            "label": label,
            "status": status,
            "tracked_download_status": tracked_status,
            "tracked_download_state": tracked_state,
            "status_messages": status_messages,
        }

    @staticmethod
    def episode_has_aired(episode: dict[str, Any]) -> bool:
        value = str(episode.get("airDateUtc") or "").strip()
        if not value:
            return False
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return when <= datetime.now(timezone.utc)

    async def series_inventory(self, tvdb_id: int) -> dict[str, Any] | None:
        series = await self.series_by_tvdb(tvdb_id)
        if not series:
            return None

        rows = await self.episodes(int(series["id"]))
        try:
            queue_rows = await self.queue()
        except SonarrError:
            queue_rows = []
        queued_by_episode_id = {
            int(row["episodeId"]): self.queue_detail(row)
            for row in queue_rows
            if row.get("episodeId") is not None
        }
        seasons: dict[int, dict[str, Any]] = {}

        for episode in rows:
            try:
                season = int(episode.get("seasonNumber") or 0)
                number = int(episode.get("episodeNumber") or 0)
                episode_id = int(episode["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if season <= 0 or number <= 0:
                continue

            state = seasons.setdefault(
                season,
                {
                    "expected": set(),
                    "available": set(),
                    "missing": set(),
                    "future": set(),
                    "monitored": set(),
                    "queued": set(),
                    "queue_status": {},
                    "queue_details": {},
                    "episode_ids": {},
                },
            )
            state["expected"].add(number)
            state["episode_ids"][number] = episode_id

            if episode.get("monitored"):
                state["monitored"].add(number)

            if episode_id in queued_by_episode_id:
                detail = queued_by_episode_id[episode_id]
                state["queued"].add(number)
                state["queue_status"][number] = detail["label"]
                state["queue_details"][number] = detail

            if episode.get("hasFile"):
                state["available"].add(number)
            else:
                state["missing"].add(number)
                if not self.episode_has_aired(episode):
                    state["future"].add(number)

        return {"series": series, "episodes": rows, "seasons": seasons}

    async def request_missing_episodes(
        self,
        *,
        tvdb_id: int,
        missing_by_season: dict[int, set[int] | tuple[int, ...] | list[int]],
    ) -> dict[str, Any]:
        inventory = await self.series_inventory(tvdb_id)
        if not inventory:
            raise SonarrError(f"TVDB series {int(tvdb_id)} is not in Sonarr.")

        wanted_ids: list[int] = []
        searchable_ids: list[int] = []
        accepted_by_season: dict[int, list[int]] = {}
        already_available_by_season: dict[int, list[int]] = {}
        unresolved: dict[int, list[int]] = {}
        rows_by_key = {}

        for episode in inventory["episodes"]:
            try:
                key = (
                    int(episode.get("seasonNumber") or 0),
                    int(episode.get("episodeNumber") or 0),
                )
            except (TypeError, ValueError):
                continue
            rows_by_key[key] = episode

        for raw_season, raw_numbers in missing_by_season.items():
            season = int(raw_season)
            for raw_number in sorted({int(value) for value in raw_numbers}):
                episode = rows_by_key.get((season, raw_number))
                if not episode:
                    unresolved.setdefault(season, []).append(raw_number)
                    continue
                if episode.get("hasFile"):
                    already_available_by_season.setdefault(
                        season,
                        [],
                    ).append(raw_number)
                    continue
                episode_id = int(episode["id"])
                wanted_ids.append(episode_id)
                accepted_by_season.setdefault(season, []).append(raw_number)
                if self.episode_has_aired(episode):
                    searchable_ids.append(episode_id)

        wanted_ids = list(dict.fromkeys(wanted_ids))
        searchable_ids = list(dict.fromkeys(searchable_ids))
        if not wanted_ids:
            if already_available_by_season and not unresolved:
                return {
                    "outcome": "already_available",
                    "already_available": True,
                    "partial": False,
                    "series_id": int(inventory["series"]["id"]),
                    "accepted_by_season": {},
                    "already_available_by_season": already_available_by_season,
                    "monitored_episode_ids": [],
                    "searchable_episode_ids": [],
                    "searched_episode_ids": [],
                    "search_failed_episode_ids": [],
                    "search_attempted": False,
                    "search_succeeded": None,
                    "monitor_succeeded": False,
                    "future_count": 0,
                    "unresolved": {},
                    "command_id": None,
                    "search_error": None,
                }
            raise SonarrError("Sonarr found no missing episodes to monitor.")

        await self.request(
            "PUT",
            "/episode/monitor",
            json={"episodeIds": wanted_ids, "monitored": True},
        )

        command = None
        search_error = None
        search_succeeded: bool | None = None
        if searchable_ids:
            try:
                command = await self.request(
                    "POST",
                    "/command",
                    json={
                        "name": "EpisodeSearch",
                        "episodeIds": searchable_ids,
                    },
                )
                search_succeeded = True
            except Exception as exc:
                # Episode monitoring is already durable at this point. Report
                # the partial outcome so callers never tell users that nothing
                # was submitted and invite a duplicate retry.
                search_error = str(exc)
                search_succeeded = False

        partial = search_succeeded is False

        return {
            "outcome": "partial" if partial else "submitted",
            "already_available": False,
            "partial": partial,
            "series_id": int(inventory["series"]["id"]),
            "accepted_by_season": accepted_by_season,
            "already_available_by_season": already_available_by_season,
            "monitored_episode_ids": wanted_ids,
            "searchable_episode_ids": searchable_ids,
            "searched_episode_ids": searchable_ids if search_succeeded else [],
            "search_failed_episode_ids": (
                searchable_ids if search_succeeded is False else []
            ),
            "search_attempted": bool(searchable_ids),
            "search_succeeded": search_succeeded,
            "monitor_succeeded": True,
            "future_count": len(wanted_ids) - len(searchable_ids),
            "unresolved": unresolved,
            "command_id": (command or {}).get("id"),
            "search_error": search_error,
        }
