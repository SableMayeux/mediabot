import base64
import tempfile
import unittest
from pathlib import Path

from mediabot.services.torrent_intake import (
    TorrentInputError,
    TorrentIntakeError,
    TorrentIntakeService,
    normalize_torrent_category,
    parse_magnet_reference,
    torrent_category_label,
    torrent_category_requires_manual_review,
)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeSession:
    def __init__(self, *, get_response=None, post_response=None):
        self.closed = False
        self.get_response = get_response
        self.post_response = post_response
        self.get_kwargs = None
        self.post_kwargs = None

    def get(self, url, **kwargs):
        self.get_kwargs = kwargs
        return self.get_response

    def post(self, url, **kwargs):
        self.post_kwargs = kwargs
        return self.post_response


class TorrentInputTests(unittest.TestCase):
    HASH = "0123456789abcdef0123456789abcdef01234567"

    def test_normalizes_fixed_category_aliases(self):
        expected = {
            "movie": "movies",
            "film": "movies",
            "series": "tv",
            "song": "music",
            "album": "music",
            "game": "games",
            "software": "applications",
            "app": "applications",
            "misc": "other",
        }
        for supplied, canonical in expected.items():
            with self.subTest(supplied=supplied):
                self.assertEqual(normalize_torrent_category(supplied), canonical)
        with self.assertRaises(TorrentInputError):
            normalize_torrent_category("../../escape")

    def test_manual_review_categories_are_explicit(self):
        for category in ("applications", "games", "other"):
            with self.subTest(category=category):
                self.assertTrue(torrent_category_requires_manual_review(category))
        for category in ("movies", "tv", "music"):
            with self.subTest(category=category):
                self.assertFalse(torrent_category_requires_manual_review(category))
        self.assertEqual(torrent_category_label("applications"), "application")
        self.assertEqual(torrent_category_label("tv"), "TV")

    def test_parses_hex_and_base32_btih(self):
        self.assertEqual(
            parse_magnet_reference(f"magnet:?xt=urn:btih:{self.HASH}").info_hash,
            self.HASH,
        )
        encoded = base64.b32encode(bytes.fromhex(self.HASH)).decode("ascii")
        self.assertEqual(
            parse_magnet_reference(f"magnet:?xt=urn:btih:{encoded}").info_hash,
            self.HASH,
        )

    def test_hybrid_uses_unambiguous_btih(self):
        btmh = "a" * 64
        reference = parse_magnet_reference(
            f"magnet:?xt=urn:btmh:1220{btmh}&xt=urn:btih:{self.HASH}"
        )
        self.assertEqual(reference.info_hash, self.HASH)

    def test_v2_only_and_conflicting_btih_are_rejected(self):
        with self.assertRaisesRegex(TorrentInputError, "v2-only"):
            parse_magnet_reference(f"magnet:?xt=urn:btmh:1220{'a' * 64}")
        with self.assertRaisesRegex(TorrentInputError, "conflicting"):
            parse_magnet_reference(
                f"magnet:?xt=urn:btih:{self.HASH}&xt=urn:btih:{'f' * 40}"
            )

    def test_noncanonical_or_whitespace_input_is_rejected(self):
        for value in (
            "https://example.test/file.torrent",
            f"magnet:?xt=urn:btih:{self.HASH} bad",
            "magnet:?dn=no-hash",
        ):
            with self.subTest(value=value):
                with self.assertRaises(TorrentInputError):
                    parse_magnet_reference(value)


