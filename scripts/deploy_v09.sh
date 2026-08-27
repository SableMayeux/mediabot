#!/bin/sh
set -eu

# Transactional MediaBot v0.9.0 deployment for MediaServer.
#
# Usage (run as root on MediaServer):
#   MEDIABOT_ALLOWED_GUILD_IDS="123456789012345678" \
#       /path/to/deploy_v09.sh /tmp/mediabot-v090-<unique-id>
#
# MEDIABOT_ALLOWED_GUILD_IDS is a deployment input. The script writes both that
# name and the application's ALLOWED_GUILD_IDS compatibility name to .env. If
# the input is omitted, an already configured value in the live .env is used.

umask 077

release_version="0.9.0"
stage_namespace="/tmp/mediabot-v090-"
target="/opt/stacks/mediabot"
container="mediabot"
service="mediabot"
lock_dir="/var/lock/mediabot-v09-deploy.lock"
manifest_files="app.py Dockerfile compose.yaml requirements.txt .env.example .dockerignore"

stage="${1:-}"
rollback_drill="${MEDIABOT_ROLLBACK_DRILL:-0}"
helper_image=""
candidate_image=""
rollback_image=""
old_image_id=""
old_image_name=""
backup=""
backup_rel=""
stamp=""
lock_held=0
backup_ready=0
bot_stopped=0
original_running=0
deployment_succeeded=0
log_probe=""

say() {
    printf '%s\n' "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_regular_file() {
    test -f "$1" || die "Required file is missing: $1"
    test ! -L "$1" || die "Refusing symbolic link: $1"
}

assert_container_stopped() {
    if docker inspect "$container" >/dev/null 2>&1; then
        state="$(docker inspect -f '{{.State.Running}}' "$container")"
        test "$state" = "false" || return 1
    fi
}

run_target_helper() {
    docker run --rm --network none --read-only --tmpfs /tmp:size=32m \
        --user 0:0 \
        -e "BACKUP_REL=$backup_rel" \
        -e "DEPLOY_STAMP=$stamp" \
        -v "$target:/target" \
        "$helper_image" "$@"
}

verify_backup() {
    run_target_helper sh -eu -c '
        root="/target/$BACKUP_REL"
        test -d "$root/runtime/mediabot"
        test -s "$root/runtime/app.py"
        test -s "$root/runtime/Dockerfile"
        test -s "$root/runtime/compose.yaml"
        test -s "$root/runtime/requirements.txt"
        test -s "$root/runtime/.env.example"
        test -s "$root/runtime/.dockerignore"
        test -s "$root/.env"
        test -s "$root/mediabot.db"
        test -s "$root/data-metadata.json"
        (cd "$root/runtime" && sha256sum -c ../runtime.sha256 >/dev/null)
        (cd "$root" && sha256sum -c env.sha256 database.sha256 >/dev/null)
    '
    run_target_helper python -c '
import os, sqlite3
path = "/target/" + os.environ["BACKUP_REL"] + "/mediabot.db"
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
try:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
finally:
    connection.close()
'
}

restore_data_metadata() {
    run_target_helper python -c '
import json, os, stat
root = "/target/data"
metadata_path = "/target/" + os.environ["BACKUP_REL"] + "/data-metadata.json"
with open(metadata_path, "r", encoding="utf-8") as handle:
    entries = json.load(handle)
for relative, metadata in sorted(entries.items(), key=lambda item: item[0].count("/"), reverse=True):
    path = root if relative == "." else os.path.join(root, relative)
    resolved = os.path.realpath(path)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise RuntimeError("unsafe metadata path")
    if not os.path.exists(path):
        continue
    os.chown(path, int(metadata["uid"]), int(metadata["gid"]))
    os.chmod(path, int(metadata["mode"]))
'
}

restore_runtime_manifest() {
    run_target_helper sh -eu -c '
        root="/target/$BACKUP_REL"
        restore="/target/.mediabot-restore-$DEPLOY_STAMP"
        displaced="/target/.mediabot-displaced-$DEPLOY_STAMP"
        test ! -e "$restore"
        test ! -e "$displaced"
        cp -a "$root/runtime/mediabot" "$restore"

        if test -e /target/mediabot; then
            mv /target/mediabot "$displaced"
        fi
        mv "$restore" /target/mediabot

        for name in app.py Dockerfile compose.yaml requirements.txt .env.example .dockerignore; do
            temporary="/target/.${name}.restore-$DEPLOY_STAMP"
            cp -a "$root/runtime/$name" "$temporary"
            mv -f "$temporary" "/target/$name"
        done

        temporary="/target/.env.restore-$DEPLOY_STAMP"
        cp -a "$root/.env" "$temporary"
        mv -f "$temporary" /target/.env

        rm -rf -- "/target/.mediabot-incoming-$DEPLOY_STAMP" \
            "/target/.mediabot-previous-$DEPLOY_STAMP"
        for name in app.py Dockerfile compose.yaml requirements.txt .env.example .dockerignore; do
            rm -f -- "/target/.${name}.install-$DEPLOY_STAMP"
        done
        rm -f -- "/target/.env.install-$DEPLOY_STAMP"
        rm -rf -- "$displaced"
    '
}

