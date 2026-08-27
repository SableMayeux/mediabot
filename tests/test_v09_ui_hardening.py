import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("SEERR_API_KEY", "test-key")
os.environ.setdefault("LOG_PATH", str(Path(tempfile.gettempdir()) / "mediabot-v09-ui.log"))
os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "mediabot-v09-ui.db"))

import app
from mediabot.core import database
from mediabot.providers.soulsync import SoulSyncError


class StatefulResponse:
    def __init__(self):
        self.done = False
        self.defer = AsyncMock(side_effect=self._defer)
        self.send_message = AsyncMock(side_effect=self._send)
        self.edit_message = AsyncMock(side_effect=self._edit)

    async def _defer(self, *args, **kwargs):
        self.done = True

    async def _send(self, *args, **kwargs):
        self.done = True

    async def _edit(self, *args, **kwargs):
        self.done = True

    def is_done(self):
        return self.done


def interaction_for(*, user_id=1, guild_id=2, followup=None):
    message = SimpleNamespace(id=44, edit=AsyncMock())
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Tester"),
        guild_id=guild_id,
        channel_id=3,
        message=message,
        response=StatefulResponse(),
        followup=SimpleNamespace(send=followup or AsyncMock()),
    )


class DiscordBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_mentions_are_disabled_globally(self):
        allowed = app.bot.allowed_mentions
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.users)
        self.assertFalse(allowed.roles)
        self.assertFalse(allowed.replied_user)

    async def test_private_delivery_failure_never_posts_sensitive_payload(self):
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=1, send=AsyncMock(side_effect=RuntimeError("closed"))),
            guild=SimpleNamespace(id=2),
            reply=AsyncMock(),
        )
        secret = "SECRET_SENTINEL"
        result = await app.send_private_output(
            ctx,
            content=secret,
            public_success="sent",
            committed=True,
        )
        self.assertFalse(result)
        public_text = " ".join(str(value) for value in ctx.reply.await_args.args)
        self.assertNotIn(secret, public_text)
        self.assertIn("change was saved", public_text)

    async def test_provider_error_body_is_not_reflected_by_music_command(self):
        previous = app.soulsync
        app.soulsync = SimpleNamespace(
            enabled=True,
            search_tracks=AsyncMock(
                side_effect=SoulSyncError("SECRET_SENTINEL", status=500, definitive=False)
            ),
        )

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        ctx = SimpleNamespace(typing=lambda: Typing(), reply=AsyncMock())
        try:
            await app.music_request.callback(ctx, query="malicious query")
        finally:
            app.soulsync = previous

        public_text = " ".join(str(value) for value in ctx.reply.await_args.args)
        self.assertNotIn("SECRET_SENTINEL", public_text)
        self.assertIn("Error ID", public_text)


class SelectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_search(self):
        view = app.SearchResultsView(
            requester_id=1,
            query="Example",
            results=[{
                "id": 10,
                "mediaType": "movie",
                "title": "Example",
                "releaseDate": "2020-01-01",
            }],
            seerr_page=1,
            total_seerr_pages=1,
            command_message=SimpleNamespace(id=55),
        )
        return view

    async def test_detail_failure_restores_search_for_retry(self):
        view = self.make_search()
        interaction = interaction_for()
        view.message = interaction.message
        previous_blocked = app.is_blocklisted
        previous_details = app.fetch_media_details
        app.is_blocklisted = Mock(return_value=True)
        app.fetch_media_details = AsyncMock(side_effect=RuntimeError("offline"))
        try:
            await view.select_slot(interaction, 0)
        finally:
            app.is_blocklisted = previous_blocked
            app.fetch_media_details = previous_details

        self.assertFalse(view.finished)
        self.assertFalse(view.selecting)
        interaction.message.edit.assert_awaited()
        self.assertIn("still open", interaction.followup.send.await_args.args[0])

    async def test_confirmation_send_failure_restores_search(self):
        view = self.make_search()
        failure_then_notice = AsyncMock(side_effect=[RuntimeError("discord"), None])
        interaction = interaction_for(followup=failure_then_notice)
        view.message = interaction.message
        previous = (
            app.is_blocklisted,
            app.already_available,
            app.already_underway,
            app.get_link,
            app.fetch_media_details,
        )
        app.is_blocklisted = Mock(return_value=False)
        app.already_available = Mock(return_value=False)
        app.already_underway = Mock(return_value=False)
        app.get_link = Mock(return_value={"seerr_user_id": 4, "seerr_username": "tester"})
        app.fetch_media_details = AsyncMock(return_value={"overview": "Details"})
        try:
            await view.select_slot(interaction, 0)
        finally:
            (
                app.is_blocklisted,
                app.already_available,
                app.already_underway,
                app.get_link,
                app.fetch_media_details,
            ) = previous

        self.assertFalse(view.finished)
        self.assertFalse(view.selecting)
        self.assertEqual(interaction.message.edit.await_count, 2)


