import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import discord
from mediabot.services.chat_access import allowed_chat_user


class ChatAccessTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, *, owner=False, member=True):
        guild = SimpleNamespace(fetch_member=AsyncMock(return_value=SimpleNamespace(id=42, bot=False)))
        if not member:
            guild.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="missing"), "missing")
        return SimpleNamespace(is_owner=AsyncMock(return_value=owner), get_guild=Mock(return_value=guild))

    async def test_current_member_can_use_guild_and_dm_chat(self):
        bot = self.bot()
        for guild_id in (10, None):
            self.assertTrue(await allowed_chat_user(bot, SimpleNamespace(id=42), {10}, guild_id=guild_id))
        self.assertEqual(bot.get_guild.return_value.fetch_member.await_count, 2)

    async def test_removed_member_and_api_error_fail_closed(self):
        bot = self.bot(member=False)
        self.assertFalse(await allowed_chat_user(bot, SimpleNamespace(id=42), {10}))
        bot.get_guild.return_value.fetch_member.side_effect = discord.HTTPException(SimpleNamespace(status=503, reason="unavailable"), "unavailable")
        self.assertFalse(await allowed_chat_user(bot, SimpleNamespace(id=42), {10}))

    async def test_owner_dm_allowed_but_untrusted_guild_denied(self):
        bot = self.bot(owner=True)
        self.assertTrue(await allowed_chat_user(bot, SimpleNamespace(id=42), {10}))
        self.assertFalse(await allowed_chat_user(bot, SimpleNamespace(id=42), {10}, guild_id=99))

    async def test_unavailable_guild_and_bot_identity_denied(self):
        bot = self.bot()
        bot.get_guild.return_value.fetch_member.return_value.bot = True
        self.assertFalse(await allowed_chat_user(bot, SimpleNamespace(id=42), {10}))
        bot.get_guild.return_value = None
        self.assertFalse(await allowed_chat_user(bot, SimpleNamespace(id=42), {10}))
