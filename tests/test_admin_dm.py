import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext.commands.view import StringView

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("SEERR_API_KEY", "test-key")
os.environ.setdefault("LOG_PATH", str(Path(tempfile.gettempdir()) / "mediabot-admin-dm-test.log"))
os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "mediabot-admin-dm-test.db"))
import app


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "missing")


class FakeGuild:
    def __init__(self, identity, administrator=True):
        self.id, self.name = identity, "Test server " + str(identity)
        self.members_by_id = {}
        self.add_member(42, "Operator", administrator=administrator)
        self.fetch_member = AsyncMock(side_effect=self.member)

    def add_member(self, identity, name, *, administrator=False, global_name=None):
        result = SimpleNamespace(id=identity, name=name, display_name=name, global_name=global_name,
            guild=self, bot=False, guild_permissions=SimpleNamespace(administrator=administrator))
        self.members_by_id[identity] = result
        return result

    async def member(self, identity):
        if identity not in self.members_by_id:
            raise not_found()
        return self.members_by_id[identity]

    async def fetch_members(self, limit=None):
        for member in self.members_by_id.values():
            yield member


class AdminDMTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guilds = {10: FakeGuild(10)}
        self.user = SimpleNamespace(id=42, bot=False, send=AsyncMock())
        self.patches = [
            patch.object(app, "ALLOWED_GUILD_IDS", frozenset({10})),
            patch.object(app, "ADMIN_DM_GUILDS", {}),
            patch.object(app.bot, "get_guild", Mock(side_effect=lambda identity: self.guilds.get(identity))),
            patch.object(app.bot, "fetch_guild", AsyncMock(side_effect=lambda identity: self.guilds[identity])),
            patch.object(app.bot, "is_owner", AsyncMock(return_value=True)),
        ]
        for replacement in self.patches:
            replacement.start()
            self.addCleanup(replacement.stop)

    def ctx(self, command="admin users", guild=None):
        return SimpleNamespace(author=self.user, guild=guild, channel=SimpleNamespace(id=700),
            bot=app.bot, command=app.bot.get_command(command), clean_prefix="$", reply=AsyncMock(),
            message=SimpleNamespace(id=999, mentions=[], channel=SimpleNamespace(id=700)))

    async def test_admin_dm_uses_one_current_scope_without_changing_transport(self):
        ctx = self.ctx()
        self.assertTrue(await ctx.command.can_run(ctx))
        self.assertIsNone(ctx.guild)
        self.assertEqual(app.admin_guild(ctx).id, 10)
        app.bot.fetch_guild.assert_awaited_once_with(10)
        self.guilds[10].fetch_member.assert_awaited_once_with(42)

    async def test_each_admin_command_retains_owner_and_current_admin_checks(self):
        for name in ("admin users", "admin link", "admin integrations", "admin logs", "admin errors"):
            self.assertTrue(await app.bot.get_command(name).can_run(self.ctx(name)))
        app.bot.is_owner.return_value = False
        for name in ("admin users", "admin link", "admin integrations", "admin logs", "admin errors"):
            with self.subTest(command=name), self.assertRaises(app.commands.NotOwner):
                await app.bot.get_command(name).can_run(self.ctx(name))
        for name in ("admin", "admin reports", "admin reports claim", "admin reports resolve", "admin reports dismiss"):
            self.assertTrue(await app.bot.get_command(name).can_run(self.ctx(name)))

    async def test_bot_owner_outside_guild_or_without_role_cannot_use_admin(self):
        self.guilds[10].members_by_id[42].guild_permissions.administrator = False
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin_users.can_run(self.ctx())
        self.guilds[10].members_by_id.pop(42)
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin_users.can_run(self.ctx())

    async def test_permissions_are_rechecked_even_when_context_was_already_resolved(self):
        ctx = self.ctx()
        self.assertTrue(await app.admin_users.can_run(ctx))
        self.guilds[10].members_by_id[42].guild_permissions.administrator = False
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin_users.can_run(ctx)
        self.assertEqual(app.bot.fetch_guild.await_count, 2)

    async def test_ambiguous_multiple_admin_guilds_require_explicit_selection(self):
        self.guilds[20] = FakeGuild(20)
        app.ALLOWED_GUILD_IDS = frozenset({10, 20})
        with self.assertRaisesRegex(app.commands.CheckFailure, "multiple allowed servers"):
            await app.admin_users.can_run(self.ctx())
        await app.admin_server.callback(self.ctx("admin server"), 20)
        self.assertEqual(app.ADMIN_DM_GUILDS, {42: 20})
        ctx = self.ctx()
        self.assertTrue(await app.admin_users.can_run(ctx))
        self.assertEqual(app.admin_guild(ctx).id, 20)

    async def test_one_authorized_guild_among_multiple_memberships_is_unambiguous(self):
        self.guilds[20] = FakeGuild(20, administrator=False)
        app.ALLOWED_GUILD_IDS = frozenset({10, 20})
        ctx = self.ctx()
        self.assertTrue(await app.admin_users.can_run(ctx))
        self.assertEqual(app.admin_guild(ctx).id, 10)

    async def test_selected_scope_is_not_silently_replaced_after_permission_loss(self):
        self.guilds[20] = FakeGuild(20)
        app.ALLOWED_GUILD_IDS = frozenset({10, 20})
        app.ADMIN_DM_GUILDS[42] = 20
        self.guilds[20].members_by_id[42].guild_permissions.administrator = False
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin_users.can_run(self.ctx())
        self.assertEqual(app.ADMIN_DM_GUILDS[42], 20)
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin_users.can_run(self.ctx())
        await app.admin_server.callback(self.ctx("admin server"), 10)
        ctx = self.ctx()
        self.assertTrue(await app.admin_users.can_run(ctx))
        self.assertEqual(app.admin_guild(ctx).id, 10)

    async def test_actual_admin_group_invocation_authorizes_origin_before_listing(self):
        self.guilds[20] = FakeGuild(20)
        app.ALLOWED_GUILD_IDS = frozenset({10, 20})
        self.guilds[10].members_by_id[42].guild_permissions.administrator = False
        def invocation():
            message = SimpleNamespace(id=999, author=self.user, guild=self.guilds[10],
                channel=SimpleNamespace(id=700), _state=app.bot._connection, attachments=[])
            ctx = app.commands.Context(message=message, bot=app.bot, view=StringView("server"),
                prefix="$", invoked_with="admin")
            ctx.reply = AsyncMock()
            return ctx
        ctx = invocation()
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin.invoke(ctx)
        ctx.reply.assert_not_awaited()
        self.guilds[10].members_by_id[42].guild_permissions.administrator = True
        ctx = invocation()
        await app.admin.invoke(ctx)
        self.assertIn("`10`", ctx.reply.await_args.args[0])
        self.assertNotIn("`20`", ctx.reply.await_args.args[0])

    async def test_role_api_failure_cannot_turn_ambiguous_servers_into_single_scope(self):
        self.guilds[20] = FakeGuild(20)
        app.ALLOWED_GUILD_IDS = frozenset({10, 20})
        async def fetch(identity):
            if identity == 20:
                raise discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "private response")
            return self.guilds[identity]
        app.bot.fetch_guild.side_effect = fetch
        with self.assertRaisesRegex(app.commands.CheckFailure, "current roles"):
            await app.admin_users.can_run(self.ctx())

    async def test_server_command_keeps_actual_scope_and_blocks_cross_server_selection(self):
        ctx = self.ctx(guild=self.guilds[10])
        self.assertTrue(await app.admin_users.can_run(ctx))
        self.assertEqual(app.admin_guild(ctx).id, 10)
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin_server.callback(ctx, 20)
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin_users.can_run(self.ctx(guild=FakeGuild(99)))

    async def test_unconfigured_server_and_bot_accounts_cannot_select_context(self):
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin_server.callback(self.ctx("admin server"), 99)
        self.user.bot = True
        with self.assertRaises(app.commands.CheckFailure):
            await app.admin_users.can_run(self.ctx())

    async def test_media_commands_still_reject_dm(self):
        for command in ("request", "discover", "music", "torrent"):
            with self.subTest(command=command), self.assertRaises(app.commands.NoPrivateMessage):
                await app.enforce_allowed_guild(self.ctx(command))

    async def test_exact_target_member_resolution_is_scoped_and_fresh(self):
        target = self.guilds[10].add_member(77, "Exact User", global_name="Global Label")
        ctx = self.ctx("admin link")
        await app.resolve_admin_context(ctx)
        for value in ("<@77>", "<@!77>", "77", "**<@77>**", "**<@!77>**", "**77**", "Exact User", "global label"):
            self.assertIs(await app.resolve_admin_member(ctx, value), target)
        self.assertEqual(self.guilds[10].fetch_member.await_args.args, (77,))

    async def test_target_id_cannot_be_replaced_by_an_unrelated_message_mention(self):
        target = self.guilds[10].add_member(77, "Correct")
        ctx = self.ctx("admin link")
        ctx.message.mentions = [SimpleNamespace(id=88)]
        await app.resolve_admin_context(ctx)
        self.assertIs(await app.resolve_admin_member(ctx, "77"), target)

    async def test_target_names_never_use_prefix_matching_or_choose_ambiguously(self):
        self.guilds[10].add_member(77, "Exact User")
        ctx = self.ctx("admin link")
        await app.resolve_admin_context(ctx)
        with self.assertRaisesRegex(app.AdminMemberResolutionError, "No current member"):
            await app.resolve_admin_member(ctx, "Exact")
        self.guilds[10].add_member(88, "Exact User")
        with self.assertRaisesRegex(app.AdminMemberResolutionError, "More than one"):
            await app.resolve_admin_member(ctx, "Exact User")

    async def test_nonmember_target_fails_before_account_mutation(self):
        ctx = self.ctx("admin link")
        await app.resolve_admin_context(ctx)
        with patch.object(app.seerr, "users", AsyncMock()) as lookup, patch.object(app, "set_link") as mutation:
            with self.assertRaises(app.AdminMemberResolutionError):
                await app.admin_link.callback(ctx, "77", seerr_username="test")
        lookup.assert_not_awaited()
        mutation.assert_not_called()

    async def test_admin_report_dm_uses_selected_guild_for_queue_and_mutation(self):
        ctx = self.ctx("admin reports")
        await app.resolve_admin_context(ctx)
        with patch.object(app, "list_media_reports", return_value=[]) as listing:
            await app.admin_reports.callback(ctx)
        self.assertEqual(listing.call_args.kwargs["discord_guild_id"], 10)
        record = {"status": "open"}
        with patch.object(app, "media_report_by_id", return_value=record), patch.object(app, "transition_media_report", AsyncMock(return_value=None)) as transition:
            await app.run_admin_report_transition(ctx, 3, "resolved")
        self.assertEqual(transition.await_args.kwargs["guild_id"], 10)

    async def test_dm_report_view_rechecks_admin_and_binds_actor_channel_and_guild(self):
        view = app.AdminReportQueueView(requester_id=42, guild_id=10, records=[{"status": "open"}], command_message=SimpleNamespace(), dm_channel_id=700)
        self.addCleanup(view.stop)
        interaction = SimpleNamespace(user=self.user, guild_id=None, channel_id=700, message=None,
            response=SimpleNamespace(send_message=AsyncMock()))
        self.assertTrue(await view.interaction_check(interaction))
        self.guilds[10].members_by_id[42].guild_permissions.administrator = False
        self.assertFalse(await view.interaction_check(interaction))
        self.guilds[10].members_by_id[42].guild_permissions.administrator = True
        interaction.channel_id = 701
        self.assertFalse(await view.interaction_check(interaction))
        interaction.channel_id = 700
        interaction.guild_id = 10
        self.assertFalse(await view.interaction_check(interaction))
        interaction.guild_id = None
        interaction.user = SimpleNamespace(id=99)
        self.assertFalse(await view.interaction_check(interaction))

    async def test_help_advertises_only_the_dm_admin_commands_actor_can_use(self):
        ctx = self.ctx("help")
        await app.mediabot_help.callback(ctx, topic="admin")
        self.assertIn("admin users", ctx.reply.await_args.args[0])
        self.assertIn("admin server", ctx.reply.await_args.args[0])
        self.assertLessEqual(len(ctx.reply.await_args.args[0]), 2000)
        app.bot.is_owner.return_value = False
        await app.mediabot_help.callback(ctx, topic="admin")
        self.assertIn("admin reports", ctx.reply.await_args.args[0])
        self.assertNotIn("admin users", ctx.reply.await_args.args[0])
        await app.mediabot_help.callback(ctx, topic="admin users")
        self.assertIn("not available", ctx.reply.await_args.args[0])
        self.guilds[10].members_by_id[42].guild_permissions.administrator = False
        await app.mediabot_help.callback(ctx, topic="admin")
        self.assertIn("not available", ctx.reply.await_args.args[0])


