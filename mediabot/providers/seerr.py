from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp
from yarl import URL

from .base import Provider


class SeerrError(RuntimeError):
    """Raised when Seerr cannot complete an API operation."""


def user_display_name(user: dict[str, Any]) -> str:
    """Follow Seerr's display-name precedence, including Jellyfin identities."""
    return next((str(user[key]).strip() for key in (
        "username", "plexUsername", "jellyfinUsername", "email", "id"
    ) if user.get(key) is not None and str(user[key]).strip()), "UNKNOWN")


def matching_users(users: list[dict[str, Any]], value: str) -> list[dict[str, Any]]:
    wanted = value.strip().casefold()
    if not wanted:
        return []
    return [user for user in users if wanted in {
        str(user[key]).strip().casefold() for key in (
            "id", "username", "email", "plexUsername", "jellyfinUsername"
        ) if user.get(key) is not None
    }]


class SeerrProvider(Provider):
    """Own Seerr HTTP transport and API-specific behavior."""

    name = "seerr"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get(
                "SEERR_URL",
                "",
            )
        ).rstrip("/")
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("SEERR_API_KEY", "")
        ).strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session and not self.session.closed:
            return

        if not self.api_key:
            raise SeerrError("Seerr API key is not configured.")

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

    def build_url(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> URL:
        """Build an already-encoded URL without yarl recanonicalizing it."""
        url = f"{self.base_url}/api/v1{path}"

        if params:
            encoded_query = urlencode(
                params,
                doseq=True,
                quote_via=quote,
                safe="",
            )
            url = f"{url}?{encoded_query}"

        # Seerr rejects '+' for spaces and literal reserved characters.
        # encoded=True preserves the RFC3986 string built above.
        return URL(url, encoded=True)

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        if not self.session or self.session.closed:
            raise SeerrError("Seerr HTTP session is not initialized.")

        params = kwargs.pop("params", None)
        request_url = self.build_url(path, params)

        async with self.session.request(
            method,
            request_url,
            **kwargs,
        ) as response:
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
                raise SeerrError(
                    f"Seerr HTTP {response.status}: {message}"
                )

            return payload

    async def health(self) -> Any:
        return await self.request("GET", "/settings/public")

    async def search(self, query: str) -> list[dict[str, Any]]:
        result = await self.request(
            "GET",
            "/search",
            params={"query": query, "page": 1},
        )
        return [
            item
            for item in result.get("results", [])
            if item.get("mediaType") in ("movie", "tv")
        ]

    async def users(self) -> list[dict[str, Any]]:
        """Fetch the complete directory; never silently return only page one."""
        users, seen = [], set()
        skip, page_size = 0, 100
        for _ in range(100):
            result = await self.request("GET", "/user", params={"take": page_size, "skip": skip})
            legacy_list = isinstance(result, list)
            page = result if legacy_list else result.get("results") if isinstance(result, dict) else None
            if not isinstance(page, list):
                raise SeerrError("Seerr returned an invalid user directory.")
            for user in page:
                if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
                    raise SeerrError("Seerr returned an invalid user identity.")
                if user["id"] in seen:
                    raise SeerrError("Seerr user pagination repeated an identity. Retry the directory lookup.")
                seen.add(user["id"])
                users.append(user)
            skip += len(page)
            if legacy_list:
                return users
            info = result.get("pageInfo", {})
            total = info.get("results") if isinstance(info, dict) else None
            if total is not None and (type(total) is not int or total < 0):
                raise SeerrError("Seerr returned invalid user pagination metadata.")
            if total is not None and skip >= total:
                return users
            if not page:
                if total is not None and skip < total:
                    raise SeerrError("Seerr returned an incomplete user directory. Retry the directory lookup.")
                return users
            if total is None and len(page) < page_size:
                return users
        raise SeerrError("Seerr user directory exceeded the pagination limit.")

    async def tv_details(self, media_id: int) -> Any:
        return await self.request("GET", f"/tv/{media_id}")

    async def genres(self, media_type: str) -> list[dict[str, Any]]:
        if media_type not in ("movie", "tv"):
            raise SeerrError("Seerr genre type must be movie or tv.")

        result = await self.request("GET", f"/genres/{media_type}")
        return result if isinstance(result, list) else []

    async def discover(
        self,
        media_type: str,
        *,
        page: int = 1,
        genre_id: int | None = None,
        genre_ids: list[int] | tuple[int, ...] | None = None,
        keyword_ids: list[int] | tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        paths = {
            "movie": "/discover/movies",
            "tv": "/discover/tv",
        }

        try:
            path = paths[media_type]
        except KeyError as exc:
            raise SeerrError(
                "Seerr discovery type must be movie or tv."
            ) from exc

        params: dict[str, Any] = {
            "page": max(1, int(page)),
            "sortBy": "popularity.desc",
        }

        normalized_genres = [
            int(value)
            for value in (genre_ids or ())
        ]

        if genre_id is not None:
            normalized_genres.insert(0, int(genre_id))

        if normalized_genres:
            params["genre"] = ",".join(
                str(value)
                for value in dict.fromkeys(normalized_genres)
            )

        normalized_keywords = [
            int(value)
            for value in (keyword_ids or ())
        ]

        if normalized_keywords:
            # Seerr forwards this to TMDB's with_keywords parameter. OR is
            # intentional: romance, love, or courtship is sufficient evidence
            # for the synthetic TV Romance genre.
            params["keywords"] = "|".join(
                str(value)
                for value in dict.fromkeys(normalized_keywords)
            )

        result = await self.request("GET", path, params=params)
        return result if isinstance(result, dict) else {}

    async def create_request(
        self,
        *,
        media_type: str,
        media_id: int,
        seerr_user_id: int,
        seasons: list[int] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "mediaType": media_type,
            "mediaId": media_id,
            "userId": seerr_user_id,
            "is4k": False,
        }

        if media_type == "tv":
            payload["seasons"] = seasons or []

        return await self.request(
            "POST",
            "/request",
            json=payload,
        )

    async def request_details(self, request_id: int) -> dict[str, Any]:
        """Return one Seerr request by its public request number."""
        result = await self.request("GET", f"/request/{int(request_id)}")
        return result if isinstance(result, dict) else {}
