# Changelog

All notable changes to Dogginator MediaBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses semantic version numbers.

## [Unreleased]

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
