# Security Policy

## Supported versions

Security fixes are applied to the current 0.9.x line. Earlier internal
milestones are not supported.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting or a private security advisory for
this repository when available. If neither is enabled, contact the repository
owner privately. Do not publish a proof of concept, token, provider URL,
database, or unredacted log in a public issue.

Include the affected version, configuration prerequisites, impact, and the
smallest reproduction that does not expose personal media data or credentials.
If a credential may have been disclosed, revoke and rotate it immediately;
repository history and log redaction are not credential revocation mechanisms.

## Deployment boundary

MediaBot is designed for a trusted household Discord guild and private media
infrastructure, not hostile multi-tenant hosting. Keep Discord guild IDs
allowlisted, protect `.env` as a secret, grant the bot only necessary Discord
permissions, and keep Seerr, Jellyfin, Sonarr, and SoulSync management APIs on a
trusted network or behind an authenticated reverse proxy.
