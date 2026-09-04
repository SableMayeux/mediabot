"""Atomic runtime heartbeat and container health validation for MediaBot."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


HEALTH_SCHEMA_VERSION = 1
READY_STATE = "ready"


class RuntimeHealthError(RuntimeError):
    """Raised when a runtime heartbeat cannot be read or validated."""


def write_runtime_health(
    path: str | os.PathLike[str],
    *,
    version: str,
    state: str,
    discord_ready: bool,
    workers: Mapping[str, bool],
    metrics: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Atomically replace the public, secret-free runtime heartbeat."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "version": str(version),
        "state": str(state),
        "discord_ready": bool(discord_ready),
        "updated_at": float(time.time() if now is None else now),
        "workers": {str(name): bool(value) for name, value in workers.items()},
        "metrics": dict(metrics or {}),
    }

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o640)
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return payload


def read_runtime_health(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeHealthError(f"heartbeat unreadable: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RuntimeHealthError("heartbeat root is not an object")
    return payload


def validate_runtime_health(
    path: str | os.PathLike[str],
    *,
    expected_version: str,
    max_age_seconds: float = 120,
    now: float | None = None,
) -> tuple[dict[str, Any], float]:
    """Return a valid payload and age, or raise a concise health error."""

    payload = read_runtime_health(path)
    if payload.get("schema_version") != HEALTH_SCHEMA_VERSION:
        raise RuntimeHealthError("heartbeat schema mismatch")
    if payload.get("version") != str(expected_version):
        raise RuntimeHealthError("heartbeat version mismatch")
    if payload.get("state") != READY_STATE:
        raise RuntimeHealthError(f"runtime state is {payload.get('state') or 'unknown'}")
    if payload.get("discord_ready") is not True:
        raise RuntimeHealthError("Discord is not ready")

    workers = payload.get("workers")
    if not isinstance(workers, dict) or workers.get("transient_ui_cleanup") is not True:
        raise RuntimeHealthError("transient cleanup worker is not running")
    if workers.get("event_lifecycle") is not True:
        raise RuntimeHealthError("event lifecycle worker is not running")
    metrics = payload.get("metrics")
    if isinstance(metrics, dict) and metrics.get("cleanup_failed") is True:
        raise RuntimeHealthError("transient cleanup worker reported a failed cycle")
    if isinstance(metrics, dict) and metrics.get("event_cycle_failed") is True:
        raise RuntimeHealthError("event lifecycle worker reported a failed cycle")
    if isinstance(metrics, dict) and int(metrics.get("request_intents_accepted") or 0):
        raise RuntimeHealthError("accepted request intents still need local tracking")

    if not isinstance(metrics, dict):
        raise RuntimeHealthError("event lifecycle metrics are missing")
    try:
        event_cycle_max_age = float(metrics["event_cycle_max_age_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeHealthError(
            "event lifecycle freshness threshold is invalid"
        ) from exc
    if not math.isfinite(event_cycle_max_age) or event_cycle_max_age < 1:
        raise RuntimeHealthError("event lifecycle freshness threshold is invalid")

    event_cycle_age_raw = metrics.get("event_last_run_age_seconds")
    if event_cycle_age_raw is None:
        try:
            uptime = float(metrics["uptime_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeHealthError(
                "event lifecycle has not completed and startup age is unavailable"
            ) from exc
        if not math.isfinite(uptime) or uptime < 0:
            raise RuntimeHealthError("event lifecycle startup age is invalid")
        if uptime > event_cycle_max_age:
            raise RuntimeHealthError(
                "event lifecycle worker has not completed its first cycle"
            )
    else:
        try:
            event_cycle_age = float(event_cycle_age_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeHealthError(
                "event lifecycle cycle age is invalid"
            ) from exc
        if not math.isfinite(event_cycle_age) or event_cycle_age < 0:
            raise RuntimeHealthError("event lifecycle cycle age is invalid")
        if event_cycle_age > event_cycle_max_age:
            raise RuntimeHealthError(
                "event lifecycle worker has not completed a recent cycle"
            )

    try:
        updated_at = float(payload["updated_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeHealthError("heartbeat timestamp is invalid") from exc
    current = float(time.time() if now is None else now)
    age = current - updated_at
    maximum = max(1.0, float(max_age_seconds))
    if age < -30:
        raise RuntimeHealthError("heartbeat timestamp is in the future")
    if age > maximum:
        raise RuntimeHealthError(f"heartbeat is stale ({int(age)}s)")
    return payload, max(0.0, age)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate MediaBot runtime health")
    parser.add_argument("--path", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--max-age", type=float, default=120)
    args = parser.parse_args(argv)
    try:
        payload, age = validate_runtime_health(
            args.path,
            expected_version=args.version,
            max_age_seconds=args.max_age,
        )
    except RuntimeHealthError as exc:
        print(f"UNHEALTHY: {exc}")
        return 1
    print(
        "HEALTHY: "
        f"version={payload['version']} state={payload['state']} age={int(age)}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
