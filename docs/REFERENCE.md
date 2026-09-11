# Command and integration reference

For a new installation, start with [Install](INSTALL.md). For your first
request and everyday examples, use [Use MediaBot](USAGE.md). This page retains
the detailed command contracts and integration boundaries.

Features below require their corresponding providers. The portable default
installs MediaBot only. Nextcloud, Life and local AI gateways, qBittorrent,
VPN/quarantine services and Home Assistant must be commissioned separately.

## Command surface

The default prefix is `$`.

| Command | Purpose |
| --- | --- |
| `$request <title> [year]` | Search for an exact movie or show, then choose seasons for TV. |
| `$music <artist and track>` | Page through track matches and request the exact song. |
| `$discover [movie\|show] [genres]` | Browse media already playable in Jellyfin. |
| `$recommend [movie\|show] [genres] [--auto]` | Rank unseen requestable media; optionally use the local model on provider candidates. |
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

Private Life utilities require the bot owner:
`$think <text>` (alias `$capture`) is owner-only and writes a private raw note.
`$life` opens a private capture selector; `$life tasks` opens current Nextcloud
tasks. A selected capture offers **Make task** and **Plan event**, each followed
by a confirmation. Enter dates as `YYYY-MM-DD HH:MM` in the configured Life timezone, or use an
explicit ISO UTC offset. A task due date is separate from its optional reminder.
`$think` and `$life` are owner-only and also work in the owner's DMs.

`$ask <question>` replies in the originating channel. `$ask --private <question>`
copies the question to DM before removing the guild command; failed deletion or
blocked DMs prevent inference. Asking directly in DM replies there. Chat is
available to current members of the configured server and the owner. New chat
commands are limited to two per user per 30 seconds; the gateway still permits
only one generation at a time. Controls recheck membership and belong only to
the requester, in the original destination. Each conversation has separate
temporary history. Public follow-up questions and answers are visible in the
channel; use a new private conversation for private follow-ups. `$help` works
in DMs; other household media commands still use the configured server.

`$torrent <movie|tv|music|game|app|other> <magnet>` accepts one BTIH magnet from the owner or a
Discord account explicitly linked to a Seerr media identity. Torrent intake is
available only in the configured Media Discord because MediaBot cannot delete
the user's DM source; if guild-message deletion fails, nothing is queued.

The required type is routing and safety metadata, not a free-form tag. Movie,
TV, and music aliases enter fixed scanner-managed paths. `game`/`games`,
`app`/`application`/`software`, and `other` enter fixed manual-review paths and
stay stopped for owner/admin review. Use **Review privately** on the intake
receipt or `$torrent review`, choose a request, inspect its manifest and scan
coverage, select files, then choose **Approve selected download**. Metadata
acquisition has a bounded temporary permit and displays progress. **Cancel
review** stops metadata acquisition; it does not delete payloads. **Recover
hold** is a separate owner-only action available only for a recorded,
validated approval-lifecycle hold; recovery never starts the download.

Completed manual payloads stop for scanning. Selected files over the gateway's
configured scan ceiling are skipped and reported as unscanned; archive coverage has
additional limits. Partial scans can permit seeding in manual quarantine,
which is separate from automatic import and does not establish safe execution.
A quarantine directory does not by itself enforce `noexec`; verify the gateway's mount policy independently. Approval and scan
receipts remain in the root-owned review service; the bot receives no qBit
credential. Refresh the private review to see download, scan, and seeding state.
Successful intake receipts remain; abandoned queue launchers expire after five
minutes. The music
route validates audio and seeds the result, but no current importer moves it
into the Navidrome library; `$music` through SoulSync remains the automatic
song-request path.

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
          +-----> SoulSync: music search and acquisition
          +-----> Markdown inbox: immutable owner thought captures
          `-----> Torrent intake: token-scoped gateway -> VPN quarantine
```

Seerr is the required video request broker. Jellyfin, Sonarr, and SoulSync are
optional integrations that enable their corresponding commands and richer
reconciliation. Trakt enrichment is optional and uses the Jellyfin Trakt
plugin for the single configured owner taste profile; MediaBot always keeps
its local 1-10 rating store authoritative.

## Configuration reference

The setup wizard writes `.env`. See [`.env.example`](../.env.example) for the
full configuration template. Never commit that file, a database, logs, runtime
health snapshots, recovery bundles or provider credentials. Do not `source`
`.env` as a shell script; Docker Compose reads its environment-file format.

