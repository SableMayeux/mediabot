import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.test_admin_dm import FakeGuild
import app
from mediabot.core import database


class AIAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = FakeGuild(10)
        self.guild.add_member(99, "Another member")
        self.owner = SimpleNamespace(id=42, bot=False)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patches = [
            patch.object(database, "DB_PATH", str(Path(temporary.name) / "fixture.db")),
            patch.object(app, "ALLOWED_GUILD_IDS", frozenset({10})),
            patch.object(app, "ADMIN_DM_GUILDS", {}),
            patch.object(app.bot, "get_guild", Mock(return_value=self.guild)),
            patch.object(app.bot, "fetch_guild", AsyncMock(return_value=self.guild)),
            patch.object(app.bot, "is_owner", AsyncMock(side_effect=lambda user: user.id == 42)),
        ]
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)

    def context(self, name, *, guild=None, user=None):
        return SimpleNamespace(author=user or self.owner, guild=guild, channel=SimpleNamespace(id=700),
            bot=app.bot, command=app.bot.get_command(name), clean_prefix="$", reply=AsyncMock(),
            message=SimpleNamespace(id=999, mentions=[], channel=SimpleNamespace(id=700)))

    async def test_owner_controls_work_in_dm_and_server_but_reject_nonowner(self):
        for name in ("admin ai", "admin ai access", "admin ai allow", "admin ai deny", "admin ai reset", "admin ai status"):
            for guild in (None, self.guild):
                ctx = self.context(name, guild=guild)
                self.assertTrue(await ctx.command.can_run(ctx))
            ctx = self.context(name, user=SimpleNamespace(id=99, bot=False))
            with self.assertRaises(app.commands.NotOwner):
                await ctx.command.can_run(ctx)

    async def test_numeric_dm_target_permissions_persist_and_reset_independently(self):
        ctx = self.context("admin ai allow")
        await ctx.command.can_run(ctx)
        await app.admin_ai_allow.callback(ctx, "99", "desktop")
        self.assertTrue(app.ai_access.access(99)["desktop"]["allowed"])
        await app.admin_ai_deny.callback(ctx, "99", "server")
        self.assertFalse(app.ai_access.access(99)["server"]["allowed"])
        await app.admin_ai_reset.callback(ctx, "99", "server")
        self.assertEqual(app.ai_access.access(99)["server"], {"allowed": True, "source": "default"})
        self.assertTrue(app.ai_access.access(99)["desktop"]["allowed"])
        self.assertFalse(ctx.reply.await_args.kwargs["allowed_mentions"].everyone)

    async def test_owner_cannot_be_locked_out(self):
        ctx = self.context("admin ai deny")
        await ctx.command.can_run(ctx)
        await app.admin_ai_deny.callback(ctx, "42", "server")
        self.assertIn("owner always retains", ctx.reply.await_args.args[0])
        self.assertTrue(app.ai_access.access(42, owner=True)["server"]["allowed"])

    async def test_access_listing_and_status_do_not_imply_availability_grants_permission(self):
        ctx = self.context("admin ai access")
        await ctx.command.can_run(ctx)
        await app.admin_ai_access.callback(ctx, "99")
        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertEqual(embed.fields[1].value, "Denied (default)")
        self.assertIn("independent of availability", embed.footer.text)
        with patch.object(app, "local_ai", SimpleNamespace(enabled=True, status=AsyncMock(return_value={
            "server": {"ready": True, "model": "Server model", "reason": "ready"},
            "desktop": {"ready": False, "model": "Desktop model", "reason": "off"}}))):
            await app.admin_ai_status.callback(ctx)
        embed = ctx.reply.await_args.kwargs["embed"]
        self.assertIn("Unavailable", embed.fields[1].value)
        self.assertIn("Server fallback requires server access", embed.fields[-1].value)

    async def test_web_private_flag_combinations_reach_view_and_help(self):
        ctx = self.context("ask")
        message = SimpleNamespace(channel=SimpleNamespace(id=700))
        ctx.reply.return_value = message
        view = SimpleNamespace(generate=AsyncMock(), stop=Mock())
        with patch.object(app, "local_ai", SimpleNamespace(enabled=True)), \
             patch.object(app, "LocalChatView", return_value=view) as factory:
            await app.ask.callback(ctx, question="--private --web Current fixture?")
        self.assertTrue(factory.call_args.kwargs["web"])
        self.assertTrue(factory.call_args.kwargs["private"])
        view.generate.assert_awaited_once_with("Current fixture?")
        self.assertIn("800 UTF-8 bytes", app.ask.help)

    async def test_backend_flags_reach_conversation_in_dm_and_server(self):
        for guild in (None, self.guild):
            for backend in ("desktop", "server"):
                ctx = self.context("ask", guild=guild)
                ctx.reply.return_value = SimpleNamespace(channel=SimpleNamespace(id=700))
                view = SimpleNamespace(generate=AsyncMock(), stop=Mock())
                with patch.object(app, "local_ai", SimpleNamespace(enabled=True)), \
                     patch.object(app, "LocalChatView", return_value=view) as factory:
                    await app.ask.callback(ctx, question="--web --" + backend + " Current fixture?")
                self.assertEqual(factory.call_args.kwargs["backend_preference"], backend)
                self.assertTrue(factory.call_args.kwargs["web"])
                view.generate.assert_awaited_once_with("Current fixture?")
                permissions = await factory.call_args.kwargs["access_policy"]()
                self.assertTrue(permissions["desktop"])
                self.assertTrue(permissions["server"])

    async def test_conflicting_backend_flags_rejected_before_view(self):
        ctx = self.context("ask")
        with patch.object(app, "LocalChatView") as factory:
            await app.ask.callback(ctx, question="--desktop --server Question")
        factory.assert_not_called()
        self.assertIn("Choose either", ctx.reply.await_args.args[0])
        self.assertIn("no server fallback", app.ask.help)


if __name__ == "__main__":
    unittest.main()
