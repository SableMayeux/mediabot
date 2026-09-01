from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import threading
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "scripts" / "patch_soulsync_request_api.py"
SPEC = importlib.util.spec_from_file_location("soulsync_request_patch", PATCH_PATH)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PATCH)


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _Config:
    def __init__(self, transfer_root: Path):
        self.transfer_root = transfer_root

    def get(self, key, default=None):
        if key == "soulseek.transfer_path":
            return str(self.transfer_root)
        return default


def _helper_namespace(transfer_root: Path) -> dict:
    namespace = {
        "Path": Path,
        "SimpleNamespace": SimpleNamespace,
        "current_app": SimpleNamespace(
            soulsync={"config_manager": _Config(transfer_root)}
        ),
        "logger": _Logger(),
        "os": __import__("os"),
        "re": re,
        "shutil": shutil,
        "subprocess": subprocess,
        "threading": threading,
        "uuid": uuid,
    }
    exec(PATCH.IMPORT_HELPER, namespace)
    return namespace


class SoulSyncRequestPatchTests(unittest.TestCase):
    def test_ffmpeg_progress_parser_uses_the_last_decoded_timestamp(self):
        helpers = _helper_namespace(Path("unused"))
        decoded = helpers["_parse_ffmpeg_progress_duration"](
            "out_time=00:00:29.907302\nprogress=continue\n"
            "out_time=00:03:07.520000\nprogress=end\n"
        )
        self.assertAlmostEqual(decoded, 187.52, places=3)

    def test_patching_is_idempotent_and_passes_exact_track_metadata(self):
        source = '''"""request"""
import threading
import uuid

def _run_search_and_download(request_id, query, notify_url):
        result = run_async(soulseek.search_and_download_best(query))
        with _requests_lock:
            if request_id in _pending_requests:
                if result:
                    _pending_requests[request_id]['status'] = 'downloading'
                    _pending_requests[request_id]['download_id'] = result
                else:
                    pass

def register_routes(bp):
    metadata = body.get("metadata") or {}
    _pending_requests[request_id] = {
                'download_id': None,
                'error': None,
            }
    thread = threading.Thread(
            target=lambda: _run_with_app_context(app, request_id, query, notify_url),
        )
    return api_success({}), 202
    return api_success({
            "completed_at": req.get('completed_at'),
        })

def _run_with_app_context(app, request_id, query, notify_url):
        _run_search_and_download(request_id, query, notify_url)
'''
        once = PATCH.patch_source(source)
        twice = PATCH.patch_source(once)

        self.assertEqual(once, twice)
        self.assertEqual(once.count(PATCH.PATCH_MARKER), 1)
        self.assertIn("expected_track=expected_track", once)
        self.assertIn("_pending_requests[request_id].update(import_result)", once)
        self.assertIn("'provenance': _request_provenance(metadata)", once)
        self.assertIn('"validation": req.get(\'validation\')', once)

    def test_invalid_source_is_quarantined_and_never_completed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "incoming.flac"
            source.write_bytes(b"preview")
            quarantine = root / "quarantine.flac"
            helpers = _helper_namespace(root / "library")
            helpers["_validate_completed_request_file"] = lambda path, metadata: (
                False,
                "only 30 seconds decoded",
                {"status": "failed", "decoded_duration_s": 30.0},
            )

            def quarantine_file(path, metadata, request_id, reason, validation):
                shutil.move(str(path), str(quarantine))
                return str(quarantine)

            helpers["_quarantine_request_file"] = quarantine_file
            result = helpers["_import_completed_request_file"](
                source,
                {"artist": "Artist", "title": "Track", "album": "Album"},
                "request-1",
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["quarantine_path"], str(quarantine))
            self.assertFalse(source.exists())
            self.assertFalse(
                (root / "library" / "Artist" / "Album" / source.name).exists()
            )

    def test_valid_incoming_replaces_only_after_bad_destination_is_quarantined(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            transfer = root / "library"
            destination = transfer / "Artist" / "Album" / "track.flac"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"bad-existing")
            source = root / "staging" / "track.flac"
            source.parent.mkdir()
            source.write_bytes(b"good-incoming")
            quarantined = root / "existing.quarantined"
            helpers = _helper_namespace(transfer)

            def validate(path, metadata):
                payload = Path(path).read_bytes()
                if payload == b"good-incoming":
                    return (
                        True,
                        None,
                        {"status": "passed", "decoded_duration_s": 180.0},
                    )
                return (
                    False,
                    "existing destination is truncated",
                    {"status": "failed"},
                )

            def quarantine_file(path, metadata, request_id, reason, validation):
                shutil.move(str(path), str(quarantined))
                return str(quarantined)

            helpers["_validate_completed_request_file"] = validate
            helpers["_quarantine_request_file"] = quarantine_file
            result = helpers["_import_completed_request_file"](
                source,
                {
                    "artist": "Artist",
                    "title": "Track",
                    "album": "Album",
                    "expected_duration_ms": 180000,
                    "external_id": "track-1",
                    "metadata_source": "spotify",
                },
                "request-2",
            )

            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(destination.read_bytes(), b"good-incoming")
            self.assertEqual(quarantined.read_bytes(), b"bad-existing")
            self.assertFalse(source.exists())
            self.assertEqual(result["provenance"]["external_id"], "track-1")


if __name__ == "__main__":
    unittest.main()
