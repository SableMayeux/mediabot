import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mediabot.services.reports import ReportQuery, ResolvedReportTarget


def load_app():
    os.environ.setdefault("DISCORD_TOKEN", "test-token")
    os.environ.setdefault("SEERR_API_KEY", "test-key")
    os.environ.setdefault(
        "LOG_PATH",
        os.path.join(tempfile.gettempdir(), "mediabot-report-ui.log"),
    )
    os.environ.setdefault(
        "DB_PATH",
        os.path.join(tempfile.gettempdir(), "mediabot-report-ui.db"),
    )
    import app

    return app


class ReportViewShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()

    def test_report_search_pages_five_exact_library_results_at_a_time(self):
        results = [
            {
                "Id": f"item-{index}",
                "Name": f"Result {index}",
                "Type": "Series",
                "ProductionYear": 2000 + index,
            }
            for index in range(7)
        ]
        view = self.app.ReportSearchView(
            requester_id=10,
            query=ReportQuery("Result"),
            results=results,
            command_message=SimpleNamespace(),
        )

        self.assertEqual(view.page_count, 2)
        self.assertEqual(len(view.page_items()), 5)
        view.display_page = 1
        view.refresh_controls()
        self.assertEqual(len(view.page_items()), 2)
        self.assertEqual(
            [button.disabled for button in view.result_buttons],
            [False, False, True, True, True],
        )

    def test_report_category_surface_matches_the_six_public_problems(self):
        target = ResolvedReportTarget(
            jellyfin_item_id="episode-27",
            jellyfin_series_id="series-1",
            media_type="episode",
            title="Breaking Bad",
            year="2008",
            season_number=2,
            episode_number=7,
            episode_title="Negro y Azul",
        )
        view = self.app.ReportCategoryView(
            requester_id=10,
            target=target,
            command_message=SimpleNamespace(),
        )

        self.assertEqual(
            [child.label for child in view.children],
            [
                "Won't Play",
                "Wrong Audio",
                "Bad Subtitles",
                "Bad Quality",
                "Wrong Episode",
                "Other",
                "Cancel",
            ],
        )
        self.assertIn("S02E07", view.build_embed().fields[0].value)


class AdminReportAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_requires_originating_admin_and_matching_guild(self):
        app = load_app()
        view = app.AdminReportQueueView(
            requester_id=10,
            guild_id=20,
            records=[{"status": "open"}],
            command_message=SimpleNamespace(),
        )

        authorized = SimpleNamespace(
            user=SimpleNamespace(
                id=10,
                guild_permissions=SimpleNamespace(administrator=True),
            ),
            guild_id=20,
            message=None,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        self.assertTrue(await view.interaction_check(authorized))
        authorized.response.send_message.assert_not_awaited()

        wrong_guild = SimpleNamespace(
            user=SimpleNamespace(
                id=10,
                guild_permissions=SimpleNamespace(administrator=True),
            ),
            guild_id=999,
            message=None,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(wrong_guild))
        wrong_guild.response.send_message.assert_awaited_once_with(
            "This administrator queue belongs to someone else.",
            ephemeral=True,
        )

        lost_permission = SimpleNamespace(
            user=SimpleNamespace(
                id=10,
                guild_permissions=SimpleNamespace(administrator=False),
            ),
            guild_id=20,
            message=None,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(lost_permission))


class ReportSelectionInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_uses_message_update_defer_without_stuck_spinner(self):
        app = load_app()
        target = ResolvedReportTarget(
            jellyfin_item_id="movie-1",
            media_type="movie",
            title="Sherlock Holmes",
            year="2009",
        )
        previous_resolve = app.reports.resolve
        app.reports.resolve = AsyncMock(return_value=target)
        source_message = SimpleNamespace(edit=AsyncMock())
        interaction = SimpleNamespace(
            message=source_message,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        view = app.ReportSearchView(
            requester_id=10,
            query=ReportQuery("Sherlock Holmes"),
            results=[{
                "Id": "movie-1",
                "Name": "Sherlock Holmes",
                "Type": "Movie",
                "ProductionYear": 2009,
            }],
            command_message=SimpleNamespace(),
        )

        try:
            await view.select_result(interaction, 0)
        finally:
            app.reports.resolve = previous_resolve

        interaction.response.defer.assert_awaited_once_with()
        interaction.followup.send.assert_not_awaited()
        source_message.edit.assert_awaited_once()
        self.assertIsInstance(
            source_message.edit.await_args.kwargs["view"],
            app.ReportCategoryView,
        )

    async def test_category_timeout_cannot_delete_an_in_flight_submission(self):
        app = load_app()
        target = ResolvedReportTarget(
            jellyfin_item_id="movie-1",
            media_type="movie",
            title="Sherlock Holmes",
            year="2009",
        )
        view = app.ReportCategoryView(
            requester_id=10,
            target=target,
            command_message=SimpleNamespace(),
        )
        view.message = SimpleNamespace(id=50, edit=AsyncMock())
        entered_defer = asyncio.Event()
        release_defer = asyncio.Event()

        async def blocked_defer(*args, **kwargs):
            entered_defer.set()
            await release_defer.wait()

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10),
            guild_id=20,
            channel_id=30,
            response=SimpleNamespace(
                defer=blocked_defer,
                send_message=AsyncMock(),
                is_done=lambda: False,
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        previous_cleanup = app.cleanup_unsuccessful_request
        app.cleanup_unsuccessful_request = AsyncMock()
        task = asyncio.create_task(
            view.submit_report(interaction, app.ReportCategory.WRONG_AUDIO)
        )
        try:
            await entered_defer.wait()
            self.assertTrue(view.submitting)
            await view.on_timeout()
            app.cleanup_unsuccessful_request.assert_not_awaited()
            self.assertFalse(view.finished)
        finally:
            task.cancel()
            release_defer.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            app.cleanup_unsuccessful_request = previous_cleanup


if __name__ == "__main__":
    unittest.main()
