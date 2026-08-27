# Changelog

All notable changes to Dogginator MediaBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses semantic version numbers.

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