restore_sqlite_atomically() {
    assert_container_stopped
run_target_helper python -c '
import json, os, shutil, sqlite3
os.umask(0o077)
root = "/target"
data = os.path.join(root, "data")
backup = os.path.join(root, os.environ["BACKUP_REL"])
live = os.path.join(data, "mediabot.db")
temporary = os.path.join(data, ".mediabot.db.restore-" + os.environ["DEPLOY_STAMP"])
with open(os.path.join(backup, "data-metadata.json"), "r", encoding="utf-8") as handle:
    metadata = json.load(handle)["mediabot.db"]
if os.path.exists(temporary):
    raise RuntimeError("restore temporary path already exists")
with open(os.path.join(backup, "mediabot.db"), "rb") as source, open(temporary, "xb") as destination:
    shutil.copyfileobj(source, destination, length=1024 * 1024)
    destination.flush()
    os.fsync(destination.fileno())
connection = sqlite3.connect(f"file:{temporary}?mode=ro", uri=True)
try:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
finally:
    connection.close()
os.chown(temporary, int(metadata["uid"]), int(metadata["gid"]))
os.chmod(temporary, int(metadata["mode"]))
for suffix in ("-wal", "-shm", "-journal"):
    try:
        os.unlink(live + suffix)
    except FileNotFoundError:
        pass
os.replace(temporary, live)
directory_fd = os.open(data, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
connection = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
try:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
finally:
    connection.close()
'

    run_target_helper sh -eu -c '
        root="/target/$BACKUP_REL"
        if test -f "$root/runtime-health.present"; then
            temporary="/target/data/.runtime-health.restore-$DEPLOY_STAMP"
            cp -a "$root/runtime-health.json" "$temporary"
            mv -f "$temporary" /target/data/runtime-health.json
        else
            rm -f -- /target/data/runtime-health.json
        fi
        rm -f -- /target/data/.mediabot-write-probe-* \
            /target/data/.runtime-health-probe-*
    '
    restore_data_metadata
}

restore_old_container() {
    restore_status=0

    if test -n "$old_image_id" && test -n "$old_image_name"; then
        case "$old_image_name" in
            sha256:*|*@sha256:*) restore_status=1 ;;
            *) docker image tag "$old_image_id" "$old_image_name" >/dev/null 2>&1 || restore_status=1 ;;
        esac
    else
        restore_status=1
    fi

    if test "$restore_status" -eq 0; then
        (cd "$target" && docker compose up -d --no-build --force-recreate "$service") \
            >/dev/null 2>&1 || restore_status=1
    fi

    if test "$restore_status" -ne 0; then
        (cd "$target" && docker compose up -d --build --force-recreate "$service") \
            >/dev/null 2>&1 || return 1
    fi

    if test "$original_running" -eq 0; then
        docker stop --time 30 "$container" >/dev/null 2>&1 || return 1
    else
        sleep 5
        test "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = "true" \
            || return 1
    fi
}

rollback_deployment() {
    rollback_failed=0
    say "Deployment failed; starting guarded rollback."

    if test "$backup_ready" -eq 1; then
        if docker inspect "$container" >/dev/null 2>&1; then
            docker stop --time 45 "$container" >/dev/null 2>&1 || rollback_failed=1
        fi
        assert_container_stopped >/dev/null 2>&1 || rollback_failed=1

        if test "$rollback_failed" -eq 0; then
            verify_backup >/dev/null 2>&1 || rollback_failed=1
        fi
        if test "$rollback_failed" -eq 0; then
            restore_runtime_manifest >/dev/null 2>&1 || rollback_failed=1
        fi
        if test "$rollback_failed" -eq 0; then
            restore_sqlite_atomically >/dev/null 2>&1 || rollback_failed=1
        fi
        if test "$rollback_failed" -eq 0; then
            restore_old_container || rollback_failed=1
        fi
    elif test "$bot_stopped" -eq 1 && test "$original_running" -eq 1; then
        docker start "$container" >/dev/null 2>&1 || rollback_failed=1
    fi

    if test "$rollback_failed" -eq 0; then
        say "Rollback completed. Original runtime, configuration, database, and metadata were restored."
        return 0
    fi

    printf 'ERROR: Automatic rollback was incomplete. Leave MediaBot stopped and recover from %s.\n' \
        "$backup" >&2
    return 1
}

