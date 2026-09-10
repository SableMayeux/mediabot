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


class LocalAIError(RuntimeError):
    pass


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
            async with self.session.post(self.base_url + route, json=payload, allow_redirects=False) as response:
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
                    if response.status == 400:
                        raise LocalAIError("Keep the conversation under 3000 UTF-8 bytes. Start a new question for a longer topic.")
                    raise LocalAIError("The local model is unavailable. No task or calendar action was taken.")
        except LocalAIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise LocalAIError("The local model did not finish. It cannot change tasks or calendar events.") from exc
        return body

    async def chat(self, request_id, messages):
        validate_identity(request_id)
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 12
                or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                       or m["role"] not in ("user", "assistant") or not isinstance(m["content"], str) for m in messages)
                or messages[-1]["role"] != "user" or not messages[-1]["content"].strip()):
            raise LocalAIError("Invalid conversation.")
        if sum(len(m["content"].encode("utf-8")) for m in messages) > 3000:
            raise LocalAIError("Keep the conversation under 3000 UTF-8 bytes.")
        body = await self.request("/v1/chat", {"request_id": request_id, "messages": messages})
        if (body.get("request_id") != request_id or not isinstance(body.get("text"), str)
                or not body["text"].strip() or body.get("retrieval_used") is not False
                or body.get("sources") != [] or body.get("tools_used") != []
                or body.get("model") != MODEL or body.get("model_manifest_sha256") != MODEL_DIGEST):
            raise LocalAIError("The local model returned a mismatched response.")
        return body

    async def cancel(self, request_id):
        validate_identity(request_id)
        body = await self.request("/v1/cancel", {"request_id": request_id})
        if body.get("request_id") != request_id or body.get("cancel_requested") is not True or not isinstance(body.get("active"), bool):
            raise LocalAIError("The local model returned a mismatched cancellation receipt.")
        return body