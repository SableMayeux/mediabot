"""Patch SoulSync's inbound request API until the upstream image carries it.

The stock endpoint treats a synchronously returned file path as complete and
moves it directly into the library. That bypasses SoulSync's normal import
pipeline, including its audio integrity and deep-decode guards. Keep this
patch deliberately narrow: validate the returned file, quarantine failures,
and publish a validated file atomically before reporting ``completed``.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


PATCH_MARKER = "# MEDIABOT_REQUEST_IMPORT_PATCH_V2"

IMPORT_HELPER = r'''
# MEDIABOT_REQUEST_IMPORT_PATCH_V2
_request_import_lock = threading.Lock()


def _safe_path_component(value, fallback):
    cleaned = re.sub(r'[<>:"/\\|?*]+', '_', str(value or '').strip())
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .')
    return (cleaned or fallback)[:180]


def _expected_duration_ms(metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    value = metadata.get('expected_duration_ms', metadata.get('duration_ms'))
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _request_provenance(metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        'request_source': 'mediabot',
        'metadata_source': str(metadata.get('metadata_source') or ''),
        'external_id': str(metadata.get('external_id') or ''),
        'expected_duration_ms': _expected_duration_ms(metadata),
        'artist': str(metadata.get('artist') or ''),
        'title': str(metadata.get('title') or ''),
        'album': str(metadata.get('album') or ''),
    }


def _expected_track_from_metadata(metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    title = str(metadata.get('title') or '').strip()
    artist = str(metadata.get('artist') or '').strip()
    duration_ms = _expected_duration_ms(metadata) or 0
    if not title and not artist and not duration_ms:
        return None
    return SimpleNamespace(
        name=title,
        artists=[artist] if artist else [],
        duration_ms=duration_ms,
        id=str(metadata.get('external_id') or ''),
        source=str(metadata.get('metadata_source') or ''),
    )


def _parse_ffmpeg_progress_duration(progress_text):
    last = 0.0
    for match in re.finditer(
        r'out_time=(\d+):(\d+):(\d+(?:\.\d+)?)',
        progress_text or '',
    ):
        seconds = (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + float(match.group(3))
        )
        last = max(last, seconds)
    return last


def _fully_decode_audio(file_path, timeout=600):
    """Decode every audio frame and return (ok, duration_s, error).

    This is intentionally fail-closed for the inbound API. A request must not
    become ``completed`` when ffmpeg is absent, times out, or reports a decoder
    error. SoulSync's normal pipeline may fail open for tooling outages, but
    this endpoint otherwise has no later gate that can catch a poisoned file.
    """
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return False, 0.0, 'ffmpeg is unavailable; full audio decode was not verified'
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                '-hide_banner',
                '-nostdin',
                '-v',
                'error',
                '-xerror',
                '-progress',
                'pipe:1',
                '-nostats',
                '-i',
                str(file_path),
                '-map',
                '0:a:0',
                '-f',
                'null',
                '-',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, 0.0, f'full audio decode timed out after {timeout}s'
    except Exception as exc:
        return False, 0.0, f'full audio decode could not start: {exc}'

    decoded_s = _parse_ffmpeg_progress_duration(completed.stdout)
    if completed.returncode != 0:
        detail = ' '.join((completed.stderr or '').split())[-500:]
        return (
            False,
            decoded_s,
            f'ffmpeg rejected the audio stream (exit {completed.returncode})'
            + (f': {detail}' if detail else ''),
        )
    if decoded_s <= 0:
        return False, 0.0, 'ffmpeg decoded no measurable audio frames'
    return True, decoded_s, None


def _validate_completed_request_file(file_path, metadata):
    """Run SoulSync's structural guard plus a mandatory full decode."""
    from core.imports.file_integrity import check_audio_integrity
    from core.imports.silence import detect_broken_audio

    expected_ms = _expected_duration_ms(metadata)
    try:
        integrity = check_audio_integrity(str(file_path), expected_ms)
    except Exception as exc:
        return False, f'audio integrity check crashed: {exc}', {
            'status': 'failed',
            'expected_duration_ms': expected_ms,
        }

    details = {
        'status': 'checking',
        'expected_duration_ms': expected_ms,
        'integrity_checks': dict(getattr(integrity, 'checks', {}) or {}),
    }
    if not integrity.ok:
        details['status'] = 'failed'
        return False, str(integrity.reason or 'audio integrity check failed'), details

    decode_ok, decoded_s, decode_error = _fully_decode_audio(file_path)
    details['decoded_duration_s'] = decoded_s
    if not decode_ok:
        details['status'] = 'failed'
        return False, decode_error or 'full audio decode failed', details

    checks = details['integrity_checks']
    try:
        container_s = float(checks.get('actual_length_s') or 0)
    except (TypeError, ValueError):
        container_s = 0.0
    expected_s = (expected_ms / 1000.0) if expected_ms else 0.0
    reference_s = expected_s or container_s
    details['container_duration_s'] = container_s
    if reference_s > 0 and decoded_s < reference_s * 0.85:
        details['status'] = 'failed'
        return (
            False,
            f'incomplete audio: only {decoded_s:.1f}s decoded of an expected '
            f'{reference_s:.1f}s ({decoded_s / reference_s:.0%})',
            details,
        )

    try:
        broken_reason = detect_broken_audio(str(file_path))
    except Exception as exc:
        details['status'] = 'failed'
        return False, f'deep audio verification crashed: {exc}', details
    if broken_reason:
        details['status'] = 'failed'
        return False, str(broken_reason), details

    details['status'] = 'passed'
    return True, None, details


