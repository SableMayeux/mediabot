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
        message = SimpleNamespace(guild=guild, channel=SimpleNamespace(id=20), edit=AsyncMock())
        return SimpleNamespace(guild=guild, author=SimpleNamespace(id=12, send=AsyncMock(return_value=message)),
            message=Mock(), send=AsyncMock(), reply=AsyncMock(return_value=message), channel=SimpleNamespace(id=20), clean_prefix="$")

    async def test_ask_uses_private_delivery_and_does_not_capture(self):
        ctx = self.context(SimpleNamespace(id=10))
        view = SimpleNamespace(generate=AsyncMock(), stop=Mock())
        with patch.object(app, "LocalChatView", return_value=view), \
             patch.object(app, "local_ai", SimpleNamespace(enabled=True)), \
             patch.object(app, "delete_message_safely", AsyncMock(return_value=True)) as delete, \
             patch.object(app, "life_capture") as capture:
            await app.ask.callback(ctx, question="--private private fixture")
        delete.assert_awaited_once()
        ctx.author.send.assert_awaited_once()
        ctx.send.assert_not_awaited()
        ctx.reply.assert_not_awaited()
        view.generate.assert_awaited_once_with("private fixture")
        capture.capture.assert_not_called()

    async def test_failed_source_deletion_prevents_inference(self):
        ctx = self.context(SimpleNamespace(id=10))
        view = SimpleNamespace(generate=AsyncMock(), stop=Mock())
        with patch.object(app, "delete_message_safely", AsyncMock(return_value=False)), \
             patch.object(app, "local_ai", SimpleNamespace(enabled=True)), \
             patch.object(app, "LocalChatView", return_value=view):
            await app.ask.callback(ctx, question="--private private fixture")
        view.generate.assert_not_awaited()
        ctx.author.send.assert_awaited_once()
        self.assertEqual(ctx.author.send.call_args.kwargs["embed"].fields[0].value, "private fixture")
        self.assertNotIn("private fixture", str(ctx.send.call_args))

    async def test_blocked_dm_has_no_public_answer_fallback(self):
        ctx = self.context(SimpleNamespace(id=10))
        ctx.author.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "closed")
        view = SimpleNamespace(generate=AsyncMock(), stop=Mock())
        with patch.object(app, "LocalChatView", return_value=view), \
             patch.object(app, "delete_message_safely", AsyncMock(return_value=True)), \
             patch.object(app, "local_ai", SimpleNamespace(enabled=True)):
            await app.ask.callback(ctx, question="--private private fixture")
        view.generate.assert_not_awaited()
        view.stop.assert_called_once()
        self.assertNotIn("private fixture", str(ctx.send.call_args))

    async def test_owner_dm_commands_and_nonowner_rejection(self):
        ctx = self.context()
        for command in (app.life, app.think):
            ctx.command = command
            with patch.object(app.bot, "is_owner", AsyncMock(return_value=True)):
                self.assertTrue(await app.enforce_allowed_guild(ctx))
            with patch.object(app.bot, "is_owner", AsyncMock(return_value=False)):
                with self.assertRaises(app.commands.NoPrivateMessage):
                    await app.enforce_allowed_guild(ctx)
            self.assertTrue(command.checks)

    async def test_public_ask_keeps_source_and_replies_here_without_capture(self):
        ctx = self.context(SimpleNamespace(id=10))
        view = SimpleNamespace(generate=AsyncMock(), stop=Mock())
        with patch.object(app, "LocalChatView", return_value=view) as factory, \
             patch.object(app, "local_ai", SimpleNamespace(enabled=True)), \
             patch.object(app, "delete_message_safely", AsyncMock()) as delete, \
             patch.object(app, "life_capture") as capture:
            await app.ask.callback(ctx, question="public fixture")
        delete.assert_not_awaited()
        ctx.author.send.assert_not_awaited()
        ctx.reply.assert_awaited_once()
        self.assertFalse(factory.call_args.kwargs["private"])
        self.assertEqual(factory.call_args.kwargs["guild_id"], 10)
        self.assertEqual(view.channel_id, 20)
        view.generate.assert_awaited_once_with("public fixture")
        capture.capture.assert_not_called()

    async def test_dm_ask_replies_in_original_dm(self):
        ctx = self.context()
        view = SimpleNamespace(generate=AsyncMock(), stop=Mock())
        with patch.object(app, "LocalChatView", return_value=view) as factory, \
             patch.object(app, "local_ai", SimpleNamespace(enabled=True)):
            await app.ask.callback(ctx, question="fixture")
        ctx.reply.assert_awaited_once()
        ctx.author.send.assert_not_awaited()
        self.assertTrue(factory.call_args.kwargs["private"])
        view.generate.assert_awaited_once_with("fixture")

    async def test_ask_requires_current_membership_and_limits_new_requests(self):
        ctx = self.context()
        with patch.object(app, "allowed_chat_user", AsyncMock(return_value=False)):
            with self.assertRaises(app.commands.CheckFailure):
                await app.local_chat_command_allowed(ctx)
        with patch.object(app, "allowed_chat_user", AsyncMock(return_value=True)):
            self.assertTrue(await app.local_chat_command_allowed(ctx))
        self.assertIn(app.local_chat_command_allowed, app.ask.checks)
        self.assertTrue(app.ask._buckets.valid)

    async def test_nonowner_dm_help_lists_chat_but_no_private_life(self):
        ctx = self.context()
        ctx.command = app.mediabot_help
        with patch.object(app.bot, "is_owner", AsyncMock(return_value=False)):
            self.assertTrue(await app.enforce_allowed_guild(ctx))
            await app.mediabot_help.callback(ctx)
        data = str(ctx.reply.call_args.kwargs["embed"].to_dict())
        self.assertIn("$ask", data)
        self.assertNotIn("$think", data)
        self.assertNotIn("$life", data)

    async def test_owner_dm_help_includes_life_but_explains_guild_commands(self):
        ctx = self.context()
        with patch.object(app.bot, "is_owner", AsyncMock(return_value=True)):
            await app.mediabot_help.callback(ctx)
            self.assertIn("$life tasks", str(ctx.reply.call_args.kwargs["embed"].to_dict()))
            await app.mediabot_help.callback(ctx, topic="torrent")
            self.assertIn("only in the configured server", ctx.reply.call_args.args[0])

    async def test_help_matches_auto_features_and_admin_owner_boundaries(self):
        ctx=self.context(SimpleNamespace(id=10))
        ctx.author.guild_permissions=SimpleNamespace(administrator=True)
        with patch.object(app.bot, 'is_owner', AsyncMock(return_value=False)):
            await app.mediabot_help.callback(ctx,topic='admin')
            rendered=str(ctx.reply.call_args.kwargs['embed'].to_dict())
            self.assertNotIn('$admin logs',rendered)
            self.assertNotIn('$admin link',rendered)
            self.assertIn('$admin reports',rendered)
            await app.mediabot_help.callback(ctx,topic='admin logs')
            self.assertIn('not available',ctx.reply.call_args.args[0])
            await app.mediabot_help.callback(ctx,topic='recommend')
            self.assertIn('--auto',str(ctx.reply.call_args.kwargs['embed'].to_dict()))
        ctx=self.context()
        with patch.object(app.bot,'is_owner',AsyncMock(return_value=True)):
            await app.mediabot_help.callback(ctx)
            rendered=str(ctx.reply.call_args.kwargs['embed'].to_dict())
            self.assertIn('--auto attempts one source-quoted task',rendered)
            self.assertIn('currently require the configured server',rendered)

    async def test_complete_owner_help_fits_discord_embed_limits(self):
        ctx=self.context(SimpleNamespace(id=10))
        ctx.author.guild_permissions=SimpleNamespace(administrator=True)
        with patch.object(app.bot,'is_owner',AsyncMock(return_value=True)):
            await app.mediabot_help.callback(ctx,topic='all')
        for call in ctx.reply.call_args_list:
            embed=call.kwargs['embed']
            self.assertLessEqual(len(embed),6000)
            self.assertLessEqual(len(embed.fields),25)
            self.assertTrue(all(len(field.value)<=1024 for field in embed.fields))

    async def test_flag_without_question_only_shows_usage(self):
        ctx = self.context()
        with patch.object(app, "LocalChatView") as factory:
            await app.ask.callback(ctx, question="--private")
        factory.assert_not_called()
        self.assertIn("$ask", ctx.reply.call_args.args[0])

    async def test_untrusted_guild_remains_denied(self):
        ctx = self.context(SimpleNamespace(id=99))
        ctx.command = app.ask
        with patch.object(app, "ALLOWED_GUILD_IDS", frozenset({10})):
            with self.assertRaises(app.commands.CheckFailure):
                await app.enforce_allowed_guild(ctx)

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

    async def test_auto_always_saves_raw_thought_before_classification(self):
        ctx=self.context(); events=[]
        capture=SimpleNamespace(capture=Mock(side_effect=lambda *a,**k: (events.append('saved'),SimpleNamespace(capture_id='abc'))[1]))
        async def promote(*a,**k):
            self.assertEqual(events,['saved']);events.append('classified')
            self.assertEqual(k['thought'],'Call the mechanic')
        with patch.object(app,'life_capture',capture),patch.object(app,'promote_automatically',side_effect=promote):
            await app.think.callback(ctx,thought='--auto Call the mechanic')
        self.assertEqual(events,['saved','classified'])
        self.assertEqual(capture.capture.call_args.args,('Call the mechanic',))

    async def test_failed_capture_never_reaches_auto_model(self):
        ctx=self.context()
        with patch.object(app,'life_capture',SimpleNamespace(capture=Mock(side_effect=app.LifeCaptureError('Unable to save')))),patch.object(app,'promote_automatically',AsyncMock()) as promote:
            await app.think.callback(ctx,thought='--auto Call the mechanic')
        promote.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
