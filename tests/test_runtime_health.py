import json
import os
import tempfile
import unittest
from pathlib import Path

from mediabot.core.runtime_health import (
    RuntimeHealthError,
    main,
    read_runtime_health,
    validate_runtime_health,
    write_runtime_health,
)


class RuntimeHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "runtime-health.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_ready(self, *, now=1000.0, version="0.9.0"):
        return write_runtime_health(
            self.path,
            version=version,
            state="ready",
            discord_ready=True,
            workers={
                "transient_ui_cleanup": True,
                "jellyfin_availability": True,
                "event_lifecycle": True,
            },
            metrics={
                "uptime_seconds": 12,
                "event_last_run_age_seconds": None,
                "event_cycle_max_age_seconds": 180,
            },
            now=now,
        )

    def test_atomic_write_is_secret_free_and_valid(self):
        expected = self.write_ready()
        actual = read_runtime_health(self.path)

        self.assertEqual(actual, expected)
        self.assertEqual(actual["version"], "0.9.0")
        self.assertNotIn("token", json.dumps(actual).casefold())
        if os.name != "nt":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_validator_rejects_stale_wrong_version_and_degraded_state(self):
        self.write_ready(now=1000)
        with self.assertRaisesRegex(RuntimeHealthError, "stale"):
            validate_runtime_health(
                self.path,
                expected_version="0.9.0",
                max_age_seconds=120,
                now=1201,
            )
        with self.assertRaisesRegex(RuntimeHealthError, "version"):
            validate_runtime_health(
                self.path,
                expected_version="1.0.0",
                now=1001,
            )

        write_runtime_health(
            self.path,
            version="0.9.0",
            state="reconnecting",
            discord_ready=False,
            workers={"transient_ui_cleanup": True, "event_lifecycle": True},
            now=1000,
        )
        with self.assertRaisesRegex(RuntimeHealthError, "runtime state"):
            validate_runtime_health(
                self.path,
                expected_version="0.9.0",
                now=1001,
            )

    def test_validator_requires_cleanup_worker(self):
        write_runtime_health(
            self.path,
            version="0.9.0",
            state="ready",
            discord_ready=True,
            workers={"transient_ui_cleanup": False, "event_lifecycle": True},
            now=1000,
        )
        with self.assertRaisesRegex(RuntimeHealthError, "cleanup worker"):
            validate_runtime_health(
                self.path,
                expected_version="0.9.0",
                now=1001,
            )

        write_runtime_health(
            self.path,
            version="0.9.0",
            state="ready",
            discord_ready=True,
            workers={"transient_ui_cleanup": True, "event_lifecycle": True},
            metrics={"cleanup_failed": True},
            now=1000,
        )
        with self.assertRaisesRegex(RuntimeHealthError, "failed cycle"):
            validate_runtime_health(
                self.path,
                expected_version="0.9.0",
                now=1001,
            )

        write_runtime_health(
            self.path,
            version="0.9.0",
            state="ready",
            discord_ready=True,
            workers={"transient_ui_cleanup": True, "event_lifecycle": True},
            metrics={"request_intents_accepted": 1},
            now=1000,
        )
        with self.assertRaisesRegex(RuntimeHealthError, "request intents"):
            validate_runtime_health(
                self.path,
                expected_version="0.9.0",
                now=1001,
            )

    def test_validator_requires_healthy_event_worker(self):
        write_runtime_health(
            self.path,
            version="0.9.0",
            state="ready",
            discord_ready=True,
            workers={"transient_ui_cleanup": True, "event_lifecycle": False},
            now=1000,
        )
        with self.assertRaisesRegex(RuntimeHealthError, "event lifecycle worker"):
            validate_runtime_health(
                self.path,
                expected_version="0.9.0",
                now=1001,
            )

        write_runtime_health(
            self.path,
            version="0.9.0",
            state="ready",
            discord_ready=True,
            workers={"transient_ui_cleanup": True, "event_lifecycle": True},
            metrics={"event_cycle_failed": True},
            now=1000,
        )
        with self.assertRaisesRegex(RuntimeHealthError, "event lifecycle worker"):
            validate_runtime_health(
                self.path,
                expected_version="0.9.0",
                now=1001,
            )

    def test_validator_requires_a_recent_completed_event_cycle(self):
        base = {
            "event_cycle_max_age_seconds": 180,
            "event_cycle_failed": False,
        }
        for metrics, message in (
            (
                {
                    **base,
                    "uptime_seconds": 181,
                    "event_last_run_age_seconds": None,
                },
                "first cycle",
            ),
            (
                {
                    **base,
                    "uptime_seconds": 500,
                    "event_last_run_age_seconds": 181,
                },
                "recent cycle",
            ),
        ):
            with self.subTest(message=message):
                write_runtime_health(
                    self.path,
                    version="0.9.0",
                    state="ready",
                    discord_ready=True,
                    workers={
                        "transient_ui_cleanup": True,
                        "event_lifecycle": True,
                    },
                    metrics=metrics,
                    now=1000,
                )
                with self.assertRaisesRegex(RuntimeHealthError, message):
                    validate_runtime_health(
                        self.path,
                        expected_version="0.9.0",
                        now=1001,
                    )

    def test_validator_allows_first_event_cycle_during_bounded_startup_grace(self):
        write_runtime_health(
            self.path,
            version="0.9.0",
            state="ready",
            discord_ready=True,
            workers={
                "transient_ui_cleanup": True,
                "event_lifecycle": True,
            },
            metrics={
                "uptime_seconds": 180,
                "event_last_run_age_seconds": None,
                "event_cycle_max_age_seconds": 180,
            },
            now=1000,
        )

        payload, _age = validate_runtime_health(
            self.path,
            expected_version="0.9.0",
            now=1001,
        )

        self.assertIsNone(payload["metrics"]["event_last_run_age_seconds"])

    def test_cli_exit_codes_are_machine_usable(self):
        self.write_ready(now=1000)
        self.assertEqual(
            main(
                [
                    "--path",
                    str(self.path),
                    "--version",
                    "0.9.0",
                    "--max-age",
                    "120",
                ]
            ),
            1,
        )
