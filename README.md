# Dogginator MediaBot

Dogginator MediaBot is a Discord-first orchestration layer for a personal media
stack. It gives household users one small, consistent command surface while
leaving media search, approval, acquisition, and playback with the services
that already own those jobs.

Current source version: **0.10.0**

## What it does

- Searches and requests movies or specific TV seasons through Seerr.
- Repairs exact missing episodes through Sonarr when a previously approved
  season is only partially present.
- Browses only media currently playable in Jellyfin with `$discover`.
- Ranks unseen, requestable media with `$recommend`, using explicit 1-10
  ratings and optional Jellyfin/Trakt taste signals.
- Requests exact music tracks through SoulSync.
- Reconciles video, episode, and music progress through one `$status` command.
- Files exact Jellyfin playback reports into a guild-local administrator queue.
- Runs reusable media-night ballots with title and availability voting,
  editable schedules, Discord Scheduled Events, reminders, and history.
- Cleans up abandoned interactive messages after five minutes while preserving
  successful receipts.

MediaBot is deliberately not a replacement for Seerr, Jellyfin, Sonarr,
Radarr, or SoulSync. The detailed provider boundaries and request lifecycles
are documented in [mediabot/ARCHITECTURE.md](mediabot/ARCHITECTURE.md).

## Command surface

The default prefix is `$`.

| Command | Purpose |
| --- | --- |
| `$request <title> [year]` | Search for an exact movie or show, then choose seasons for TV. |
| `$music <artist and track>` | Page through track matches and request the exact song. |
| `$discover [movie\|show] [genres]` | Browse media already playable in Jellyfin. |
| `$recommend [movie\|show] [genres]` | Rank unseen media that can be requested. |
| `$rate <title> <1-10>` | Choose an exact title and save a durable rating. Run `$rate` alone to list ratings. |
| `$status <title or #request>` | Reconcile library, request, episode, and music state. |
| `$report <title> [SxxExx]` | File a playback-problem report for exact Jellyfin media. |
| `$event` | Open the current event dashboard and its restart-safe controls. |
| `$event create <name> [--votes N]` | Administrator: open a reusable media-night ballot. |
| `$event nominate <title> [year]` | Search for and add one exact movie or show; this does not request it. |
| `$event vote` | Open your title ballot. |
| `$event time [YYYY-MM-DD HH:MM,...]` | Vote on proposed times, or let an administrator add times with selectors or text. |
| `$event schedule [YYYY-MM-DD HH:MM,...]` | Administrator: preview and publish the ranked lineup. |
| `$event reschedule <id> [YYYY-MM-DD HH:MM,...]` | Administrator: move an existing schedule without rebuilding its ballot. |
| `$event reopen <id>` | Administrator: return a scheduled event to nominations and voting. |
| `$event tonight` | Show the guild's scheduled lineup for the current local day. |
| `$event history` | Show recent completed, cancelled, and archived events. |
| `$event complete\|cancel\|archive <id>` | Administrator: close or soft-hide one event. |
| `$event clear` | Administrator: complete expired schedules and archive terminal events. |
| `$new [count]` | Show recently added Jellyfin media. |
| `$help [command]` | Show the current user-facing command model and generated details. |

`$random` remains a compatibility alias for `$discover --random`.
`$randomrequest` and `$rr` alias `$recommend --random`; `$ratings` aliases
`$rate` with no arguments. They are aliases, not separate product concepts.

Genre expressions default to AND and also accept `and`/`&&`, `or`/`||`,
commas, and parentheses. For example:

```text
$recommend Fantasy Romance --count 4
$recommend (Fantasy and Romance) or Action --count 3
$discover Comedy --random --count 3
```

### Event workflow

An administrator creates an event, members nominate exact Seerr results, and
members vote independently on titles and every proposed time they can attend.
The durable dashboard exposes **Vote titles**, **Vote times**, and administrator
management controls, so the useful path does not require memorizing every
subcommand.

Discord does not provide bots with a native calendar-picker component.
MediaBot uses date and time selectors for the common path plus a **Custom**
modal for an exact local `YYYY-MM-DD HH:MM` value. Plain-text commands remain
available for accessibility and fast administration. Times are saved in UTC
and rendered with Discord timestamps so each reader sees their own timezone.

Scheduling closes voting for now rather than making the ballot immutable. An
administrator can reschedule it or reopen voting. Each future slot is mirrored
to a native Discord Scheduled Event when the bot has event permissions, and a
restart-safe worker sends 24-hour, 1-hour, and start-time reminders. Completed
and cancelled events remain auditable in history; archive and clear only hide
them from the active dashboard. Unfinished transient cards still disappear
after five minutes, while the event dashboard and successful actions persist.

## Architecture

```text
Discord command or button
          |
          v
   MediaBot service layer -----> SQLite state and reconciliation
          |
          +-----> Seerr: search, approval, requests, TMDB metadata
          +-----> Jellyfin: playable library, history, links, reports
          +-----> Sonarr: exact episode inventory and partial repair
          `-----> SoulSync: music search and acquisition
