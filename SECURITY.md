# Security Policy

## Supported versions

Security fixes are applied to the current 2.x line. The stable 1.x media
contract remains migration-compatible, but earlier release lines are not
independently supported.

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

The owner-only torrent command must never connect to qBittorrent directly.
Keep its token-scoped intake gateway on an internal Docker network, reserve a
dedicated qBittorrent API key for that gateway, and preserve qBittorrent's VPN
interface binding plus quarantine scanner. Treat a magnet URL as sensitive:
MediaBot deletes the source message before processing and redacts magnets from
logs, but deletion is not retroactive disclosure recovery.

Private `$think` captures may contain personal data. Store the Markdown inbox
on trusted local storage with restrictive permissions, mirror it only into a
private notes account, and keep raw captures even if later task or calendar
classification fails.
