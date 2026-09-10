import asyncio
import unittest

from mediabot.services.torrent_intake import TorrentInputError, TorrentIntakeError, TorrentIntakeService


REVIEW_ID = "1" * 32
INFO_HASH = "2" * 40


class Response:
    def __init__(self, status=200, payload=None, error=None):
        self.status, self.payload, self.error = status, payload, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return self.payload


class Session:
    closed = False

    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def status(**changes):
    return {"id": REVIEW_ID, "info_hash": INFO_HASH, "actor_id": "42", "phase": "ready", **changes}


class TorrentReviewClientTests(unittest.IsolatedAsyncioTestCase):
    def service(self, payload=None, status_code=200, error=None):
        client = TorrentIntakeService(base_url="http://intake.test")
        client.session = Session(Response(status_code, payload, error))
        return client

    async def test_only_fixed_review_routes_are_callable(self):
        for action, identity in [("../torrents/start", REVIEW_ID), ("approve", "../escape"), ("open", REVIEW_ID), ("list", REVIEW_ID)]:
            with self.subTest(action=action, identity=identity):
                client = self.service(status())
                with self.assertRaises(TorrentInputError):
                    await client.review_request(action, actor_id=42, actor_role="owner", review_id=identity)
                self.assertEqual(client.session.calls, [])

    async def test_only_owner_and_admin_can_send_review_requests(self):
        for role in ("member", "Owner", "", None):
            with self.subTest(role=role):
                client = self.service(status())
                with self.assertRaises(TorrentInputError):
                    await client.review_request("list", actor_id=42, actor_role=role)
                self.assertEqual(client.session.calls, [])

    async def test_redirects_never_forward_review_identity_or_token(self):
        client = self.service({"error": "redirect"}, 302)
        with self.assertRaises(TorrentIntakeError):
            await client.review_request("status", actor_id=42, actor_role="owner", review_id=REVIEW_ID)
        url, kwargs = client.session.calls[0]
        self.assertEqual(url, f"http://intake.test/v1/reviews/{REVIEW_ID}/status")
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["json"], {"actor_id": "42", "actor_role": "owner"})

    async def test_approval_sends_exact_manifest_and_selection(self):
        client = self.service(status(phase="approved", receipt={"selected_indexes": [0, 4]}))
        await client.review_request("approve", actor_id=42, actor_role="admin", review_id=REVIEW_ID,
                                    manifest_sha256="a" * 64, selected_indexes=[0, 4])
        self.assertEqual(client.session.calls[0][1]["json"], {
            "actor_id": "42", "actor_role": "admin", "manifest_sha256": "a" * 64,
            "selected_indexes": [0, 4],
        })

    async def test_uncertain_approval_is_not_automatically_retried(self):
        client = self.service(error=asyncio.TimeoutError())
        with self.assertRaisesRegex(TorrentIntakeError, "Refresh before retrying"):
            await client.review_request("approve", actor_id=42, actor_role="owner", review_id=REVIEW_ID,
                                        manifest_sha256="a" * 64, selected_indexes=[0])
        self.assertEqual(len(client.session.calls), 1)

    async def test_private_upstream_error_detail_is_not_echoed(self):
        client = self.service({"error": "unknown", "detail": "private-magnet-token-and-path"}, 503)
        with self.assertRaises(TorrentIntakeError) as caught:
            await client.review_request("status", actor_id=42, actor_role="owner", review_id=REVIEW_ID)
        self.assertNotIn("private-magnet", str(caught.exception))

    async def test_status_rejects_a_different_review_receipt(self):
        client = self.service(status(id="9" * 32))
        with self.assertRaises(TorrentIntakeError):
            await client.review_request("status", actor_id=42, actor_role="owner", review_id=REVIEW_ID)

    async def test_session_response_is_bound_to_the_actor(self):
        client = self.service(status(actor_id="99"))
        with self.assertRaises(TorrentIntakeError):
            await client.review_request("status", actor_id=42, actor_role="owner", review_id=REVIEW_ID)

    async def test_open_response_is_bound_to_the_requested_job(self):
        client = self.service(status(info_hash="9" * 40))
        with self.assertRaises(TorrentIntakeError):
            await client.review_request("open", actor_id=42, actor_role="owner", info_hash=INFO_HASH)

    async def test_retry_can_return_a_new_session_for_the_same_actor(self):
        client = self.service(status(id="9" * 32, phase="metadata"))
        result = await client.review_request("retry", actor_id=42, actor_role="owner", review_id=REVIEW_ID)
        self.assertEqual(result["id"], "9" * 32)

    async def test_backend_stale_manifest_error_explains_refresh(self):
        client = self.service({"error": "stale_manifest"}, 409)
        with self.assertRaisesRegex(TorrentIntakeError, "[Ff]ile list changed|[Mm]anifest changed"):
            await client.review_request("approve", actor_id=42, actor_role="owner", review_id=REVIEW_ID,
                                        manifest_sha256="a" * 64, selected_indexes=[0])


if __name__ == "__main__":
    unittest.main()
