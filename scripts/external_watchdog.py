"""External reachability checks with one GitHub issue per ongoing incident."""
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit


def probe(url, kind):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Configure an HTTPS public health URL without credentials.")
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "MediaBot-external-watchdog"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(16384)
            payload = json.loads(body) if kind != "media" else None
            valid = body.strip().lower() == b"healthy" if kind == "media" else isinstance(payload, dict) and bool(payload.get("version"))
            return response.status == 200 and valid
    except (OSError, ValueError):
        return False


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True)


def main():
    outcomes = {kind: probe(os.environ[key], kind) for kind, key in (("media", "MEDIA_HEALTH_URL"), ("requests", "REQUESTS_HEALTH_URL"))}
    now = datetime.now(timezone.utc).isoformat()
    print(json.dumps({"checked_at": now, "healthy": outcomes}))
    title = "[Homelab watchdog] Public media services are unreachable"
    issues = json.loads(gh("issue", "list", "--state", "open", "--search", title + " in:title", "--json", "number,title"))
    existing = next((issue for issue in issues if issue["title"] == title), None)
    if not all(outcomes.values()) and existing is None:
        failed = ", ".join(kind for kind, healthy in outcomes.items() if not healthy)
        body = f"External check failed at {now}. Unhealthy services: {failed}.\n\nThis confirms public endpoint failure; it does not establish whether the cause is WAN, tunnel, DNS, or the application. No private credentials or household data were transmitted. The issue closes automatically after both endpoints recover."
        gh("issue", "create", "--title", title, "--body", body)
    elif all(outcomes.values()) and existing is not None:
        gh("issue", "close", str(existing["number"]), "--comment", f"Both endpoints recovered at {now}.")


if __name__ == "__main__":
    main()