| Integration | Settings and requirements |
| --- | --- |
| Discord and Seerr | `DISCORD_TOKEN`, `ALLOWED_GUILD_IDS`, `SEERR_URL`, `SEERR_API_KEY`, `SEERR_PUBLIC_URL`. Required for the basic service. |
| Jellyfin | `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `JELLYFIN_PUBLIC_URL`, optional `JELLYFIN_TASTE_USER`. Enables playable library, history and reports. |
| Sonarr | `SONARR_URL`, `SONARR_API_KEY`. Enables exact episode reconciliation and partial-season repair. |
| SoulSync | `SOULSYNC_URL`, `SOULSYNC_API_KEY`, `SOULSYNC_PUBLIC_URL`. Requires a compatible music request API. The historical patch script targets a specific upstream implementation; audit it before use. |
| Raw capture | `LIFE_CAPTURE_PATH`, normally `/app/data/life-inbox` in the portable volume. Blank disables capture. |
| Life gateway | `LIFE_GATEWAY_URL`, `LIFE_GATEWAY_TOKEN_PATH`, `NEXTCLOUD_PUBLIC_ORIGIN`. Requires its own service, owner allowlist, Nextcloud app credential, network and readable token mount. |
| Local AI gateway | `LOCAL_AI_URL`, `LOCAL_AI_TOKEN_PATH`. Requires its own bounded authenticated gateway and model service; these are not Ollama API settings. |
| Torrent gateway | `TORRENT_INTAKE_URL`, `TORRENT_INTAKE_TOKEN_PATH`. Requires the matching narrow gateway and its VPN/quarantine/review services. Do not substitute qBittorrent's admin URL. |

Blank optional provider URL/key pairs leave those integrations disabled. Merely
setting a gateway URL does not create its Docker network or mount its token.
The portable installer does not commission these advanced services. The Life,
AI and torrent tokens must remain separate and narrowly scoped; do not give
MediaBot Nextcloud administrator credentials or a Docker socket.

`TZ` sets the portable installation's timezone and defaults to `UTC`.
`EVENT_TIMEZONE` and `LIFE_TIMEZONE` can override it for new events and Life input.
Use IANA names such as `Europe/London`, not an ambiguous abbreviation. Existing
events retain their stored timezone. Enter explicit offsets when precision matters.

Operational settings include `DB_PATH`, `LOG_PATH`, `LOG_MAX_BYTES`,
`LOG_BACKUP_COUNT`, `RUNTIME_HEALTH_PATH`, `REQUEST_UI_TIMEOUT`,
`JELLYFIN_POLL_SECONDS` and `MEDIA_SUBMISSION_DRAIN_SECONDS`. Default writable
runtime files live beneath `/app/data`.

Event settings include `EVENT_RECONCILE_SECONDS` (minimum 30),
`EVENT_COMPLETION_GRACE_HOURS` (minimum 1), and `EVENT_REMINDER_MENTION`
(an optional role ID or `<@&ROLE_ID>`). Role mentions require exactly one
allowlisted guild because the role belongs to that guild. Multi-guild
installations must leave this blank. Arbitrary user mentions and `@everyone`
are rejected. The default is quiet reminders.

## Development and release verification

The bot runtime is tested on Python 3.13. A local development environment needs
its dependencies and exported configuration, including writable local paths for
`DB_PATH`, `LOG_PATH` and `RUNTIME_HEALTH_PATH`:

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
python -m compileall -q app.py mediabot scripts tests
python -m unittest discover -s tests -q
# Export the required environment variables before starting the bot.
python app.py
```

The setup CLI is a separate standard-library tool that works on Python 3.10+.
It supplies configuration to Docker Compose, not to a directly launched
`python app.py`. GitHub Actions runs dependency, compilation and regression
checks. Read the release workflow for the current complete gate list.

The retained `scripts/deploy_v*.sh` scripts document guarded transactions for
the original custom stack. They back up runtime/database state, perform
integration gates and roll back a failed candidate. Their paths, mounts,
external networks, secret permissions and staging contract require a matching
commissioned environment. They are not generic installers. Use
[the portable installation/update procedure](INSTALL.md) on a new host.

### Optional automatic Life task

Use `$think --auto I need to call the mechanic` to save the raw capture and ask
for one automatic task decision. An accepted task title must be quoted from the
thought. The result stays private. Ambiguous text, a note decision, invalid model
output, or unavailable services leaves the raw capture for `$life` review.
No dates or reminders are inferred, even when a thought mentions relative time.
Set those explicitly in Life or Nextcloud. A small local model can miss a real
task; it is not a guaranteed classifier. An unconfirmed write retains the same
request ID for the displayed retry. Plain `$think` still only captures.

Imperative fragments and unfamiliar abbreviations are accepted as task
language. Explicit leading uncertainty (`maybe`, `what if`, `I might`) and
example framing abstain before inference. The classifier can still make
mistakes; this is not an accuracy guarantee. Capture IDs and outcome codes
are logged for diagnosis without logging the thought or proposed task title.

`$recommend --auto --count 3` optionally lets the local model rank up to six
already eligible provider candidates. Trakt rank, community rating and
existing taste evidence are summarized within the same 3,000-byte context
limit. The model can return only a permutation of those candidate indices;
displayed titles, IDs and explanations remain provider-derived. Existing
watched/rated exclusions, genre constraints, movie/show balance and request
confirmation remain in force. Invalid output, unavailable inference or an
oversized prompt retain standard ranking. This option does not guarantee
better recommendations and cannot be combined with `--random`.

`$discover`/`$random` still browse already playable Jellyfin media and do not
accept `--auto`. Use `$recommend --auto` for the Trakt/taste-backed option.
The flag does not enable arbitrary tools, execute commands, or expose private
Life data to other users. Both server and DM `$help` describe the commands
available in that location; owner-only administrator subcommands stay hidden
from other administrators.

Chat cards retain the full question. Follow-ups and new topics create separate
messages rather than replacing the preceding exchange. Context is temporary,
limited to 12 messages and 3000 UTF-8 bytes, and expires after ten minutes.
Discord retains the messages. New topic clears model context only. There is no
persistent conversational memory or automatic channel reading.

Stable version tags publish GitHub releases automatically only after tag CI
passes. Release notes come from the matching changelog entry; a missing entry
fails publication. Companion services and live deployment are separately
verified and are not implied merely by creating a release page.