on_exit() {
    exit_code="$1"
    trap - EXIT HUP INT TERM
    set +e

    if test "$deployment_succeeded" -ne 1 && test "$exit_code" -eq 0; then
        exit_code=1
    fi

    if test "$deployment_succeeded" -ne 1 \
        && { test "$backup_ready" -eq 1 || test "$bot_stopped" -eq 1; }; then
        rollback_deployment || exit_code=1
    fi

    if test -n "$candidate_image"; then
        docker image rm "$candidate_image" >/dev/null 2>&1 || :
    fi
    if test "$backup_ready" -eq 0 && test -n "$rollback_image"; then
        docker image rm "$rollback_image" >/dev/null 2>&1 || :
    fi
    if test -n "$log_probe"; then
        rm -f -- "$log_probe" >/dev/null 2>&1 || :
    fi
    if test "$lock_held" -eq 1; then
        rmdir "$lock_dir" >/dev/null 2>&1 || :
    fi
    exit "$exit_code"
}

wait_for_health() {
    attempt=0
    while test "$attempt" -lt 48; do
        attempt=$((attempt + 1))
        if docker inspect "$container" >/dev/null 2>&1; then
            state="$(docker inspect -f '{{.State.Status}}' "$container")"
            health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container")"
            if test "$state" = "running" && test "$health" = "healthy"; then
                return 0
            fi
            case "$state:$health" in
                exited:*|dead:*|*:unhealthy) return 1 ;;
            esac
        fi
        sleep 5
    done
    return 1
}

# ---------------------------------------------------------------------------
# 1. Validate invocation, immutable stage boundaries, and prerequisites.
# ---------------------------------------------------------------------------

say "1. Validate v0.9.0 stage, deployment input, and host prerequisites"

test "$(id -u)" -eq 0 || die "Run this deployment as root."
case "$rollback_drill" in
    0|1) ;;
    *) die "MEDIABOT_ROLLBACK_DRILL must be 0 or 1." ;;
esac

case "$stage" in
    "$stage_namespace"*) ;;
    *) die "Stage must be under ${stage_namespace}<unique-id>." ;;
esac
test -d "$stage" || die "Stage directory does not exist."
test ! -L "$stage" || die "Stage directory may not be a symbolic link."
stage="$(readlink -f -- "$stage")"
case "$stage" in
    "$stage_namespace"*) ;;
    *) die "Resolved stage escaped the v0.9.0 staging namespace." ;;
esac

test -d "$target" || die "Target stack is missing: $target"
test ! -L "$target" || die "Target stack may not be a symbolic link."
test "$(readlink -f -- "$target")" = "$target" || die "Unexpected target resolution."

mkdir "$lock_dir" 2>/dev/null \
    || die "Another deployment is active, or the stale lock $lock_dir needs inspection."
lock_held=1
trap 'on_exit "$?"' EXIT
trap 'exit 130' HUP INT TERM

stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
backup_rel=".codex-backups/${stamp}-v090"
backup="$target/$backup_rel"
candidate_image="mediabot-v090-candidate:${stamp}"
rollback_image="mediabot-rollback:pre-v090-${stamp}"
release_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

for name in $manifest_files; do
    require_regular_file "$stage/$name"
    require_regular_file "$target/$name"
done
test -d "$stage/mediabot" && test ! -L "$stage/mediabot" \
    || die "Staged mediabot package is missing or unsafe."
test -d "$target/mediabot" && test ! -L "$target/mediabot" \
    || die "Live mediabot package is missing or unsafe."
test -z "$(find "$target/mediabot" -type l -print -quit)" \
    || die "Live mediabot package contains a symbolic link."
require_regular_file "$stage/mediabot/core/runtime_health.py"
require_regular_file "$stage/mediabot/core/database.py"
require_regular_file "$stage/mediabot/core/event_store.py"
require_regular_file "$stage/mediabot/core/transient_store.py"
test -d "$stage/tests" && test ! -L "$stage/tests" \
    || die "Staged regression tests are missing or unsafe."
require_regular_file "$target/.env"
require_regular_file "$target/data/mediabot.db"
test "$(stat -c '%u:%g %a' "$target/.env")" = "0:0 600" \
    || die "Live .env must be owned by root:root with mode 600 before deployment."
test -d "$target/data" && test ! -L "$target/data" \
    || die "Live data directory is missing or unsafe."

