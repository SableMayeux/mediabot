import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("SEERR_API_KEY", "test-key")
os.environ.setdefault("LOG_PATH", str(Path(tempfile.gettempdir()) / "mediabot-test.log"))
os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "mediabot-test.db"))

import app
from mediabot.services.torrent_intake import TorrentIntakeResult


class TorrentCategoryCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_stays_stopped_pending_owner_review(self):
        previous_is_owner = app.bot.is_owner
        previous_get_link = app.get_link
        previous_submit = app.torrent_intake.submit
        app.bot.is_owner = AsyncMock(return_value=False)
        app.get_link = Mock(return_value={"seerr_user_id": 42})
        app.torrent_intake.submit = AsyncMock(
            return_value=TorrentIntakeResult(
                info_hash="0123456789abcdef0123456789abcdef01234567",
                category="applications",
                duplicate=False,
            )
        )
        magnet = (
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
            "&dn=private-installer-name"
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            author=SimpleNamespace(id=12),
            _torrent_source_deleted=True,
            send=AsyncMock(),
        )
        try:
            await app.torrent.callback(ctx, "app", magnet=magnet)
        finally:
            app.bot.is_owner = previous_is_owner
            app.get_link = previous_get_link
            app.torrent_intake.submit = previous_submit

        response = ctx.send.await_args.args[0]
        self.assertIn("stopped in manual quarantine pending owner review", response)
        self.assertIn("cannot be started", response)
        self.assertNotIn("private-installer-name", response)
        self.assertIn("0123456789ab", response)


if __name__ == "__main__":
    unittest.main()
