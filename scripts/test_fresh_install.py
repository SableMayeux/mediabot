#!/usr/bin/env python3
"""Build and verify a disposable media-only installation without Discord login.

Requires Linux Docker and Compose v2.20+. This creates only uniquely labeled
test resources, uses synthetic credentials, and never reads the operator's
.env or database. It does not prove real Discord authorization or readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
LABEL = "org.mediabot.fresh-install"
PROJECT_LABEL = "com.docker.compose.project"
PROJECT_PATTERN = re.compile(r"mediabot-fresh-[a-f0-9]{16}\Z")


class SmokeFailure(RuntimeError):
    pass


def assert_owned_resource(payload, project, kind):
    """Cleanup requires both an exact Compose project and our private label."""
    if PROJECT_PATTERN.fullmatch(project) is None:
        raise SmokeFailure("Invalid disposable project identifier")
    labels = payload.get("Labels", {})
    if kind in {"container", "image"}:
        labels = payload.get("Config", {}).get("Labels", {})
    if not isinstance(labels, dict) or labels.get(PROJECT_LABEL) != project or labels.get(LABEL) != project:
        raise SmokeFailure("Cleanup refused: test resource ownership does not match")


def run(command, *, cwd, environment, timeout=180, acceptable=(0,)):
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True,
                            text=True, timeout=timeout, check=False)
    if result.returncode not in acceptable:
        # Compose config and provider output can contain values. Never echo raw
        # command output on failure, even though this test uses synthetic keys.
        details = [line for line in result.stderr.splitlines() if line.startswith((
            "Fresh-install runtime failed:", "MediaBot data initialization failed:"))]
        suffix = "; " + details[-1] if details else ""
        raise SmokeFailure("Command failed (exit " + str(result.returncode) + "): " +
                           " ".join(command[:3]) + suffix)
    return result


def cleanup_resources(project, compose, *, cwd, environment):
    for kind, listing in (("container", ["ps", "-aq"]),
                          ("network", ["network", "ls", "-q"]),
                          ("volume", ["volume", "ls", "-q"])):
        ids = run(["docker", *listing, "--filter", "label=" + PROJECT_LABEL + "=" + project],
                  cwd=cwd, environment=environment).stdout.split()
        for identifier in ids:
            command = ["docker", "inspect", identifier] if kind == "container" else [
                "docker", kind, "inspect", identifier]
            payload = json.loads(run(command, cwd=cwd, environment=environment).stdout)[0]
            assert_owned_resource(payload, project, kind)
    run([*compose, "down", "--volumes", "--remove-orphans", "--timeout", "10"],
        cwd=cwd, environment=environment)


def smoke(root):
    sys.path.insert(0, str(root / "scripts"))
    from setup import DEFAULTS, write_environment

    project = "mediabot-fresh-" + uuid.uuid4().hex[:16]
    image = project + ":test"
    # Deliberately include dollar/hash/quotes and trailing backslashes. Compare
    # hashes in the container so broken dotenv escaping is caught before release.
    token = "fixture-discord-$literal#'\"-" + "\\"
    api_key = "fixture-seerr-$literal#'\"-" + "\\"
    values = {**DEFAULTS,
        "DISCORD_TOKEN": token, "ALLOWED_GUILD_IDS": "900000000000000001",
        "SEERR_URL": "http://seerr-fixture:5055", "SEERR_API_KEY": api_key,
    }
    example_keys = {line.split("=", 1)[0] for line in (root / ".env.example").read_text().splitlines()
                    if line and not line.startswith("#") and "=" in line}
    environment = {key: value for key, value in os.environ.items()
                   if key not in example_keys and not key.startswith("COMPOSE_")}
    environment["COMPOSE_ANSI"] = "never"
    environment["COMPOSE_PROGRESS"] = "quiet"
    platform = json.loads(run(["docker", "info", "--format", '{{json .OSType}}'],
                              cwd=root, environment=environment).stdout)
    if platform != "linux":
        raise SmokeFailure("Fresh-install validation requires Linux containers")
    version = run(["docker", "compose", "version", "--short"], cwd=root,
                  environment=environment).stdout.strip().lstrip("v")
    parts = re.match(r"(\d+)\.(\d+)", version)
    if not parts or tuple(map(int, parts.groups())) < (2, 20):
        raise SmokeFailure("Docker Compose v2.20 or later is required")

    with tempfile.TemporaryDirectory(prefix=project + "-") as temporary:
        stage = Path(temporary).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if stage.parent != temp_root or not stage.name.startswith(project + "-"):
            raise SmokeFailure("Disposable directory escaped its expected temporary root")
        for name in ("app.py", "Dockerfile", "compose.yaml", "requirements.txt", ".dockerignore", "LICENSE"):
            shutil.copy2(root / name, stage / name)
        shutil.copytree(root / "mediabot", stage / "mediabot",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        shutil.copytree(root / "scripts" / "fixtures", stage / "fixtures")
        # Fixtures are public test code mounted from the host. Make only this
        # disposable copy readable when the checkout owner differs from UID 1000.
        fixture_root = stage / "fixtures"
        for item in [fixture_root, *fixture_root.rglob("*")]:
            if item.is_symlink() or not item.resolve().is_relative_to(fixture_root):
                raise SmokeFailure("Fixture copy contains an unexpected symlink")
            item.chmod(0o755 if item.is_dir() else 0o644)
        write_environment(stage / ".env", values)
        labels = {LABEL: project}
        fixture_mount = {"type": "bind", "source": str(stage / "fixtures"),
                         "target": "/fixtures", "read_only": True}
        hashes = {"SMOKE_" + key + "_SHA256": hashlib.sha256(values[key].encode()).hexdigest()
                  for key in ("DISCORD_TOKEN", "SEERR_API_KEY")}
        overlay = {
            "services": {
                "mediabot": {"image": image, "labels": labels, "restart": "no",
                             "build": {"context": ".", "labels": {**labels, PROJECT_LABEL: project}},
                             "volumes": [fixture_mount], "environment": hashes,
                             "healthcheck": {"disable": True}},
                "data-init": {"image": image, "labels": labels},
                "seerr-fixture": {
                    "image": image, "labels": labels, "restart": "no",
                    "user": "1000:1000", "read_only": True, "pids_limit": 32,
                    "mem_limit": "128m", "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"], "volumes": [fixture_mount],
                    "command": ["python", "/fixtures/fresh_install_seerr.py"],
                    "environment": {"SMOKE_API_KEY_SHA256": hashes["SMOKE_SEERR_API_KEY_SHA256"]},
                },
            },
            "networks": {"default": {"internal": True, "labels": labels}},
            "volumes": {"mediabot_data": {"labels": labels}},
        }
        override = stage / "smoke.compose.json"
        override.write_text(json.dumps(overlay), encoding="utf-8")
        compose = ["docker", "compose", "--project-name", project, "--project-directory", str(stage),
                   "--env-file", str(stage / ".env"), "-f", str(stage / "compose.yaml"), "-f", str(override)]
        configured = json.loads(run([*compose, "config", "--format", "json"], cwd=stage,
                                    environment=environment).stdout)
        # `compose config` may re-escape literal dollars for serialization.
        # The authoritative byte-for-byte check runs inside the actual container.
        if any(item.get("external") for item in configured.get("networks", {}).values()):
            raise SmokeFailure("Portable configuration unexpectedly requires an external network")
        if any(item.get("ports") or item.get("secrets") or item.get("container_name")
               for item in configured["services"].values()):
            raise SmokeFailure("Portable configuration inherited ports, secrets or a fixed container name")
        for name, item in configured["services"].items():
            for mount in item.get("volumes", []):
                fixture = (mount.get("type") == "bind" and mount.get("read_only") is True
                           and mount.get("source") == str(stage / "fixtures")
                           and mount.get("target") == "/fixtures")
                volume = (mount.get("type") == "volume" and mount.get("source") == "mediabot_data"
                          and mount.get("target") == "/app/data")
                if not fixture and not volume:
                    raise SmokeFailure("Portable configuration inherited an unexpected mount")
            if name == "data-init":
                if item.get("network_mode") != "none":
                    raise SmokeFailure("Data initializer has unexpected network access")
            elif item.get("network_mode") or set(item.get("networks", {})) != {"default"}:
                raise SmokeFailure("Test service has an unexpected network attachment")
        if not configured["networks"]["default"].get("internal"):
            raise SmokeFailure("Fixture network is not internal")
        print("Fresh-install gate: build shipping image in disposable project", flush=True)
        image_built = False
        try:
            run([*compose, "build", "--pull", "mediabot"], cwd=stage, environment=environment, timeout=600)
            image_built = True
            run([*compose, "run", "--rm", "--no-deps", "data-init"], cwd=stage, environment=environment)
            run([*compose, "up", "-d", "--no-deps", "seerr-fixture"], cwd=stage, environment=environment)
            readiness = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5055/ready', timeout=2).read()"
            for attempt in range(30):
                ready = run([*compose, "exec", "-T", "seerr-fixture", "python", "-c", readiness],
                            cwd=stage, environment=environment, timeout=10, acceptable=(0, 1))
                if ready.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                raise SmokeFailure("Authenticated provider fixture did not start")
            results = []
            for expected_run in (1, 2):
                if expected_run == 2:
                    run([*compose, "run", "--rm", "--no-deps", "data-init"],
                        cwd=stage, environment=environment)
                output = run([*compose, "run", "--rm", "--no-deps", "mediabot", "python",
                              "/fixtures/fresh_install_runtime.py", "--expected-run", str(expected_run)],
                             cwd=stage, environment=environment).stdout
                markers = [line.removeprefix("FRESH_INSTALL_RESULT=") for line in output.splitlines()
                           if line.startswith("FRESH_INSTALL_RESULT=")]
                if len(markers) != 1:
                    raise SmokeFailure("Runtime did not produce its completion receipt")
                results.append(json.loads(markers[0]))
                print("Fresh-install gate: application initialization " + str(expected_run) + " passed", flush=True)
            return {"result": "passed", "project": project, "checks": results,
                    "discord_live_tested": False, "owned_resources_cleaned": True}
        finally:
            cleanup_resources(project, compose, cwd=stage, environment=environment)
            if image_built:
                # Remove only our unique image tag, never shared image IDs or dangling images.
                payload = json.loads(run(["docker", "image", "inspect", image],
                                         cwd=stage, environment=environment).stdout)[0]
                assert_owned_resource(payload, project, "image")
                run(["docker", "image", "rm", image], cwd=stage, environment=environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository checkout to test")
    args = parser.parse_args()
    try:
        receipt = smoke(args.root.resolve())
    except (SmokeFailure, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        message = str(exc) if isinstance(exc, SmokeFailure) else type(exc).__name__
        print("Fresh-install gate failed: " + message, file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