test ! -e "$stage/.env" || die "Refusing a staged .env."
test ! -e "$stage/data" || die "Refusing staged runtime data."
test ! -e "$stage/.codex-backups" || die "Refusing staged backups."
test -z "$(find "$stage" -type l -print -quit)" || die "Stage contains a symbolic link."
test -z "$(find "$stage" \( -type d -name __pycache__ -o -type f -name '*.py[co]' \) -print -quit)" \
    || die "Stage contains generated Python bytecode."
test -z "$(find "$target/data" -type l -print -quit)" || die "Live data contains a symbolic link."
if test -e "$target/.codex-backups"; then
    test -d "$target/.codex-backups" && test ! -L "$target/.codex-backups" \
        || die "Live backup root is not a real directory."
    case "$(readlink -f -- "$target/.codex-backups")" in
        "$target/.codex-backups") ;;
        *) die "Live backup root resolves outside the target stack." ;;
    esac
    test "$(stat -c '%u:%g' "$target/.codex-backups")" = "0:0" \
        || die "Live backup root must be owned by root:root."
    test -z "$(find "$target/.codex-backups" -maxdepth 0 -perm /022 -print)" \
        || die "Live backup root may not be group- or world-writable."
fi

for rule in '.env' '.env.*' '!.env.example' 'data/' '.codex-backups/' 'tests/' 'scripts/'; do
    grep -qxF "$rule" "$stage/.dockerignore" \
        || die "Staged .dockerignore is missing required rule: $rule"
done

docker info >/dev/null 2>&1 || die "Docker daemon is unavailable."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is unavailable."
docker inspect "$container" >/dev/null 2>&1 || die "Live MediaBot container is missing."

for required_setting in \
    DISCORD_TOKEN SEERR_API_KEY JELLYFIN_API_KEY SOULSYNC_API_KEY SONARR_API_KEY \
    SEERR_URL JELLYFIN_URL SOULSYNC_URL SONARR_URL; do
    grep -Eq "^${required_setting}=.+$" "$target/.env" \
        || die "Live .env is missing required setting: $required_setting"
done

base_image="$(sed -n '1{s/^FROM[[:space:]][[:space:]]*//;p;}' "$stage/Dockerfile")"
case "$base_image" in
    *@sha256:*) ;;
    *) die "Dockerfile base image must be pinned by sha256 digest." ;;
esac
docker image inspect "$base_image" >/dev/null 2>&1 || docker pull "$base_image" >/dev/null
helper_image="$(docker image inspect -f '{{.Id}}' "$base_image")"
test -n "$helper_image" || die "Could not resolve the pinned helper image."

allowed_guild_ids="${MEDIABOT_ALLOWED_GUILD_IDS:-}"
if test -z "$allowed_guild_ids"; then
    allowed_guild_ids="$(sed -n 's/^MEDIABOT_ALLOWED_GUILD_IDS=//p' "$target/.env" | tail -n 1)"
fi
if test -z "$allowed_guild_ids"; then
    allowed_guild_ids="$(sed -n 's/^ALLOWED_GUILD_IDS=//p' "$target/.env" | tail -n 1)"
fi
allowed_guild_ids="$(printf '%s' "$allowed_guild_ids" | tr -d '[:space:]')"
case "$allowed_guild_ids" in
    ''|,*|*,|*,,*|*[!0-9,]*)
        die "Set MEDIABOT_ALLOWED_GUILD_IDS to one or more comma-separated Discord server IDs."
        ;;
esac
old_ifs="$IFS"
IFS=,
set -- $allowed_guild_ids
IFS="$old_ifs"
for guild_id in "$@"; do
    case "$guild_id" in
        0*) die "MEDIABOT_ALLOWED_GUILD_IDS contains an invalid Discord server ID." ;;
    esac
    test "${#guild_id}" -le 20 \
        || die "MEDIABOT_ALLOWED_GUILD_IDS contains an overlong Discord server ID."
done

original_running="$(docker inspect -f '{{if .State.Running}}1{{else}}0{{end}}' "$container")"
old_image_id="$(docker inspect -f '{{.Image}}' "$container")"
old_image_name="$(docker inspect -f '{{.Config.Image}}' "$container")"
test -n "$old_image_id" || die "Could not record the live image ID."
docker image tag "$old_image_id" "$rollback_image" >/dev/null

# ---------------------------------------------------------------------------
# 2. Compile, test, and build the exact staged release before downtime.
# ---------------------------------------------------------------------------

say "2. Compile, test, and build the exact staged v0.9.0 release"

