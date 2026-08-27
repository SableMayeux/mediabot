import unittest
from unittest.mock import AsyncMock

from mediabot.providers.jellyfin import JellyfinProvider


class JellyfinCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_excludes_virtual_missing_library_entries(self):
        provider = JellyfinProvider()
        provider.request = AsyncMock(return_value={"Items": []})

        await provider.search("Breaking Bad", limit=25)

        method, path = provider.request.await_args.args
        params = provider.request.await_args.kwargs["params"]
        self.assertEqual((method, path), ("GET", "/Items"))
        self.assertEqual(params["IsMissing"], "false")
        self.assertEqual(params["IncludeItemTypes"], "Movie,Series")

    async def test_catalog_builds_bounded_movie_genre_query(self):
        provider = JellyfinProvider()
        provider.request = AsyncMock(
            return_value={"Items": [], "TotalRecordCount": 0}
        )

        result = await provider.catalog(
            item_type="Movie",
            genre="Science Fiction",
            start_index=14,
            limit=1,
        )

        self.assertEqual(result["TotalRecordCount"], 0)
        provider.request.assert_awaited_once()
        method, path = provider.request.await_args.args
        params = provider.request.await_args.kwargs["params"]

        self.assertEqual((method, path), ("GET", "/Items"))
        self.assertEqual(params["IncludeItemTypes"], "Movie")
        self.assertEqual(params["Genres"], "Science Fiction")
        self.assertEqual(params["StartIndex"], 14)
        self.assertEqual(params["Limit"], 1)
        self.assertEqual(params["EnableTotalRecordCount"], "true")

    async def test_catalog_defaults_to_movies_and_series(self):
        provider = JellyfinProvider()
        provider.request = AsyncMock(
            return_value={"Items": [], "TotalRecordCount": 0}
        )

        await provider.catalog()

        params = provider.request.await_args.kwargs["params"]
        self.assertEqual(params["IncludeItemTypes"], "Movie,Series")
        self.assertNotIn("Genres", params)

    async def test_external_trakt_rating_posts_tmdb_payload(self):
        provider = JellyfinProvider()
        provider.request = AsyncMock(return_value={"added": {"movies": 1}})

        await provider.trakt_rate_external(
            "user-guid",
            media_type="movie",
            tmdb_id=603,
            title="The Matrix",
            year="1999",
            rating=9,
        )

        provider.request.assert_awaited_once_with(
            "POST",
            "/Trakt/Users/user-guid/External/Rate",
            json={
                "mediaType": "movie",
                "tmdbId": 603,
                "title": "The Matrix",
                "year": 1999,
                "rating": 9,
            },
        )

    async def test_trakt_ratings_normalize_extended_movie_payload(self):
        provider = JellyfinProvider()
        provider.request = AsyncMock(return_value=[{
            "rating": 9,
            "movie": {
                "ids": {"tmdb": 603},
                "genres": ["science-fiction", "action"],
            },
        }])

        result = await provider.trakt_ratings("user-guid", "movie")

        self.assertEqual(result, [{
            "media_type": "movie",
            "tmdb_id": 603,
            "rating": 9.0,
            "genres": "science-fiction,action",
        }])
        provider.request.assert_awaited_once_with(
            "GET",
            "/Trakt/Users/user-guid/RatedMovies",
        )

    async def test_series_season_episode_counts_ignores_specials(self):
        provider = JellyfinProvider()
        provider.request = AsyncMock(return_value={
            "Items": [
                {"ParentIndexNumber": 0, "IndexNumber": 1},
                {"ParentIndexNumber": 1, "IndexNumber": 1},
                {"ParentIndexNumber": 1, "IndexNumber": 2},
                {"ParentIndexNumber": 2, "IndexNumber": 1},
            ],
        })

        result = await provider.series_season_episode_counts("series-guid")

        self.assertEqual(result, {1: 2, 2: 1})
        provider.request.assert_awaited_once_with(
            "GET",
            "/Shows/series-guid/Episodes",
            params={
                "Fields": "ParentIndexNumber,IndexNumber,IndexNumberEnd",
                "EnableTotalRecordCount": "true",
                "IsMissing": "false",
                "StartIndex": 0,
                "Limit": 1000,
            },
        )

    async def test_episode_numbers_preserve_gaps(self):
        provider = JellyfinProvider()
        provider.request = AsyncMock(return_value={
            "Items": [
                {"ParentIndexNumber": 7, "IndexNumber": 1},
                {"ParentIndexNumber": 7, "IndexNumber": 2},
                {"ParentIndexNumber": 7, "IndexNumber": 6},
                {"ParentIndexNumber": 7, "IndexNumber": 8},
            ],
        })

        result = await provider.series_season_episode_numbers("series-guid")

        self.assertEqual(result, {7: {1, 2, 6, 8}})

    async def test_combined_episode_file_expands_index_number_end(self):
        provider = JellyfinProvider()
        provider.request = AsyncMock(return_value={
            "Items": [{
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "IndexNumberEnd": 2,
            }],
            "TotalRecordCount": 1,
        })

        result = await provider.series_season_episode_numbers("series-guid")

        self.assertEqual(result, {1: {1, 2}})

    async def test_episode_inventory_paginates_beyond_one_thousand(self):
        provider = JellyfinProvider()
        first_page = [
            {"ParentIndexNumber": 1, "IndexNumber": index}
            for index in range(1, 1001)
        ]
        provider.request = AsyncMock(side_effect=[
            {"Items": first_page, "TotalRecordCount": 1001},
            {
                "Items": [{"ParentIndexNumber": 2, "IndexNumber": 1}],
                "TotalRecordCount": 1001,
            },
        ])

        result = await provider.series_season_episode_numbers("series-guid")

        self.assertEqual(len(result[1]), 1000)
        self.assertEqual(result[2], {1})
        self.assertEqual(provider.request.await_count, 2)

    async def test_series_episode_resolves_combined_episode_file(self):
        provider = JellyfinProvider()
        combined = {
            "Id": "episode-1-2",
            "Name": "Pilot / Part Two",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
            "IndexNumberEnd": 2,
        }
        provider.request = AsyncMock(return_value={
            "Items": [combined],
            "TotalRecordCount": 1,
        })

        result = await provider.series_episode(
            "series-guid",
            season_number=1,
            episode_number=2,
        )

        self.assertEqual(result, combined)
        params = provider.request.await_args.kwargs["params"]
        self.assertEqual(params["Season"], 1)
        self.assertEqual(params["IsMissing"], "false")

    async def test_series_episode_returns_none_for_unindexed_episode(self):
        provider = JellyfinProvider()
        provider.request = AsyncMock(return_value={
            "Items": [{
                "Id": "episode-1",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
            }],
            "TotalRecordCount": 1,
        })

        result = await provider.series_episode(
            "series-guid",
            season_number=1,
            episode_number=7,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
