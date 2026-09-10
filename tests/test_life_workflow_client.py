import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mediabot.services.life_workflow import LifeWorkflowError, LifeWorkflowService


CAPTURE_ID = "91395e62-b056-4918-890b-81aa3669ca3d"
REQUEST_ID = "323d8a34-e802-4856-9339-83b81ca0c2c0"
TASK_ID = "a" * 64


class Content:
    def __init__(self, payload, chunk_size=7):
        self.payload, self.chunk_size = payload, chunk_size

    async def iter_chunked(self, maximum):
        for offset in range(0, len(self.payload), min(maximum, self.chunk_size)):
            yield self.payload[offset:offset + min(maximum, self.chunk_size)]


class Response:
    def __init__(self, payload, status=200, error=None):
        self.status, self.error = status, error
        self.content = Content(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    closed = False

    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def action_receipt(action="tasks/create", **changes):
    task = {"id": TASK_ID, "title": "Action", "capture_id": CAPTURE_ID, "status": "NEEDS-ACTION"}
    return {"task": task, "receipt": {"request_id": REQUEST_ID, "operation": action, "resource_id": TASK_ID, "capture_id": CAPTURE_ID}, **changes}


class LifeClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, body=None, status=200, error=None):
        result = LifeWorkflowService(base_url="http://life-gateway:8091")
        result.session = Session(Response(body if body is not None else {"items": []}, status, error))
        return result

    async def test_only_fixed_actions_and_decimal_actor_are_accepted(self):
        for action, actor in (("../admin", 42), ("tasks", True), ("tasks", -1), ("tasks", "42")):
            client = self.client()
            with self.assertRaises(LifeWorkflowError):
                await client.request(action, actor_id=actor)
            self.assertEqual(client.session.calls, [])

    async def test_chunked_response_is_fully_read_and_redirects_disabled(self):
        client = self.client({"items": [{"id": TASK_ID, "title": "A native task", "etag": '"abc"', "status": "NEEDS-ACTION"}], "task_reminders_supported": False})
        result = await client.request("tasks", actor_id=42)
        self.assertEqual(result["items"][0]["title"], "A native task")
        url, options = client.session.calls[0]
        self.assertEqual(url, "http://life-gateway:8091/v1/life/tasks/list")
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(options["json"], {"actor_id": "42"})

    async def test_response_size_limit_enforced_across_chunks(self):
        client = self.client(b" " * 262145)
        with self.assertRaisesRegex(LifeWorkflowError, "exceeded"):
            await client.request("tasks", actor_id=42)

    async def test_current_gateway_errors_have_actionable_mapping(self):
        for code, expected in (("conflict", "Refresh"), ("request_conflict", "fresh review"), ("forbidden_capture", "different owner"), ("past_reminder", "future reminder"), ("shared_scheduling", "attendees"), ("already_promoted", "Open Nextcloud")):
            client = self.client({"error": code, "detail": "private-secret-must-not-echo"}, 409)
            with self.assertRaises(LifeWorkflowError) as caught:
                await client.request("tasks", actor_id=42)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("private-secret", str(caught.exception))

    async def test_unknown_errors_never_echo_upstream_private_detail(self):
        client = self.client({"error": "unknown", "detail": "private-secret-must-not-echo"}, 502)
        with self.assertRaises(LifeWorkflowError) as caught:
            await client.request("tasks", actor_id=42)
        self.assertNotIn("private-secret", str(caught.exception))

    async def test_uncertain_transport_is_not_automatically_retried(self):
        client = self.client(error=asyncio.TimeoutError())
        with self.assertRaisesRegex(LifeWorkflowError, "same proposal"):
            await client.request("create_task", actor_id=42, capture_id=CAPTURE_ID, title="Action", request_id=REQUEST_ID)
        self.assertEqual(len(client.session.calls), 1)
        self.assertEqual(client.session.calls[0][1]["json"]["request_id"], REQUEST_ID)

    async def test_success_receipt_is_bound_to_request_capture_operation_and_resource(self):
        good = action_receipt()
        client = self.client(good)
        self.assertEqual(await client.request("create_task", actor_id=42, capture_id=CAPTURE_ID, title="Action", request_id=REQUEST_ID), good)
        for mutation in (lambda x: x["receipt"].update(request_id="other"), lambda x: x["receipt"].update(operation="events/create"), lambda x: x["receipt"].update(resource_id="b" * 64), lambda x: x["task"].update(capture_id="other"), lambda x: x["task"].update(id=None)):
            body = action_receipt()
            mutation(body)
            with self.assertRaisesRegex(LifeWorkflowError, "mismatched"):
                await self.client(body).request("create_task", actor_id=42, capture_id=CAPTURE_ID, title="Action", request_id=REQUEST_ID)

    async def test_completion_receipt_cannot_confirm_different_task(self):
        body = action_receipt("tasks/complete")
        with self.assertRaisesRegex(LifeWorkflowError, "mismatched"):
            await self.client(body).request("complete_task", actor_id=42, task_id="b" * 64, etag='"x"', request_id=REQUEST_ID)

    async def test_oversize_or_nonobject_lists_rejected(self):
        for body in ({"items": [None]}, {"items": [{}] * 51}, {"items": "bad"}):
            with self.assertRaisesRegex(LifeWorkflowError, "invalid list"):
                await self.client(body).request("tasks", actor_id=42)

    async def test_list_items_cannot_crash_selection_and_completion_ui(self):
        good = {"id": TASK_ID, "title": "Action", "etag": '"a"', "status": "NEEDS-ACTION", "due_at": None}
        for change in ({"id": "bad"}, {"title": None}, {"etag": None}, {"status": None}, {"due_at": 123}, {"reminder": "bad"}, {"reminder": {"at": 123, "status": "scheduled"}}):
            with self.assertRaises(LifeWorkflowError):
                await self.client({"items": [{**good, **change}]}).request("tasks", actor_id=42)
        self.assertEqual((await self.client({"items": [good]}).request("tasks", actor_id=42))["items"], [good])
        with self.assertRaises(LifeWorkflowError):
            await self.client({"items": [{"id": CAPTURE_ID, "title": "Capture", "created_at": 123}]}).request("captures", actor_id=42)

    async def test_origin_path_and_credentials_in_url_are_rejected(self):
        for url in ("http://life-gateway:8091/surprise", "http://user:password@life-gateway", "file:///tmp/x", "http://life-gateway?secret=x"):
            client = LifeWorkflowService(base_url=url)
            with self.assertRaisesRegex(LifeWorkflowError, "address is invalid"):
                await client.start()

    async def test_missing_or_malformed_token_never_opens_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            client = LifeWorkflowService(base_url="http://life-gateway:8091", token_path=path)
            with self.assertRaises(LifeWorkflowError):
                await client.start()
            path.write_text("bad token", encoding="utf-8")
            with self.assertRaises(LifeWorkflowError):
                await client.start()
            self.assertIsNone(client.session)


if __name__ == "__main__":
    unittest.main()
