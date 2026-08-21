import os
from urllib.parse import quote

import aiohttp


class JellyfinError(Exception):
    pass


class JellyfinProvider:

    def __init__(self):

        self.base_url = os.environ.get(
            "JELLYFIN_URL",
            "http://host.docker.internal:8096"
        ).rstrip("/")

        self.public_url = os.environ.get(
            "JELLYFIN_PUBLIC_URL",
            "http://host.docker.internal:8096"
        ).rstrip("/")

        self.api_key = os.environ.get(
            "JELLYFIN_API_KEY",
            ""
        ).strip()

        self.session = None
        self.server_id = None

    @property
    def enabled(self):

        return bool(
            self.api_key
        )

    async def start(self):

        if not self.enabled:
            return

        self.session = aiohttp.ClientSession(
            headers={
                "X-Emby-Token":
                    self.api_key,

                "Accept":
                    "application/json",
            },
            timeout=aiohttp.ClientTimeout(
                total=20
            ),
        )

        try:
            info = await self.system_info()

            self.server_id = (
                info.get("Id")
                or info.get("ServerId")
            )

        except Exception:
            pass

    async def close(self):

        if self.session:
            await self.session.close()

        self.session = None

    async def request(
        self,
        method,
        path,
        **kwargs
    ):

        if not self.enabled:
            raise JellyfinError(
                "Jellyfin integration is not configured."
            )

        if not self.session:
            raise JellyfinError(
                "Jellyfin session is not initialized."
            )

        url = (
            self.base_url
            + path
        )

        async with self.session.request(
            method,
            url,
            **kwargs
        ) as response:

            if response.status >= 400:

                body = await response.text()

                raise JellyfinError(
                    f"Jellyfin HTTP "
                    f"{response.status}: "
                    f"{body[:500]}"
                )

            content_type = (
                response.headers.get(
                    "Content-Type",
                    ""
                )
            )

            if (
                "application/json"
                in content_type
            ):
                return await response.json()

            return await response.text()

    async def health(self):

        return await self.request(
            "GET",
            "/System/Info"
        )

    async def system_info(self):

        return await self.request(
            "GET",
            "/System/Info"
        )

    async def search(
        self,
        query,
        limit=10
    ):

        result = await self.request(
            "GET",
            "/Items",
            params={
                "Recursive": "true",
                "SearchTerm": query,
                "IncludeItemTypes":
                    "Movie,Series",

                "Fields":
                    "Overview,Genres,"
                    "ProviderIds,"
                    "DateCreated,"
                    "People,Studios",

                "EnableImages": "true",
                "EnableTotalRecordCount":
                    "true",

                "Limit":
                    int(limit),
            }
        )

        return result.get(
            "Items",
            []
        )

    async def latest(
        self,
        limit=10
    ):

        result = await self.request(
            "GET",
            "/Items",
            params={
                "Recursive": "true",

                "IncludeItemTypes":
                    "Movie,Series",

                "Fields":
                    "Overview,Genres,"
                    "ProviderIds,"
                    "DateCreated",

                "SortBy":
                    "DateCreated",

                "SortOrder":
                    "Descending",

                "EnableImages":
                    "true",

                "Limit":
                    int(limit),
            }
        )

        return result.get(
            "Items",
            []
        )

    @staticmethod
    def _tmdb_id(
        item
    ):

        providers = (
            item.get("ProviderIds")
            or {}
        )

        for key, value in providers.items():

            if (
                str(key).casefold()
                == "tmdb"
            ):
                return str(value)

        return None

    async def find_by_tmdb(
        self,
        *,
        tmdb_id,
        title,
        media_type
    ):

        expected_type = (
            "Movie"
            if media_type == "movie"
            else "Series"
        )

        results = await self.search(
            title,
            limit=25
        )

        wanted = str(
            tmdb_id
        )

        for item in results:

            if (
                item.get("Type")
                != expected_type
            ):
                continue

            found = self._tmdb_id(
                item
            )

            if (
                found
                and found == wanted
            ):
                return item

        return None

    def watch_url(
        self,
        item_id
    ):

        item_id = quote(
            str(item_id),
            safe=""
        )

        if self.server_id:

            sid = quote(
                str(self.server_id),
                safe=""
            )

            return (
                f"{self.public_url}"
                f"/web/#/details"
                f"?id={item_id}"
                f"&serverId={sid}"
            )

        return (
            f"{self.public_url}"
            f"/web/#/details"
            f"?id={item_id}"
        )
