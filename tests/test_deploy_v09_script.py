from pathlib import Path
import os
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_v010.sh"


class DeployV010ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.legacy_source = (ROOT / "scripts" / "deploy_v09.sh").read_text(
            encoding="utf-8"
        )

    def test_deployer_exists_and_ancient_deployers_are_retired(self):
        self.assertFalse((ROOT / "scripts" / "deploy_v06.sh").exists())
        self.assertFalse((ROOT / "scripts" / "deploy_v05.sh").exists())
        self.assertTrue(SCRIPT.is_file())

    def test_release_and_stage_are_exact(self):
        self.assertIn('release_version="0.10.0"', self.source)
        self.assertIn('stage_namespace="/tmp/mediabot-v0100-"', self.source)
        self.assertNotIn("mediabot-v091-", self.source)
        self.assertNotIn("mediabot-v080-", self.source)

    def test_complete_runtime_manifest_is_backed_up_and_installed(self):
        required = (
            "app.py",
            "mediabot",
            "Dockerfile",
            "compose.yaml",
            "requirements.txt",
            ".env.example",
            ".dockerignore",
        )
        for name in required:
            self.assertIn(name, self.source)
        self.assertIn("runtime.sha256", self.source)
        self.assertIn("verify_backup", self.source)

    def test_sqlite_backup_and_atomic_restore_contract(self):
        self.assertIn("source.backup(destination)", self.source)
        self.assertIn('os.replace(temporary, live)', self.source)
        self.assertIn('("-wal", "-shm", "-journal")', self.source)
        self.assertIn("assert_container_stopped", self.source)
        self.assertIn("data-metadata.json", self.source)
        self.assertIn("restore_data_metadata", self.source)

    def test_guild_scope_uid_migration_and_write_probe_are_mandatory(self):
        self.assertIn("MEDIABOT_ALLOWED_GUILD_IDS", self.source)
        self.assertIn('"ALLOWED_GUILD_IDS=" + value', self.source)
        self.assertIn("os.chown(directory, 1000, 1000)", self.source)
        self.assertIn(".mediabot-write-probe-v0100", self.source)
        self.assertIn('connection.execute("BEGIN IMMEDIATE")', self.source)

    def test_event_schema_v2_is_a_release_gate(self):
        for table in (
            "event_time_options",
            "event_time_votes",
            "event_reminder_state",
        ):
            self.assertIn(f'"{table}"', self.source)
        self.assertIn(
            'SELECT MAX(version) FROM event_schema_migrations',
            self.source,
        )
        self.assertIn("EVENT_SCHEMA_VERSION", self.source)
        self.assertIn("EventStore._validate_v2(connection)", self.source)

    def test_cross_version_deployers_share_stable_and_transition_locks(self):
        for source in (self.source, self.legacy_source):
            self.assertIn(
                'lock_dir="/var/lock/mediabot-deploy.lock"',
                source,
            )
            self.assertIn(
                'legacy_lock_dir="/var/lock/mediabot-v09-deploy.lock"',
                source,
            )
            trap = source.index("trap 'on_exit")
            stable = source.index('mkdir "$lock_dir"', trap)
            legacy = source.index('mkdir "$legacy_lock_dir"', stable)
            self.assertLess(trap, stable)
            self.assertLess(stable, legacy)
            on_exit = source[source.index("on_exit() {"):source.index("wait_for_health() {")]
            self.assertLess(
                on_exit.index('rmdir "$legacy_lock_dir"'),
                on_exit.index('rmdir "$lock_dir"'),
            )

    def test_release_gates_cover_runtime_and_container_hardening(self):
        required = (
            "wait_for_health",
            "runtime_health",
            "PROVIDERS ok",
            "DATABASE ok",
            "ReadonlyRootfs",
            "PidsLimit",
            "CapDrop",
            "no-new-privileges:true",
            '"max-size"',
            '"max-file"',
            "RestartCount",
            "rollback_deployment",
        )
        for marker in required:
            self.assertIn(marker, self.source)

    def test_forced_rollback_drill_runs_only_after_candidate_health(self):
        self.assertIn("MEDIABOT_ROLLBACK_DRILL", self.source)
        health_index = self.source.index('wait_for_health || die')
        drill_index = self.source.index("Intentional rollback drill trigger")
        self.assertLess(health_index, drill_index)

    def test_packaged_test_gate_mounts_every_inspected_path(self):
        self.assertIn('-v "$stage/scripts:/scripts:ro"', self.source)
        self.assertIn('-v "$stage/tests:/tests:ro"', self.source)
        self.assertIn("-e DISCORD_TOKEN=test-token", self.source)
        self.assertIn("-e SEERR_API_KEY=test-key", self.source)

    @unittest.skipUnless(os.name != "nt" and shutil.which("sh"), "POSIX sh is unavailable")
    def test_posix_shell_syntax(self):
        result = subprocess.run(
            ["sh", "-n", str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
