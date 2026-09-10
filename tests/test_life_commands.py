import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("SEERR_API_KEY", "test-key")
os.environ.setdefault("LOG_PATH", str(Path(tempfile.gettempdir()) / "mediabot-test.log"))
os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "mediabot-test.db"))

import app


class PrivateLifeCommandTests(unittest.IsolatedAsyncioTestCase):
    def context(self, guild=None):
        return SimpleNamespace(guild=guild, author=SimpleNamespace(id=12, send=AsyncMock()),
            message=Mock(), send=AsyncMock(), reply=AsyncMock(), channel=SimpleNamespace(id=20))

    async def test_ask_uses_private_delivery_and_does_not_capture(self):
        ctx = self.context(SimpleNamespace(id=10))
        view = SimpleNamespace(generate=AsyncMock(), stop=Mock())
        with patch.object(app, "LocalChatView", return_value=view), \
             patch.object(app, "local_ai", SimpleNamespace(enabled=True)), \
             patch.object(app, "delete_message_safely", AsyncMock(return_value=True)) as delete, \
             patch.object(app, "life_capture") as capture:
            await app.ask.callback(ctx, question="private fixture")
        delete.assert_awaited_once()
        ctx.author.send.assert_awaited_once()
        ctx.send.assert_not_awaited()
        ctx.reply.assert_not_awaited()
        view.generate.assert_awaited_once_with("private fixture")
        capture.capture.assert_not_called()

    async def test_failed_source_deletion_prevents_inference(self):
        ctx = self.context(SimpleNamespace(id=10))
        with patch.object(app, "delete_message_safely", AsyncMock(return_value=False)), \
             patch.object(app, "LocalChatView") as factory:
            await app.ask.callback(ctx, question="private fixture")
        factory.assert_not_called()
        ctx.author.send.assert_not_awaited()
        self.assertNotIn("private fixture", str(ctx.send.call_args))

    async def test_blocked_dm_has_no_public_answer_fallback(self):
        ctx = self.context()
        ctx.author.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "closed")
        view = SimpleNamespace(generate=AsyncMock(), stop=Mock())
        with patch.object(app, "LocalChatView", return_value=view), \
             patch.object(app, "local_ai", SimpleNamespace(enabled=True)):
            await app.ask.callback(ctx, question="private fixture")
        view.generate.assert_not_awaited()
        view.stop.assert_called_once()
        self.assertNotIn("private fixture", str(ctx.send.call_args))

    async def test_owner_dm_commands_and_nonowner_rejection(self):
        ctx = self.context()
        for command in (app.life, app.ask, app.think):
            ctx.command = command
            with patch.object(app.bot, "is_owner", AsyncMock(return_value=True)):
                self.assertTrue(await app.enforce_allowed_guild(ctx))
            with patch.object(app.bot, "is_owner", AsyncMock(return_value=False)):
                with self.assertRaises(app.commands.NoPrivateMessage):
                    await app.enforce_allowed_guild(ctx)
            self.assertTrue(command.checks)

    async def test_raw_capture_survives_disabled_gateway(self):
        ctx = self.context()
        captured = SimpleNamespace(capture_id="fixture-id", title="fixture", created_at="2026-09-10T12:00:00Z")
        capture = SimpleNamespace(capture=Mock(return_value=captured))
        with patch.object(app, "life_capture", capture), \
             patch.object(app, "life_workflow", SimpleNamespace(enabled=False)):
            await app.think.callback(ctx, thought="unaltered raw fixture")
        capture.capture.assert_called_once()
        self.assertEqual(capture.capture.call_args.args, ("unaltered raw fixture",))
        self.assertNotIn("view", ctx.reply.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
