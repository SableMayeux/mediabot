import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


class AppImportTests(unittest.TestCase):
    def test_app_uses_extracted_seerr_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["DISCORD_TOKEN"] = "test-token"
            os.environ["SEERR_API_KEY"] = "test-key"
            os.environ["LOG_PATH"] = os.path.join(
                temp_dir,
                "mediabot.log",
            )
            os.environ["DB_PATH"] = os.path.join(
                temp_dir,
                "mediabot.db",
            )

            import app
            from mediabot.providers.seerr import SeerrProvider

            self.assertEqual(app.BOT_VERSION, "1.0.0")
            self.assertIsInstance(app.seerr, SeerrProvider)
            self.assertEqual(app.seerr.api_key, "test-key")
            for command_name in ("discover", "recommend", "rate", "report"):
                self.assertIn(command_name, {command.name for command in app.bot.commands})
            self.assertIn("music", {command.name for command in app.bot.commands})
            self.assertIsNone(app.bot.get_command("song"))
            self.assertIsNone(app.bot.get_command("musicstatus"))
            self.assertEqual(app.bot.get_command("rr").name, "recommend")
            self.assertEqual(app.bot.get_command("randomrequest").name, "recommend")
            self.assertEqual(app.bot.get_command("random").name, "discover")
            self.assertEqual(app.bot.get_command("ratings").name, "rate")

            for handler in list(app.logger.handlers):
                if (
                    isinstance(handler, logging.FileHandler)
                    and handler.baseFilename.startswith(temp_dir)
                ):
                    app.logger.removeHandler(handler)
                    handler.close()


class FakeDiscordMessage:
    def __init__(self):
        self.id = 123
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1


class FakeInteractionResponse:
    def __init__(self):
        self.defer_calls = 0

    async def defer(self):
        self.defer_calls += 1


class FakeInteraction:
    def __init__(self, message):
        self.message = message
        self.response = FakeInteractionResponse()


class RankedBatchStateTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def load_app():
        os.environ.setdefault("DISCORD_TOKEN", "test-token")
        os.environ.setdefault("SEERR_API_KEY", "test-key")
        os.environ.setdefault(
            "LOG_PATH",
            os.path.join(tempfile.gettempdir(), "mediabot-batch-test.log"),
        )
        os.environ.setdefault(
            "DB_PATH",
            os.path.join(tempfile.gettempdir(), "mediabot-batch-test.db"),
        )
        import app

        return app

    async def test_original_command_deleted_only_after_every_card_dismissed(self):
        app = self.load_app()
        message = FakeDiscordMessage()
        state = app.RankedBatchState(
            origin_message=message,
            card_ids=("a", "b", "c"),
        )

        await state.set_state("a", "dismissed")
        await state.set_state("b", "dismissed")
        self.assertEqual(message.delete_calls, 0)

        await state.set_state("c", "dismissed")
        self.assertEqual(message.delete_calls, 1)

    async def test_orphaned_button_gets_clean_expiry_response(self):
        app = self.load_app()
        previous_sleep = app.asyncio.sleep
        app.asyncio.sleep = AsyncMock()
        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            type=app.discord.InteractionType.component,
            response=response,
            message=None,
        )

        try:
            await app.on_interaction(interaction)
        finally:
            app.asyncio.sleep = previous_sleep

        response.send_message.assert_awaited_once()
        self.assertTrue(response.send_message.await_args.kwargs["ephemeral"])

    async def test_requested_card_preserves_original_command(self):
        app = self.load_app()
        message = FakeDiscordMessage()
        state = app.RankedBatchState(
            origin_message=message,
            card_ids=("a", "b", "c"),
        )

        await state.set_state("a", "pending")
        await state.set_state("a", "requested")
        await state.set_state("b", "dismissed")
        await state.set_state("c", "dismissed")

        self.assertEqual(message.delete_calls, 0)

    async def test_pending_card_can_later_complete_all_dismissed_batch(self):
        app = self.load_app()
        message = FakeDiscordMessage()
        state = app.RankedBatchState(
            origin_message=message,
            card_ids=("a", "b"),
        )

        await state.set_state("a", "pending")
        await state.set_state("b", "dismissed")
        await state.set_state("a", "dismissed")

        self.assertEqual(message.delete_calls, 1)

    async def test_recommendation_dismiss_button_deletes_card_and_command(self):
        app = self.load_app()
        card_id = ("movie", 82654)
        command_message = FakeDiscordMessage()
        card_message = FakeDiscordMessage()
        state = app.RankedBatchState(
            origin_message=command_message,
            card_ids=(card_id,),
        )
        item = {
            "id": 82654,
            "mediaType": "movie",
            "title": "Warm Bodies",
        }
        batch = SimpleNamespace(
            items=(item,),
            options=SimpleNamespace(
                media_type=None,
                randomize=False,
                pool_size=40,
            ),
            genre_name=None,
            eligible_count=1,
        )
        view = app.RecommendationCardView(
            requester_id=1,
            batch=batch,
            details_by_key={("movie", 82654): item},
            batch_state=state,
            batch_card_id=card_id,
        )
        interaction = FakeInteraction(card_message)

        await view.cancel_search(interaction)

        self.assertEqual(view.cancel_button.label, "Dismiss")
        self.assertEqual(interaction.response.defer_calls, 1)
        self.assertEqual(card_message.delete_calls, 1)
        self.assertEqual(command_message.delete_calls, 1)

    async def test_cleanup_deletes_single_request_card_and_command(self):
        app = self.load_app()
        command_message = FakeDiscordMessage()
        card_message = FakeDiscordMessage()

        await app.cleanup_unsuccessful_request(
            origin_message=card_message,
            command_message=command_message,
        )

        self.assertEqual(card_message.delete_calls, 1)
        self.assertEqual(command_message.delete_calls, 1)

    def test_tv_season_options_exclude_in_progress_and_available(self):
        app = self.load_app()
        details = {
            "seasons": [
                {"seasonNumber": 1, "episodeCount": 11},
                {"seasonNumber": 2, "episodeCount": 10},
                {"seasonNumber": 3, "episodeCount": 8},
                {"seasonNumber": 0, "episodeCount": 20},
            ],
            "mediaInfo": {
                "seasons": [
                    {"seasonNumber": 1, "status": 5},
                    {"seasonNumber": 2, "status": 1},
                    {"seasonNumber": 3, "status": 3},
                ],
            },
        }

        selectable, blocked, counts = app.tv_season_request_options(details)

        self.assertEqual(selectable, [2])
        self.assertEqual(blocked, {1: "Available", 3: "Processing"})
        self.assertEqual(counts, {1: 11, 2: 10, 3: 8})

    def test_partial_season_remains_requestable(self):
        app = self.load_app()
        details = {
            "seasons": [{"seasonNumber": 7, "episodeCount": 8}],
            "mediaInfo": {
                "seasons": [{"seasonNumber": 7, "status": 4}],
            },
        }

        selectable, blocked, counts = app.tv_season_request_options(details)

        self.assertEqual(selectable, [7])
        self.assertEqual(blocked, {})
        self.assertEqual(counts, {7: 8})

    def test_zero_episode_season_is_not_requestable(self):
        app = self.load_app()
        selectable, blocked, counts = app.tv_season_request_options({
            "seasons": [{"seasonNumber": 7, "episodeCount": 0}],
            "mediaInfo": {"seasons": []},
        })
        self.assertEqual(selectable, [])
        self.assertEqual(blocked, {7: "Not released"})
        self.assertEqual(counts, {7: 0})

    def test_episode_ranges_preserve_internal_gaps(self):
        app = self.load_app()
        self.assertEqual(
            app.compact_episode_ranges([1, 2, 5, 7, 8]),
            "E1-E2, E5, E7-E8",
        )

    async def test_partial_inventory_reports_exact_missing_episodes(self):
        app = self.load_app()
        previous_key = app.jellyfin.api_key
        previous_find = app.jellyfin.find_by_tmdb
        previous_numbers = app.jellyfin.series_season_episode_numbers
        app.jellyfin.api_key = "test-key"
        app.jellyfin.find_by_tmdb = AsyncMock(return_value={"Id": "series"})
        app.jellyfin.series_season_episode_numbers = AsyncMock(
            return_value={7: {1, 2, 3, 4, 5, 6}}
        )
        details = {
            "seasons": [{"seasonNumber": 7, "episodeCount": 8}],
            "mediaInfo": {
                "seasons": [{"seasonNumber": 7, "status": 4}],
            },
        }

        try:
            selectable, blocked, counts, missing, direct = (
                await app.tv_season_request_inventory(
                    {"id": 60625, "name": "Rick and Morty"},
                    details,
                )
            )
        finally:
            app.jellyfin.api_key = previous_key
            app.jellyfin.find_by_tmdb = previous_find
            app.jellyfin.series_season_episode_numbers = previous_numbers

        self.assertEqual(selectable, [])
        self.assertEqual(blocked, {7: "Partial - Sonarr repair unavailable"})
        self.assertEqual(counts, {7: 8})
        self.assertEqual(missing, {7: (7, 8)})
        self.assertEqual(direct, {})

    async def test_sonarr_inventory_makes_exact_gaps_requestable(self):
        app = self.load_app()
        previous_key = app.sonarr.api_key
        previous_inventory = app.sonarr.series_inventory
        app.sonarr.api_key = "test-key"
        app.sonarr.series_inventory = AsyncMock(return_value={
            "series": {"id": 1},
            "seasons": {
                7: {
                    "expected": set(range(1, 9)),
                    "available": set(range(1, 7)),
                    "missing": {7, 8},
                    "queued": set(),
                },
            },
        })
        details = {
            "externalIds": {"tvdbId": 12345},
            "seasons": [{"seasonNumber": 7, "episodeCount": 8}],
            "mediaInfo": {"seasons": [{"seasonNumber": 7, "status": 4}]},
        }

        try:
            selectable, blocked, counts, missing, direct = (
                await app.tv_season_request_inventory(
                    {"id": 60625, "name": "Rick and Morty"},
                    details,
                )
            )
        finally:
            app.sonarr.api_key = previous_key
            app.sonarr.series_inventory = previous_inventory

        self.assertEqual(selectable, [7])
        self.assertEqual(blocked, {})
        self.assertEqual(counts, {7: 8})
        self.assertEqual(missing, {7: (7, 8)})
        self.assertEqual(direct, {7: (7, 8)})

    async def test_empty_unapproved_sonarr_season_stays_in_seerr(self):
        app = self.load_app()
        previous_key = app.sonarr.api_key
        previous_inventory = app.sonarr.series_inventory
        app.sonarr.api_key = "test-key"
        app.sonarr.series_inventory = AsyncMock(return_value={
            "series": {"id": 1},
            "seasons": {
                2: {
                    "expected": set(range(1, 11)),
                    "available": set(),
                    "missing": set(range(1, 11)),
                    "future": set(),
                    "monitored": set(),
                    "queued": set(),
                    "queue_status": {},
                },
            },
        })
        details = {
            "externalIds": {"tvdbId": 12345},
            "seasons": [{"seasonNumber": 2, "episodeCount": 10}],
            "mediaInfo": {"seasons": [{"seasonNumber": 2, "status": 1}]},
        }

        try:
            selectable, blocked, counts, missing, direct = (
                await app.tv_season_request_inventory(
                    {"id": 60625, "name": "Rick and Morty"},
                    details,
                )
            )
        finally:
            app.sonarr.api_key = previous_key
            app.sonarr.series_inventory = previous_inventory

        self.assertEqual(selectable, [2])
        self.assertEqual(blocked, {})
        self.assertEqual(counts, {2: 10})
        self.assertEqual(missing, {2: tuple(range(1, 11))})
        self.assertEqual(direct, {})

    async def test_monitored_upcoming_episode_is_not_offered_as_repair(self):
        app = self.load_app()
        previous_key = app.sonarr.api_key
        previous_inventory = app.sonarr.series_inventory
        app.sonarr.api_key = "test-key"
        app.sonarr.series_inventory = AsyncMock(return_value={
            "series": {"id": 1},
            "seasons": {
                7: {
                    "expected": {1, 2},
                    "available": {1},
                    "missing": {2},
                    "future": {2},
                    "monitored": {2},
                    "queued": set(),
                    "queue_status": {},
                },
            },
        })
        details = {
            "externalIds": {"tvdbId": 12345},
            "seasons": [{"seasonNumber": 7, "episodeCount": 2}],
            "mediaInfo": {"seasons": [{"seasonNumber": 7, "status": 4}]},
        }

        try:
            selectable, blocked, _, missing, direct = (
                await app.tv_season_request_inventory(
                    {"id": 60625, "name": "Rick and Morty"},
                    details,
                )
            )
        finally:
            app.sonarr.api_key = previous_key
            app.sonarr.series_inventory = previous_inventory

        self.assertEqual(selectable, [])
        self.assertEqual(blocked, {7: "Upcoming E2 monitored"})
        self.assertEqual(missing, {})
        self.assertEqual(direct, {})

    def test_season_selector_starts_safe_and_classifies_early_gaps_as_partial(self):
        app = self.load_app()
        view = app.SeasonSelectionView(
            requester_id=1,
            item={"id": 1, "mediaType": "tv", "name": "Example"},
            details={"id": 1, "mediaType": "tv", "name": "Example"},
            seerr_user_id=2,
            seerr_username="example",
            origin_message=SimpleNamespace(),
            seasons=[7],
            season_catalog={7: 10},
            missing_episodes={7: (1, 2)},
            direct_episode_requests={7: (1, 2)},
        )

        self.assertEqual(view.selected, set())
        self.assertEqual(view.season_description(7), "Repair - missing E1-E2")

    async def test_plain_title_year_retries_without_year_only_after_no_results(self):
        app = self.load_app()
        previous = app.fetch_search_page
        desired = {
            "id": 10528,
            "mediaType": "movie",
            "title": "Sherlock Holmes",
            "releaseDate": "2009-12-23",
        }
        app.fetch_search_page = AsyncMock(side_effect=[([], 1), ([desired], 1)])
        try:
            query, year, results, _ = await app.resolve_seerr_search_query(
                "Sherlock Holmes 2009"
            )
        finally:
            app.fetch_search_page = previous

        self.assertEqual(query, "Sherlock Holmes")
        self.assertEqual(year, "2009")
        self.assertEqual(results[0]["id"], 10528)

    async def test_numeric_title_is_not_stripped_when_raw_search_works(self):
        app = self.load_app()
        previous = app.fetch_search_page
        desired = {
            "id": 11545,
            "mediaType": "movie",
            "title": "Class of 1984",
            "releaseDate": "1982-08-20",
        }
        app.fetch_search_page = AsyncMock(return_value=([desired], 1))
        try:
            query, year, results, _ = await app.resolve_seerr_search_query(
                "Class of 1984"
            )
        finally:
            app.fetch_search_page = previous

        self.assertEqual(query, "Class of 1984")
        self.assertIsNone(year)
        self.assertEqual(results[0]["id"], 11545)

    async def test_trailing_year_beats_irrelevant_raw_search_results(self):
        app = self.load_app()
        previous = app.fetch_search_page
        wrong = {
            "id": 999,
            "mediaType": "tv",
            "name": "Sherlock Holmes",
            "firstAirDate": "1984-01-01",
        }
        desired = {
            "id": 10528,
            "mediaType": "movie",
            "title": "Sherlock Holmes",
            "releaseDate": "2009-12-23",
        }
        app.fetch_search_page = AsyncMock(
            side_effect=[([wrong], 1), ([wrong, desired], 1)]
        )
        try:
            query, year, results, _ = await app.resolve_seerr_search_query(
                "Sherlock Holmes 2009"
            )
        finally:
            app.fetch_search_page = previous

        self.assertEqual(query, "Sherlock Holmes")
        self.assertEqual(year, "2009")
        self.assertEqual(results[0]["id"], 10528)

    def test_music_results_paginate_five_per_page(self):
        app = self.load_app()
        tracks = [
            {"name": f"Track {index}", "artists": ["Artist"]}
            for index in range(12)
        ]
        view = app.MusicSearchView(
            requester_id=1,
            query="Artist",
            tracks=tracks,
            source="test",
            command_message=SimpleNamespace(),
        )
        self.assertEqual(view.page_count, 3)
        self.assertEqual(len(view.page_tracks()), 5)
        view.display_page = 2
        view.refresh_controls()
        self.assertEqual(len(view.page_tracks()), 2)

    def test_rating_search_does_not_persist_before_selection(self):
        app = self.load_app()
        previous = app.save_rating
        app.save_rating = Mock()
        try:
            app.RatingSearchView(
                requester_id=1,
                query="Sherlock Holmes",
                target_year="2009",
                numeric_rating=9,
                results=[{
                    "id": 10528,
                    "mediaType": "movie",
                    "title": "Sherlock Holmes",
                    "releaseDate": "2009-12-23",
                }],
                seerr_page=1,
                total_seerr_pages=1,
                command_message=SimpleNamespace(),
            )
            app.save_rating.assert_not_called()
        finally:
            app.save_rating = previous

    async def test_incremental_availability_waits_for_every_episode(self):
        app = self.load_app()
        previous = app.jellyfin.series_season_episode_numbers
        app.jellyfin.series_season_episode_numbers = AsyncMock(
            return_value={1: set(range(1, 12)), 2: set(range(1, 10))}
        )
        record = {
            "media_type": "tv",
            "requested_seasons": "1,2",
            "requested_episode_counts": '{"1": 11, "2": 10}',
            "requested_episode_numbers": (
                '{"1": [1,2,3,4,5,6,7,8,9,10,11], '
                '"2": [1,2,3,4,5,6,7,8,9,10]}'
            ),
        }

        try:
            incomplete = await app.tracked_seasons_are_available(
                record,
                {"Id": "series"},
            )
            app.jellyfin.series_season_episode_numbers.return_value = {
                1: set(range(1, 12)),
                2: set(range(1, 11)),
            }
            complete = await app.tracked_seasons_are_available(
                record,
                {"Id": "series"},
            )
        finally:
            app.jellyfin.series_season_episode_numbers = previous

        self.assertFalse(incomplete)
        self.assertTrue(complete)


if __name__ == "__main__":
    unittest.main()
