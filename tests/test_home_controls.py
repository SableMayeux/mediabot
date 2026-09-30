import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

from mediabot.services.home_controls import cast_webhook_url, restore_home
from mediabot.ui.personal_home import HomeView


URL = "http://10.0.0.55:8123/api/webhook/" + "a" * 64


class HomeControlTests(unittest.IsolatedAsyncioTestCase):
    def test_capability_is_restricted_to_explicit_private_address_ranges(self):
        for url in (URL, URL.replace("10.0.0.55", "192.168.1.2"),
                    "https://[fd01::1]/api/webhook/" + "a" * 64):
            with patch.dict(os.environ, {"HOME_ASSISTANT_CAST_WEBHOOK": url}):
                self.assertEqual(cast_webhook_url(), url)
        for url in ("", URL.replace("10.0.0.55", "example.com"),
                    URL.replace("10.0.0.55", "8.8.8.8"),
                    URL.replace("10.0.0.55", "127.0.0.1"),
                    URL.replace("10.0.0.55", "169.254.169.254"),
                    URL.replace("10.0.0.55", "192.0.2.2"),
                    URL.replace("10.0.0.55", "user:pass@10.0.0.55"),
                    URL.replace(":8123", ":bad"), URL.replace(":8123", ":0"),
                    URL + "?service=other", URL + "#fragment",
                    URL.replace("/api/webhook/", "/api/services/")):
            with self.subTest(url=url), patch.dict(os.environ, {"HOME_ASSISTANT_CAST_WEBHOOK": url}):
                self.assertIsNone(cast_webhook_url())

    async def test_fixed_empty_payload_and_no_redirect_following(self):
        for status, expected in ((200, "accepted"), (302, "rejected"), (401, "rejected")):
            response = SimpleNamespace(status=status)
            request = AsyncMock()
            request.__aenter__.return_value = response
            session = AsyncMock()
            session.post = Mock(return_value=request)
            context = AsyncMock()
            context.__aenter__.return_value = session
            with patch.dict(os.environ, {"HOME_ASSISTANT_CAST_WEBHOOK": URL}), \
                    patch("mediabot.services.home_controls.aiohttp.ClientSession", return_value=context):
                self.assertEqual(await restore_home(), expected)
            session.post.assert_called_once_with(URL, json={}, allow_redirects=False)

    async def test_timeout_is_not_replayed_or_reported_as_success(self):
        session = AsyncMock()
        session.post = Mock(side_effect=TimeoutError())
        context = AsyncMock()
        context.__aenter__.return_value = session
        with patch.dict(os.environ, {"HOME_ASSISTANT_CAST_WEBHOOK": URL}), \
                patch("mediabot.services.home_controls.aiohttp.ClientSession", return_value=context):
            self.assertEqual(await restore_home(), "unconfirmed")
        session.post.assert_called_once()

    async def test_unconfigured_does_not_make_a_request(self):
        with patch.dict(os.environ, {"HOME_ASSISTANT_CAST_WEBHOOK": ""}), \
                patch("mediabot.services.home_controls.aiohttp.ClientSession") as session:
            self.assertEqual(await restore_home(), "unconfigured")
        session.assert_not_called()

    async def test_home_control_only_appears_for_owner_when_configured(self):
        for owner, configured, expected in ((False, True, False), (True, False, False), (True, True, True)):
            home = HomeView(42, AsyncMock(), AsyncMock(), owner=owner, home_control=configured)
            self.addCleanup(home.stop)
            self.assertEqual(any(button.label == "Show HA on Denny's" for button in home.children), expected)

    async def test_non_owner_command_check_prevents_device_action(self):
        import app
        ctx = SimpleNamespace(bot=SimpleNamespace(is_owner=AsyncMock(return_value=False)),
                              author=SimpleNamespace(id=99), reply=AsyncMock())
        command = app.bot.get_command("ha")
        predicate = next(check for check in command.checks if check.__qualname__ == "is_owner.<locals>.predicate")
        with patch.object(app, "restore_home", AsyncMock()) as restore:
            with self.assertRaises(commands.NotOwner):
                await predicate(ctx)
        restore.assert_not_awaited()

    async def test_unknown_action_never_contacts_ha(self):
        import app
        ctx = SimpleNamespace(reply=AsyncMock())
        with patch.object(app, "restore_home", AsyncMock()) as restore:
            await app.bot.get_command("ha").callback(ctx, "other-device")
        restore.assert_not_awaited()

    def test_webhook_capability_is_redacted_in_application_logs(self):
        import app
        value = app._redact_sensitive_text("request failed: " + URL)
        self.assertNotIn("a" * 64, value)
        self.assertIn("[REDACTED]", value)
