import unittest
from unittest.mock import AsyncMock

from mediabot.providers.seerr import SeerrError, SeerrProvider


class SeerrProviderUrlTests(unittest.TestCase):
    def setUp(self):
        self.provider = SeerrProvider(
            base_url="http://seerr.test:5055",
            api_key="test-key",
        )

    def test_spaces_use_percent_twenty(self):
        url = self.provider.build_url(
            "/search",
            {"query": "Spider Man", "page": 1},
        )
        self.assertEqual(
            str(url),
            "http://seerr.test:5055/api/v1/search"
            "?query=Spider%20Man&page=1",
        )

    def test_reserved_characters_remain_encoded(self):
        url = self.provider.build_url(
            "/search",
            {
                "query": "Bill & Ted's Excellent Adventure",
                "page": 1,
            },
        )
        self.assertEqual(
            str(url),
            "http://seerr.test:5055/api/v1/search"
            "?query=Bill%20%26%20Ted%27s%20Excellent%20Adventure"
            "&page=1",
        )

    def test_colon_and_hyphen_query(self):
        url = self.provider.build_url(
            "/search",
            {
                "query": "Spider-Man: Across the Spider-Verse",
                "page": 2,
            },
        )
        self.assertEqual(
            str(url),
            "http://seerr.test:5055/api/v1/search"
            "?query=Spider-Man%3A%20Across%20the%20Spider-Verse&page=2",
        )


class SeerrProviderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_api_key_fails_loudly(self):
        provider = SeerrProvider(
            base_url="http://seerr.test:5055",
            api_key="",
        )

        with self.assertRaisesRegex(SeerrError, "API key"):
            await provider.start()

    async def test_start_and_close_are_repeatable(self):
        provider = SeerrProvider(
            base_url="http://seerr.test:5055",
            api_key="test-key",
        )

        await provider.start()
        first_session = provider.session
        await provider.start()

        self.assertIs(provider.session, first_session)
        self.assertFalse(first_session.closed)

        await provider.close()
        await provider.close()

        self.assertIsNone(provider.session)


class SeerrDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_discovery_uses_popularity_and_genre(self):
        provider = SeerrProvider(
            base_url="http://seerr.test:5055",
            api_key="test-key",
        )
        provider.request = AsyncMock(
            return_value={"results": [], "totalPages": 1}
        )

        await provider.discover("movie", page=3, genre_id=27)

        method, path = provider.request.await_args.args
        params = provider.request.await_args.kwargs["params"]
        self.assertEqual((method, path), ("GET", "/discover/movies"))
        self.assertEqual(
            params,
            {"page": 3, "sortBy": "popularity.desc", "genre": "27"},
        )

    async def test_multiple_genres_are_sent_as_tmdb_and_filter(self):
        provider = SeerrProvider(
            base_url="http://seerr.test:5055",
            api_key="test-key",
        )
        provider.request = AsyncMock(
            return_value={"results": [], "totalPages": 1}
        )

        await provider.discover(
            "movie",
            genre_ids=(27, 35, 10749),
        )

        params = provider.request.await_args.kwargs["params"]
        self.assertEqual(params["genre"], "27,35,10749")

    async def test_tv_genres_use_tv_endpoint(self):
        provider = SeerrProvider(
            base_url="http://seerr.test:5055",
            api_key="test-key",
        )
        provider.request = AsyncMock(
            return_value=[{"id": 35, "name": "Comedy"}]
        )

        genres = await provider.genres("tv")

        self.assertEqual(genres[0]["name"], "Comedy")
        provider.request.assert_awaited_once_with("GET", "/genres/tv")

    async def test_request_details_uses_public_request_number(self):
        provider = SeerrProvider(
            base_url="http://seerr.test:5055",
            api_key="test-key",
        )
        provider.request = AsyncMock(return_value={"id": 272, "status": 2})

        result = await provider.request_details(272)

        self.assertEqual(result["status"], 2)
        provider.request.assert_awaited_once_with("GET", "/request/272")


if __name__ == "__main__":
    unittest.main()