docker build --tag "$candidate_image" "$stage"
docker run --rm --network none --read-only --tmpfs /tmp:size=128m \
    --user 1000:1000 \
    -e PYTHONPYCACHEPREFIX=/tmp/pycache \
    --entrypoint python "$candidate_image" \
    -m compileall -q /app/app.py /app/mediabot

docker run --rm --network none --read-only --tmpfs /tmp:size=256m \
    --user 1000:1000 \
    -e DISCORD_TOKEN=test-token \
    -e SEERR_API_KEY=test-key \
    -e ALLOWED_GUILD_IDS=1 \
    -e DB_PATH=/tmp/mediabot-test.db \
    -e LOG_PATH=/tmp/mediabot-test.log \
    -e RUNTIME_HEALTH_PATH=/tmp/runtime-health.json \
    -v "$stage/scripts:/scripts:ro" \
    -v "$stage/tests:/tests:ro" \
    --entrypoint python "$candidate_image" \
    -m unittest discover -s /tests -q

docker run --rm --network none --read-only --tmpfs /tmp:size=64m \
    --user 1000:1000 \
    -e DISCORD_TOKEN=deployment-version-validation \
    -e SEERR_API_KEY=deployment-version-validation \
    -e ALLOWED_GUILD_IDS=1 \
    -e DB_PATH=/tmp/mediabot-version.db \
    -e LOG_PATH=/tmp/mediabot-version.log \
    --entrypoint python "$candidate_image" -c \
    "import app; assert app.BOT_VERSION == '$release_version'; assert app.ALLOWED_GUILD_IDS == frozenset({1})"

# ---------------------------------------------------------------------------
# 3. Stop the writer and take a SQLite Online Backup API snapshot.
# ---------------------------------------------------------------------------

say "3. Stop MediaBot and prove the SQLite writer is quiescent"
bot_stopped=1
docker stop --time 45 "$container" >/dev/null
assert_container_stopped || die "Could not prove $container is stopped."

run_target_helper python -c '
import sqlite3
path = "/target/data/mediabot.db"
connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
try:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
finally:
    connection.close()
'

say "4. Create and verify a complete rollback backup"
run_target_helper sh -eu -c '
    root="/target/$BACKUP_REL"
    mkdir -p /target/.codex-backups
    test ! -e "$root"
    mkdir -m 700 "$root"
    mkdir -m 700 "$root/runtime"
    for name in app.py Dockerfile compose.yaml requirements.txt .env.example .dockerignore; do
        cp -a "/target/$name" "$root/runtime/$name"
    done
    cp -a /target/mediabot "$root/runtime/mediabot"
    cp -a /target/.env "$root/.env"
    chmod 600 "$root/.env"
    if test -f /target/data/runtime-health.json; then
        cp -a /target/data/runtime-health.json "$root/runtime-health.json"
        : > "$root/runtime-health.present"
    fi
    (cd "$root/runtime" && find . -type f -print0 | sort -z | xargs -0 sha256sum > ../runtime.sha256)
    (cd "$root" && sha256sum .env > env.sha256)
'

run_target_helper python -c '
import json, os, sqlite3, stat
os.umask(0o077)
target = "/target"
data = os.path.join(target, "data")
backup = os.path.join(target, os.environ["BACKUP_REL"])

metadata = {}
for directory, directories, files in os.walk(data):
    for name in ["."] + directories + files:
        path = directory if name == "." else os.path.join(directory, name)
        if os.path.islink(path):
            raise RuntimeError("data symlink discovered during backup")
        relative = os.path.relpath(path, data)
        info = os.stat(path, follow_symlinks=False)
        metadata[relative] = {
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode),
        }
