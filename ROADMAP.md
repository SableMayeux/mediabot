# Dogginator MediaBot roadmap

The version number describes product boundaries, not a feature counter.

## 1.x: stable household media orchestration

Version 1.0 is the supported baseline for the current stack:

- one small Discord command surface for requests, music, discovery,
  recommendations, ratings, status, playback reports, and events;
- Seerr/Jellyfin/Sonarr/Radarr/SoulSync remain authoritative for the jobs they
  already own;
- restart-safe state, idempotent provider submissions, guarded deployment, and
  recoverable cleanup are compatibility requirements;
- ordinary interaction paths stay usable without memorizing provider-specific
  commands.

Compatible 1.x work should improve reliability, accessibility, ranking quality,
provider compatibility, observability, and administration without fragmenting
the command model again.

## 2.x: services beyond the media stack

Version 2.x begins when MediaBot deliberately coordinates systems outside its
current media-provider boundary. Candidate integrations include:

- Home Assistant intents and household automation;
- private local AI for conversational routing or richer media understanding;
- external outage/status services that live outside the homelab failure domain;
- photo/document search and other user-owned libraries.

These are not automatic 1.x dependencies. Each integration must be optional,
least-privileged, independently observable, and safe when either side is
offline. A general shell, secret broker, or unbounded home-control agent is not
part of the plan.

## Release rule

No milestone ships merely because the feature list is long. A release needs a
documented user path, migration/rollback behavior, automated regression
coverage, and a healthy deployed canary.