def _request_quarantine_context(metadata, request_id, validation):
    metadata = metadata if isinstance(metadata, dict) else {}
    provenance = _request_provenance(metadata)
    artist = provenance['artist']
    title = provenance['title']
    track_info = {
        'id': provenance['external_id'],
        'name': title,
        'artists': [{'name': artist}] if artist else [],
        'album': {'name': provenance['album'] or 'Singles'},
        'duration_ms': provenance['expected_duration_ms'] or 0,
        'metadata_source': provenance['metadata_source'],
    }
    return {
        'context_key': f'mediabot-request:{request_id}',
        'request_id': request_id,
        'source': 'mediabot_api',
        'track_info': track_info,
        'original_search_result': {
            'title': title,
            'artist': artist,
            'source': provenance['metadata_source'],
            'external_id': provenance['external_id'],
        },
        'request_provenance': provenance,
        'request_validation': validation,
    }


def _quarantine_request_file(file_path, metadata, request_id, reason, validation):
    from core.imports.guards import move_to_quarantine

    context = _request_quarantine_context(metadata, request_id, validation)
    automation_engine = current_app.soulsync.get('automation_engine')
    try:
        return move_to_quarantine(
            str(file_path),
            context,
            f'Inbound MediaBot request rejected: {reason}',
            automation_engine,
            trigger='integrity',
        )
    except Exception as exc:
        logger.error(
            'Inbound request quarantine failed; leaving file in place: %s (%s)',
            file_path,
            exc,
        )
        return None


def _publish_request_file(source, destination):
    """Copy, fsync, and atomically publish a validated request file."""
    temporary = destination.with_name(
        f'.{destination.name}.mediabot-{uuid.uuid4().hex}.tmp'
    )
    try:
        shutil.copy2(str(source), str(temporary))
        with temporary.open('r+b') as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(destination))
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise

    try:
        source.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning('Validated source remained after atomic publish: %s (%s)', source, exc)