metadata_temporary = os.path.join(backup, ".data-metadata.json.tmp")
with open(metadata_temporary, "x", encoding="utf-8") as handle:
    json.dump(metadata, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(metadata_temporary, 0o600)
os.replace(metadata_temporary, os.path.join(backup, "data-metadata.json"))

source_path = os.path.join(data, "mediabot.db")
temporary = os.path.join(backup, ".mediabot.db.backup.tmp")
destination_path = os.path.join(backup, "mediabot.db")
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
destination = sqlite3.connect(temporary)
try:
    source.execute("PRAGMA busy_timeout=30000")
    assert source.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    source.backup(destination)
    assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not destination.execute("PRAGMA foreign_key_check").fetchall()
finally:
    destination.close()
    source.close()
with open(temporary, "rb") as handle:
    os.fsync(handle.fileno())
os.chmod(temporary, 0o600)
os.replace(temporary, destination_path)
directory_fd = os.open(backup, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
'

run_target_helper sh -eu -c '
    root="/target/$BACKUP_REL"
    (cd "$root" && sha256sum mediabot.db > database.sha256)
    chmod 600 "$root/data-metadata.json" "$root/mediabot.db" \
        "$root/runtime.sha256" "$root/env.sha256" "$root/database.sha256"
'
verify_backup
backup_ready=1

# ---------------------------------------------------------------------------
# 5. Install all build inputs and atomically update configuration.
# ---------------------------------------------------------------------------

say "5. Install the complete runtime manifest and guarded guild scope"

docker run --rm --network none --read-only --tmpfs /tmp:size=32m \
    --user 0:0 \
    -e "DEPLOY_STAMP=$stamp" \
    -v "$target:/target" \
    -v "$stage:/stage:ro" \
    "$helper_image" sh -eu -c '
        incoming="/target/.mediabot-incoming-$DEPLOY_STAMP"
        displaced="/target/.mediabot-previous-$DEPLOY_STAMP"
        test ! -e "$incoming"
        test ! -e "$displaced"
        cp -a /stage/mediabot "$incoming"
        chown -R 0:0 "$incoming"
        find "$incoming" -type d -exec chmod 755 {} \;
        find "$incoming" -type f -exec chmod 644 {} \;

        mv /target/mediabot "$displaced"
        mv "$incoming" /target/mediabot

        for name in app.py Dockerfile compose.yaml requirements.txt .env.example .dockerignore; do
            temporary="/target/.${name}.install-$DEPLOY_STAMP"
            cp -a "/stage/$name" "$temporary"
            chown 0:0 "$temporary"
            chmod 644 "$temporary"
            mv -f "$temporary" "/target/$name"
        done
        rm -rf -- "$displaced"
    '

docker run --rm --network none --read-only --tmpfs /tmp:size=32m \
    --user 0:0 \
    -e "MEDIABOT_ALLOWED_GUILD_IDS=$allowed_guild_ids" \
    -e "DEPLOY_STAMP=$stamp" \
    -v "$target:/target" \
    "$helper_image" python -c '
import os, stat
os.umask(0o077)
path = "/target/.env"
value = os.environ["MEDIABOT_ALLOWED_GUILD_IDS"]
info = os.stat(path, follow_symlinks=False)
with open(path, "r", encoding="utf-8") as handle:
    lines = [
        line for line in handle.read().splitlines()
        if not line.startswith("ALLOWED_GUILD_IDS=")
        and not line.startswith("MEDIABOT_ALLOWED_GUILD_IDS=")
    ]
lines.extend([
    "MEDIABOT_ALLOWED_GUILD_IDS=" + value,
    "ALLOWED_GUILD_IDS=" + value,
])
temporary = "/target/.env.install-" + os.environ["DEPLOY_STAMP"]
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
    handle.write("\n".join(lines).rstrip("\n") + "\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chown(temporary, info.st_uid, info.st_gid)
os.chmod(temporary, stat.S_IMODE(info.st_mode))
os.replace(temporary, path)
directory_fd = os.open("/target", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
'

for name in $manifest_files; do
    cmp -s "$stage/$name" "$target/$name" || die "Installed manifest mismatch: $name"
done
diff -qr "$stage/mediabot" "$target/mediabot" >/dev/null \
    || die "Installed mediabot package differs from the staged package."

# ---------------------------------------------------------------------------
# 6. Standardize SQLite and migrate data ownership before the new user starts.
# ---------------------------------------------------------------------------

say "6. Standardize SQLite, migrate live data to uid/gid 1000, and write-probe"
assert_container_stopped || die "Could not prove $container is stopped before database migration."

run_target_helper python -c '
import os, sqlite3
path = "/target/data/mediabot.db"
connection = sqlite3.connect(path, timeout=30)
try:
    connection.execute("PRAGMA busy_timeout=30000")
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    assert str(mode).lower() == "delete"
finally:
    connection.close()
for suffix in ("-wal", "-shm", "-journal"):
    try:
        os.unlink(path + suffix)
    except FileNotFoundError:
        pass
'

run_target_helper python -c '
import os
root = "/target/data"
for directory, directories, files in os.walk(root):
    if os.path.islink(directory):
        raise RuntimeError("unsafe data symlink")
    os.chown(directory, 1000, 1000)
    os.chmod(directory, 0o750)
    for name in files:
        path = os.path.join(directory, name)
        if os.path.islink(path):
            raise RuntimeError("unsafe data symlink")
        os.chown(path, 1000, 1000)
        os.chmod(path, 0o640)
'

docker run --rm --network none --read-only --tmpfs /tmp:size=32m \
    --user 1000:1000 \
    -v "$target/data:/app/data" \
    --entrypoint python "$candidate_image" -c '
import os, sqlite3
root = "/app/data"
probe = os.path.join(root, ".mediabot-write-probe-v090")
descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
try:
    os.write(descriptor, b"ok\n")
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.unlink(probe)
connection = sqlite3.connect(os.path.join(root, "mediabot.db"), timeout=30)
try:
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("BEGIN IMMEDIATE")
    connection.rollback()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
finally:
    connection.close()
'

# ---------------------------------------------------------------------------
# 7. Rebuild/recreate, then wait on the application-level heartbeat.
# ---------------------------------------------------------------------------

say "7. Build and recreate MediaBot v0.9.0"
(cd "$target" && docker compose up -d --build --force-recreate "$service")
bot_stopped=0

say "8. Poll application health and validate exact source/version/schema/providers"
wait_for_health || die "MediaBot did not become healthy within 240 seconds."

if test "$rollback_drill" -eq 1; then
    say "Rollback drill reached a healthy v0.9.0 candidate; forcing guarded rollback."
    die "Intentional rollback drill trigger."
fi

test "$(docker inspect -f '{{.RestartCount}}' "$container")" = "0" \
    || die "MediaBot restarted during deployment."

docker exec "$container" python -m mediabot.core.runtime_health \
    --path /app/data/runtime-health.json \
    --version "$release_version" \
    --max-age 120 >/dev/null

docker exec "$container" python -c \
    "import app, os; assert app.BOT_VERSION == '$release_version'; assert app.ALLOWED_GUILD_IDS; assert app.ALLOWED_GUILD_IDS == app.parse_allowed_guild_ids(os.environ['MEDIABOT_ALLOWED_GUILD_IDS']); assert app.LOG_MAX_BYTES == 2097152; assert app.LOG_BACKUP_COUNT == 5; print('VERSION_AND_APP_LOG_LIMITS ok')"

docker exec "$container" python -c '
import sqlite3
connection = sqlite3.connect("/app/data/mediabot.db", timeout=30)
try:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = \"table\"")}
    required = {
        "request_messages", "media_ratings", "music_requests", "media_reports",
        "media_request_intents", "transient_ui_entries", "transient_ui_batches",
        "event_schema_migrations", "events", "event_nominations", "event_votes", "event_slots",
    }
    assert required <= tables
    assert connection.execute("SELECT MAX(version) FROM event_schema_migrations").fetchone()[0] == 1
finally:
    connection.close()
print("DATABASE ok")
'

docker exec "$container" python -c '
import asyncio, app
async def main():
    providers = (
        ("Seerr", app.seerr),
        ("Jellyfin", app.jellyfin),
        ("SoulSync", app.soulsync),
        ("Sonarr", app.sonarr),
    )
    for name, provider in providers:
        if getattr(provider, "enabled", True) is False:
            raise RuntimeError(name + " provider is disabled")
        await provider.start()
        try:
            await provider.health()
        finally:
            await provider.close()
try:
    asyncio.run(main())
except Exception as exc:
    print("PROVIDERS failed: " + type(exc).__name__)
    raise SystemExit(1)
else:
    print("PROVIDERS ok")
'

source_digest="$(docker run --rm --network none --read-only --tmpfs /tmp:size=16m \
    --user 0:0 -v "$stage:/source:ro" "$helper_image" python -c '
import hashlib, os
root = "/source"
paths = ["app.py"]
for directory, _, files in os.walk(os.path.join(root, "mediabot")):
    for name in files:
        if not name.endswith((".pyc", ".pyo")):
            paths.append(os.path.relpath(os.path.join(directory, name), root))
digest = hashlib.sha256()
for relative in sorted(paths):
    digest.update(relative.encode("utf-8") + b"\0")
    with open(os.path.join(root, relative), "rb") as handle:
        digest.update(handle.read())
print(digest.hexdigest())
')"
container_digest="$(docker exec "$container" python -c '
import hashlib, os
root = "/app"
paths = ["app.py"]
for directory, _, files in os.walk(os.path.join(root, "mediabot")):
    for name in files:
        if not name.endswith((".pyc", ".pyo")):
            paths.append(os.path.relpath(os.path.join(directory, name), root))
digest = hashlib.sha256()
for relative in sorted(paths):
    digest.update(relative.encode("utf-8") + b"\0")
    with open(os.path.join(root, relative), "rb") as handle:
        digest.update(handle.read())
print(digest.hexdigest())
')"
test "$source_digest" = "$container_digest" || die "Running source does not match staged v0.9.0 source."

# ---------------------------------------------------------------------------
# 9. Enforce container security, permissions, log caps, and clean startup.
# ---------------------------------------------------------------------------

say "9. Verify container hardening, data permissions, and bounded logs"

test "$(docker inspect -f '{{.Config.User}}' "$container")" = "1000:1000" \
    || die "Container does not run as uid/gid 1000."
test "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$container")" = "true" \
    || die "Container root filesystem is not read-only."
test "$(docker inspect -f '{{.HostConfig.Init}}' "$container")" = "true" \
    || die "Container init is not enabled."
test "$(docker inspect -f '{{.HostConfig.PidsLimit}}' "$container")" = "128" \
    || die "Container PID limit is incorrect."
test "$(docker inspect -f '{{.HostConfig.Memory}}' "$container")" = "536870912" \
    || die "Container memory limit is incorrect."
case "$(docker inspect -f '{{json .HostConfig.CapDrop}}' "$container")" in
    *ALL*) ;;
    *) die "Container capabilities were not fully dropped." ;;
