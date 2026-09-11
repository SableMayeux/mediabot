"""Initialize only the root of a fresh, dedicated Docker data volume."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


DATA_PATH = Path("/app/data")
APP_UID = 1000
APP_GID = 1000


def initialize_volume(path: Path = DATA_PATH) -> str:
    """Never recurse, replace files, follow symlinks, or adopt populated data."""
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.resolve() != path.absolute():
        raise RuntimeError("Data volume must be a real directory without symlinks.")
    if info.st_uid == APP_UID and info.st_gid == APP_GID:
        if stat.S_IMODE(info.st_mode) & 0o700 != 0o700:
            raise RuntimeError("Existing data directory is not writable by UID 1000.")
        return "Existing application-owned data volume retained."
    # Only an empty directory created by Docker is eligible for adoption.
    # Inspect metadata before content so private, application-owned files are
    # never enumerated during subsequent starts.
    if info.st_uid != 0 or info.st_gid != 0:
        raise RuntimeError("Unexpected data ownership; inspect the volume before migration.")
    if next(path.iterdir(), None) is not None:
        raise RuntimeError("Data volume contains existing files; refusing to change ownership.")
    os.chmod(path, 0o750, follow_symlinks=False)
    os.chown(path, APP_UID, APP_GID, follow_symlinks=False)
    return "Fresh data volume initialized for UID/GID 1000."


def main() -> int:
    try:
        print(initialize_volume())
    except (OSError, RuntimeError) as exc:
        print(f"MediaBot data initialization failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
