import copy
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from scripts.homelab_deployment import (
    DATA_SOURCE, LIFE_SOURCE, GATEWAY_NETWORKS, SECRET_FILES,
    DeploymentBoundaryError, validate_compose, validate_container,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_v271.sh"


def compose_fixture():
    return {
        "services": {"mediabot": {
            "container_name": "mediabot", "user": "1000:1000", "read_only": True,
            "environment": {"LIFE_CAPTURE_PATH": "/life-vault/Life/Inbox"},
            "volumes": [
                {"type": "bind", "source": DATA_SOURCE, "target": "/app/data"},
                {"type": "bind", "source": LIFE_SOURCE, "target": "/life-vault/Life/Inbox"},
            ],
            "networks": {"default": {}, **{key: {} for key in GATEWAY_NETWORKS}},
            "secrets": [{"source": key} for key in SECRET_FILES],
            "healthcheck": {"test": ["CMD", "python", "--version", "2.7.1"]},
        }},
        "networks": {key: {"external": True, "name": name} for key, name in GATEWAY_NETWORKS.items()},
        "secrets": {key: {"file": path} for key, path in SECRET_FILES.items()},
    }


class HomelabBoundaryTests(unittest.TestCase):
    def test_preserved_bind_storage_and_gateways_are_accepted(self):
        validate_compose(compose_fixture(), "2.7.1")

    def test_portable_named_volume_or_initializer_cannot_replace_live_data(self):
        for mutation in ("named-data", "data-init", "wrong-bind", "readonly-data"):
            document = compose_fixture()
            service = document["services"]["mediabot"]
            if mutation == "named-data":
                service["volumes"][0].update(type="volume", source="mediabot_data")
            elif mutation == "data-init":
                document["services"]["data-init"] = {}
            elif mutation == "wrong-bind":
                service["volumes"][0]["source"] = "/tmp/empty-data"
            else:
                service["volumes"][0]["read_only"] = True
            with self.subTest(mutation=mutation), self.assertRaises(DeploymentBoundaryError):
                validate_compose(document, "2.7.1")

    def test_wrong_version_or_replaced_gateway_or_secret_fails(self):
        for mutation in ("version", "network", "secret", "ports"):
            document = compose_fixture()
            if mutation == "version":
                document["services"]["mediabot"]["healthcheck"]["test"][-1] = "2.7.0"
            elif mutation == "network":
                document["networks"]["life_intake"]["name"] = "other-network"
            elif mutation == "secret":
                document["secrets"]["life_gateway_token"]["file"] = "/tmp/wrong-token"
            else:
                document["services"]["mediabot"]["ports"] = [{"target": 8080}]
            with self.subTest(mutation=mutation), self.assertRaises(DeploymentBoundaryError):
                validate_compose(document, "2.7.1")

    def test_live_volume_type_and_readonly_secret_are_rechecked(self):
        mounts = [{"Destination": "/app/data", "Type": "bind", "Source": DATA_SOURCE, "RW": True},
                  {"Destination": "/life-vault/Life/Inbox", "Type": "bind", "Source": LIFE_SOURCE, "RW": True}]
        mounts += [{"Destination": "/run/secrets/" + key, "Type": "bind", "Source": path, "RW": False}
                   for key, path in SECRET_FILES.items()]
        document = [{"Name": "/mediabot", "Mounts": mounts,
                     "NetworkSettings": {"Networks": {name: {} for name in GATEWAY_NETWORKS.values()}}}]
        validate_container(document)
        wrong = copy.deepcopy(document)
        wrong[0]["Mounts"][0]["Type"] = "volume"
        with self.assertRaises(DeploymentBoundaryError):
            validate_container(wrong)
        wrong = copy.deepcopy(document)
        wrong[0]["Mounts"][2]["RW"] = True
        with self.assertRaises(DeploymentBoundaryError):
            validate_container(wrong)


class HomelabDeployScriptTests(unittest.TestCase):
    def test_source_and_selected_compose_have_distinct_verified_manifests(self):
        source = SCRIPT.read_text()
        self.assertIn('release_version="2.7.1"', source)
        self.assertIn('stage_namespace="/tmp/mediabot-v271-"', source)
        self.assertIn('source=/stage/deploy/compose.homelab.yaml', source)
        self.assertIn('cmp -s "$source" "$target/$name"', source)
        self.assertIn('release-source.sha256', source)
        self.assertIn('selected-homelab-compose.sha256', source)
        self.assertNotIn('cp "$homelab_compose" "$stage/compose.yaml"', source)
        self.assertLess(source.index('--kind compose --version'), source.index('bot_stopped=1'))
        self.assertLess(source.index('sha256sum -c SOURCE.sha256'), source.index('bot_stopped=1'))
        self.assertIn('-e MEDIABOT_COMPOSE_PATH=/source/compose.yaml', source)
        self.assertIn('-v "$stage:/source:ro"', source)
        self.assertIn('--workdir /source', source)

    @unittest.skipUnless(os.name != "nt" and shutil.which("sh"), "POSIX sh is unavailable")
    def test_shell_syntax(self):
        result = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name != "nt" and shutil.which("sh"), "POSIX sh is unavailable")
    def test_rollback_restores_existing_license_or_its_prior_absence(self):
        source = SCRIPT.read_text()
        function = source.split("restore_runtime_manifest() {", 1)[1].split("\n}\n", 1)[0]
        body = function.split("run_target_helper sh -eu -c '\n", 1)[1].rsplit("\n    '", 1)[0]
        for prior_license in (False, True):
            with self.subTest(prior_license=prior_license), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "target"
                # The extracted rollback body is scoped to this exact synthetic tree.
                self.assertRegex(str(target), r"^/[A-Za-z0-9_./-]+$")
                backup = target / ".codex-backups" / "fixture"
                runtime = backup / "runtime"
                (runtime / "mediabot").mkdir(parents=True)
                (runtime / "mediabot" / "__init__.py").write_text("old-package")
                (target / "mediabot").mkdir()
                (target / "mediabot" / "__init__.py").write_text("new-package")
                names = "app.py Dockerfile compose.yaml requirements.txt .env.example .dockerignore".split()
                for name in names:
                    (runtime / name).write_text("old-" + name)
                    (target / name).write_text("new-" + name)
                (backup / ".env").write_text("private-old-fixture")
                (target / ".env").write_text("private-new-fixture")
                (target / "LICENSE").write_text("new-license")
                if prior_license:
                    (runtime / "LICENSE").write_text("old-license")
                environment = {**os.environ, "BACKUP_REL": ".codex-backups/fixture", "DEPLOY_STAMP": "fixture"}
                result = subprocess.run(["sh", "-eu", "-c", body.replace("/target", str(target))],
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((target / "compose.yaml").read_text(), "old-compose.yaml")
                self.assertEqual((target / ".env").read_text(), "private-old-fixture")
                self.assertEqual((target / "mediabot" / "__init__.py").read_text(), "old-package")
                self.assertEqual((target / "LICENSE").exists(), prior_license)
                if prior_license:
                    self.assertEqual((target / "LICENSE").read_text(), "old-license")


if __name__ == "__main__":
    unittest.main()