def _import_completed_request_file(file_path, metadata, request_id):
    """Validate and atomically import a synchronously completed download."""
    source = Path(str(file_path))
    provenance = _request_provenance(metadata)
    if not source.is_file():
        return {
            'status': 'downloading',
            'download_id': str(file_path),
            'error': None,
            'quarantine_path': None,
            'validation': {'status': 'pending'},
            'provenance': provenance,
        }

    source_ok, source_error, source_validation = _validate_completed_request_file(
        source, metadata
    )
    if not source_ok:
        quarantine_path = _quarantine_request_file(
            source,
            metadata,
            request_id,
            source_error,
            source_validation,
        )
        return {
            'status': 'failed',
            'download_id': str(quarantine_path or source),
            'error': source_error,
            'quarantine_path': str(quarantine_path) if quarantine_path else None,
            'validation': source_validation,
            'provenance': provenance,
        }

    metadata = metadata if isinstance(metadata, dict) else {}
    artist = _safe_path_component(metadata.get('artist'), 'Unknown Artist')
    album = _safe_path_component(metadata.get('album'), 'Singles')
    config_mgr = current_app.soulsync.get('config_manager')
    transfer_value = (
        config_mgr.get('soulseek.transfer_path', './Transfer')
        if config_mgr
        else './Transfer'
    )
    transfer_root = Path(transfer_value).expanduser().resolve()
    destination_dir = transfer_root / artist / album
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name

    with _request_import_lock:
        try:
            same_file = source.resolve() == destination.resolve()
        except OSError:
            same_file = False

        if destination.exists() and not same_file:
            existing_ok, existing_error, existing_validation = (
                _validate_completed_request_file(destination, metadata)
            )
            if existing_ok:
                logger.info(
                    'Inbound request found an existing validated destination; '
                    'leaving the new source staged: %s',
                    destination,
                )
                source_validation = existing_validation
            else:
                existing_quarantine = _quarantine_request_file(
                    destination,
                    metadata,
                    request_id,
                    f'existing destination is invalid: {existing_error}',
                    existing_validation,
                )
                if not existing_quarantine:
                    return {
                        'status': 'failed',
                        'download_id': str(source),
                        'error': (
                            'existing destination failed validation and could not '
                            'be quarantined; validated replacement was left staged'
                        ),
                        'quarantine_path': None,
                        'validation': existing_validation,
                        'provenance': provenance,
                    }
                try:
                    _publish_request_file(source, destination)
                except Exception as exc:
                    return {
                        'status': 'failed',
                        'download_id': str(source),
                        'error': f'validated file could not be published: {exc}',
                        'quarantine_path': str(existing_quarantine),
                        'validation': source_validation,
                        'provenance': provenance,
                    }
        elif not same_file:
            try:
                _publish_request_file(source, destination)
            except Exception as exc:
                return {
                    'status': 'failed',
                    'download_id': str(source),
                    'error': f'validated file could not be published: {exc}',
                    'quarantine_path': None,
                    'validation': source_validation,
                    'provenance': provenance,
                }

    scan_manager = current_app.soulsync.get('web_scan_manager')
    if scan_manager:
        try:
            scan_manager.request_scan('Inbound music request completed')
        except Exception as exc:
            logger.warning(f'Inbound request library scan failed: {exc}')

    logger.info(
        'Inbound request imported after full audio validation: %s',
        destination,
    )
    return {
        'status': 'completed',
        'download_id': str(destination),
        'error': None,
        'quarantine_path': None,
        'validation': source_validation,
        'provenance': provenance,
    }
