import unittest

from mediabot.services.discovery import (
    DiscoveryService,
    DiscoveryUsageError,
    RandomRequestOptions,
)


class FakeSeerr:
    def __init__(self, *, genres=None, pages=None, branch_pages=None):
        self.genre_results = genres or {
            "movie": [
                {"id": 27, "name": "Horror"},
                {"id": 35, "name": "Comedy"},
                {"id": 10749, "name": "Romance"},
                {"id": 878, "name": "Science Fiction"},
                {"id": 10402, "name": "Music"},
            ],
            "tv": [
                {"id": 35, "name": "Comedy"},
                {"id": 10765, "name": "Sci-Fi & Fantasy"},
            ],
        }
        self.pages = pages or {}
        self.branch_pages = branch_pages or {}
        self.discover_calls = []

    async def genres(self, media_type):
        return self.genre_results[media_type]

    async def discover(
        self,
        media_type,
        *,
        page,
        genre_id=None,
        genre_ids=None,
        keyword_ids=None,
    ):
        self.discover_calls.append(
            {
                "media_type": media_type,
                "page": page,
                "genre_id": genre_id,
                "genre_ids": tuple(genre_ids or ()),
                "keyword_ids": tuple(keyword_ids or ()),
            }
        )
        branch_key = (media_type, tuple(genre_ids or ()))

        if branch_key in self.branch_pages:
            pages = self.branch_pages[branch_key]
        else:
            pages = self.pages.get(media_type, self.pages)

        return pages[page]


class RandomRequestParsingTests(unittest.TestCase):
    def setUp(self):
        self.service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=40,
            max_pool_size=200,
        )

    def test_movie_genre_and_explicit_pool(self):
        self.assertEqual(
            self.service.parse_random_request(
                "movie science fiction --top 75"
            ),
            RandomRequestOptions(
                media_type="movie",
                genre="science fiction",
                pool_size=75,
                count=1,
            ),
        )

    def test_show_alias_and_equals_pool(self):
        self.assertEqual(
            self.service.parse_random_request("show comedy --top=25"),
            RandomRequestOptions(
                media_type="tv",
                genre="comedy",
                pool_size=25,
                count=1,
            ),
        )

    def test_count_modifier(self):
        self.assertEqual(
            self.service.parse_random_request(
                "movie horror --top 50 --count 3"
            ),
            RandomRequestOptions(
                media_type="movie",
                genre="horror",
                pool_size=50,
                count=3,
            ),
        )

    def test_count_cannot_exceed_pool(self):
        with self.assertRaisesRegex(DiscoveryUsageError, "larger than"):
            self.service.parse_random_request(
                "movie --top 2 --count 3"
            )

    def test_missing_media_type_fails(self):
        with self.assertRaisesRegex(DiscoveryUsageError, "movie.*show"):
            self.service.parse_random_request("")

    def test_pool_above_ceiling_fails(self):
        with self.assertRaisesRegex(DiscoveryUsageError, "between 1 and 200"):
            self.service.parse_random_request("movie --top 201")


class GenreResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tv_scifi_alias_resolves(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=40,
            max_pool_size=200,
        )

        genre_id, name = await service.resolve_genre("tv", "science fiction")

        self.assertEqual((genre_id, name), (10765, "Sci-Fi & Fantasy"))

    async def test_unknown_genre_lists_available_values(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=40,
            max_pool_size=200,
        )

        with self.assertRaisesRegex(DiscoveryUsageError, "Horror"):
            await service.resolve_genre("movie", "nope")

    async def test_multiple_genres_and_love_alias_resolve(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=40,
            max_pool_size=200,
        )

        genre_ids, names = await service.resolve_genres(
            "movie",
            ("horror", "comedy", "love"),
        )

        self.assertEqual(genre_ids, (27, 35, 10749))
        self.assertEqual(names, ("Horror", "Comedy", "Romance"))

    async def test_tv_romance_resolves_to_semantic_keyword_genre(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=40,
            max_pool_size=200,
        )

        genre_id, name = await service.resolve_genre("tv", "romance")

        self.assertEqual(
            (genre_id, name),
            (DiscoveryService.TV_ROMANCE_GENRE_ID, "Romance"),
        )

    async def test_longest_match_preserves_multiword_genre(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=40,
            max_pool_size=200,
        )

        genre_ids, names = await service.resolve_genres(
            "movie",
            ("science", "fiction", "comedy"),
        )

        self.assertEqual(genre_ids, (878, 35))
        self.assertEqual(names, ("Science Fiction", "Comedy"))

    async def test_musical_alias_resolves_to_tmdb_music(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=40,
            max_pool_size=200,
        )

        singular = await service.resolve_genre("movie", "musical")
        plural = await service.resolve_genre("movie", "musicals")

        self.assertEqual(singular, (10402, "Music"))
        self.assertEqual(plural, (10402, "Music"))


class RandomDiscoverySelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_excludes_adult_owned_and_in_progress_titles(self):
        pages = {
            1: {
                "totalPages": 1,
                "results": [
                    {"id": 1, "mediaType": "movie", "adult": True},
                    {
                        "id": 2,
                        "mediaType": "movie",
                        "mediaInfo": {"status": 5},
                    },
                    {
                        "id": 3,
                        "mediaType": "movie",
                        "mediaInfo": {"status": 3},
                    },
                    {"id": 4, "mediaType": "movie", "title": "Eligible"},
                ],
            }
        }
        service = DiscoveryService(
            FakeSeerr(pages=pages),
            default_pool_size=10,
            max_pool_size=20,
            randbelow=lambda total: 0,
        )

        pick = await service.random_request("movie")

        self.assertEqual(pick.item["id"], 4)
        self.assertEqual(pick.eligible_count, 1)

    async def test_builds_top_x_across_pages_then_samples(self):
        pages = {
            1: {
                "totalPages": 2,
                "results": [
                    {"id": 1, "mediaType": "movie"},
                    {"id": 2, "mediaType": "movie"},
                ],
            },
            2: {
                "totalPages": 2,
                "results": [
                    {"id": 3, "mediaType": "movie"},
                    {"id": 4, "mediaType": "movie"},
                ],
            },
        }
        provider = FakeSeerr(pages=pages)
        service = DiscoveryService(
            provider,
            default_pool_size=3,
            max_pool_size=10,
            randbelow=lambda total: 2,
        )

        pick = await service.random_request("movie horror --top 3")

        self.assertEqual(pick.item["id"], 3)
        self.assertEqual(pick.eligible_count, 3)
        self.assertEqual(pick.genre_name, "Horror")
        self.assertEqual(
            [call["page"] for call in provider.discover_calls],
            [1, 2],
        )
        self.assertTrue(
            all(call["genre_id"] == 27 for call in provider.discover_calls)
        )

    async def test_multiple_candidates_are_unique(self):
        pages = {
            1: {
                "totalPages": 1,
                "results": [
                    {"id": 1, "mediaType": "movie"},
                    {"id": 2, "mediaType": "movie"},
                    {"id": 3, "mediaType": "movie"},
                    {"id": 4, "mediaType": "movie"},
                ],
            }
        }
        draws = iter((1, 0, 0))
        service = DiscoveryService(
            FakeSeerr(pages=pages),
            default_pool_size=4,
            max_pool_size=10,
            max_count=5,
            randbelow=lambda total: next(draws),
        )

        pick = await service.random_request(
            "movie --top 4 --count 3"
        )

        self.assertEqual([item["id"] for item in pick.items], [2, 1, 3])
        self.assertEqual(len({item["id"] for item in pick.items}), 3)


class RankedDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_media_type_is_optional_and_modifiers_still_compose(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=40,
            max_pool_size=200,
        )

        options = service.parse_discover(
            "horror comedy love --top 100 --count 3 --random"
        )

        self.assertIsNone(options.media_type)
        self.assertEqual(options.genre_expression, "horror comedy love")
        self.assertEqual(options.pool_size, 100)
        self.assertEqual(options.count, 3)
        self.assertTrue(options.randomize)

    def test_single_dash_count_is_tolerated(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=40,
            max_pool_size=200,
        )

        options = service.parse_discover(
            "(fantasy and romance) or action -count 3"
        )

        self.assertEqual(options.count, 3)
        self.assertEqual(
            options.genre_expression,
            "(fantasy and romance) or action",
        )

    async def test_ranked_and_random_modes_share_requestable_pool(self):
        pages = {
            1: {
                "totalPages": 1,
                "results": [
                    {"id": 1, "mediaType": "movie"},
                    {"id": 2, "mediaType": "movie"},
                    {"id": 3, "mediaType": "movie"},
                ],
            }
        }
        service = DiscoveryService(
            FakeSeerr(pages=pages),
            default_pool_size=3,
            max_pool_size=10,
            randbelow=lambda total: total - 1,
        )

        ranked = await service.discover("movie --count 2")
        randomized = await service.discover("movie --count 2 --random")

        self.assertEqual([item["id"] for item in ranked.items], [1, 2])
        self.assertEqual([item["id"] for item in randomized.items], [3, 2])
        self.assertFalse(ranked.options.randomize)
        self.assertTrue(randomized.options.randomize)

    async def test_general_multigenre_query_skips_incompatible_tv_catalog(self):
        pages = {
            "movie": {
                1: {
                    "totalPages": 1,
                    "results": [
                        {
                            "id": 82654,
                            "mediaType": "movie",
                            "title": "Warm Bodies",
                            "genreIds": [27, 35, 10749],
                        }
                    ],
                }
            }
        }
        provider = FakeSeerr(pages=pages)
        service = DiscoveryService(
            provider,
            default_pool_size=20,
            max_pool_size=200,
        )

        result = await service.discover("horror comedy love")

        self.assertEqual(result.items[0]["title"], "Warm Bodies")
        self.assertEqual(
            result.genre_filter,
            "Horror AND Comedy AND Romance",
        )
        self.assertEqual(
            provider.discover_calls[0]["genre_ids"],
            (27, 35, 10749),
        )

    async def test_general_or_query_does_not_silently_drop_tv_branch(self):
        genres = {
            "movie": [
                {"id": 14, "name": "Fantasy"},
                {"id": 28, "name": "Action"},
                {"id": 10749, "name": "Romance"},
            ],
            "tv": [
                {"id": 10759, "name": "Action & Adventure"},
                {"id": 10765, "name": "Sci-Fi & Fantasy"},
            ],
        }
        branch_pages = {
            ("movie", (14, 10749)): {
                1: {
                    "totalPages": 1,
                    "results": [
                        {
                            "id": 1,
                            "mediaType": "movie",
                            "title": "Fantasy Romance",
                            "genreIds": [14, 10749],
                        }
                    ],
                }
            },
            ("movie", (28,)): {
                1: {
                    "totalPages": 1,
                    "results": [
                        {
                            "id": 2,
                            "mediaType": "movie",
                            "title": "Action Movie",
                            "genreIds": [28],
                        }
                    ],
                }
            },
            ("tv", (10765,)): {
                1: {
                    "totalPages": 1,
                    "results": [
                        {
                            "id": 3,
                            "mediaType": "tv",
                            "title": "Fantasy Romance Show",
                            "genreIds": [10765],
                        }
                    ],
                }
            },
            ("tv", (10759,)): {
                1: {
                    "totalPages": 1,
                    "results": [
                        {
                            "id": 4,
                            "mediaType": "tv",
                            "title": "Action Show",
                            "genreIds": [10759],
                        }
                    ],
                }
            },
        }
        provider = FakeSeerr(genres=genres, branch_pages=branch_pages)
        service = DiscoveryService(
            provider,
            default_pool_size=20,
            max_pool_size=200,
        )

        result = await service.discover(
            "(fantasy and romance) or action --count 2"
        )

        self.assertEqual(
            [item["title"] for item in result.items],
            ["Fantasy Romance", "Fantasy Romance Show"],
        )
        self.assertTrue(provider.discover_calls)
        self.assertEqual(
            {call["media_type"] for call in provider.discover_calls},
            {"movie", "tv"},
        )
        romance_calls = [
            call for call in provider.discover_calls
            if call["media_type"] == "tv" and call["genre_ids"] == (10765,)
        ]
        self.assertEqual(
            romance_calls[0]["keyword_ids"],
            DiscoveryService.TV_ROMANCE_KEYWORD_IDS,
        )
        self.assertIn(
            DiscoveryService.TV_ROMANCE_GENRE_ID,
            result.items[1]["genreIds"],
        )

    async def test_parenthesized_or_compiles_to_exact_tmdb_branches(self):
        branch_pages = {
            ("movie", (10749, 35)): {
                1: {
                    "totalPages": 1,
                    "results": [
                        {
                            "id": 82654,
                            "mediaType": "movie",
                            "title": "Warm Bodies",
                        }
                    ],
                }
            },
            ("movie", (27,)): {
                1: {
                    "totalPages": 1,
                    "results": [
                        {
                            "id": 348,
                            "mediaType": "movie",
                            "title": "Alien",
                        }
                    ],
                }
            },
        }
        provider = FakeSeerr(branch_pages=branch_pages)
        service = DiscoveryService(
            provider,
            default_pool_size=20,
            max_pool_size=200,
        )

        result = await service.discover(
            "movie (love comedy) || horror --count 2"
        )

        self.assertEqual(
            [item["title"] for item in result.items],
            ["Warm Bodies", "Alien"],
        )
        self.assertEqual(
            result.genre_filter,
            "(Romance AND Comedy) OR Horror",
        )
        self.assertEqual(
            [call["genre_ids"] for call in provider.discover_calls],
            [(10749, 35), (27,)],
        )

    def test_and_precedence_and_parentheses_expand_to_dnf(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=20,
            max_pool_size=200,
        )
        catalogs = service.seerr.genre_results

        branches, rendered = service.compile_genre_expression(
            media_type="movie",
            expression="(horror || comedy) love",
            catalogs=catalogs,
        )

        self.assertEqual(
            branches,
            ((27, 10749), (35, 10749)),
        )
        self.assertEqual(
            rendered,
            "(Horror AND Romance) OR (Comedy AND Romance)",
        )

    def test_word_operators_and_commas_are_supported(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=20,
            max_pool_size=200,
        )

        branches, _ = service.compile_genre_expression(
            media_type="movie",
            expression="horror and comedy or love, comedy",
            catalogs=service.seerr.genre_results,
        )

        self.assertEqual(branches, ((27, 35), (10749, 35)))

    def test_malformed_boolean_expression_fails_loudly(self):
        service = DiscoveryService(
            FakeSeerr(),
            default_pool_size=20,
            max_pool_size=200,
        )

        with self.assertRaisesRegex(DiscoveryUsageError, "Unmatched"):
            service.compile_genre_expression(
                media_type="movie",
                expression="(horror || comedy",
                catalogs=service.seerr.genre_results,
            )

        with self.assertRaisesRegex(DiscoveryUsageError, "single"):
            service.compile_genre_expression(
                media_type="movie",
                expression="horror | comedy",
                catalogs=service.seerr.genre_results,
            )


if __name__ == "__main__":
    unittest.main()
