"""Authenticated local AI routing with bounded web evidence and pinned models."""
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
    "gemma4:12b-it-qat": {
        "digest": "sha256:38044be4f923e5a55264ed7df4eaac2676651a905f735197c504045140c02bd3",
        "label": "Gemma 4 12B",
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


def validate_evidence(evidence):
    if not isinstance(evidence, list) or len(evidence) > 3:
        raise LocalAIError("Use up to three bounded web sources.")
    total = 0
    for index, source in enumerate(evidence, 1):
        if (not isinstance(source, dict)
                or set(source) != {"id", "title", "url", "text", "retrieved_at", "kind"}
                or any(not isinstance(value, str) for value in source.values())
                or source["id"] != "S" + str(index) or source["kind"] not in ("page", "snippet")
                or not source["text"].strip() or not 0 < len(source["title"]) <= 180
                or not 0 < len(source["retrieved_at"]) <= 40 or len(source["url"]) > 2048):
            raise LocalAIError("Invalid web evidence.")
        parsed = urlsplit(source["url"])
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(ord(c) < 33 for c in source["url"])):
            raise LocalAIError("Invalid web source URL.")
        total += len(source["text"].encode("utf-8"))
    if total > 6000:
        raise LocalAIError("Web evidence exceeds the context budget.")
    return evidence


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
                options["timeout"] = aiohttp.ClientTimeout(total=195 if "desktop" in payload.get("allowed_backends", []) else 135)
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
                    if code == "desktop_unavailable_no_fallback":
                        raise LocalAIError("Desktop AI is off, busy, or unavailable. Your account does not have server fallback access.")
                    if code in {"desktop_request_interrupted", "desktop_request_failed", "desktop_response_mismatch"}:
                        raise LocalAIError("Desktop AI did not finish this request. It was not retried on another GPU. Try again when the desktop is available.")
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

    async def status(self):
        await self.start()
        try:
            async with self.session.get(self.base_url + "/v1/status", allow_redirects=False,
                                        timeout=aiohttp.ClientTimeout(total=8)) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > 16384:
                        raise LocalAIError("AI gateway status is oversized.")
                if response.status != 200:
                    raise LocalAIError("AI gateway status is unavailable.")
                body = json.loads(raw)
                if (not isinstance(body, dict) or body.get("model") != MODEL
                        or body.get("model_manifest_sha256") != MODEL_DIGEST
                        or any(not isinstance(body.get(key), dict) or type(body[key].get("ready")) is not bool
                               for key in ("server", "desktop"))):
                    raise LocalAIError("AI gateway status needs the current gateway release.")
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise LocalAIError("AI gateway status is unavailable.") from exc

    async def chat(self, request_id, messages, *, profile="structured", evidence=None, allowed_backends=("server",)):
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
        evidence = validate_evidence([] if evidence is None else evidence)
        if (not isinstance(allowed_backends, (list, tuple)) or not 1 <= len(allowed_backends) <= 2
                or any(not isinstance(b, str) or b not in ("server", "desktop") for b in allowed_backends)
                or len(set(allowed_backends)) != len(allowed_backends)):
            raise LocalAIError("Your account has no permitted AI backend.")
        if profile == "structured" and (evidence or list(allowed_backends) != ["server"]):
            raise LocalAIError("Structured work uses the server without web search.")
        payload = {"request_id": request_id, "messages": messages}
        legacy_profile = False
        if profile == "conversation":
            payload["profile"] = profile
        if evidence:
            payload["evidence"] = evidence
        if list(allowed_backends) != ["server"]:
            payload["allowed_backends"] = list(allowed_backends)
        try:
            body = await self.request("/v1/chat", payload)
        except _ConversationProfileUnsupported:
            if evidence or list(allowed_backends) != ["server"]:
                raise LocalAIError("Web search and desktop routing need the current AI gateway release.") from None
            # A definite 400 means the legacy gateway did not admit this request.
            # Never retry a timeout, disconnect, resource failure, or ambiguous result.
            legacy_profile = True
            body = await self.request("/v1/chat", {"request_id": request_id, "messages": messages})
        model = body.get("model")
        backend = body.get("backend", "server")
        expected_sources = [{key: value for key, value in source.items() if key != "text"} for source in evidence]
        accepted = CONVERSATION_MODELS if profile == "conversation" and not legacy_profile else {MODEL: CONVERSATION_MODELS[MODEL]}
        if (body.get("request_id") != request_id or not isinstance(body.get("text"), str)
                or not body["text"].strip() or body.get("retrieval_used") is not bool(evidence)
                or body.get("sources") != expected_sources or body.get("tools_used") != (["web_search"] if evidence else [])
                or backend not in allowed_backends
                or (backend == "desktop") != (model == "gemma4:12b-it-qat")
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
