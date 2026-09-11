"""Read-only setup checks, without starting Discord or importing the application.

Only static diagnostic text is returned. Credentials, response bodies, server
names, URLs, and exception messages are never included in the report.
"""

from __future__ import annotations

import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from mediabot.core.configuration import configured_timezone, validate_core_configuration


DISCORD_API = "https://discord.com/api/v10"
MAX_RESPONSE_BYTES = 512 * 1024
REQUEST_TIMEOUT = 10
MAX_GUILD_PAGES = 20
MEMBERS_FLAGS = (1 << 14) | (1 << 15)
CONTENT_FLAGS = (1 << 18) | (1 << 19)
BASE_PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 14) | (1 << 15) | (1 << 16)
ADMINISTRATOR = 1 << 3
FAILED = object()

ERROR_DETAILS = {
    "credentials": "Authentication was rejected. Verify the configured token or API key.",
    "not_found": "API endpoint was not found. Verify the base URL and supported service version.",
    "redirect": "The API redirected the request. Configure its final base URL; credentials were not forwarded.",
    "rate_limit": "The service is rate limited. Retry the check later.",
    "unavailable": "The service is unavailable. Check its service status and connectivity.",
    "tls": "TLS certificate validation failed. Correct the server certificate or trust configuration.",
    "response": "The service returned an unexpected or oversized response.",
    "request": "The connection failed. Check the address, DNS, port and network access from Docker.",
    "url": "Configure an HTTP(S) base URL without credentials, query, or fragment.",
}


class ProbeError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ERROR_DETAILS else "request"
        super().__init__(ERROR_DETAILS[self.code])


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def valid_base_url(value):
    try:
        parsed = urlsplit(value)
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname
                    and parsed.username is None and parsed.password is None
                    and not parsed.query and not parsed.fragment
                    and not any(ord(c) < 33 or ord(c) == 127 for c in value)
                    and (parsed.port is None or 1 <= parsed.port <= 65535))
    except (ValueError, TypeError):
        return False


def request_json(url, headers, *, timeout=REQUEST_TIMEOUT):
    """Perform one bounded GET. Never follow any redirect or use an env proxy."""
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "MediaBot-Setup/1", **headers}, method="GET")
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise ProbeError("response")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProbeError("response")
            return json.loads(raw)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        if code in {401, 403}:
            raise ProbeError("credentials") from None
        if code == 404:
            raise ProbeError("not_found") from None
        if 300 <= code < 400:
            raise ProbeError("redirect") from None
        if code == 429:
            raise ProbeError("rate_limit") from None
        raise ProbeError("unavailable" if code >= 500 else "response") from None
    except ssl.SSLError:
        raise ProbeError("tls") from None
    except URLError as exc:
        raise ProbeError("tls" if isinstance(exc.reason, ssl.SSLError) else "request") from None
    except (OSError, TimeoutError):
        raise ProbeError("request") from None
    except (ValueError, UnicodeError):
        raise ProbeError("response") from None


def positive_id(value):
    return ((type(value) is int and 0 < value < (1 << 64))
            or (isinstance(value, str) and value.isascii() and value.isdigit()
                and len(value) <= 20 and 0 < int(value) < (1 << 64)))


