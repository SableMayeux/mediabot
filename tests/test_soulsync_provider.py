import unittest
from unittest.mock import AsyncMock

from mediabot.providers.soulsync import SoulSyncError, SoulSyncProvider


class SoulSyncProviderTests(unittest.IsolatedAsyncioTestCase):
    class FakeResponse:
        def __init__(self, status, payload):
            self.status = status
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def json(self):
            return self.payload

        async def text(self):
            return str(self.payload)

    class FakeSession:
        def __init__(self, response):
            self.response = response
            self.closed = False

        def request(self, method, url, **kwargs):
            return self.response

    async def test_unconfigured_provider_is_disabled(self):
        provider = SoulSyncProvider(base_url="http://soulsync.test", api_key="")

        await provider.start()

        self.assertFalse(provider.enabled)
        self.assertIsNone(provider.session)

        with self.assertRaisesRegex(SoulSyncError, "not configured"):
            await provider.health()

    async def test_search_tracks_uses_public_v1_contract(self):
        provider = SoulSyncProvider(
            base_url="http://soulsync.test",
            api_key="test-key",
        )
        provider.request = AsyncMock(
            return_value={"tracks": [{"name": "Reze"}], "source": "itunes"}
        )

        result = await provider.search_tracks("song name", limit=99)

        self.assertEqual(result["tracks"][0]["name"], "Reze")
        provider.request.assert_awaited_once_with(
            "POST",
            "/search/tracks",
            json={"query": "song name", "source": "auto", "limit": 25},
        )

    async def test_music_request_passes_discord_metadata(self):
        provider = SoulSyncProvider(
            base_url="http://soulsync.test",
            api_key="test-key",
        )
        provider.request = AsyncMock(
            return_value={"request_id": "abc", "status": "queued"}
        )

        result = await provider.create_music_request(
            "Artist - Track",
            metadata={"discord_user_id": "42"},
        )

        self.assertEqual(result["request_id"], "abc")
        provider.request.assert_awaited_once_with(
            "POST",
            "/request",
            json={
                "query": "Artist - Track",
                "metadata": {"discord_user_id": "42"},
            },
        )

    async def test_library_track_lookup_encodes_artist_and_title(self):
        provider = SoulSyncProvider(
            base_url="http://soulsync.test",
            api_key="test-key",
        )
        provider.request = AsyncMock(
            return_value={"tracks": [{"name": "Pink Pony Club"}]}
        )

        tracks = await provider.library_tracks(
            title="Pink Pony Club",
            artist="Chappell Roan",
            limit=10,
        )

        self.assertEqual(tracks[0]["name"], "Pink Pony Club")
        provider.request.assert_awaited_once_with(
            "GET",
            "/library/tracks?title=Pink%20Pony%20Club&artist=Chappell%20Roan&limit=10",
        )

    async def test_http_rejection_distinguishes_safe_retry_from_ambiguity(self):
        provider = SoulSyncProvider(
            base_url="http://soulsync.test",
            api_key="test-key",
        )
        provider.session = self.FakeSession(
            self.FakeResponse(
                422,
                {"success": False, "error": {"message": "no eligible source"}},
            )
        )
        with self.assertRaises(SoulSyncError) as rejected:
            await provider.create_music_request("Artist - Track")
        self.assertTrue(rejected.exception.definitive)
        self.assertEqual(rejected.exception.status, 422)

        provider.session = self.FakeSession(
            self.FakeResponse(
                503,
                {"success": False, "error": {"message": "commit unknown"}},
            )
        )
        with self.assertRaises(SoulSyncError) as ambiguous:
            await provider.create_music_request("Artist - Track")
        self.assertFalse(ambiguous.exception.definitive)
        self.assertEqual(ambiguous.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