'''


def _replace_one_of(
    text: str,
    alternatives: tuple[str, ...],
    replacement: str,
    label: str,
) -> str:
    for old in alternatives:
        if old in text:
            return text.replace(old, replacement, 1)
    if replacement in text:
        return text
    raise RuntimeError(f"SoulSync {label} anchor was not found")


def patch_source(text: str) -> str:
    """Return a v2-patched SoulSync ``api/request.py`` source string."""
    if "import subprocess\n" not in text:
        if "import shutil\n" in text:
            text = text.replace(
                "import shutil\n",
                "import shutil\nimport subprocess\n",
                1,
            )
        elif "import threading\n" in text:
            text = text.replace(
                "import threading\n",
                "import os\nimport re\nimport shutil\nimport subprocess\nimport threading\n",
                1,
            )
        else:
            raise RuntimeError("SoulSync import anchor was not found")
    if "import re\n" not in text:
        text = text.replace("import subprocess\n", "import re\nimport subprocess\n", 1)
    if "import shutil\n" not in text:
        text = text.replace("import subprocess\n", "import shutil\nimport subprocess\n", 1)
    if "import os\n" not in text:
        text = text.replace("import re\n", "import os\nimport re\n", 1)
    if "from pathlib import Path\n" not in text:
        text = text.replace("import uuid\n", "import uuid\nfrom pathlib import Path\n", 1)
    if "from types import SimpleNamespace\n" not in text:
        text = text.replace(
            "from pathlib import Path\n",
            "from pathlib import Path\nfrom types import SimpleNamespace\n",
            1,
        )

    worker_anchor = "\ndef _run_search_and_download(request_id, query, notify_url, metadata=None):\n"
    pristine_worker_anchor = "\ndef _run_search_and_download(request_id, query, notify_url):\n"
    anchor = worker_anchor if worker_anchor in text else pristine_worker_anchor
    if anchor not in text:
        raise RuntimeError("SoulSync request worker anchor was not found")

    helper_start = text.find("\ndef _safe_path_component(")
    marker_start = text.find("\n" + PATCH_MARKER)
    block_start = marker_start if marker_start >= 0 else helper_start
    worker_start = text.find(anchor)
    if block_start >= 0:
        if block_start > worker_start:
            raise RuntimeError("SoulSync request helper appears after its worker")
        text = text[:block_start] + "\n" + IMPORT_HELPER.strip("\n") + text[worker_start:]
    else:
        text = text[:worker_start] + "\n" + IMPORT_HELPER.strip("\n") + text[worker_start:]

    text = text.replace(pristine_worker_anchor, worker_anchor, 1)
    download_call = (
        "        expected_track = _expected_track_from_metadata(metadata)\n"
        "        result = run_async(\n"
        "            soulseek.search_and_download_best(\n"
        "                query, expected_track=expected_track\n"
        "            )\n"
        "        )\n"
    )
    text = _replace_one_of(
        text,
        (
            "        result = run_async(soulseek.search_and_download_best(query))\n",
            download_call,
        ),
        download_call,
        "download call",
    )

    old_result_blocks = (
        """                if result:
                    _pending_requests[request_id]['status'] = 'downloading'
                    _pending_requests[request_id]['download_id'] = result
""",
        """                if result:
                    imported = _import_completed_request_file(result, metadata)
                    _pending_requests[request_id]['status'] = (
                        'completed' if imported else 'downloading'
                    )
                    _pending_requests[request_id]['download_id'] = imported or result
""",
    )
    new_result_block = """                if result:
                    import_result = _import_completed_request_file(
                        result, metadata, request_id
                    )
                    _pending_requests[request_id].update(import_result)
"""
    text = _replace_one_of(
        text,
        old_result_blocks + (new_result_block,),
        new_result_block,
        "request result",
    )

    text = text.replace(
        "target=lambda: _run_with_app_context(app, request_id, query, notify_url),",
        "target=lambda: _run_with_app_context(app, request_id, query, notify_url, metadata),",
        1,
    )
    text = text.replace(
        "def _run_with_app_context(app, request_id, query, notify_url):",
        "def _run_with_app_context(app, request_id, query, notify_url, metadata=None):",
        1,
    )
    text = text.replace(
        "        _run_search_and_download(request_id, query, notify_url)\n",
        "        _run_search_and_download(request_id, query, notify_url, metadata)\n",
        1,
    )

    if "'provenance': _request_provenance(metadata)" not in text:
        text = _replace_one_of(
            text,
            ("                'download_id': None,\n                'error': None,\n",),
            "                'download_id': None,\n"
            "                'error': None,\n"
            "                'provenance': _request_provenance(metadata),\n"
            "                'validation': {'status': 'pending'},\n"
            "                'quarantine_path': None,\n",
            "pending request record",
        )

    if '"validation": req.get(\'validation\')' not in text:
        text = _replace_one_of(
            text,
            ("            \"completed_at\": req.get('completed_at'),\n",),
            "            \"completed_at\": req.get('completed_at'),\n"
            "            \"validation\": req.get('validation'),\n"
            "            \"quarantine_path\": req.get('quarantine_path'),\n"
            "            \"provenance\": req.get('provenance'),\n",
            "status response",
        )

    if "        }), 202\n" in text:
        text = text.replace("        }), 202\n", "        }, status=202)\n", 1)
    return text


def main() -> None:
    stack = Path(os.environ.get("SOULSYNC_PATCH_STACK", "/stack"))
    source = Path(os.environ.get("SOULSYNC_PATCH_SOURCE", "/source/request.py"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = stack / ".codex-backups" / f"{stamp}-request-import-fix"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source, backup / "request.py")

    text = patch_source(source.read_text(encoding="utf-8"))
    override = stack / "overrides" / "request.py"
    override.parent.mkdir(parents=True, exist_ok=True)
    temporary = override.with_suffix(".py.codex-new")
    temporary.write_text(text, encoding="utf-8")
    compile(temporary.read_text(encoding="utf-8"), str(temporary), "exec")
    if hasattr(os, "chown"):
        os.chown(temporary, 0, 0)
    os.chmod(temporary, 0o644)
    os.replace(temporary, override)

    print(f"Backup={backup}")
    print(f"Override={override}")
    print("ResponseContract=202")
    print("CompletedDirectDownloads=ValidatedTransfer")
    print("AudioValidation=IntegrityPlusFullDecode")


if __name__ == "__main__":
    main()