class AdminReportActionBindingTests(unittest.IsolatedAsyncioTestCase):
    def view(self):
        view = app.AdminReportQueueView(requester_id=42, guild_id=10,
            records=[{"report_id": 1, "status": "open"}, {"report_id": 2, "status": "open"}],
            command_message=SimpleNamespace(), dm_channel_id=700)
        view.message = SimpleNamespace(edit=AsyncMock())
        view.build_embed = Mock(return_value=discord.Embed(title="Queue"))
        self.addCleanup(view.stop)
        return view

    def interaction(self, custom_id):
        return SimpleNamespace(user=SimpleNamespace(id=42), data={"custom_id": custom_id},
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    async def test_busy_duplicate_actions_and_navigation_cannot_retarget_next_report(self):
        for status in ("resolved", "dismissed"):
            with self.subTest(status=status):
                view = self.view()
                button = view.resolve_button if status == "resolved" else view.dismiss_button
                original_id = button.custom_id
                first = self.interaction(original_id)
                duplicate = self.interaction(original_id)
                navigation = self.interaction(view.next_button.custom_id)
                entered, release = asyncio.Event(), asyncio.Event()
                async def transition(**kwargs):
                    entered.set()
                    await release.wait()
                    return {"report_id": kwargs["report_id"], "status": status}
                with patch.object(app, "transition_media_report", AsyncMock(side_effect=transition)) as mutation:
                    task = asyncio.create_task(view.apply_status(first, status))
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=2)
                        await asyncio.wait_for(view.apply_status(duplicate, status), timeout=2)
                        await asyncio.wait_for(view.next(navigation), timeout=2)
                        self.assertEqual(view.current()["report_id"], 1)
                        duplicate.response.send_message.assert_awaited_once()
                        navigation.response.send_message.assert_awaited_once()
                    finally:
                        release.set()
                        await task
                    self.assertEqual(view.current()["report_id"], 2)
                    delayed = self.interaction(original_id)
                    await view.apply_status(delayed, status)
                    self.assertIn("older report view", delayed.response.send_message.await_args.args[0])
                    mutation.assert_awaited_once()
                    self.assertEqual(mutation.await_args.kwargs["report_id"], 1)

    async def test_delayed_action_is_stale_even_after_navigation_returns_to_same_report(self):
        view = self.view()
        stale_id = view.resolve_button.custom_id
        await view.next(self.interaction(view.next_button.custom_id))
        await view.previous(self.interaction(view.previous_button.custom_id))
        self.assertEqual(view.current()["report_id"], 1)
        with patch.object(app, "transition_media_report", AsyncMock()) as mutation:
            stale = self.interaction(stale_id)
            await view.apply_status(stale, "resolved")
        mutation.assert_not_awaited()
        self.assertIn("older report view", stale.response.send_message.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