def unsigned_int(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 40:
        return int(value)
    raise ProbeError("response")


def allowed_guild_ids(value):
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values or not all(positive_id(item) for item in values):
        raise ValueError
    return {int(item) for item in values}


def fetch_guilds(request, headers, required):
    found, after = {}, 0
    for _ in range(MAX_GUILD_PAGES):
        url = DISCORD_API + "/users/@me/guilds?limit=200"
        if after:
            url += "&after=" + str(after)
        page = request(url, headers, timeout=REQUEST_TIMEOUT)
        if not isinstance(page, list) or len(page) > 200:
            raise ProbeError("response")
        for row in page:
            if not isinstance(row, dict) or not positive_id(row.get("id")):
                raise ProbeError("response")
            found[int(row["id"])] = row
        if required.issubset(found) or len(page) < 200:
            return found
        next_after = max(int(row["id"]) for row in page)
        if next_after <= after:
            raise ProbeError("response")
        after = next_after
    raise ProbeError("response")


def run_checks(environment=None, request=request_json):
    values = dict(os.environ if environment is None else environment)
    rows = []

    def add(check, ok, detail):
        rows.append({"check": check, "ok": bool(ok), "detail": detail})

    def probe(check, operation):
        try:
            return operation()
        except ProbeError as exc:
            add(check, False, ERROR_DETAILS[exc.code])
        except Exception:
            add(check, False, ERROR_DETAILS["request"])
        return FAILED

    try:
        guild_ids = allowed_guild_ids(values.get("ALLOWED_GUILD_IDS", ""))
        validate_core_configuration(
            discord_token=values.get("DISCORD_TOKEN", ""),
            seerr_url=values.get("SEERR_URL", ""),
            seerr_api_key=values.get("SEERR_API_KEY", ""),
            guild_ids=guild_ids,
        )
        if not valid_base_url(values["SEERR_URL"]):
            raise ValueError
    except (ValueError, RuntimeError, TypeError):
        add("Required configuration", False, "Set DISCORD_TOKEN, ALLOWED_GUILD_IDS, SEERR_URL and SEERR_API_KEY to valid values.")
        return rows
    add("Required configuration", True, "Required settings and allowed server IDs are valid.")
    try:
        for name in ("EVENT_TIMEZONE", "LIFE_TIMEZONE"):
            configured_timezone(name, environment=values)
        add("Timezone", True, "Event and Life timezone settings are valid.")
    except (ValueError, TypeError):
        add("Timezone", False, "TZ, EVENT_TIMEZONE and LIFE_TIMEZONE must use valid IANA timezone names.")

    discord_headers = {"Authorization": "Bot " + values["DISCORD_TOKEN"]}
    application = probe("Discord application", lambda: request(DISCORD_API + "/applications/@me", discord_headers, timeout=REQUEST_TIMEOUT))
    if application is not FAILED:
        if not isinstance(application, dict) or not positive_id(application.get("id")):
            add("Discord application", False, "The token did not return a valid bot application identity.")
        else:
            add("Discord application", True, "Bot token authenticated against the current application API.")
            try:
                flags = unsigned_int(application.get("flags_new", application.get("flags")))
                members, content = bool(flags & MEMBERS_FLAGS), bool(flags & CONTENT_FLAGS)
                add("Discord intents", members and content,
                    "Server Members and Message Content intents are enabled." if members and content else
                    "Enable both Server Members Intent and Message Content Intent in the Discord Developer Portal.")
            except ProbeError:
                add("Discord intents", False, "The application response did not expose usable intent flags.")
    guilds = probe("Discord server membership", lambda: fetch_guilds(request, discord_headers, guild_ids))
    if guilds is not FAILED:
        members = guild_ids.issubset(guilds)
        add("Discord server membership", members,
            "Bot is installed in every allowed server." if members else
            "Bot is missing from one or more allowed servers. Invite it to each configured server.")
        if members:
            try:
                permissions_ok = all(
                    guilds[ident].get("owner") is True
                    or (unsigned_int(guilds[ident].get("permissions")) & ADMINISTRATOR)
                    or (unsigned_int(guilds[ident].get("permissions")) & BASE_PERMISSIONS) == BASE_PERMISSIONS
                    for ident in guild_ids
                )
                add("Discord base permissions", permissions_ok,
                    "Base server permissions are present. Channel overrides still apply." if permissions_ok else
                    "Grant View Channels, Send Messages, Embed Links, Attach Files and Read Message History in every allowed server.")
            except ProbeError:
                add("Discord base permissions", False, "The guild response did not expose usable permission values.")

    seerr = probe("Seerr authentication", lambda: request(values["SEERR_URL"].rstrip("/") + "/api/v1/auth/me", {"X-Api-Key": values["SEERR_API_KEY"]}, timeout=REQUEST_TIMEOUT))
    if seerr is not FAILED:
        ok = isinstance(seerr, dict) and type(seerr.get("id")) is int and seerr["id"] > 0
        add("Seerr authentication", ok, "API key authenticated as a Seerr user." if ok else "Seerr did not return a valid authenticated user identity.")

    providers = (
        ("Jellyfin", "JELLYFIN", "/System/Info", "X-Emby-Token", ""),
        ("Sonarr", "SONARR", "/api/v3/system/status", "X-Api-Key", ""),
        ("SoulSync", "SOULSYNC", "/api/v1/system/status", "Authorization", "Bearer "),
    )
    for label, prefix, route, header, auth_prefix in providers:
        base, key = values.get(prefix + "_URL", "").strip(), values.get(prefix + "_API_KEY", "").strip()
        if not base and not key:
            add(label, True, "Disabled; no optional provider request was sent.")
            continue
        if not base or not key:
            add(label, False, "Configure both the provider URL and API key, or leave both blank.")
            continue
        if not valid_base_url(base):
            add(label, False, ERROR_DETAILS["url"])
            continue
        payload = probe(label, lambda: request(base.rstrip("/") + route, {header: auth_prefix + key}, timeout=REQUEST_TIMEOUT))
        if payload is FAILED:
            continue
        ok = isinstance(payload, dict)
        if prefix == "JELLYFIN":
            ok = ok and isinstance(payload.get("Id"), str) and bool(payload["Id"]) and isinstance(payload.get("Version"), str) and bool(payload["Version"])
        elif prefix == "SONARR":
            ok = ok and isinstance(payload.get("version"), str) and bool(payload["version"])
        else:
            data = payload.get("data") if ok else None
            ok = ok and payload.get("success") is True and isinstance(data, dict) and isinstance(data.get("services"), dict)
        add(label, ok, "Authenticated service status response received." if ok else "The API returned an unexpected service status response.")
    return rows


def main():
    try:
        rows = run_checks()
    except Exception:
        rows = [{"check": "Setup check", "ok": False, "detail": "The configuration could not be checked. Review .env and the installation guide."}]
    print(json.dumps(rows, separators=(",", ":")))
    return 0 if rows and all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
