import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from mediabot.core.transient_store import TransientUIStore


class FakeMessage:
    def __init__(self, message_id, channel, guild):
        self.id = int(message_id)
        self.channel = channel
        self.guild = guild
        self.delete_calls = 0
        self.edit_calls = []
        self.author = SimpleNamespace(id=999)

    async def delete(self):
        self.delete_calls += 1

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)


class FakeChannel:
    def __init__(self, channel_id):
        self.id = int(channel_id)
        self.messages = {}

    async def fetch_message(self, message_id):
        return self.messages[int(message_id)]


class TransientAppIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def load_app():
        os.environ.setdefault("DISCORD_TOKEN", "test-token")
        os.environ.setdefault("SEERR_API_KEY", "test-key")
        os.environ.setdefault(
            "LOG_PATH",
            os.path.join(tempfile.gettempdir(), "mediabot-transient-app.log"),
        )
        os.environ.setdefault(
            "DB_PATH",
            os.path.join(tempfile.gettempdir(), "mediabot-transient-app.db"),
        )
        import app

        return app

    async def asyncSetUp(self):
        self.app = self.load_app()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_store = self.app.transient_ui_store
        self.previous_get_channel = self.app.bot.get_channel
        self.app.transient_ui_store = TransientUIStore(
            os.path.join(self.temp_dir.name, "transient.db")
        )

        self.guild = SimpleNamespace(id=20)
        self.channel = FakeChannel(30)
        self.command = FakeMessage(40, self.channel, self.guild)
        self.card = FakeMessage(41, self.channel, self.guild)
        self.channel.messages = {
            self.command.id: self.command,
            self.card.id: self.card,
        }
        self.app.bot.get_channel = Mock(return_value=self.channel)

    async def asyncTearDown(self):
        self.app.transient_ui_store = self.previous_store
        self.app.bot.get_channel = self.previous_get_channel
        self.temp_dir.cleanup()

    async def test_expiry_worker_deletes_registered_card_and_command(self):
        record = self.app.register_transient_card(
            message=self.card,
            command_message=self.command,
            kind="report_search",
        )
        self.assertIsNotNone(record)
        self.app.transient_ui_store.reset(
            record.entry_id,
            expires_at=time.time() - 1,
        )

        stats = await self.app.run_transient_ui_cleanup_once(
            now=time.time(),
            claim_token="integration-worker",
        )

        self.assertEqual(stats["cards"], 1)
        self.assertEqual(stats["commands"], 1)
        self.assertEqual(self.card.delete_calls, 1)
        self.assertEqual(self.command.delete_calls, 1)
        self.assertEqual(
            self.app.transient_ui_store.get(record.entry_id).state,
            "expired",
        )

    async def test_accepted_card_survives_cleanup_and_preserves_command(self):
        record = self.app.register_transient_card(
            message=self.card,
            command_message=self.command,
            kind="report_search",
        )
        self.assertIsNotNone(record)
        self.assertIsNotNone(
            self.app.mark_transient_message_terminal(self.card, "accepted")
        )

        stats = await self.app.run_transient_ui_cleanup_once(
            now=time.time() + 10_000,
            claim_token="integration-worker",
        )

        self.assertEqual(stats["claimed"], 0)
        self.assertEqual(self.card.delete_calls, 0)
        self.assertEqual(self.command.delete_calls, 0)
        self.assertEqual(
            self.app.transient_ui_store.get(record.entry_id).state,
            "accepted",
        )

    async def test_saved_rating_actions_expire_to_static_kept_card(self):
        record = self.app.register_transient_card(
            message=self.card,
            command_message=self.command,
            kind="rating_search",
        )
        self.assertTrue(
            self.app.transient_ui_store.transition_card(
                channel_id=self.channel.id,
                card_message_id=self.card.id,
                kind="rating_saved_actions",
                expires_at=time.time() - 1,
            )
        )

        stats = await self.app.run_transient_ui_cleanup_once(
            now=time.time(),
            claim_token="rating-worker",
        )

        self.assertEqual(stats["preserved"], 1)
        self.assertEqual(self.card.delete_calls, 0)
        self.assertEqual(self.card.edit_calls, [{"view": None}])
        self.assertEqual(self.command.delete_calls, 0)
        self.assertEqual(
            self.app.transient_ui_store.get(record.entry_id).state,
            "kept",
        )

    async def test_saved_event_vote_expires_to_static_kept_receipt(self):
        record = self.app.register_transient_card(
            message=self.card,
            command_message=self.command,
            kind="event_vote",
        )
        self.assertTrue(
            self.app.transient_ui_store.transition_card(
                channel_id=self.channel.id,
                card_message_id=self.card.id,
                kind="event_vote_saved_actions",
                expires_at=time.time() - 1,
            )
        )

        stats = await self.app.run_transient_ui_cleanup_once(
            now=time.time(),
            claim_token="event-vote-worker",
        )

        self.assertEqual(stats["preserved"], 1)
        self.assertEqual(self.card.delete_calls, 0)
        self.assertEqual(self.card.edit_calls, [{"view": None}])
        self.assertEqual(self.command.delete_calls, 0)
        self.assertEqual(
            self.app.transient_ui_store.get(record.entry_id).state,
            "kept",
        )

    async def test_batch_command_waits_until_every_card_is_expired(self):
        second = FakeMessage(42, self.channel, self.guild)
        self.channel.messages[second.id] = second
        batch_id = "recommendation:30:40"
        first_record = self.app.register_transient_card(
            message=self.card,
            command_message=self.command,
            kind="recommendation",
            batch_id=batch_id,
            expected_batch_size=2,
        )
        second_record = self.app.register_transient_card(
            message=second,
            command_message=self.command,
            kind="recommendation",
            batch_id=batch_id,
            expected_batch_size=2,
        )
        now = time.time()
        self.app.transient_ui_store.reset(
            first_record.entry_id,
            expires_at=now - 1,
        )

        first_stats = await self.app.run_transient_ui_cleanup_once(
            now=now,
            claim_token="batch-worker-one",
        )
        self.assertEqual(first_stats["cards"], 1)
        self.assertEqual(self.command.delete_calls, 0)

        self.app.transient_ui_store.reset(
            second_record.entry_id,
            expires_at=now - 1,
        )
        second_stats = await self.app.run_transient_ui_cleanup_once(
            now=now,
            claim_token="batch-worker-two",
        )
        self.assertEqual(second_stats["cards"], 1)
        self.assertEqual(self.command.delete_calls, 1)

    async def test_failed_command_delete_is_retried_without_active_card(self):
        record = self.app.register_transient_card(
            message=self.card,
            command_message=self.command,
            kind="music_search",
        )
        now = time.time()
        self.app.transient_ui_store.reset(
            record.entry_id,
            expires_at=now - 1,
        )
        previous_delete_by_id = self.app.delete_discord_message_by_id
        delete_by_id = AsyncMock(side_effect=[False, True])
        self.app.delete_discord_message_by_id = delete_by_id
        try:
            first_stats = await self.app.run_transient_ui_cleanup_once(
                now=now,
                claim_token="retry-worker-one",
            )
            second_stats = await self.app.run_transient_ui_cleanup_once(
                now=now + 31,
                claim_token="retry-worker-two",
            )
        finally:
            self.app.delete_discord_message_by_id = previous_delete_by_id

        self.assertEqual(first_stats["cards"], 1)
        self.assertEqual(first_stats["commands"], 0)
        self.assertEqual(second_stats["commands"], 1)
        self.assertEqual(delete_by_id.await_count, 2)

    async def test_authorized_search_interaction_renews_expiry(self):
        record = self.app.register_transient_card(
            message=self.card,
            command_message=self.command,
            kind="media_search",
        )
        old_expiry = record.expires_at
        view = self.app.SearchResultsView(
            requester_id=123,
            query="test",
            results=[],
            seerr_page=1,
            total_seerr_pages=1,
            command_message=self.command,
        )
        view.message = self.card
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            message=self.card,
        )

        self.assertTrue(await view.interaction_check(interaction))
        self.assertGreater(
            self.app.transient_ui_store.get(record.entry_id).expires_at,
            old_expiry - 1,
        )

    async def test_orphan_component_deletes_registered_card_and_command(self):
        record = self.app.register_transient_card(
            message=self.card,
            command_message=self.command,
            kind="report_search",
        )
        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            type=self.app.discord.InteractionType.component,
            response=response,
            message=self.card,
        )
        previous_sleep = self.app.asyncio.sleep
        previous_user = self.app.bot._connection.user
        self.app.asyncio.sleep = AsyncMock()
        self.app.bot._connection.user = SimpleNamespace(id=999)
        try:
            await self.app.on_interaction(interaction)
        finally:
            self.app.asyncio.sleep = previous_sleep
            self.app.bot._connection.user = previous_user

        response.send_message.assert_awaited_once()
        self.assertEqual(self.card.delete_calls, 1)
        self.assertEqual(self.command.delete_calls, 1)
        self.assertEqual(
            self.app.transient_ui_store.get(record.entry_id).state,
            "dismissed",
        )

    async def test_on_ready_starts_cleanup_watcher_only_once(self):
        previous_watcher = self.app.transient_ui_cleanup_watcher
        previous_runtime_watcher = self.app.runtime_health_watcher
        previous_health_writer = self.app.write_runtime_health_snapshot
        previous_jellyfin = self.app.jellyfin
        previous_user = self.app.bot._connection.user
        watcher = SimpleNamespace(
            is_running=Mock(side_effect=[False, True]),
            start=Mock(),
        )
        runtime_watcher = SimpleNamespace(
            is_running=Mock(side_effect=[False, True]),
            start=Mock(),
        )
        self.app.transient_ui_cleanup_watcher = watcher
        self.app.runtime_health_watcher = runtime_watcher
        self.app.write_runtime_health_snapshot = Mock()
        self.app.jellyfin = SimpleNamespace(enabled=False)
        self.app.bot._connection.user = SimpleNamespace(id=999)
        try:
            await self.app.on_ready()
            await self.app.on_ready()
        finally:
            self.app.transient_ui_cleanup_watcher = previous_watcher
            self.app.runtime_health_watcher = previous_runtime_watcher
            self.app.write_runtime_health_snapshot = previous_health_writer
            self.app.jellyfin = previous_jellyfin
            self.app.bot._connection.user = previous_user

        watcher.start.assert_called_once_with()
        runtime_watcher.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