esac
case "$(docker inspect -f '{{json .HostConfig.SecurityOpt}}' "$container")" in
    *no-new-privileges:true*) ;;
    *) die "Container no-new-privileges is missing." ;;
esac
test "$(docker inspect -f '{{.HostConfig.LogConfig.Type}}' "$container")" = "json-file" \
    || die "Container log driver is not json-file."
test "$(docker inspect -f '{{index .HostConfig.LogConfig.Config "max-size"}}' "$container")" = "10m" \
    || die "Container max-size log limit is incorrect."
test "$(docker inspect -f '{{index .HostConfig.LogConfig.Config "max-file"}}' "$container")" = "3" \
    || die "Container max-file log limit is incorrect."
published_ports="$(docker inspect -f '{{range $port, $bindings := .NetworkSettings.Ports}}{{if $bindings}}{{$port}}{{end}}{{end}}' "$container")"
test -z "$published_ports" || die "MediaBot unexpectedly publishes a host port."

test "$(stat -c '%u:%g %a' "$target/.env")" = "0:0 600" \
    || die ".env ownership or mode is unsafe."
test "$(stat -c '%u:%g %a' "$target/data")" = "1000:1000 750" \
    || die "Data directory ownership or mode is incorrect."
test "$(stat -c '%u:%g %a' "$target/data/mediabot.db")" = "1000:1000 640" \
    || die "SQLite ownership or mode is incorrect."
