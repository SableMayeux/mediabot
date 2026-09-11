"""Exercise the real application startup, stopping at Discord's boundary."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import traceback


DISABLED_ENDPOINTS = (
    "TORRENT_INTAKE_URL", "LIFE_GATEWAY_URL", "LOCAL_AI_URL",
    "NEXTCLOUD_PUBLIC_ORIGIN", "JELLYFIN_URL", "JELLYFIN_API_KEY",
    "SOULSYNC_URL", "SOULSYNC_API_KEY", "SONARR_URL", "SONARR_API_KEY",
)
SENTINEL_USER = 900000000000000001


async def exercise(expected_run):
    assert os.getuid() == os.getgid() == 1000, "Application UID/GID changed"
    assert all(not os.getenv(key) for key in DISABLED_ENDPOINTS), "Optional integration inherited"
    assert os.getenv("LIFE_CAPTURE_PATH") == "/app/data/life-inbox"
    assert os.getenv("SEERR_URL") == "http://seerr-fixture:5055"
    for key in ("DISCORD_TOKEN", "SEERR_API_KEY"):
        actual = hashlib.sha256(os.environ[key].encode()).hexdigest()
        assert actual == os.environ["SMOKE_" + key + "_SHA256"], "Credential serialization changed"
    data = Path("/app/data")
    assert data.stat().st_uid == data.stat().st_gid == 1000
    assert os.statvfs("/app").f_flag & os.ST_RDONLY, "Root filesystem is not read-only"
    database = data / "mediabot.db"
    assert database.exists() is (expected_run == 2), "Unexpected initial database state"

    sys.path.insert(0, "/app")
    import app
    from mediabot.core.runtime_health import (
        RuntimeHealthError, read_runtime_health, validate_runtime_health,
    )

    calls = []

    async def intercept_discord(client, token, shutdown_requested):
        assert client is app.bot
        assert token == os.environ["DISCORD_TOKEN"]
        assert not client.is_ready()
        # Use real authenticated provider transport, not a mocked health method.
        health = await app.seerr.health()
        assert health.get("smoke_authenticated") is True
        assert not any(provider.enabled for provider in (
            app.jellyfin, app.soulsync, app.sonarr, app.torrent_intake,
            app.life_workflow, app.local_ai,
        ))
        with app.db() as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            assert {"user_links", "request_messages", "media_request_intents",
                    "event_schema_migrations", "event_time_options"} <= tables
            row = connection.execute(
                "SELECT discord_username, seerr_user_id FROM user_links WHERE discord_user_id=?",
                (SENTINEL_USER,),
            ).fetchone()
            if expected_run == 1:
                assert row is None
                app.set_link(SENTINEL_USER, "fresh-install-sentinel", 1, "fixture")
            else:
                assert tuple(row) == ("fresh-install-sentinel", 1), "Stored user link was lost"
        calls.append("intercepted")
        shutdown_requested.set()

    # This is the only replaced application function. No Discord token is used.
    app.run_client_until_shutdown = intercept_discord
    await app.main()
    assert calls == ["intercepted"], "Application did not reach the Discord boundary"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM user_links").fetchone()[0] == 1
    heartbeat = read_runtime_health(app.RUNTIME_HEALTH_PATH)
    assert heartbeat["discord_ready"] is False
    assert heartbeat["state"] == "stopping"
    try:
        validate_runtime_health(app.RUNTIME_HEALTH_PATH, expected_version=app.BOT_VERSION)
    except RuntimeHealthError:
        pass
    else:
        raise AssertionError("Offline Discord unexpectedly passed production health")
    assert all(provider.session is None or provider.session.closed for provider in (
        app.seerr, app.jellyfin, app.soulsync, app.sonarr, app.torrent_intake,
        app.life_workflow, app.local_ai,
    )), "Provider session did not close"
    print("FRESH_INSTALL_RESULT=" + json.dumps({
        "run": expected_run, "version": app.BOT_VERSION, "database_integrity": "ok",
        "seerr_authenticated": True, "credential_round_trip": True,
        "optional_integrations_disabled": True, "uid_gid": "1000:1000",
        "read_only_root": True, "discord_login": "intercepted",
        "production_health": "correctly_rejected_offline_discord",
        "retained_user_link": expected_run == 2,
    }, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-run", type=int, choices=(1, 2), required=True)
    args = parser.parse_args()
    try:
        asyncio.run(exercise(args.expected_run))
    except Exception as exc:
        # Do not let a credential-bearing provider exception appear in CI logs.
        detail = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
        frames = traceback.extract_tb(exc.__traceback__)
        detail += " [" + ", ".join(Path(frame.filename).name + ":" + str(frame.lineno)
                                   for frame in frames[-4:]) + "]"
        print("Fresh-install runtime failed: " + detail, file=sys.stderr)
        raise SystemExit(1) from None
