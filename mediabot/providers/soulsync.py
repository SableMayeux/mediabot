from __future__ import annotations

import os
from typing import Any

import aiohttp
from urllib.parse import quote, urlencode

from .base import Provider


class SoulSyncError(RuntimeError):
    """Raised when SoulSync cannot complete an API operation."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        definitive: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = int(status) if status is not None else None
        self.definitive = bool(definitive)


class SoulSyncProvider(Provider):
    """Own SoulSync v1 transport and music request behavior."""

    name = "soulsync"

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
                "SOULSYNC_URL",
                "http://host.docker.internal:8008",
            )
        ).rstrip("/")
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("SOULSYNC_API_KEY", "")
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
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            timeout=self.timeout,
        )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

        self.session = None

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        if not self.enabled:
            raise SoulSyncError("SoulSync is not configured.")

        if not self.session or self.session.closed:
            raise SoulSyncError("SoulSync HTTP session is not initialized.")

        url = f"{self.base_url}/api/v1{path}"

        async with self.session.request(method, url, **kwargs) as response:
            try:
                payload = await response.json()
            except Exception:
                payload = {
                    "success": False,
                    "error": {"message": await response.text()},
                }

            error = payload.get("error") if isinstance(payload, dict) else None

            if response.status >= 400 or not payload.get("success", False):
                message = (
                    error.get("message")
                    if isinstance(error, dict)
                    else str(error or payload)
                )
                raise SoulSyncError(
                    f"SoulSync HTTP {response.status}: {message}",
                    status=response.status,
                    # A 4xx, or an explicit unsuccessful 2xx payload, proves
                    # the API rejected the request. A 5xx can occur after a
                    # server-side commit and therefore remains ambiguous.
                    definitive=(
                        400 <= response.status < 500
                        or response.status < 400
                    ),
                )

            return payload.get("data")

    async def health(self) -> dict[str, Any]:
        result = await self.request("GET", "/system/status")
        return result if isinstance(result, dict) else {}

    async def search_tracks(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/search/tracks",
            json={
                "query": str(query).strip(),
                "source": "auto",
                "limit": max(1, min(25, int(limit))),
            },
        )
        return result if isinstance(result, dict) else {"tracks": []}

    async def create_music_request(
        self,
        query: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/request",
            json={
                "query": str(query).strip(),
                "metadata": metadata or {},
            },
        )
        return result if isinstance(result, dict) else {}

    async def request_status(self, request_id: str) -> dict[str, Any]:
        result = await self.request(
            "GET",
            f"/request/{str(request_id).strip()}",
        )
        return result if isinstance(result, dict) else {}

    async def library_tracks(
        self,
        *,
        title: str,
        artist: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "title": str(title).strip(),
                "artist": str(artist).strip(),
                "limit": max(1, min(50, int(limit))),
            },
            quote_via=quote,
            safe="",
        )
        result = await self.request(
            "GET",
            f"/library/tracks?{query}",
        )
        return list((result or {}).get("tracks") or [])