class TorrentIntakeServiceTests(unittest.IsolatedAsyncioTestCase):
    HASH = TorrentInputTests.HASH

    async def test_session_lifecycle_uses_the_gateway_token_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "token"
            token_path.write_text("T" * 42, encoding="utf-8")
            service = TorrentIntakeService(
                base_url="http://intake.test",
                token_path=token_path,
            )
            with self.assertRaisesRegex(TorrentIntakeError, "malformed"):
                await service.start()
            self.assertIsNone(service.session)

            token_path.write_text("T" * 43, encoding="utf-8")
            await service.start()
            try:
                self.assertIsNotNone(service.session)
                self.assertFalse(service.session.closed)
                self.assertEqual(
                    service.session.headers["Authorization"],
                    "Bearer " + "T" * 43,
                )
            finally:
                await service.close()
            self.assertIsNone(service.session)

    async def test_health_requires_an_explicit_ok_receipt(self):
        service = TorrentIntakeService(base_url="http://intake.test")
        service.session = FakeSession(
            get_response=FakeResponse(200, {"ok": True}),
            post_response=FakeResponse(400, {"error": "invalid_request"}),
        )
        self.assertEqual(await service.health(), {"ok": True})
        self.assertIs(service.session.get_kwargs["allow_redirects"], False)
        self.assertEqual(service.session.post_kwargs["json"], {})
        self.assertIs(service.session.post_kwargs["allow_redirects"], False)

        service.session = FakeSession(
            get_response=FakeResponse(200, {"ok": False}),
            post_response=FakeResponse(400, {"error": "invalid_request"}),
        )
        with self.assertRaisesRegex(TorrentIntakeError, "invalid health"):
            await service.health()

    async def test_health_proves_the_loaded_bearer_without_mutating(self):
        service = TorrentIntakeService(base_url="http://intake.test")
        service.session = FakeSession(
            get_response=FakeResponse(200, {"ok": True}),
            post_response=FakeResponse(401, {"error": "unauthorized"}),
        )

        with self.assertRaisesRegex(TorrentIntakeError, "authentication receipt"):
            await service.health()

        self.assertEqual(service.session.post_kwargs["json"], {})

    async def test_submit_checks_hash_and_category_without_leaking_magnet(self):
        magnet = f"magnet:?xt=urn:btih:{self.HASH}&dn=private-title"
        service = TorrentIntakeService(base_url="http://intake.test")
        session = FakeSession(
            post_response=FakeResponse(
                201,
                {
                    "ok": True,
                    "info_hash": self.HASH,
                    "category": "movies",
                    "duplicate": False,
                },
            )
        )
        service.session = session

        result = await service.submit("movie", magnet)

        self.assertEqual(result.info_hash, self.HASH)
        self.assertEqual(result.category, "movies")
        self.assertIs(result.duplicate, False)
        self.assertEqual(session.post_kwargs["json"]["magnet"], magnet)
        self.assertIs(session.post_kwargs["allow_redirects"], False)

        session.post_response = FakeResponse(
            201,
            {
                "ok": True,
                "info_hash": "f" * 40,
                "category": "movies",
                "duplicate": False,
            },
        )
        with self.assertRaises(TorrentIntakeError) as raised:
            await service.submit("movie", magnet)
        self.assertNotIn("private-title", str(raised.exception))

    async def test_submit_requires_duplicate_to_be_an_actual_boolean(self):
        magnet = f"magnet:?xt=urn:btih:{self.HASH}&dn=private-title"
        service = TorrentIntakeService(base_url="http://intake.test")
        session = FakeSession()
        service.session = session
        valid_receipt = {
            "ok": True,
            "info_hash": self.HASH,
            "category": "movies",
        }

        for invalid_duplicate in (None, 0, 1, "false", [], {}):
            with self.subTest(duplicate=invalid_duplicate):
                session.post_response = FakeResponse(
                    200,
                    {**valid_receipt, "duplicate": invalid_duplicate},
                )
                with self.assertRaisesRegex(
                    TorrentIntakeError,
                    "invalid duplicate receipt",
                ) as raised:
                    await service.submit("movie", magnet)
                self.assertNotIn("private-title", str(raised.exception))

        session.post_response = FakeResponse(200, valid_receipt)
        with self.assertRaisesRegex(TorrentIntakeError, "invalid receipt"):
            await service.submit("movie", magnet)

        session.post_response = FakeResponse(
            200,
            {**valid_receipt, "duplicate": True},
        )
        result = await service.submit("movie", magnet)
        self.assertIs(result.duplicate, True)

    async def test_submit_requires_exact_status_and_payload_contract(self):
        magnet = f"magnet:?xt=urn:btih:{self.HASH}&dn=private-title"
        service = TorrentIntakeService(base_url="http://intake.test")
        session = FakeSession()
        service.session = session
        receipt = {
            "ok": True,
            "info_hash": self.HASH,
            "category": "movies",
            "duplicate": False,
        }

        session.post_response = FakeResponse(200, receipt)
        with self.assertRaisesRegex(TorrentIntakeError, "mismatched status"):
            await service.submit("movie", magnet)

        session.post_response = FakeResponse(201, {**receipt, "unexpected": True})
        with self.assertRaisesRegex(TorrentIntakeError, "invalid receipt"):
            await service.submit("movie", magnet)

        session.post_response = FakeResponse(307, receipt)
        with self.assertRaisesRegex(TorrentIntakeError, "rejected"):
            await service.submit("movie", magnet)
        self.assertIs(session.post_kwargs["allow_redirects"], False)


if __name__ == "__main__":
    unittest.main()
