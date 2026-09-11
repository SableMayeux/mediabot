import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from mediabot.core.configuration import configured_timezone, validate_core_configuration
from mediabot.core.initialize_volume import initialize_volume
from mediabot.providers.jellyfin import JellyfinProvider
from mediabot.providers.sonarr import SonarrProvider
from mediabot.providers.soulsync import SoulSyncProvider


class PortableConfigurationTests(unittest.TestCase):
    def test_timezone_is_explicit_and_has_no_operator_location_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_timezone("EVENT_TIMEZONE"), "UTC")
        with patch.dict(os.environ, {"TZ": "Europe/London", "EVENT_TIMEZONE": ""}, clear=True):
            self.assertEqual(configured_timezone("EVENT_TIMEZONE"), "Europe/London")
        with patch.dict(os.environ, {"TZ": "Europe/London", "EVENT_TIMEZONE": "America/Denver"}, clear=True):
            self.assertEqual(configured_timezone("EVENT_TIMEZONE"), "America/Denver")
        with patch.dict(os.environ, {"TZ": "invalid/timezone"}, clear=True):
            with self.assertRaisesRegex(ValueError, "IANA timezone"):
                configured_timezone("EVENT_TIMEZONE")

    def test_missing_core_configuration_reports_variable_names(self):
        with self.assertRaisesRegex(RuntimeError, "DISCORD_TOKEN, SEERR_URL, SEERR_API_KEY, ALLOWED_GUILD_IDS"):
            validate_core_configuration(discord_token="", seerr_url="", seerr_api_key="", guild_ids=())

    def test_invalid_url_never_echoes_embedded_credentials(self):
        for url in ("http://user:private-value@server/", "ftp://server", "http://server:invalid", "http://server/?key=private-value"):
            with self.subTest(url=url):
                with self.assertRaises(RuntimeError) as caught:
                    validate_core_configuration(discord_token="secret-token", seerr_url=url, seerr_api_key="secret-key", guild_ids={1})
                self.assertNotIn("private-value", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))

    def test_reverse_proxy_subpath_is_valid(self):
        validate_core_configuration(discord_token="token", seerr_url="https://requests.example.test/seerr", seerr_api_key="key", guild_ids={1})


class OptionalProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_configuration_opens_no_optional_http_sessions(self):
        with patch.dict(os.environ, {}, clear=True):
            providers = (JellyfinProvider(), SonarrProvider(), SoulSyncProvider())
        for provider in providers:
            self.assertFalse(provider.enabled)
            self.assertEqual(provider.base_url, "")
            await provider.start()
            self.assertIsNone(provider.session)

    async def test_optional_key_without_url_stays_disabled(self):
        with patch.dict(os.environ, {
            "JELLYFIN_API_KEY": "test-key", "SONARR_API_KEY": "test-key", "SOULSYNC_API_KEY": "test-key",
        }, clear=True):
            providers = (JellyfinProvider(), SonarrProvider(), SoulSyncProvider())
        for provider in providers:
            self.assertFalse(provider.enabled)
            await provider.start()
            self.assertIsNone(provider.session)

    async def test_blank_public_url_uses_configured_jellyfin_url(self):
        with patch.dict(os.environ, {
            "JELLYFIN_URL": "https://media.example.test/", "JELLYFIN_API_KEY": "test-key", "JELLYFIN_PUBLIC_URL": "",
        }, clear=True):
            provider = JellyfinProvider()
        self.assertEqual(provider.public_url, "https://media.example.test")
        self.assertTrue(provider.enabled)

    async def test_explicit_blank_provider_url_does_not_pick_up_environment(self):
        with patch.dict(os.environ, {"SONARR_URL": "http://example.test", "SOULSYNC_URL": "http://example.test"}, clear=True):
            for provider in (SonarrProvider(base_url="", api_key="key"), SoulSyncProvider(base_url="", api_key="key")):
                self.assertFalse(provider.enabled)


class VolumeInitializationTests(unittest.TestCase):
    def test_populated_root_volume_is_rejected_without_touching_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            personal = path / "personal.txt"
            personal.write_bytes(b"preserve this content")
            info = Mock(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0)
            with patch.object(Path, "lstat", return_value=info), patch("os.chown", create=True) as chown, patch("os.chmod") as chmod:
                with self.assertRaisesRegex(RuntimeError, "existing files"):
                    initialize_volume(path)
            chown.assert_not_called()
            chmod.assert_not_called()
            self.assertEqual(personal.read_bytes(), b"preserve this content")

    def test_existing_application_volume_is_not_enumerated_or_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            info = Mock(st_mode=stat.S_IFDIR | 0o750, st_uid=1000, st_gid=1000)
            with patch.object(Path, "lstat", return_value=info), patch.object(Path, "iterdir") as contents, patch("os.chown", create=True) as chown, patch("os.chmod") as chmod:
                self.assertIn("retained", initialize_volume(path))
            contents.assert_not_called()
            chown.assert_not_called()
            chmod.assert_not_called()

    def test_symlink_is_rejected_without_following_it(self):
        info = Mock(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_gid=0)
        with patch.object(Path, "lstat", return_value=info), patch("os.chown", create=True) as chown:
            with self.assertRaisesRegex(RuntimeError, "without symlinks"):
                initialize_volume(Path("/app/data"))
        chown.assert_not_called()

    def test_wrong_owned_empty_directory_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            info = Mock(st_mode=stat.S_IFDIR | 0o755, st_uid=2000, st_gid=2000)
            with patch.object(Path, "lstat", return_value=info), patch("os.chown", create=True) as chown:
                with self.assertRaisesRegex(RuntimeError, "Unexpected data ownership"):
                    initialize_volume(Path(directory))
            chown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
