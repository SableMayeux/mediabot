"""Patch SoulSync's inbound request API until the upstream image carries it."""

import os
import shutil
import time
from pathlib import Path


stack = Path("/stack")
source = Path("/source/request.py")
stamp = time.strftime("%Y%m%d-%H%M%S")
backup = stack / ".codex-backups" / f"{stamp}-request-import-fix"
backup.mkdir(parents=True, exist_ok=False)
shutil.copy2(source, backup / "request.py")

text = source.read_text(encoding="utf-8")

if "        }), 202\n" in text:
    text = text.replace("        }), 202\n", "        }, status=202)\n", 1)

if "def _import_completed_request_file(" not in text:
    text = text.replace(
        "import threading\nimport uuid\n",
        "import os\nimport re\nimport shutil\nimport threading\nimport uuid\nfrom pathlib import Path\n",
        1,
    )

    import_helper = '''\n\ndef _safe_path_component(value, fallback):
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', '_', str(value or '').strip())
    cleaned = re.sub(r'\\s+', ' ', cleaned).strip(' .')
    return (cleaned or fallback)[:180]


def _import_completed_request_file(file_path, metadata):
    """Move a synchronously completed direct download into the music library."""
    source = Path(str(file_path))
    if not source.is_file():
        return None

    metadata = metadata or {}
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

    if not destination.exists():
        shutil.move(str(source), str(destination))

    scan_manager = current_app.soulsync.get('web_scan_manager')
    if scan_manager:
        try:
            scan_manager.request_scan('Inbound music request completed')
        except Exception as exc:
            logger.warning(f'Inbound request library scan failed: {exc}')

    logger.info(f'Inbound request imported: {destination}')
    return str(destination)
'''
    anchor = "\ndef _run_search_and_download(request_id, query, notify_url):\n"
    if anchor not in text:
        raise RuntimeError("SoulSync request worker anchor was not found")
    text = text.replace(anchor, import_helper + anchor, 1)
    text = text.replace(
        "def _run_search_and_download(request_id, query, notify_url):",
        "def _run_search_and_download(request_id, query, notify_url, metadata=None):",
        1,
    )
    text = text.replace(
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
        1,
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

override = stack / "overrides" / "request.py"
temporary = override.with_suffix(".py.codex-new")
temporary.write_text(text, encoding="utf-8")
compile(temporary.read_text(encoding="utf-8"), str(temporary), "exec")
os.chown(temporary, 0, 0)
os.chmod(temporary, 0o644)
os.replace(temporary, override)

print(f"Backup={backup}")
print(f"Override={override}")
print("ResponseContract=202")
print("CompletedDirectDownloads=Transfer")
