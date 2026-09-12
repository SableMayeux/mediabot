"""Fixed local text API. No note retrieval, web access, or state-changing tools."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import uuid

import aiohttp

MODEL = "llama3.2:3b-instruct-q4_K_M"
MODEL_DIGEST = "sha256:a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72"
CONVERSATION_MODELS = {
    MODEL: {"digest": MODEL_DIGEST, "label": "Llama 3.2 3B"},
    "qwen3.5:4b": {
        "digest": "sha256:2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd",
        "label": "Qwen 3.5 4B",
    },
}


class LocalAIError(RuntimeError):
    pass


class _ConversationProfileUnsupported(LocalAIError):
    """A legacy gateway definitively rejected the new field before inference."""


def validate_endpoint(value):
    text = str(value or "")
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        if (text != text.strip() or any(ord(char) < 33 for char in text)
                or parsed.scheme != "http" or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or (parsed.hostname, parsed.port) not in {
                    ("local-ai-gateway", 8080), ("127.0.0.1", 11888), ("localhost", 11888), ("::1", 11888)
                }):
            raise ValueError
    except ValueError as exc:
        raise LocalAIError("Local model URL must name the configured private gateway.") from exc
    return text.rstrip("/")


def validate_identity(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise LocalAIError("Invalid request identity.") from exc
    return value


class LocalAIService:
    def __init__(self, *, base_url=None, token_path=None):
        self.base_url = validate_endpoint(base_url if base_url is not None else os.getenv("LOCAL_AI_URL", ""))
        self.token_path = Path(token_path or os.getenv("LOCAL_AI_TOKEN_PATH", "/run/secrets/local_ai_token"))
        self.session = None
        self._session_lock = asyncio.Lock()

    @property
    def enabled(self):
        return bool(self.base_url)

    async def start(self):
        if not self.enabled:
            raise LocalAIError("Local conversation is not configured yet.")
        async with self._session_lock:
            if self.session and not self.session.closed:
                return
            try:
                token = self.token_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise LocalAIError("Local model credential is unavailable.") from exc
            if len(token) < 43 or not token.isascii() or any(c.isspace() for c in token):
                raise LocalAIError("Local model credential is invalid.")
            self.session = aiohttp.ClientSession(
                headers={"Authorization": "Bearer " + token},
                timeout=aiohttp.ClientTimeout(total=70), trust_env=False,
            )

    async def close(self):
        async with self._session_lock:
            if self.session and not self.session.closed:
                await self.session.close()
            self.session = None

    async def request(self, route, payload):
        if route not in {"/v1/chat", "/v1/cancel"}:
            raise LocalAIError("Unsupported local model request.")
        await self.start()
        try:
            options = {"json": payload, "allow_redirects": False}
            if payload.get("profile") == "conversation":
                options["timeout"] = aiohttp.ClientTimeout(total=135)
            async with self.session.post(self.base_url + route, **options) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise LocalAIError("Local model returned an oversized response.")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise LocalAIError("Local model returned an invalid response.")
                if route == "/v1/cancel" and response.status == 202:
                    return body
                if response.status != 200 or route == "/v1/cancel":
                    code = str(body.get("error", ""))
                    if response.status == 429:
                        raise LocalAIError("The local model is handling another request. Try again shortly.")
                    if response.status == 409 and code == "cancelled":
                        raise LocalAIError("Local generation cancelled.")
                    if code in {"gpu_or_memory_busy", "foreign_gpu_workload", "gpu_vram_low", "gpu_temperature", "host_memory_low"}:
                        raise LocalAIError("The local model is yielding to media activity or resource limits. Try again when the GPU is free.")
                    if code in {"gpu_monitor_stale", "gpu_monitor_unavailable", "gpu_monitor_failed", "gpu_monitor_stopped"}:
                        raise LocalAIError("The local model's GPU safety monitor is unavailable. Try again after it recovers.")
                    if code == "request_timeout":
                        raise LocalAIError("Local generation reached its deadline. Try a shorter question.")
                    if (response.status == 400 and code == "invalid_request"
                            and payload.get("profile") == "conversation"):
                        raise _ConversationProfileUnsupported("The gateway uses the older generation protocol.")
                    if response.status == 400:
                        raise LocalAIError("Keep the conversation under 3000 UTF-8 bytes. Start a new question for a longer topic.")
                    raise LocalAIError("The local model is unavailable. No task or calendar action was taken.")
        except LocalAIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise LocalAIError("The local model did not finish. It cannot change tasks or calendar events.") from exc
        return body

    async def chat(self, request_id, messages, *, profile="structured"):
        validate_identity(request_id)
        if profile not in ("structured", "conversation"):
            raise LocalAIError("Unsupported generation profile.")
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 12
                or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                       or m["role"] not in ("user", "assistant") or not isinstance(m["content"], str) for m in messages)
                or messages[-1]["role"] != "user" or not messages[-1]["content"].strip()):
            raise LocalAIError("Invalid conversation.")
        if sum(len(m["content"].encode("utf-8")) for m in messages) > 3000:
            raise LocalAIError("Keep the conversation under 3000 UTF-8 bytes.")
        payload = {"request_id": request_id, "messages": messages}
        legacy_profile = False
        if profile == "conversation":
            payload["profile"] = profile
        try:
            body = await self.request("/v1/chat", payload)
        except _ConversationProfileUnsupported:
            # A definite 400 means the legacy gateway did not admit this request.
            # Never retry a timeout, disconnect, resource failure, or ambiguous result.
            legacy_profile = True
            body = await self.request("/v1/chat", {"request_id": request_id, "messages": messages})
        model = body.get("model")
        accepted = CONVERSATION_MODELS if profile == "conversation" and not legacy_profile else {MODEL: CONVERSATION_MODELS[MODEL]}
        if (body.get("request_id") != request_id or not isinstance(body.get("text"), str)
                or not body["text"].strip() or body.get("retrieval_used") is not False
                or body.get("sources") != [] or body.get("tools_used") != []
                or not isinstance(model, str) or model not in accepted
                or body.get("model_manifest_sha256") != accepted[model]["digest"]):
            raise LocalAIError("The local model returned a mismatched response.")
        if (profile == "conversation" and not legacy_profile and body.get("profile") != "conversation"
                or "profile" in body and body["profile"] != ("structured" if legacy_profile else profile)):
            raise LocalAIError("The local model returned a mismatched generation profile.")
        if legacy_profile:
            body = {**body, "legacy_profile": True}
        return body

    async def cancel(self, request_id):
        validate_identity(request_id)
        body = await self.request("/v1/cancel", {"request_id": request_id})
        if body.get("request_id") != request_id or body.get("cancel_requested") is not True or not isinstance(body.get("active"), bool):
            raise LocalAIError("The local model returned a mismatched cancellation receipt.")
        return body
