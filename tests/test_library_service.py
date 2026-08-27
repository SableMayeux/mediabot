import asyncio
import unittest

from mediabot.services.library import (
    LibraryService,
    LibraryUsageError,
    RandomLibraryOptions,
)
from mediabot.services.discovery import DiscoveryService


class FakeJellyfin:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def catalog(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[kwargs["start_index"]]

    async def search(self, query, limit=10):
        return [query, limit]

    async def latest(self, limit=10):
        return [limit]


class RandomFilterTests(unittest.TestCase):
    def test_empty_filters_select_everything(self):
        self.assertEqual(
            LibraryService.parse_random_filters(""),
            RandomLibraryOptions(None, None, 1),
        )

    def test_type_alias_and_genre(self):
        self.assertEqual(
            LibraryService.parse_random_filters("  show   Science Fiction "),
            RandomLibraryOptions("Series", "Science Fiction", 1),
        )

    def test_unrecognized_first_word_is_the_genre(self):
        self.assertEqual(
            LibraryService.parse_random_filters("science fiction"),
            RandomLibraryOptions(None, "science fiction", 1),
        )

    def test_count_modifier(self):
        self.assertEqual(
            LibraryService.parse_random_filters(
                "movie horror --count 3"
            ),
            RandomLibraryOptions("Movie", "horror", 3),
        )

    def test_count_above_discord_limit_fails(self):
        with self.assertRaisesRegex(LibraryUsageError, "between 1 and 5"):
            LibraryService.parse_random_filters("movie --count 6")


class RandomSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_candidates_returns_none(self):
        provider = FakeJellyfin(
            {0: {"Items": [], "TotalRecordCount": 0}}
        )
        service = LibraryService(provider, randbelow=lambda total: 0)

        item, total = await service.random_item(genre="Definitely Missing")

        self.assertIsNone(item)
        self.assertEqual(total, 0)
        self.assertEqual(len(provider.calls), 1)

    async def test_first_item_reuses_count_page(self):
        expected = {"Id": "first", "Name": "First"}
        provider = FakeJellyfin(
            {0: {"Items": [expected], "TotalRecordCount": 7}}
        )
        service = LibraryService(provider, randbelow=lambda total: 0)

        item, total = await service.random_item(item_type="Movie")

        self.assertEqual(item, expected)
        self.assertEqual(total, 7)
        self.assertEqual(len(provider.calls), 1)

    async def test_random_index_fetches_only_selected_item(self):
        expected = {"Id": "selected", "Name": "Selected"}
        provider = FakeJellyfin(
            {
                0: {
                    "Items": [{"Id": "first"}],
                    "TotalRecordCount": 20,
                },
                11: {
                    "Items": [expected],
                    "TotalRecordCount": 20,
                },
            }
        )
        service = LibraryService(provider, randbelow=lambda total: 11)

        item, total = await service.random_item(
            item_type="Series",
            genre="Comedy",
        )

        self.assertEqual(item, expected)
        self.assertEqual(total, 20)
        self.assertEqual(provider.calls[1]["start_index"], 11)
        self.assertEqual(provider.calls[1]["item_type"], "Series")
        self.assertEqual(provider.calls[1]["genre"], "Comedy")

    async def test_multiple_items_are_unique_without_replacement(self):
        pages = {
            0: {
                "Items": [{"Id": "zero"}],
                "TotalRecordCount": 5,
            },
            1: {"Items": [{"Id": "one"}], "TotalRecordCount": 5},
            3: {"Items": [{"Id": "three"}], "TotalRecordCount": 5},
        }
        draws = iter((1, 0, 0))
        provider = FakeJellyfin(pages)
        service = LibraryService(
            provider,
            randbelow=lambda total: next(draws),
        )

        items, total = await service.random_items(count=3)

        self.assertEqual(total, 5)
        self.assertEqual(
            [item["Id"] for item in items],
            ["one", "zero", "three"],
        )
        self.assertEqual(len({item["Id"] for item in items}), 3)

    async def test_search_and_latest_are_provider_independent_seams(self):
        provider = FakeJellyfin({})
        service = LibraryService(provider)

        self.assertEqual(
            await service.search("Alien", limit=4),
            ["Alien", 4],
        )
        self.assertEqual(await service.latest(limit=6), [6])


class AvailableDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_enrichment_is_bounded_before_remote_fanout(self):
        items = [
            {
                "Id": f"series-{index}",
                "Type": "Series",
                "Name": f"Series {index}",
                "Genres": ["Drama"],
                "CommunityRating": 10 - (index / 1000),
            }
            for index in range(5000)
        ]
        provider = FakeJellyfin({
            0: {"TotalRecordCount": len(items), "Items": items},
        })
        service = LibraryService(provider, max_semantic_candidates=30)
        expression_service = DiscoveryService(
            object(),
            default_pool_size=40,
            max_pool_size=200,
        )
        resolved_ids = []

        async def resolve(item):
            resolved_ids.append(item["Id"])
            return {"Romance"}

        result = await service.discover(
            "show Romance --top 40",
            expression_service=expression_service,
            semantic_genre_resolver=resolve,
        )

        self.assertEqual(len(resolved_ids), 30)
        self.assertEqual(resolved_ids[0], "series-0")
        self.assertEqual(resolved_ids[-1], "series-29")
        self.assertIsNotNone(result)

    async def test_semantic_enrichment_has_a_total_deadline(self):
        items = [
            {
                "Id": f"series-{index}",
                "Type": "Series",
                "Name": f"Series {index}",
                "Genres": ["Drama"],
                "CommunityRating": 8,
            }
            for index in range(20)
        ]
        provider = FakeJellyfin({0: {"Items": items, "TotalRecordCount": 20}})
        service = LibraryService(
            provider,
            max_semantic_candidates=20,
            semantic_timeout_seconds=0.01,
        )
        expression_service = DiscoveryService(object())
        blocker = asyncio.Event()

        async def never_finishes(_item):
            await blocker.wait()
            return {"Romance"}

        result = await service.discover(
            "show Romance",
            expression_service=expression_service,
            semantic_genre_resolver=never_finishes,
        )

        self.assertIsNone(result)

    async def test_discover_uses_only_library_items_and_balances_types(self):
        provider = FakeJellyfin({
            0: {
                "TotalRecordCount": 4,
                "Items": [
                    {
                        "Id": "m1",
                        "Type": "Movie",
                        "Name": "Movie One",
                        "Genres": ["Fantasy", "Romance"],
                        "CommunityRating": 9.0,
                    },
                    {
                        "Id": "m2",
                        "Type": "Movie",
                        "Name": "Movie Two",
                        "Genres": ["Fantasy", "Romance"],
                        "CommunityRating": 8.0,
                    },
                    {
                        "Id": "s1",
                        "Type": "Series",
                        "Name": "Show One",
                        "Genres": ["Fantasy", "Drama"],
                        "CommunityRating": 8.5,
                    },
                    {
                        "Id": "s2",
                        "Type": "Series",
                        "Name": "Show Two",
                        "Genres": ["Fantasy", "Drama"],
                        "CommunityRating": 7.5,
                    },
                ],
            },
        })
        service = LibraryService(provider)
        expression_service = DiscoveryService(
            object(),
            default_pool_size=40,
            max_pool_size=200,
        )

        result = await service.discover(
            "Fantasy Romance --count 4",
            expression_service=expression_service,
            semantic_genre_resolver=lambda item: self._semantic_romance(item),
        )

        self.assertEqual(len(result.items), 4)
        self.assertEqual(
            [item["Type"] for item in result.items].count("Movie"),
            2,
        )
        self.assertEqual(
            [item["Type"] for item in result.items].count("Series"),
            2,
        )
        self.assertEqual(result.genre_filter, "Fantasy AND Romance")
        self.assertEqual(provider.calls[0]["sort_by"], "CommunityRating,DateCreated")

    @staticmethod
    async def _semantic_romance(item):
        return {"Romance"} if item.get("Type") == "Series" else set()

    async def test_discover_ranks_available_items_by_community_rating(self):
        provider = FakeJellyfin({
            0: {
                "TotalRecordCount": 2,
                "Items": [
                    {
                        "Id": "low",
                        "Type": "Movie",
                        "Name": "Low",
                        "Genres": ["Comedy"],
                        "CommunityRating": 5.0,
                    },
                    {
                        "Id": "high",
                        "Type": "Movie",
                        "Name": "High",
                        "Genres": ["Comedy"],
                        "CommunityRating": 9.0,
                    },
                ],
            },
        })
        service = LibraryService(provider)
        expression_service = DiscoveryService(
            object(),
            default_pool_size=40,
            max_pool_size=200,
        )

        result = await service.discover(
            "movie Comedy",
            expression_service=expression_service,
        )

        self.assertEqual(result.items[0]["Id"], "high")


if __name__ == "__main__":
    unittest.main()