class SeasonAndMusicScopeTests(unittest.IsolatedAsyncioTestCase):
    def make_season_view(self):
        return app.SeasonSelectionView(
            requester_id=1,
            item={"id": 1, "mediaType": "tv", "name": "Long Show"},
            details={"id": 1, "mediaType": "tv", "name": "Long Show"},
            seerr_user_id=2,
            seerr_username="tester",
            origin_message=SimpleNamespace(id=40),
            seasons=range(1, 61),
            season_catalog={season: 10 for season in range(1, 91)},
            blocked_seasons={season: "Available" for season in range(61, 91)},
            missing_episodes={1: (7, 8), 2: tuple(range(1, 11))},
            direct_episode_requests={1: (7, 8)},
            command_message=SimpleNamespace(id=41),
        )

    async def test_long_season_selector_and_confirmation_preserve_scope(self):
        view = self.make_season_view()
        embed = view.build_embed()
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertLessEqual(len(embed.fields), 25)
        self.assertLessEqual(len(view.children[0].options), 25)
        rendered = "\n".join(field.value for field in embed.fields)
        self.assertIn("Repair - missing E7-E8", rendered)
        self.assertIn("Empty - request E1-E10", rendered)

        view.selected = {1, 2}
        interaction = interaction_for()
        await view.continue_request(interaction)
        confirmation = interaction.response.edit_message.await_args.kwargs["embed"]
        scope = "\n".join(field.value for field in confirmation.fields)
        self.assertIn("S1 exact repair", scope)
        self.assertIn("E7-E8", scope)
        self.assertIn("S2 whole season", scope)
        self.assertTrue(all(len(field.value) <= 1024 for field in confirmation.fields))

    async def test_music_timeout_is_terminal_and_artist_prevents_false_dedupe(self):
        self.assertTrue(
            app.music_track_matches(
                {"name": "Home", "artist": "Edward Sharpe & The Magnetic Zeros"},
                title="Home",
                artist="Edward Sharpe & The Magnetic Zeros",
            )
        )
        self.assertFalse(
            app.music_track_matches(
                {"name": "Home", "artist": "Phillip Phillips"},
                title="Home",
                artist="Edward Sharpe & The Magnetic Zeros",
            )
        )

        view = app.ConfirmMusicRequestView(
            requester_id=1,
            track={"name": "Home", "artists": ["Artist"]},
            origin_message=SimpleNamespace(id=1),
            command_message=SimpleNamespace(id=2),
        )
        previous_cleanup = app.cleanup_unsuccessful_request
        app.cleanup_unsuccessful_request = AsyncMock()
        try:
            await view.on_timeout()
        finally:
            cleanup = app.cleanup_unsuccessful_request
            app.cleanup_unsuccessful_request = previous_cleanup
        self.assertTrue(view.finished)
        self.assertTrue(view.submitting)
        cleanup.assert_awaited_once()


class ShutdownAndRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_drains_active_submission_before_client_close(self):
        order = []
        closed = asyncio.Event()

        async def submission():
            await asyncio.sleep(0.01)
            order.append("submission")

        class Client:
            async def start(self, token):
                await closed.wait()

            async def close(self):
                order.append("close")
                closed.set()

        task = asyncio.create_task(submission())
        previous = set(app.ACTIVE_MEDIA_SUBMISSIONS)
        app.ACTIVE_MEDIA_SUBMISSIONS.clear()
        app.ACTIVE_MEDIA_SUBMISSIONS.add(task)
        shutdown = asyncio.Event()
        shutdown.set()
        try:
            await app.run_client_until_shutdown(Client(), "token", shutdown)
        finally:
            app.ACTIVE_MEDIA_SUBMISSIONS.clear()
            app.ACTIVE_MEDIA_SUBMISSIONS.update(previous)
        self.assertEqual(order, ["submission", "close"])

    async def test_accepted_intent_recovery_materializes_tracking_row(self):
        temp_dir = tempfile.TemporaryDirectory()
        previous_path = database.DB_PATH
        database.DB_PATH = str(Path(temp_dir.name) / "mediabot.db")
        try:
            database.init_tracking_db()
            database.begin_media_request_intent(
                intent_id="recover-me",
                media_type="tv",
                tmdb_id=10,
                title="Example",
                year="2020",
                requester_discord_id=1,
                discord_guild_id=2,
                discord_channel_id=3,
                discord_message_id=4,
                direct_tracking_id=-99,
                requested_seasons=[7],
                requested_episode_counts={7: 8},
                requested_episode_numbers={7: range(1, 9)},
            )
            database.record_media_request_acceptance(
                intent_id="recover-me",
                seerr_request_id=99,
                accepted_seasons=[7],
                accepted_episode_counts={7: 8},
                accepted_episode_numbers={7: range(1, 9)},
                request_status="Approved",
            )
            result = app.recover_accepted_media_request_intents()
            row = database.request_by_id(99, discord_guild_id=2)
            self.assertEqual(result, {"recovered": 1, "failed": 0})
            self.assertEqual(row["title"], "Example")
            self.assertEqual(database.media_request_intent_stats()["tracked"], 1)
        finally:
            database.DB_PATH = previous_path
            temp_dir.cleanup()


class EventEmbedLimitTests(unittest.TestCase):
    def test_long_lineup_is_split_into_valid_complete_embeds(self):
        lines = [f"slot-{index:02d} " + ("x" * 220) for index in range(1, 32)]
        embeds = app.build_event_line_embeds(
            title="Tonight",
            description="All scheduled media",
            field_name="Lineup",
            lines=lines,
        )
        rendered = "\n".join(
            field.value for embed in embeds for field in embed.fields
        )
        self.assertGreater(len(embeds), 1)
        self.assertTrue(all(f"slot-{index:02d}" in rendered for index in range(1, 32)))
        self.assertTrue(all(len(embed) <= 6000 for embed in embeds))
        self.assertTrue(all(len(embed.fields) <= 25 for embed in embeds))
        self.assertTrue(
            all(len(field.value) <= 1024 for embed in embeds for field in embed.fields)
        )


if __name__ == "__main__":
    unittest.main()
