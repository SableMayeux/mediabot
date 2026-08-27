import asyncio
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("SEERR_API_KEY", "test-key")
os.environ.setdefault("LOG_PATH", str(Path(tempfile.gettempdir()) / "mediabot-test.log"))
os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "mediabot-test.db"))

import app


class RuntimeHardeningTests(unittest.IsolatedAsyncioTestCase):
    class TypingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def test_untrusted_guild_is_blocked_and_rejected(self):
        previous = app.ALLOWED_GUILD_IDS
        app.ALLOWED_GUILD_IDS = frozenset({10})
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=11),
            author=SimpleNamespace(id=12),
            command="request",
        )
        guild = SimpleNamespace(id=11, name="Untrusted", leave=AsyncMock())
        try:
            with self.assertRaisesRegex(app.commands.CheckFailure, "not configured"):
                await app.enforce_allowed_guild(ctx)
            await app.on_guild_join(guild)
        finally:
            app.ALLOWED_GUILD_IDS = previous

        guild.leave.assert_awaited_once_with()
        self.assertGreaterEqual(len(app.admin_users.checks), 2)
        self.assertGreaterEqual(len(app.admin_link.checks), 2)

    async def test_allowed_guild_passes_and_direct_messages_are_blocked(self):
        previous = app.ALLOWED_GUILD_IDS
        app.ALLOWED_GUILD_IDS = frozenset({10})
        try:
            with self.assertRaises(app.commands.NoPrivateMessage):
                await app.enforce_allowed_guild(SimpleNamespace(guild=None))
            self.assertTrue(
                await app.enforce_allowed_guild(SimpleNamespace(guild=SimpleNamespace(id=10)))
            )
        finally:
            app.ALLOWED_GUILD_IDS = previous

    async def test_semantic_resolver_shares_global_concurrency_and_cache(self):
        class FakeSeerr:
            def __init__(self):
                self.active = 0
                self.maximum = 0
                self.calls = 0

            async def request(self, method, path):
                self.calls += 1
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                try:
                    await asyncio.sleep(0.01)
                    return {"keywords": [{"id": 9840, "name": "romance"}]}
                finally:
                    self.active -= 1

        previous_seerr = app.seerr
        previous_semaphore = app.SEMANTIC_RESOLVER_SEMAPHORE
        previous_cache = app.SEMANTIC_GENRE_CACHE
        fake = FakeSeerr()
        app.seerr = fake
        app.SEMANTIC_RESOLVER_SEMAPHORE = asyncio.Semaphore(3)
        app.SEMANTIC_GENRE_CACHE = {}
        items = [
            {
                "Id": f"item-{index}",
                "Type": "Series",
                "ProviderIds": {"Tmdb": str(1000 + index)},
            }
            for index in range(20)
        ]
        try:
            first = await asyncio.gather(
                *(app.available_semantic_genres(item) for item in items[:10]),
                *(app.available_semantic_genres(item) for item in items[10:]),
            )
            second = await asyncio.gather(
                *(app.available_semantic_genres(item) for item in items)
            )
        finally:
            app.seerr = previous_seerr
            app.SEMANTIC_RESOLVER_SEMAPHORE = previous_semaphore
            app.SEMANTIC_GENRE_CACHE = previous_cache

        self.assertTrue(all(result == {"Romance"} for result in first + second))
        self.assertLessEqual(fake.maximum, 3)
        self.assertEqual(fake.calls, 20)

    async def test_log_formatter_redacts_message_and_traceback_secrets(self):
        try:
            raise ValueError("SONARR_API_KEY=not-a-real-secret")
        except ValueError:
            record = logging.LogRecord(
                "mediabot",
                logging.ERROR,
                __file__,
                1,
                "Authorization: bearer-value",
                (),
                __import__("sys").exc_info(),
            )

        rendered = app.RedactingFormatter("%(message)s").format(record)
        self.assertNotIn("not-a-real-secret", rendered)
        self.assertNotIn("bearer-value", rendered)
        self.assertIn("[REDACTED]", rendered)

    async def test_recommendation_survives_optional_jellyfin_taste_failure(self):
        previous_taste = app.configured_taste_user
        previous_ratings = app.ratings_for_user
        previous_recommendations = app.recommendations
        app.configured_taste_user = AsyncMock(side_effect=RuntimeError("offline"))
        app.ratings_for_user = Mock(return_value=[])
        app.recommendations = SimpleNamespace(recommend=AsyncMock(return_value=None))
        ctx = SimpleNamespace(
            invoked_with="recommend",
            author=SimpleNamespace(id=1),
            typing=lambda: self.TypingContext(),
            reply=AsyncMock(),
        )
        try:
            await app.recommend_media.callback(ctx, filters="movie Comedy")
        finally:
            app.configured_taste_user = previous_taste
            app.ratings_for_user = previous_ratings
            app.recommendations = previous_recommendations

        ctx.reply.assert_awaited_once_with(
            "No unseen, unrated, requestable recommendations matched."
        )

    async def test_status_falls_through_when_jellyfin_is_unavailable(self):
        previous_jellyfin = app.jellyfin
        previous_library = app.library
        previous_latest = app.latest_media_request
        previous_music = app.reply_with_music_status
        previous_search = app.resolve_seerr_search_query
        app.jellyfin = SimpleNamespace(enabled=True)
        app.library = SimpleNamespace(search=AsyncMock(side_effect=RuntimeError("offline")))
        app.latest_media_request = Mock(return_value=None)
        app.reply_with_music_status = AsyncMock(return_value=False)
        app.resolve_seerr_search_query = AsyncMock(
            return_value=(
                "Alien",
                "1979",
                [{"id": 348, "mediaType": "movie", "title": "Alien", "releaseDate": "1979-05-25"}],
                1,
            )
        )
        ctx = SimpleNamespace(author=SimpleNamespace(id=1), reply=AsyncMock())
        try:
            await app.status.callback(ctx, query="Alien 1979")
        finally:
            app.jellyfin = previous_jellyfin
            app.library = previous_library
            app.latest_media_request = previous_latest
            app.reply_with_music_status = previous_music
            app.resolve_seerr_search_query = previous_search

        response = ctx.reply.await_args.args[0]
        self.assertIn("Seerr status", response)
        self.assertIn("Jellyfin was unavailable", response)


if __name__ == "__main__":
    unittest.main()
