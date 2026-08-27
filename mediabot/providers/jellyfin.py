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
            self.base_url
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

                # Search results back user-facing actions such as playback
                # reports.  Jellyfin may expose virtual/missing library
                # entries when missing-media display is enabled; those are
                # metadata placeholders, not playable items.
                "IsMissing": "false",

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

    async def users(self):
        result = await self.request("GET", "/Users")
        return result if isinstance(result, list) else []

    async def resolve_user(self, name_or_id):
        wanted = str(name_or_id or "").strip().casefold()

        if not wanted:
            return None

        for user in await self.users():
            if str(user.get("Id", "")).casefold() == wanted:
                return user

            if str(user.get("Name", "")).casefold() == wanted:
                return user

        return None

    async def user_taste_items(self, user_id, limit=250):
        """Return played or favorited movies/shows with user data."""
        result = await self.request(
            "GET",
            f"/Users/{quote(str(user_id), safe='')}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series",
                "Fields": (
                    "Genres,ProviderIds,CommunityRating,"
                    "ProductionYear,DateCreated"
                ),
                "SortBy": "DatePlayed",
                "SortOrder": "Descending",
                "EnableUserData": "true",
                "EnableTotalRecordCount": "true",
                "Limit": max(1, int(limit)),
            },
        )

        items = result.get("Items", [])
        return [
            item
            for item in items
            if (
                (item.get("UserData") or {}).get("Played")
                or (item.get("UserData") or {}).get("IsFavorite")
                or (item.get("UserData") or {}).get("Likes") is True
                or (item.get("UserData") or {}).get("Rating")
            )
        ]

    async def trakt_authorization_status(self, user_id):
        return await self.request(
            "GET",
            f"/Trakt/Users/{quote(str(user_id), safe='')}/PollAuthorizationStatus",
        )

    async def trakt_authorize(self, user_id):
        return await self.request(
            "POST",
            f"/Trakt/Users/{quote(str(user_id), safe='')}/Authorize",
        )

    async def trakt_rate(self, user_id, item_id, rating):
        numeric_rating = int(rating)

        if not 1 <= numeric_rating <= 10:
            raise ValueError("Trakt rating must be between 1 and 10.")

        return await self.request(
            "POST",
            (
                f"/Trakt/Users/{quote(str(user_id), safe='')}"
                f"/Items/{quote(str(item_id), safe='')}/Rate"
            ),
            params={"rating": numeric_rating},
        )

    async def trakt_rate_external(
        self,
        user_id,
        *,
        media_type,
        tmdb_id,
        title,
        year,
        rating,
    ):
        normalized_type = str(media_type).casefold()
        numeric_rating = int(rating)

        if normalized_type not in ("movie", "tv"):
            raise ValueError("Trakt media type must be movie or tv.")

        if not 1 <= numeric_rating <= 10:
            raise ValueError("Trakt rating must be between 1 and 10.")

        try:
            numeric_year = int(year) if year else None
        except (TypeError, ValueError):
            numeric_year = None

        return await self.request(
            "POST",
            (
                f"/Trakt/Users/{quote(str(user_id), safe='')}"
                "/External/Rate"
            ),
            json={
                "mediaType": normalized_type,
                "tmdbId": int(tmdb_id),
                "title": str(title),
                "year": numeric_year,
                "rating": numeric_rating,
            },
        )

    async def trakt_recommendations(self, user_id, media_type):
        endpoints = {
            "movie": "RecommendedMovies",
            "tv": "RecommendedShows",
        }

        try:
            endpoint = endpoints[str(media_type).casefold()]
        except KeyError as exc:
            raise ValueError("Recommendation media type must be movie or tv.") from exc

        result = await self.request(
            "POST",
            (
                f"/Trakt/Users/{quote(str(user_id), safe='')}"
                f"/{endpoint}"
            ),
        )

        return result if isinstance(result, list) else []

    async def trakt_ratings(self, user_id, media_type):
        normalized_type = str(media_type).casefold()
        endpoints = {
            "movie": ("RatedMovies", "movie"),
            "tv": ("RatedShows", "show"),
        }

        try:
            endpoint, payload_key = endpoints[normalized_type]
        except KeyError as exc:
            raise ValueError("Rating media type must be movie or tv.") from exc

        result = await self.request(
            "GET",
            (
                f"/Trakt/Users/{quote(str(user_id), safe='')}"
                f"/{endpoint}"
            ),
        )
        normalized = []

        for row in result if isinstance(result, list) else []:
            media = row.get(payload_key) or {}
            tmdb_id = (media.get("ids") or {}).get("tmdb")
            rating = row.get("rating")

            if tmdb_id is None or rating is None:
                continue

            normalized.append({
                "media_type": normalized_type,
                "tmdb_id": int(tmdb_id),
                "rating": float(rating),
                "genres": ",".join(media.get("genres") or []),
            })

        return normalized

    async def catalog(
        self,
        *,
        item_type=None,
        genre=None,
        start_index=0,
        limit=1,
        sort_by="SortName",
        sort_order="Ascending",
    ):

        include_types = (
            item_type
            if item_type
            else "Movie,Series"
        )

        params = {
            "Recursive": "true",
            "IncludeItemTypes":
                include_types,
            "Fields": (
                "Overview,Genres,ProviderIds,"
                "DateCreated,CommunityRating,"
                "OfficialRating,RunTimeTicks,"
                "ProductionYear,ImageTags"
            ),
            "SortBy": str(sort_by),
            "SortOrder": str(sort_order),
            "EnableImages": "true",
            "EnableTotalRecordCount": "true",
            "StartIndex": max(
                0,
                int(start_index)
            ),
            "Limit": max(
                1,
                int(limit)
            ),
        }

        if genre:
            params["Genres"] = genre

        return await self.request(
            "GET",
            "/Items",
            params=params
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

    async def series_season_episode_counts(self, series_id):
        numbers = await self.series_season_episode_numbers(series_id)
        return {
            season_number: len(episode_numbers)
            for season_number, episode_numbers in numbers.items()
        }

    async def series_episode(
        self,
        series_id,
        *,
        season_number,
        episode_number,
    ):
        """Return one exact, indexed Jellyfin episode or ``None``.

        Jellyfin can represent combined episodes with ``IndexNumberEnd``. A
        request for either number must resolve to that same library item so a
        playback report identifies the actual file instead of a guessed title.
        """
        wanted_season = int(season_number)
        wanted_episode = int(episode_number)
        start_index = 0
        page_size = 500

        if wanted_season < 1 or wanted_episode < 1:
            return None

        while True:
            result = await self.request(
                "GET",
                f"/Shows/{quote(str(series_id), safe='')}/Episodes",
                params={
                    "Fields": (
                        "Overview,ParentIndexNumber,IndexNumber,"
                        "IndexNumberEnd,ProviderIds"
                    ),
                    "EnableImages": "true",
                    "EnableTotalRecordCount": "true",
                    "IsMissing": "false",
                    "Season": wanted_season,
                    "StartIndex": start_index,
                    "Limit": page_size,
                },
            )
            items = result.get("Items") or []

            for episode in items:
                try:
                    found_season = int(episode.get("ParentIndexNumber") or 0)
                    found_start = int(episode.get("IndexNumber") or 0)
                    found_end = int(
                        episode.get("IndexNumberEnd") or found_start
                    )
                except (TypeError, ValueError):
                    continue

                if (
                    found_season == wanted_season
                    and found_start <= wanted_episode <= max(found_start, found_end)
                ):
                    return episode

            start_index += len(items)
            total = int(result.get("TotalRecordCount") or start_index)
            if not items or start_index >= total or len(items) < page_size:
                return None

    async def series_season_episode_numbers(self, series_id):
        numbers = {}
        start_index = 0
        page_size = 1000

        while True:
            result = await self.request(
                "GET",
                f"/Shows/{quote(str(series_id), safe='')}/Episodes",
                params={
                    "Fields": "ParentIndexNumber,IndexNumber,IndexNumberEnd",
                    "EnableTotalRecordCount": "true",
                    "IsMissing": "false",
                    "StartIndex": start_index,
                    "Limit": page_size,
                },
            )
            items = result.get("Items") or []

            for episode in items:
                try:
                    season_number = int(episode.get("ParentIndexNumber") or 0)
                    episode_number = int(episode.get("IndexNumber") or 0)
                    episode_end = int(
                        episode.get("IndexNumberEnd") or episode_number
                    )
                except (TypeError, ValueError):
                    continue

                if season_number > 0 and episode_number > 0:
                    numbers.setdefault(season_number, set()).update(
                        range(episode_number, max(episode_number, episode_end) + 1)
                    )

            start_index += len(items)
            total = int(result.get("TotalRecordCount") or start_index)
            if not items or start_index >= total or len(items) < page_size:
                break

        return numbers

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
