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

## 2.0: private owner foundation

Version 2.0 begins when MediaBot deliberately coordinates systems outside its
current media-provider boundary:

- raw thoughts land in an immutable Markdown inbox before anything interprets
  or promotes them;
- torrent intake for the owner and administrator-linked media accounts crosses
  a deliberately narrow boundary into the existing VPN/quarantine pipeline;
- household media commands and permissions remain compatible with 1.x.

## Later 2.x: broader integrations

The 2.x line provides explicit capture-to-task/event promotion, task completion,
one-shot task reminders and local text conversation. Version 2.6 includes an
opt-in, source-constrained task decision with `$think --auto` and optional
provider-candidate reranking with `$recommend --auto`. These commands still
require their separately commissioned gateways. Web-assisted conversation,
explicit GPU selection, model access controls and a cancellable inference
queue are available through the later 2.x releases. Document and image
retrieval remain separate work. Local conversation cannot execute Life
operations.

## Make the release usable on another server

Independent operators should be able to follow the repository without access
to the original homelab or its maintainer:

- the portable Compose installation starts with Discord and Seerr, with no
  mandatory Life, AI or torrent gateway, private host path or external network;
- the setup CLI gathers credentials locally, validates the configured services
  from a container and starts an installation with its own durable data volume;
- installation and member guides explain account linking, the first successful
  request, updates, backups, permissions and real failure states;
- advanced integrations remain explicit additions, with their deployment and
  credential boundaries documented instead of implied by a command list.

This supports independent copies for independent media stacks. Shared
multi-tenant hosting is not implemented. Multiple trusted guilds in one copy
share its provider configuration, account links and ratings.

Further work includes a complete deployable package for each advanced gateway,
tested upgrade/restore paths, and multi-user Life enrollment with isolated
storage and authorization. Home Assistant and voice-device setup remain
separate from installing the Discord bot.

## Delivered in 2.11 and 2.12

- `$home` provides an actor-bound personal DM screen with requests, discovery,
  ratings, reports and download progress. Each action checks current trusted
  server membership; owner tools remain owner-only.
- `$tonight` selects playable Jellyfin movies with known runtimes and optional
  group preferences. Genre/runtime filters and self-service group consent
  controls update the same card. It does not invent availability.
- Torrent review survives a bot restart, tracks approval and shows the actual
  download path. VPN binding, file selection and scan coverage remain explicit.
- The current homelab runs Music Assistant with a read-only NAS music library,
  HA playback controls and a private HTTPS Cast dashboard on Denny's.
- `$ha home` and the owner home-screen button explicitly stop Denny's music
  and restore the dashboard through a fixed local-only webhook.
- HA receives scoped owner notifications. The optional external watchdog
  checks public media/request health and reports failure/recovery transitions.
- Deployment verifies SQLite backups by restoring an isolated copy. This
  does not establish that a complete NAS disaster recovery was tested.

## Remaining integrations

Candidate integrations include:

- HA Companion phone enrollment and targeted push/action buttons;
- local Assist voice input and an explicit microphone/device setup;
- multi-user Life enrollment with isolated storage and authorization;
- complete portable packaging for the optional gateways;
- complete NAS disaster recovery and a physical-device restore drill;
- further local AI retrieval or richer media understanding;
- photo/document search and other user-owned libraries.

Explicit note-to-task promotion into the chosen CalDAV task store and
calendar/reminder actions remain available without LLM classification. Optional
classification must never become the only copy of a capture or create
commitments without a visible receipt.

These are not automatic 1.x dependencies. Each integration must be optional,
least-privileged, independently observable, and safe when either side is
offline. A general shell, secret broker, or unbounded home-control agent is not
part of the plan.

## Release rule

No milestone ships merely because the feature list is long. A release needs a
documented user path, migration/rollback behavior, automated regression
coverage, and a healthy deployed canary.
