import unittest

from mediabot.services.discovery import DiscoveryService
from mediabot.services.recommendations import RecommendationService


class FakeSeerr:
    async def genres(self, media_type):
        return [
            {"id": 27, "name": "Horror"},
            {"id": 35, "name": "Comedy"},
        ]

    async def discover(
        self,
        media_type,
        *,
        page,
        genre_id=None,
        genre_ids=None,
        keyword_ids=None,
    ):
        return {
            "totalPages": 1,
            "results": [
                {
                    "id": 1,
                    "mediaType": media_type,
                    "title": "Popular Comedy",
                    "genreIds": [35],
                    "voteAverage": 9,
                },
                {
                    "id": 2,
                    "mediaType": media_type,
                    "title": "Taste Horror",
                    "genreIds": [27],
                    "voteAverage": 7,
                },
                {
                    "id": 3,
                    "mediaType": media_type,
                    "title": "Already Rated",
                    "genreIds": [27],
                    "voteAverage": 10,
                },
            ],
        }


class BranchSeerr:
    async def genres(self, media_type):
        return [
            {"id": 14, "name": "Fantasy"},
            {"id": 28, "name": "Action"},
            {"id": 10749, "name": "Romance"},
        ]

    async def discover(
        self,
        media_type,
        *,
        page,
        genre_id=None,
        genre_ids=None,
        keyword_ids=None,
    ):
        branch = tuple(genre_ids or ())
        results = {
            (14, 10749): [
                {
                    "id": 10,
                    "mediaType": media_type,
                    "title": "Fantasy Romance",
                    "genreIds": [14, 10749],
                    "voteAverage": 6,
                },
            ],
            (28,): [
                {
                    "id": 20,
                    "mediaType": media_type,
                    "title": "Action One",
                    "genreIds": [28],
                    "voteAverage": 9,
                },
                {
                    "id": 21,
                    "mediaType": media_type,
                    "title": "Action Two",
                    "genreIds": [28],
                    "voteAverage": 8,
                },
                {
                    "id": 99,
                    "mediaType": media_type,
                    "title": "Upstream Filter Leak",
                    "genreIds": [],
                    "voteAverage": 10,
                },
            ],
        }.get(branch, [])
        return {"totalPages": 1, "results": results}


class BalancedSeerr:
    async def genres(self, media_type):
        return [{"id": 18, "name": "Drama"}]

    async def discover(
        self,
        media_type,
        *,
        page,
        genre_id=None,
        genre_ids=None,
        keyword_ids=None,
    ):
        offset = 0 if media_type == "movie" else 100
        return {
            "totalPages": 1,
            "results": [
                {
                    "id": offset + index,
                    "mediaType": media_type,
                    "title": f"{media_type} {index}",
                    "genreIds": [18],
                    "voteAverage": 10 - index,
                }
                for index in range(1, 5)
            ],
        }


class RecommendationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rating_affinity_beats_popularity_and_excludes_rated_title(self):
        discovery = DiscoveryService(
            FakeSeerr(),
            default_pool_size=3,
            max_pool_size=10,
        )
        service = RecommendationService(discovery)
        ratings = [
            {
                "media_type": "movie",
                "tmdb_id": 99,
                "rating": 10,
                "genres": "Horror",
            },
            {
                "media_type": "movie",
                "tmdb_id": 3,
                "rating": 8,
                "genres": "Horror",
            },
        ]

        result = await service.recommend("movie", ratings=ratings)

        self.assertEqual(result.items[0]["id"], 2)
        self.assertNotIn(3, [item["id"] for item in result.items])
        self.assertIn("Horror", result.reasons[("movie", 2)])

    async def test_trakt_match_is_a_real_scoring_signal(self):
        discovery = DiscoveryService(
            FakeSeerr(),
            default_pool_size=3,
            max_pool_size=10,
        )
        service = RecommendationService(discovery)

        result = await service.recommend(
            "movie",
            ratings=[],
            trakt_items=[{"mediaType": "movie", "ids": {"tmdb": 2}}],
        )

        self.assertEqual(result.items[0]["id"], 2)
        self.assertIn("Trakt suggestion #1", result.reasons[("movie", 2)])

    async def test_or_branches_are_represented_and_filter_leaks_are_rejected(self):
        discovery = DiscoveryService(
            BranchSeerr(),
            default_pool_size=10,
            max_pool_size=10,
        )
        service = RecommendationService(discovery)

        result = await service.recommend(
            "movie (fantasy and romance) or action --count 3",
            ratings=[],
        )

        selected_ids = [item["id"] for item in result.items]
        self.assertIn(10, selected_ids)
        self.assertNotIn(99, selected_ids)
        self.assertIn(
            "matches your genres: Fantasy AND Romance",
            result.reasons[("movie", 10)],
        )
        self.assertTrue(
            any(
                "matches your genres: Action" in result.reasons[("movie", item_id)]
                for item_id in selected_ids
                if item_id != 10
            )
        )

    async def test_mixed_count_four_balances_movies_and_tv(self):
        discovery = DiscoveryService(
            BalancedSeerr(),
            default_pool_size=8,
            max_pool_size=10,
        )
        service = RecommendationService(discovery)

        result = await service.recommend("--count 4", ratings=[])
        types = [item["mediaType"] for item in result.items]

        self.assertEqual(types.count("movie"), 2)
        self.assertEqual(types.count("tv"), 2)

    async def test_watched_history_excludes_titles_without_implying_likes(self):
        discovery = DiscoveryService(
            FakeSeerr(),
            default_pool_size=3,
            max_pool_size=10,
        )
        service = RecommendationService(discovery)
        baseline = await service.recommend("movie --count 3", ratings=[])
        with_history = await service.recommend(
            "movie --count 3",
            ratings=[],
            jellyfin_items=[{
                "Type": "Movie",
                "ProviderIds": {"Tmdb": "99"},
                "Genres": ["Horror"],
                "UserData": {"Played": True},
            }],
        )

        self.assertEqual(
            baseline.scores[("movie", 2)],
            with_history.scores[("movie", 2)],
        )

    async def test_trakt_rating_drives_taste_without_double_counting_local_copy(self):
        discovery = DiscoveryService(
            FakeSeerr(),
            default_pool_size=3,
            max_pool_size=10,
        )
        service = RecommendationService(discovery)
        trakt_rating = {
            "media_type": "movie",
            "tmdb_id": 3,
            "rating": 10,
            "genres": "Horror",
        }
        trakt_only = await service.recommend(
            "movie --count 2",
            ratings=[],
            trakt_ratings=[trakt_rating],
        )
        duplicated = await service.recommend(
            "movie --count 2",
            ratings=[trakt_rating],
            trakt_ratings=[trakt_rating],
        )

        self.assertEqual(trakt_only.items[0]["id"], 2)
        self.assertEqual(
            trakt_only.scores[("movie", 2)],
            duplicated.scores[("movie", 2)],
        )
        self.assertEqual(trakt_only.signals["trakt_ratings"], 1)

    async def test_trakt_recommendation_rank_decays(self):
        discovery = DiscoveryService(
            BalancedSeerr(),
            default_pool_size=4,
            max_pool_size=10,
        )
        service = RecommendationService(discovery)
        result = await service.recommend(
            "movie --count 2",
            ratings=[],
            trakt_items=[
                {"mediaType": "movie", "ids": {"tmdb": 2}},
                {"mediaType": "movie", "ids": {"tmdb": 1}},
            ],
        )

        self.assertIn("Trakt suggestion #1", result.reasons[("movie", 2)])
        self.assertIn("Trakt suggestion #2", result.reasons[("movie", 1)])


if __name__ == "__main__":
    unittest.main()