test "$(stat -c '%u:%g %a' "$target/data/mediabot.log")" = "1000:1000 640" \
    || die "Application log ownership or mode is incorrect."
test "$(stat -c '%u:%g %a' "$target/data/runtime-health.json")" = "1000:1000 640" \
    || die "Runtime heartbeat ownership or mode is incorrect."
for name in $manifest_files; do
    test "$(stat -c '%u:%g %a' "$target/$name")" = "0:0 644" \
        || die "Runtime manifest permissions are incorrect: $name"
done
test -z "$(find "$target/mediabot" -type d ! -perm 0755 -print -quit)" \
    || die "Package directory modes are incorrect."
test -z "$(find "$target/mediabot" -type f ! -perm 0644 -print -quit)" \
    || die "Package file modes are incorrect."
test -z "$(find "$target/mediabot" ! -user root -o ! -group root | head -n 1)" \
    || die "Package ownership is incorrect."

log_probe="$(mktemp /tmp/mediabot-v090-logs.XXXXXX)"
chmod 600 "$log_probe"
docker logs --since "$release_started_at" "$container" >"$log_probe" 2>&1 \
    || die "Could not read fresh MediaBot logs."
if grep -Eqi 'Traceback \(most recent call last\)|SyntaxError|ImportError|PermissionError' "$log_probe"; then
    die "Fresh MediaBot logs contain a fatal runtime error. Raw logs were intentionally not printed."
fi
rm -f -- "$log_probe"
log_probe=""

test "$(docker inspect -f '{{.State.Status}}' "$container")" = "running"
test "$(docker inspect -f '{{.State.Health.Status}}' "$container")" = "healthy"
test "$(docker inspect -f '{{.RestartCount}}' "$container")" = "0"

deployment_succeeded=1
trap - EXIT HUP INT TERM
if test -n "$candidate_image"; then
    docker image rm "$candidate_image" >/dev/null 2>&1 || :
fi
if test "$lock_held" -eq 1; then
    rmdir "$lock_dir" || die "Deployment succeeded but the lock could not be removed: $lock_dir"
    lock_held=0
fi

say "10. MediaBot v0.9.0 deployment passed every release gate"
say "Rollback backup: $backup"
say "Rollback image: $rollback_image"
