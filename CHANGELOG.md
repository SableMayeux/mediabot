# Changelog

All notable changes to Dogginator MediaBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses semantic version numbers.

## [Unreleased]

## [2.7.1] - 2026-09-11

### Added

- Run administrator commands directly in the bot's DMs using current
  administrator membership in an allowed Discord server. When several servers
  are eligible, `$admin server <server ID>` chooses the target context.
  Owner-only account and diagnostic commands retain their owner requirement.

### Fixed

- Recognize Seerr's `jellyfinUsername` when displaying and linking imported
  Jellyfin accounts, alongside existing usernames and numeric Seerr IDs.
- Follow Seerr user pagination and split private user-list output into complete
  pages, so users beyond the first API page or Discord message length limit
  remain discoverable for linking.
- Explain that a Jellyfin account must first exist in Seerr through its import
  flow or the member's first login. `$admin link` maps that existing account;
  it does not silently import users or create accounts.
- Update server/DM help and onboarding for private administration while keeping
  ordinary media commands in the server and retaining existing permissions.
- Keep admin server listings scoped to their originating channel's server.
  A revoked DM server selection must be changed explicitly before continuing.
- Bind report controls to the displayed report and reject concurrent or stale
  actions, preventing a repeated click from affecting the next report.
- Add a transactional homelab deployer that selects the preserved homelab
  Compose file explicitly, validates existing storage and gateway boundaries,
  and retains the portable source tree for release validation and rollback.

## [2.7.0] - 2026-09-11

### Added

- A standalone installation wizard with hidden credential prompts, private
  configuration files, validated provider URLs, container-based authentication
  checks, startup health checks and instance-specific status. It uses Python's
  standard library on the host and asks for API credentials, not server passwords.
- Portable Docker Compose with project-scoped persistent storage and a restricted
  initializer for empty volumes. The bot runs as UID/GID 1000 with a read-only
  root filesystem, without external networks, host bind mounts or gateway secrets.
- Installation, account-linking, first-request, update, backup/restore and
  troubleshooting guides, with an explicit command-access and optional-feature
  matrix. Independent operators use separate bot applications and installations.
- GPL-3.0-only licensing, including the license text in the source and image.
- A clean-install CI gate that builds the shipping image with synthetic
  credentials, initializes the real application twice and verifies retained
  SQLite data, authenticated fixture transport and credential serialization.
  Discord login is intercepted; production health must reject that offline state.
  Tagged releases wait for both unit tests and this gate.

### Changed

- The default install requires Discord and Seerr. Jellyfin, Sonarr, SoulSync,
  AI, Life workflow and torrent gateways are optional. Gateway servers remain
  separately provisioned components, not bundled services or multi-user Life.
- New installations use `TZ=UTC`, with explicit event/Life timezone overrides.
  Empty browser URLs fall back to the provider's configured API URL; an absent
  Nextcloud origin no longer exposes a private homelab address.

### Fixed

- Normalize application source permissions inside the image so a restrictive
  host umask cannot prevent the unprivileged bot from importing its own code.
- Refuse to overwrite existing configuration or replace an existing deployment's
  different data mount. Rebuild both the bot and volume initializer on updates.
- Create the configured raw-capture inbox during startup so integration status
  does not fail merely because the owner has not captured a thought yet.

### Migration

- **Existing customized deployments:** the root `compose.yaml` now targets new
  standalone installations. The previous homelab layout is preserved separately
  in `deploy/compose.homelab.yaml`, with its explicit original timezone settings.
  Follow `docs/INSTALL.md#existing-customized-deployments`; do not overlay the
  two files or change project names to bypass the installer's data-mount guard.
  Historical `deploy_v*.sh` scripts remain version-specific homelab tools.
- Back up the configuration and database before an upgrade. No existing media,
  data volumes, VPN configuration or gateway storage is migrated by this release.

## [2.6.0] - 2026-09-11

### Added

- Opt-in `$recommend --auto` ranks a bounded shortlist of existing provider
  candidates using summarized Trakt, rating and taste evidence. Model output
  must be an exact permutation of candidate indices. Titles, IDs and displayed
  explanations remain provider-derived, and unavailable or invalid inference
  retains standard ranking. Existing exclusions and request confirmation remain.
