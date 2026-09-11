import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
import ssl

from mediabot.core import setup_check as checker


class SetupCheckTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "DISCORD_TOKEN": "private-discord-sentinel",
            "ALLOWED_GUILD_IDS": "123,456",
            "SEERR_URL": "https://seerr.test/subpath",
            "SEERR_API_KEY": "private-seerr-sentinel",
            "TZ": "UTC",
        }
        self.application = {"id": "999", "flags": checker.MEMBERS_FLAGS | checker.CONTENT_FLAGS}
        self.guilds = [{"id": str(ident), "permissions": str(checker.BASE_PERMISSIONS)} for ident in (123, 456)]
        self.seerr = {"id": 1, "email": "private@example.test"}
        self.requests = []
        self.optional = {}

    def request(self, url, headers, *, timeout):
        self.requests.append((url, headers, timeout))
        if url.endswith("/applications/@me"):
            return self.application
        if "/users/@me/guilds?" in url:
            return self.guilds
        if url.endswith("/api/v1/auth/me"):
            return self.seerr
        for name in self.optional:
            if name in url:
                return self.optional[name]
        raise RuntimeError("private-unexpected-url-sentinel " + url)

    def check(self):
        return checker.run_checks(self.environment, self.request)

    def row(self, label):
        return next(row for row in self.check() if row["check"] == label)

    def test_minimal_install_checks_authentication_with_optional_providers_disabled(self):
        rows = self.check()
        self.assertTrue(all(row["ok"] for row in rows))
        self.assertTrue(all(set(row) == {"check", "ok", "detail"} for row in rows))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[0][0], "https://discord.com/api/v10/applications/@me")
        self.assertEqual(self.requests[0][1], {"Authorization": "Bot private-discord-sentinel"})
        self.assertEqual(self.requests[2][0], "https://seerr.test/subpath/api/v1/auth/me")
        self.assertEqual(self.requests[2][1], {"X-Api-Key": "private-seerr-sentinel"})
        self.assertNotIn("private", json.dumps(rows))

    def test_required_settings_fail_before_any_network_request(self):
        for field in ("DISCORD_TOKEN", "ALLOWED_GUILD_IDS", "SEERR_URL", "SEERR_API_KEY"):
            with self.subTest(field=field):
                env = {**self.environment, field: ""}
                request = Mock()
                rows = checker.run_checks(env, request)
                self.assertFalse(rows[0]["ok"])
                request.assert_not_called()

    def test_invalid_urls_ids_and_timezone_have_static_diagnostics(self):
        for changes in (
            {"SEERR_URL": "https://username:private-secret@seerr.test"},
            {"SEERR_URL": "https://seerr.test:0"},
            {"ALLOWED_GUILD_IDS": "private-invalid-guild"},
            {"TZ": "private-invalid-timezone"},
        ):
            rows = checker.run_checks({**self.environment, **changes}, self.request)
            self.assertFalse(all(row["ok"] for row in rows))
            self.assertNotIn("private", json.dumps(rows))

    def test_both_privileged_intents_are_required(self):
        for flags in (0, 1 << 15, 1 << 19):
            self.application["flags"] = flags
            self.assertFalse(self.row("Discord intents")["ok"])
        for flags in ((1 << 14) | (1 << 18), (1 << 15) | (1 << 19)):
            self.application["flags"] = flags
            self.assertTrue(self.row("Discord intents")["ok"])

    def test_string_flags_and_bad_application_identity(self):
        self.application = {"id": "999", "flags_new": str((1 << 15) | (1 << 19))}
        self.assertTrue(self.row("Discord intents")["ok"])
        for payload in ({"id": True}, {"id": "0"}, None, []):
            self.application = payload
            self.assertFalse(self.row("Discord application")["ok"])

    def test_missing_guild_and_base_permissions_are_reported(self):
        self.guilds = self.guilds[:1]
        self.assertFalse(self.row("Discord server membership")["ok"])
        self.guilds += [{"id": "456", "permissions": "0"}]
        self.assertFalse(self.row("Discord base permissions")["ok"])
        self.guilds[-1]["permissions"] = str(checker.ADMINISTRATOR)
        self.assertTrue(self.row("Discord base permissions")["ok"])
        self.assertIn("Channel overrides", self.row("Discord base permissions")["detail"])

    def test_paginated_guild_membership_checks_after_cursor(self):
        pages = [[{"id": str(number), "permissions": str(checker.BASE_PERMISSIONS)} for number in range(1, 201)], [{"id": "456", "permissions": str(checker.BASE_PERMISSIONS)}]]
        calls = []
        def request(url, headers, *, timeout):
            calls.append(url)
            return pages.pop(0)
        result = checker.fetch_guilds(request, {"Authorization": "Bot secret"}, {123, 456})
        self.assertIn(456, result)
        self.assertEqual(calls[-1], checker.DISCORD_API + "/users/@me/guilds?limit=200&after=200")

    def test_nonadvancing_pagination_is_rejected(self):
        page = [{"id": str(number)} for number in range(1, 201)]
        with self.assertRaises(checker.ProbeError):
            checker.fetch_guilds(lambda *args, **kwargs: page, {}, {456})

    def test_seerr_requires_real_positive_integer_identity(self):
        for payload in ({"id": True}, {"id": "1"}, {"id": 0}, {}, None):
            self.seerr = payload
            self.assertFalse(self.row("Seerr authentication")["ok"])

    def test_partial_optional_pair_fails_without_calling_it(self):
        for prefix in ("JELLYFIN", "SONARR", "SOULSYNC"):
            for field in ("_URL", "_API_KEY"):
                self.requests = []
                rows = checker.run_checks({**self.environment, prefix + field: "https://optional.test" if field == "_URL" else "private-key"}, self.request)
                label = {"JELLYFIN": "Jellyfin", "SONARR": "Sonarr", "SOULSYNC": "SoulSync"}[prefix]
                self.assertFalse(next(row for row in rows if row["check"] == label)["ok"])
                self.assertEqual(len(self.requests), 3)

    def test_provider_endpoints_auth_headers_and_minimal_schemas(self):
        for prefix in ("JELLYFIN", "SONARR", "SOULSYNC"):
            self.environment[prefix + "_URL"] = "https://" + prefix.lower() + ".test"
            self.environment[prefix + "_API_KEY"] = "private-" + prefix.lower()
        self.optional = {
            "jellyfin.test": {"Id": "server-id", "Version": "10.11.0"},
            "sonarr.test": {"version": "4.0.0"},
            "soulsync.test": {"success": True, "data": {"services": {"soulseek": True}}},
        }
        self.assertTrue(all(row["ok"] for row in self.check()))
        self.assertEqual(self.requests[-3][:2], ("https://jellyfin.test/System/Info", {"X-Emby-Token": "private-jellyfin"}))
        self.assertEqual(self.requests[-2][:2], ("https://sonarr.test/api/v3/system/status", {"X-Api-Key": "private-sonarr"}))
        self.assertEqual(self.requests[-1][:2], ("https://soulsync.test/api/v1/system/status", {"Authorization": "Bearer private-soulsync"}))
        self.optional["soulsync.test"] = {"success": False, "error": {"message": "private-server-error"}}
        rows = self.check()
        self.assertFalse(next(row for row in rows if row["check"] == "SoulSync")["ok"])
        self.assertNotIn("private", json.dumps(rows))

    def test_arbitrary_exception_text_never_reaches_report(self):
        request = Mock(side_effect=RuntimeError("private-token private-body https://private-url.test"))
        rows = checker.run_checks(self.environment, request)
        self.assertNotIn("private", json.dumps(rows))
        self.assertFalse(all(row["ok"] for row in rows))

    def test_tls_failure_has_static_diagnostic(self):
        opener = Mock()
        opener.open.side_effect = URLError(ssl.SSLCertVerificationError("private-certificate-error"))
        with patch.object(checker, "build_opener", return_value=opener):
            with self.assertRaises(checker.ProbeError) as caught:
                checker.request_json("https://server.test", {"Authorization": "secret"})
        self.assertEqual(caught.exception.code, "tls")
        self.assertNotIn("private", str(caught.exception))


