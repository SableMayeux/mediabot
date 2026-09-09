import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from mediabot.services.life_capture import LifeCaptureError, LifeCaptureService


class LifeCaptureServiceTests(unittest.TestCase):
    def test_capture_is_atomic_markdown_with_stable_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            inbox = Path(temp_dir) / "Life" / "Inbox"
            service = LifeCaptureService(inbox)

            result = service.capture(
                'Call Mom about "Sunday"\nThen book dinner.',
                discord_user_id=123,
                discord_channel_id=456,
                discord_guild_id=789,
                now=datetime(2026, 9, 8, 18, 30, tzinfo=timezone.utc),
                capture_id="1d40c4ad-4e2f-4f4d-b321-7380cb0c3d95",
            )

            self.assertEqual(
                result.path.name,
                "2026-09-08T183000Z-1d40c4ad-4e2f-4f4d-b321-7380cb0c3d95.md",
            )
            payload = result.path.read_text(encoding="utf-8")
            self.assertIn('status: "inbox"', payload)
            self.assertIn('user_id: "123"', payload)
            self.assertIn('channel_id: "456"', payload)
            self.assertIn('guild_id: "789"', payload)
            self.assertIn('Call Mom about "Sunday"\nThen book dinner.', payload)
            self.assertEqual(list(inbox.glob("*.tmp")), [])
            if os.name != "nt":
                self.assertEqual(result.path.stat().st_mode & 0o777, 0o600)

    def test_blank_capture_is_rejected_without_creating_inbox(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            inbox = Path(temp_dir) / "Inbox"
            service = LifeCaptureService(inbox)

            with self.assertRaisesRegex(LifeCaptureError, "blank"):
                service.capture(
                    "   ",
                    discord_user_id=123,
                    discord_channel_id=None,
                    discord_guild_id=None,
                )

            self.assertFalse(inbox.exists())

    def test_long_title_is_bounded_but_body_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LifeCaptureService(Path(temp_dir) / "Inbox")
            raw_text = "a" * 200

            result = service.capture(
                raw_text,
                discord_user_id=123,
                discord_channel_id=None,
                discord_guild_id=None,
            )

            self.assertEqual(len(result.title), 80)
            self.assertTrue(result.title.endswith("..."))
            self.assertIn(raw_text, result.path.read_text(encoding="utf-8"))

    def test_publish_is_no_overwrite_and_preserves_the_existing_capture(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            inbox = Path(temp_dir) / "Inbox"
            service = LifeCaptureService(inbox)
            captured_at = datetime(2026, 9, 8, 18, 30, tzinfo=timezone.utc)
            capture_id = "1d40c4ad-4e2f-4f4d-b321-7380cb0c3d95"

            original = service.capture(
                "original thought",
                discord_user_id=123,
                discord_channel_id=None,
                discord_guild_id=None,
                now=captured_at,
                capture_id=capture_id,
            )
            original_payload = original.path.read_bytes()

            with self.assertRaisesRegex(LifeCaptureError, "existing capture was preserved"):
                service.capture(
                    "replacement thought",
                    discord_user_id=456,
                    discord_channel_id=None,
                    discord_guild_id=None,
                    now=captured_at,
                    capture_id=capture_id,
                )

            self.assertEqual(original.path.read_bytes(), original_payload)
            self.assertNotIn(b"replacement thought", original.path.read_bytes())
            self.assertEqual(list(inbox.glob(".*.tmp")), [])

    def test_directory_entries_are_synced_after_publish_and_temp_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            inbox = Path(temp_dir) / "Inbox"
            service = LifeCaptureService(inbox)
            capture_id = "1d40c4ad-4e2f-4f4d-b321-7380cb0c3d95"
            final_path = inbox / (
                "2026-09-08T183000Z-"
                "1d40c4ad-4e2f-4f4d-b321-7380cb0c3d95.md"
            )
            observed_states = []

            def observe_directory_sync(path):
                self.assertEqual(path, inbox)
                observed_states.append(
                    (final_path.exists(), len(list(inbox.glob(".*.tmp"))))
                )

            with mock.patch.object(
                service,
                "_fsync_directory",
                side_effect=observe_directory_sync,
            ) as directory_sync:
                service.capture(
                    "durable thought",
                    discord_user_id=123,
                    discord_channel_id=None,
                    discord_guild_id=None,
                    now=datetime(2026, 9, 8, 18, 30, tzinfo=timezone.utc),
                    capture_id=capture_id,
                )

            self.assertEqual(directory_sync.call_count, 2)
            self.assertEqual(observed_states, [(True, 1), (True, 0)])


if __name__ == "__main__":
    unittest.main()