- Capture outcome logging records IDs and result codes without private thought
  text, so note classifications can be distinguished from unavailable services
  and unconfirmed task writes.

### Fixed

- Recognize imperative task fragments containing unfamiliar abbreviations in
  `$think --auto`. Explicit leading uncertainty and example framing abstain
  before inference; task titles still require an exact source quote and no
  dates or reminders are inferred.
- Show torrent selected size, full payload size, download progress and location
  separately. Zero selected bytes no longer imply a completed download. Explain
  that manual jobs need `$torrent review` approval and remain at the displayed
  download path after completion, without automatic Jellyfin import.
- Align server and DM help with actual command access and current AI options.
  Hide owner-only administrator subcommands from other administrators. Media
  commands remain in the configured server; Life storage remains owner-only.
  Split complete command help across messages to stay within Discord limits.

## [2.5.0] - 2026-09-10

### Added

- Owner-only `$think --auto` saves the raw thought first, then asks the local
  model whether it clearly describes a task. Only a title quoted from the
  thought is accepted. Notes, uncertainty, invalid output, and unavailable
  services leave the capture available for manual review. No dates, reminders,
  calendar events, or additional actions are inferred.
- Automatic task creation uses a stable request ID and the existing verified
  Nextcloud receipts. An unconfirmed submission offers a retry of that exact
  task and request, without rerunning the model.
- Publish GitHub release pages from recorded changelog entries after tag CI
  succeeds, so releases stay aligned with version tags.

### Fixed

- Preserve the full question in public and private local-chat cards. Follow-ups
  and new topics create new messages, keeping earlier questions and answers.
  Failed and cancelled generations retain their questions as well.
- Copy private guild questions to the requested DM before deleting their source.
  Blocked DM delivery leaves the original question intact. Failed deletion
  still prevents inference and never falls back to a public answer.

## [2.4.0] - 2026-09-10

### Changed

- Let current members of the configured server use `$ask`, replying in the
  originating channel by default. `$ask --private` retains fail-closed DM
  delivery. Each conversation's history and controls remain requester-bound;
  current membership is rechecked, and Life access remains owner-only.
- Make `$help` work in DMs with the appropriate DM command list. Advertise
  shared chat in normal server help and private Life utilities to the owner.

## [2.3.0] - 2026-09-10

### Added

- Owner-only `$life` views for captured thoughts and Nextcloud tasks, explicit
  task/event proposals, one-shot task reminders, and ETag-checked completion.
  Captures remain unchanged; action retries retain the same durable request ID.
- Owner-only `$ask` with private DM delivery, bounded conversation history,
  cancellation and a separate authenticated local model gateway. This release
  has no personal-note retrieval, web search or model-triggered actions.
- Separate gateway secrets and networks, with release checks that keep the bot
  outside the Nextcloud database and unauthenticated model networks.

## [2.2.0] - 2026-09-10

### Added

- Private owner/admin torrent review with file selection, scan limits, metadata
  progress, cancel/retry, and separate evidence-gated owner hold recovery.
- A fixed review client behind the existing intake boundary, keeping qBittorrent
  credentials and approval receipts out of MediaBot.

### Fixed

- Keep successful torrent intake receipts and remove the stale instruction that
  routine approval requires inspecting qBittorrent manually.

## [2.1.3] - 2026-09-10

### Fixed

- Updated the GitHub Actions release gate to validate the current transactional
  deployer. The v2.1.2 tag failed CI because the workflow still named the
  retired v2.1.1 script; production remained untouched.

## [2.1.2] - 2026-09-10

### Fixed

- Made the Compose-version regression test use an explicit read-only fixture in
  the isolated deployment sandbox. The v2.1.1 deployment stopped during its
  preflight tests before production was modified because the sandbox could not
  resolve the repository-relative Compose path.

## [2.1.1] - 2026-09-10

### Fixed

- Kept the Docker health probe's expected version in lockstep with the running
  application. The v2.1.0 deployment gate exposed the stale v2.0.0 probe and
  automatically restored the healthy previous release.

## [2.1.0] - 2026-09-10

### Added