```

Seerr is the required video request broker. Jellyfin, Sonarr, and SoulSync are
optional integrations that enable their corresponding commands and richer
reconciliation. Trakt enrichment is optional and uses the Jellyfin Trakt
plugin for the single configured owner taste profile; MediaBot always keeps
its local 1-10 rating store authoritative.

## Requirements

- Docker Engine with Docker Compose v2 (recommended), or Python 3.13.
- A Discord bot application with **Message Content Intent** and **Server
  Members Intent** enabled. Grant **Create Events** and **Manage Events** for
  native Discord Scheduled Event publishing, edits, and cleanup.
- A Seerr instance and API key.
- A writable persistent data directory for SQLite, logs, and the runtime health
  heartbeat.
- Optional Jellyfin, Sonarr, and SoulSync API credentials.

MediaBot refuses to start without at least one trusted guild in
`ALLOWED_GUILD_IDS`. Direct messages are rejected, and the bot leaves guilds
outside the allowlist.

## Configuration

Copy `.env.example` to `.env`, replace every placeholder, and review every URL
for your environment. At minimum, configure:

```dotenv
DISCORD_TOKEN=replace-with-discord-bot-token
ALLOWED_GUILD_IDS=123456789012345678

SEERR_API_KEY=replace-with-seerr-api-key
SEERR_URL=http://host.docker.internal:5055
SEERR_PUBLIC_URL=https://requests.example.com
```

`ALLOWED_GUILD_IDS` accepts a comma-separated list of positive Discord guild
IDs. Internal provider URLs are used by the container; `*_PUBLIC_URL` values
must be browser-reachable links suitable for Discord users.

Optional integration groups:

| Integration | Variables |
| --- | --- |
| Jellyfin | `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `JELLYFIN_PUBLIC_URL`, `JELLYFIN_TASTE_USER` |
| Sonarr | `SONARR_URL`, `SONARR_API_KEY` |
| SoulSync | `SOULSYNC_URL`, `SOULSYNC_API_KEY`, `SOULSYNC_PUBLIC_URL` |

Operational settings include `DB_PATH`, `LOG_PATH`, `LOG_MAX_BYTES`,
`LOG_BACKUP_COUNT`, `RUNTIME_HEALTH_PATH`, `REQUEST_UI_TIMEOUT`,
`JELLYFIN_POLL_SECONDS`, and `MEDIA_SUBMISSION_DRAIN_SECONDS`. The Compose
defaults expect these writable files beneath `/app/data`.

Event operations use `EVENT_RECONCILE_SECONDS` (worker interval, minimum 30),
`EVENT_COMPLETION_GRACE_HOURS` (delay after the last slot before automatic
completion, minimum 1), and `EVENT_REMINDER_MENTION` (an optional role ID or
`<@&ROLE_ID>`; blank by default for quiet reminders). Because role IDs belong
to one Discord guild, the ping setting is accepted only when exactly one
`ALLOWED_GUILD_ID` is configured; multi-guild deployments must leave it blank.
Arbitrary user mentions and `@everyone` are rejected.

Never commit `.env`, a database, logs, runtime health snapshots, recovery
bundles, or provider credentials.

## Run with Docker Compose

The container runs as UID/GID `1000:1000` with a read-only root filesystem.
Prepare the bind mount with matching write access before startup:

```sh
cp .env.example .env
# Edit .env and replace every environment-specific value.
mkdir -p data
chown 1000:1000 data

docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail 100 mediabot
```

The Compose healthcheck validates the versioned runtime heartbeat rather than
merely checking whether a Python process exists.

## Run directly for development

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check

# Export the required environment variables, then:
python app.py
```

Set `DB_PATH`, `LOG_PATH`, and `RUNTIME_HEALTH_PATH` to writable local paths
when running outside the container.

## Verification

The repository uses the standard library `unittest` runner:

```sh
python -m pip check
python -m compileall -q app.py mediabot scripts tests
python -m unittest discover -s tests -q
test -x scripts/deploy_v010.sh
sh -n scripts/deploy_v010.sh
```

The GitHub Actions workflow runs the same dependency, compilation, deployer
syntax, and full unit-test gates on Python 3.13.

## Deployment note

`scripts/deploy_v010.sh` is a guarded, transactional deployer for the original
Compose layout. It backs up the runtime and SQLite database, verifies hashes
and database integrity, performs security and health gates, and rolls back on
failure. It is intentionally opinionated: audit its target paths, service
name, ownership model, and staging contract before using it on another host.

To prove rollback before a release, run the same staged deployment with
`MEDIABOT_ROLLBACK_DRILL=1`. The candidate must become healthy first; the
deployer then deliberately fails and restores the verified pre-deploy runtime,
database, environment, ownership, and container state. A drill exits non-zero
by design and must confirm that the original runtime, configuration, database,
and metadata were restored before a real deployment is attempted.

## Security

Read [SECURITY.md](SECURITY.md) before exposing the bot or any provider API.
Keep `.env` protected, grant the Discord bot only the permissions it needs,
and keep provider management endpoints on a trusted network or authenticated
reverse proxy.
