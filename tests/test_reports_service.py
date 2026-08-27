import unittest

from mediabot.services.reports import (
    REPORT_CATEGORY_LABELS,
    ReportCategory,
    ReportQuery,
    ReportService,
    ReportUsageError,
    normalize_report_category,
    parse_report_query,
)


class FakeJellyfin:
    def __init__(self, *, items=(), episode=None):
        self.items = list(items)
        self.episode = episode
        self.search_calls = []
        self.episode_calls = []

    async def search(self, query, limit=10):
        self.search_calls.append((query, limit))
        return list(self.items)

    async def series_episode(self, series_id, *, season_number, episode_number):
        self.episode_calls.append((series_id, season_number, episode_number))
        return self.episode


class ReportQueryTests(unittest.TestCase):
    def test_plain_title_is_not_rewritten(self):
        self.assertEqual(
            parse_report_query("Blade Runner 2049"),
            ReportQuery("Blade Runner 2049"),
        )

    def test_parses_episode_shorthand(self):
        parsed = parse_report_query("Breaking Bad S02E07")
        self.assertEqual(parsed.search_query, "Breaking Bad")
        self.assertEqual(parsed.season_number, 2)
        self.assertEqual(parsed.episode_number, 7)
        self.assertEqual(parsed.episode_label, "S02E07")

    def test_parses_readable_episode_form(self):
        parsed = parse_report_query("Breaking Bad season 2 episode 7")
        self.assertEqual(parsed, ReportQuery("Breaking Bad", 2, 7))

    def test_zero_episode_is_rejected(self):
        with self.assertRaisesRegex(ReportUsageError, "real season and episode"):
            parse_report_query("Breaking Bad S02E00")

    def test_empty_query_is_rejected(self):
        with self.assertRaisesRegex(ReportUsageError, "Tell me what"):
            parse_report_query("  ")


class ReportCategoryTests(unittest.TestCase):
    def test_expected_public_categories_are_complete(self):
        self.assertEqual(
            list(REPORT_CATEGORY_LABELS.values()),
            [
                "Won't Play",
                "Wrong Audio",
                "Bad Subtitles",
                "Bad Quality",
                "Wrong Episode",
                "Other",
            ],
        )

    def test_plain_alias_normalizes(self):
        self.assertIs(
            normalize_report_category("bad subs"),
            ReportCategory.BAD_SUBTITLES,
        )
        self.assertIs(
            normalize_report_category("Won't Play"),
            ReportCategory.WONT_PLAY,
        )

    def test_other_requires_details(self):
        with self.assertRaisesRegex(ReportUsageError, "what is wrong"):
            normalize_report_category("Other")
        self.assertIs(
            normalize_report_category("Other", details="Colors are inverted"),
            ReportCategory.OTHER,
        )


class ReportResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_episode_search_filters_out_movies(self):
        provider = FakeJellyfin(items=[
            {"Id": "m1", "Name": "Movie", "Type": "Movie"},
            {"Id": "s1", "Name": "Series", "Type": "Series"},
        ])
        service = ReportService(provider)

        results = await service.search(ReportQuery("Series", 2, 7))

        self.assertEqual([item["Id"] for item in results], ["s1"])
        self.assertEqual(provider.search_calls, [("Series", 25)])

    async def test_resolves_exact_episode_id(self):
        provider = FakeJellyfin(
            episode={"Id": "episode-27", "Name": "Negro y Azul"}
        )
        service = ReportService(provider)
        item = {
            "Id": "series-1",
            "Name": "Breaking Bad",
            "Type": "Series",
            "ProductionYear": 2008,
        }

        target = await service.resolve(item, ReportQuery("Breaking Bad", 2, 7))

        self.assertEqual(target.jellyfin_item_id, "episode-27")
        self.assertEqual(target.jellyfin_series_id, "series-1")
        self.assertEqual(target.target_key, "episode:episode-27")
        self.assertEqual(
            target.display_name,
            "Breaking Bad - S02E07 - Negro y Azul",
        )
        self.assertEqual(provider.episode_calls, [("series-1", 2, 7)])

    async def test_missing_episode_is_not_silently_reported_as_series(self):
        service = ReportService(FakeJellyfin(episode=None))
        item = {"Id": "s1", "Name": "Breaking Bad", "Type": "Series"}

        with self.assertRaisesRegex(ReportUsageError, "not indexed in Jellyfin"):
            await service.resolve(item, ReportQuery("Breaking Bad", 2, 7))

    async def test_resolves_whole_movie_without_episode_lookup(self):
        provider = FakeJellyfin()
        service = ReportService(provider)
        target = await service.resolve(
            {
                "Id": "movie-1",
                "Name": "Sherlock Holmes",
                "Type": "Movie",
                "ProductionYear": 2009,
            },
            ReportQuery("Sherlock Holmes 2009"),
        )

        self.assertEqual(target.media_type, "movie")
        self.assertEqual(target.display_name, "Sherlock Holmes (2009)")
        self.assertEqual(provider.episode_calls, [])


if __name__ == "__main__":
    unittest.main()