- Added fixed torrent routes and aliases for music, games, applications, and
  other payloads. Games, applications, and other payloads stay stopped in
  manual-review quarantine and cannot feed Starr automation.
- Added an audio-aware scanner route for torrent music. SoulSync remains the
  automatic Navidrome import path.

### Changed

- Allowed Discord users with an administrator-verified Seerr media link to use
  the guarded `$torrent` intake command; the bot owner retains access.

### Security

- Kept torrent intake hidden from normal help, guild-only, and fail-closed on
  source deletion before linked-account or input validation.

## [2.0.0] - 2026-09-09

### Added

- Added an owner-only `$think`/`$capture` command that atomically preserves raw
  thoughts as private Markdown before any task classification.
- Added an owner-only `$torrent movie|tv <magnet>` command backed by a narrow
  token-authenticated gateway to the existing VPN and quarantine pipeline.
- Added private-capture and torrent-gateway status to the owner integration
  health report.

### Security

- Delete torrent command messages before owner validation or intake; a failed
  deletion fails closed and direct-message intake is rejected.
- Accept one unambiguous BTIH only, redact complete magnet URLs from logs, and
  keep qBittorrent credentials outside the MediaBot container.
- Refuse gateway redirects and prove the mounted intake token against the live
  gateway with a non-mutating authenticated health probe.
- Keep both owner utilities hidden from normal help and unavailable to other
  Discord users.

### Verified

- Added capture durability, magnet parsing, early deletion, deletion-failure,
  direct-message rejection, hidden-help, and log-redaction regression coverage.

## [1.1.0] - 2026-09-06

### Added

- Added a fresh availability reply that mentions the exact Discord requester
  after the requested movie, seasons, or repaired episodes arrive in Jellyfin.
- Added durable availability-notification delivery fields so a missing original
  card or temporary Discord failure remains retryable.

### Changed

- Restricted allowed mentions to the stored requester ID; titles cannot ping
  `@everyone`, roles, or unrelated users.
- Backfilled previously completed request rows during migration so upgrading
  does not reannounce the historical library.

### Verified

- Added database migration, pending-delivery, exact-mention, and failed-send
  regression coverage.

## [1.0.1] - 2026-09-06

### Fixed

- Treated Discord DNS, connection, TLS, and timeout failures as retryable
  event-reconciliation failures instead of emitting an unhandled worker
  traceback.
- Kept an unavailable Discord API listing from discarding local event state;
  the lifecycle worker retries reconciliation on its next bounded cycle.

### Verified

- Added a regression for the raw `aiohttp` connection error that discord.py can
  surface before an HTTP response exists.

## [1.0.0] - 2026-09-04

### Changed

- Promoted the unified request, discovery, recommendation, rating, status,
  report, and event command model to the first stable release contract.
- Declared the durable request/event stores and current provider boundaries
  stable for compatible 1.x migrations.
- Reserved 2.x for integrations beyond the current personal-media stack.
- Renamed the guarded deployment entry point for the 1.0 release line.

### Verified

- Re-ran the complete command, provider, storage, event-lifecycle, security,
  shutdown, deployment, and rollback regression suite before promotion.
- Kept the v0.10 event schema additive; this release requires no destructive
  database migration.

## [0.10.0] - 2026-09-04

### Added

- Added guild-scoped candidate-time voting with persistent availability,
  per-reader Discord timestamps, date/time selectors, and an exact local-time
  modal and command fallback.
- Added restart-safe event dashboard controls for title voting, time voting,
  proposing times, and scheduled-event management.
- Added native Discord Scheduled Event synchronization for every future media
  slot, with durable remote IDs and marker-based recovery after partial failure.
- Added at-most-once 24-hour, 1-hour, and start-time reminders with an optional
  configured role ping.
- Added event history plus administrator reschedule, reopen, archive, and clear
  operations.

### Changed

- Made a published event schedule mutable: administrators can move slots or
  reopen the existing ballot without recreating its nominations and votes.
- Advanced title votes and time availability through one event revision so a
  schedule preview cannot publish stale results.
- Extended the event reconciliation worker to retry native Discord publishing,
  deliver due reminders, complete schedules after a configurable grace period,
  and retire finished dashboard controls.
