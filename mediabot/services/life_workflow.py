"""Owner-only client for fixed Life operations; Nextcloud credentials stay outside the bot."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp


class LifeWorkflowError(RuntimeError):
    pass


class LifeWorkflowService:
    ROUTES = {
        "captures": "captures/list", "tasks": "tasks/list",
        "create_task": "tasks/create", "complete_task": "tasks/complete",
        "create_event": "events/create",
    }

    def __init__(self, *, base_url=None, token_path=None):
        self.base_url = str(base_url if base_url is not None else os.getenv("LIFE_GATEWAY_URL", "")).rstrip("/")
        self.token_path = Path(token_path or os.getenv("LIFE_GATEWAY_TOKEN_PATH", "/run/secrets/life_gateway_token"))
        self.session = None

    @property
    def enabled(self):
        return bool(self.base_url)

    async def start(self):
        if not self.enabled:
            raise LifeWorkflowError("Life actions are not configured. Your raw captures are still saved.")
        if self.session and not self.session.closed:
            return
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise LifeWorkflowError("Life service address is invalid.")
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LifeWorkflowError("Life service credential is unavailable. Your raw captures are safe.") from exc
        if len(token) < 43 or any(c.isspace() for c in token):
            raise LifeWorkflowError("Life service credential is invalid.")
        self.session = aiohttp.ClientSession(headers={"Authorization": "Bearer " + token}, timeout=aiohttp.ClientTimeout(total=25))

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def request(self, action, *, actor_id, **fields):
        if action not in self.ROUTES or type(actor_id) is not int or actor_id <= 0:
            raise LifeWorkflowError("Invalid Life operation.")
        await self.start()
        try:
            async with self.session.post(self.base_url + "/v1/life/" + self.ROUTES[action],
                    json={**fields, "actor_id": str(actor_id)}, allow_redirects=False) as response:
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 262144:
                        raise LifeWorkflowError("Life service response exceeded its limit.")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                body = json.loads(raw)
                if response.status not in {200, 201}:
                    code = body.get("error") if isinstance(body, dict) else ""
                    messages = {
                        "forbidden": "This is private to the configured owner.",
                        "forbidden_capture": "That raw capture belongs to a different owner.",
                        "unauthorized": "The Life connection credential was rejected. Your raw captures are safe.",
                        "conflict": "The task changed in Nextcloud. Refresh before completing it.",
                        "stale_task": "The task changed in Nextcloud. Refresh before completing it.",
                        "precondition_failed": "The task changed in Nextcloud. Refresh before completing it.",
                        "not_found": "That item is unavailable. Refresh the list.",
                        "capture_not_found": "The original capture could not be found. It was not replaced or deleted.",
                        "recurring_task": "Complete this recurring task in Nextcloud so its recurrence is preserved.",
                        "already_promoted": "This capture already has this kind of action. Open Nextcloud to inspect or edit it.",
                        "idempotency_conflict": "This proposal conflicts with its earlier receipt. Open a fresh review.",
                        "request_conflict": "This proposal conflicts with its earlier receipt. Open a fresh review.",
                        "shared_scheduling": "Edit this scheduled task in Nextcloud so its attendees are preserved.",
                        "invalid_request": "The proposed fields were rejected. Check the title and dates.",
                        "invalid_text": "Use a title of 1 to 200 characters without control characters.",
                        "invalid_time": "Check the dates, explicit time zone offsets, and event start/end order.",
                        "invalid_reminder": "Choose a reminder from 0 to 10080 minutes before the event.",
                        "past_reminder": "The reminder time has already passed. Open a fresh proposal with a future reminder.",
                        "upstream_unavailable": "Nextcloud did not confirm the action. Retry this same proposal to reconcile it.",
                        "verification_failed": "Nextcloud did not return the expected item. Keep this proposal and inspect Nextcloud before proceeding.",
                    }
                    raise LifeWorkflowError(messages.get(code, "Nextcloud did not confirm this action. Refresh before making a different proposal."))
        except LifeWorkflowError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise LifeWorkflowError("Life service did not confirm the action. The same proposal can be retried safely.") from exc
        if not isinstance(body, dict):
            raise LifeWorkflowError("Life service returned an invalid receipt.")
        if action in {"captures", "tasks"}:
            if not isinstance(body.get("items"), list) or len(body["items"]) > 50 or any(not isinstance(item, dict) for item in body["items"]):
                raise LifeWorkflowError("Life service returned an invalid list.")
            for item in body["items"]:
                pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}" if action == "captures" else r"[0-9a-f]{64}"
                if not re.fullmatch(pattern, str(item.get("id", ""))) or not isinstance(item.get("title"), str):
                    raise LifeWorkflowError("Life service returned an invalid list item.")
                time_key = "created_at" if action == "captures" else "due_at"
                if item.get(time_key) is not None and not isinstance(item[time_key], str):
                    raise LifeWorkflowError("Life service returned an invalid item time.")
                if action == "tasks":
                    if (not isinstance(item.get("etag"), str) or not item["etag"] or len(item["etag"]) > 256
                            or not isinstance(item.get("status"), str)
                            or ("recurring" in item and type(item["recurring"]) is not bool)):
                        raise LifeWorkflowError("Life service returned an incomplete task.")
                    reminder = item.get("reminder")
                    if reminder is not None and (not isinstance(reminder, dict)
                            or not isinstance(reminder.get("at"), str)
                            or reminder.get("status") not in {"scheduled", "sending", "delivered", "uncertain", "cancelled", "failed"}):
                        raise LifeWorkflowError("Life service returned an invalid reminder state.")
        else:
            entity = body.get("event" if action == "create_event" else "task")
            receipt = body.get("receipt")
            if (not isinstance(receipt, dict) or not isinstance(entity, dict)
                    or receipt.get("request_id") != fields.get("request_id")
                    or receipt.get("operation") != self.ROUTES[action]
                    or not re.fullmatch(r"[0-9a-f]{64}", str(entity.get("id", "")))
                    or receipt.get("resource_id") != entity.get("id")
                    or (action == "complete_task" and entity.get("id") != fields.get("task_id"))
                    or ("capture_id" in fields and (receipt.get("capture_id") != fields["capture_id"] or entity.get("capture_id") != fields["capture_id"]))):
                raise LifeWorkflowError("Life service returned a mismatched action receipt. Refresh before proceeding.")
        return body