class HTTPTransportTests(unittest.TestCase):
    def setUp(self):
        self.hits = []
        hits = self.hits
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.path, self.headers.get("Authorization")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.end_headers()
                    return
                if self.path in {"/401", "/403", "/404", "/429", "/503"}:
                    self.send_response(int(self.path[1:]))
                    self.end_headers()
                    self.wfile.write(b"private-error-body")
                    return
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(b"x" * (checker.MAX_RESPONSE_BYTES + 1) if self.path == "/oversized" else b'{"ok":true}')
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_redirect_does_not_forward_credentials_or_request_target(self):
        with self.assertRaises(checker.ProbeError) as caught:
            checker.request_json(self.url + "/redirect", {"Authorization": "private-token"})
        self.assertEqual(caught.exception.code, "redirect")
        self.assertEqual(self.hits, [("/redirect", "private-token")])
        self.assertNotIn("private", str(caught.exception))

    def test_http_failure_details_are_bounded_and_sanitized(self):
        for status, code in ((401, "credentials"), (403, "credentials"), (404, "not_found"), (429, "rate_limit"), (503, "unavailable")):
            with self.assertRaises(checker.ProbeError) as caught:
                checker.request_json(self.url + "/" + str(status), {})
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("private", str(caught.exception))

    def test_oversized_response_is_rejected(self):
        with self.assertRaises(checker.ProbeError) as caught:
            checker.request_json(self.url + "/oversized", {})
        self.assertEqual(caught.exception.code, "response")


class CaptureStartupTests(unittest.TestCase):
    def test_configured_inbox_is_created_before_providers_and_preserves_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ.setdefault("LOG_PATH", str(Path(tempfile.gettempdir()) / "mediabot-setup-check-test.log"))
            os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "mediabot-setup-check-test.db"))
            import app
            from mediabot.services.life_capture import LifeCaptureService
            inbox = Path(directory) / "capture"
            with patch.multiple(app, DISCORD_TOKEN="test", SEERR_API_KEY="test", SEERR_URL="http://seerr.test", ALLOWED_GUILD_IDS={123}, life_capture=LifeCaptureService(inbox)), patch.object(app, "init_db", side_effect=RuntimeError("test-stop-before-network")):
                with self.assertRaisesRegex(RuntimeError, "test-stop-before-network"):
                    asyncio.run(app.main())
                self.assertTrue(inbox.is_dir())
                existing = inbox / "personal.md"
                existing.write_bytes(b"untouched capture")
                with self.assertRaisesRegex(RuntimeError, "test-stop-before-network"):
                    asyncio.run(app.main())
                self.assertEqual(existing.read_bytes(), b"untouched capture")


if __name__ == "__main__":
    unittest.main()