- Upgraded the additive event schema to version 2 while retaining the original
  event, nomination, vote, and slot records.

### Fixed

- Prevented an expired scheduled event from remaining the guild's current event
  indefinitely.
- Prevented restarts and late worker runs from sending duplicate or obsolete
  reminder stages.
- Kept cancelled, dismissed, timed-out, and successful event interactions on
  the same cleanup and durable-receipt rules as the rest of MediaBot.

## [0.9.1] - 2026-09-01

### Changed

- Added provider provenance, stable external IDs, and expected duration metadata
  to exact-track requests sent from MediaBot to SoulSync.

### Fixed

- Stopped MediaBot's synchronous SoulSync request path from publishing audio
  until the complete stream decodes and passes SoulSync's integrity checks.
- Quarantined failed incoming files and invalid existing destinations instead
  of marking preview-length or otherwise truncated audio as completed.
- Published validated replacements atomically and exposed validation and
  provenance details through SoulSync's request-status response.

## [0.9.0] - 2026-08-26

### Added

- Durable media-request intents that record provider submissions before and
  immediately after an external side effect, then reconcile accepted work into
  tracked request messages after restart.
- A versioned runtime-health heartbeat and Docker healthcheck covering Discord
  readiness, required workers, cleanup status, Jellyfin reconciliation, and
  unresolved accepted request intents.
- Graceful shutdown handling that drains active media submissions before
  closing providers and Discord.
- Tokenized pagination state for interactive search, music, rating, report,
  and event-vote flows so a delayed click cannot act on a newer page.
- Python 3.13 GitHub Actions CI with dependency, compilation, shell-syntax, and
  full unit-test gates.
- A guarded v0.9 deployment script with verified runtime and SQLite backups,
  health/security gates, atomic database restoration, and automatic rollback.

### Changed

- Restricted all commands to explicitly allowlisted Discord guilds, rejected
  direct messages, and automatically left untrusted guilds.
- Hardened the Compose runtime with a pinned Python base image and dependency
  set, non-root UID/GID, read-only root filesystem, dropped capabilities,
  `no-new-privileges`, bounded processes/memory, and log rotation.
- Hardened SQLite connections with foreign-key enforcement, a busy timeout,
  full synchronous durability, and explicit rollback behavior.
- Made five-minute transient-interface cleanup durable across process and host
  restarts while preserving successful rating, request, and event receipts.
- Unified request state under `$status` and kept `$random`, `$randomrequest`,
  `$rr`, and `$ratings` as compatibility aliases rather than separate concepts.
- Added bounded concurrency and caching to semantic TV genre resolution.

### Fixed

- Prevented concurrent accept, cancel, page, season, vote, and timeout actions
  from double-submitting or overwriting a terminal receipt.
- Scoped request-number and latest-request status lookups to the current guild.
- Ranked Jellyfin status results by exact title, year, and similarity instead
  of accepting the first provider result.
- Distinguished definitive SoulSync rejections from ambiguous network/server
  failures so safe retries do not create duplicate music requests.
- Prevented stale controls from extending an expired interaction lease or
  selecting an item from a different page.
- Preserved a completed local rating receipt when its optional follow-up
  request flow is cancelled or expires.
- Redacted configured tokens, API keys, authorization headers, and common token
  shapes from application logs and error responses.

## [0.8.0] - 2026-08-25

### Added

- Generic, guild-scoped media events with nominations, voting, scheduling,
  completion/cancellation, and a read-only tonight view.
- Versioned Spooktober presets on the shared event engine.
- Exact existing-series episode inventory and partial-season repair through
  Sonarr while retaining Seerr as the approval broker for new/empty seasons.
- Jellyfin playback-problem reporting with a guild-local administrator queue.
- Durable local 1-10 ratings and optional owner-profile Trakt enrichment.

### Changed

- Separated `$discover` (playable Jellyfin inventory) from `$recommend`
  (unseen requestable media).
- Added human-readable boolean genre expressions, semantic TV Romance matching,
  mixed movie/show batches, and rank-decaying taste signals.
- Consolidated music acquisition around exact-track `$music` requests and the
  shared `$status` command.

Versions before 0.8.0 were internal milestones and are not reconstructed here.
