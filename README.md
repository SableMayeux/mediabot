# Dogginator MediaBot

**Request media, find something to watch and organize a media night from Discord.**
MediaBot connects your server to your existing media stack, with search cards,
confirmation buttons, account-linked requests and progress receipts.

Current source version: **2.7.1**. Get stable source releases and change notes from
[Releases](https://github.com/SableMayeux/mediabot/releases).

```text
$request Interstellar 2014       Search, choose the exact title, confirm
$discover movie Comedy          Browse something already playable
$recommend --count 3            Find something new to request
$status #123                    Check a tracked request
$event                          Vote on the next media night
$help                           See what your account can use
```

## Run it on your server

The supported starting point is **Linux, Docker Engine with Compose 2.20+, and an
existing Seerr installation**. You create your own Discord bot application and
connect it to your own media services. No remote server passwords are required.

1. Follow [Install MediaBot](docs/INSTALL.md) to create/invite the bot and gather
   the Discord token, server ID and Seerr API settings.
2. Clone this repository and check out a published stable release.
3. Configure, validate connectivity and start:

```sh
python3 scripts/setup.py
python3 scripts/setup.py --check
python3 scripts/setup.py --start
```

The setup tool needs Python 3.10+ and uses no extra Python packages. It creates
your private `.env`, checks services from Docker and starts the bot with durable
storage. It refuses to overwrite an existing configuration. The container
supplies the bot's Python 3.13 runtime.

Once it is healthy, follow [Your first request](docs/USAGE.md#your-first-request)
to link a Discord member to Seerr and submit a confirmed request. Give members
[the user guide](docs/USAGE.md), not the installation checklist.

Administrators can use `$admin` in the server or directly in the bot's DMs.
Account linking still requires the bot owner, and the media account must exist
in Seerr first. See [private administration](docs/USAGE.md#private-administration).

## Choose the features you need

| Feature | Required integration | Included in the basic setup? |
| --- | --- | --- |
| Movie/TV search, requests, recommendations, ratings, status and media-night ballots | Discord + existing Seerr | Yes |
| Browse playable media, recent additions and playback reports | Existing Jellyfin | Optional wizard configuration |
| Exact episode inventory and repair of incomplete approved seasons | Existing Sonarr | Optional wizard configuration |
| Exact music-track requests and progress | Compatible SoulSync request API | Optional wizard configuration |
| Private raw `$think` captures | Bot owner's persistent inbox | Available in the bot data volume |
| `$life` task/calendar workflow | Separate Life gateway + Nextcloud | Advanced setup, owner-only |
| `$ask`, `$think --auto`, `$recommend --auto` | Separate authenticated local AI gateway/model | Advanced setup; no model downloaded by the installer |
| `$torrent` intake and manual approval | Separate VPN/quarantine/review gateway | Advanced setup; no qBittorrent or VPN installed here |
| Multi-user Life, Home Assistant and voice devices | Additional services and enrollment | Not included in this installer |

Seerr remains responsible for video requests and approval. Jellyfin owns
playback, Sonarr/Radarr own their acquisition workflows and SoulSync owns music
acquisition. Optional integrations can be added after the basic request path
works. A release number does not mean every companion service is installed.

Use **one independent installation per independently administered media stack**.
An installation can allow several trusted Discord servers, but they share its
providers, credentials, account mappings and ratings. Adding a guild ID does
not create an isolated tenant. Another server owner should deploy their own
copy with their own application and services.

## Documentation

- [Install, configure, update, back up and troubleshoot](docs/INSTALL.md)
- [Member and administrator walkthrough](docs/USAGE.md)
- [Full command and integration reference](docs/REFERENCE.md)
- [Provider boundaries and architecture](mediabot/ARCHITECTURE.md)
- [Roadmap](ROADMAP.md), [changelog](CHANGELOG.md) and [security policy](SECURITY.md)

The portable Compose configuration runs as an unprivileged user with a
read-only root filesystem and a persistent named volume. It does not require
the original operator's host paths, external networks or gateway credentials.
Existing custom deployments should read the
[migration note](docs/INSTALL.md#existing-customized-deployments) before changing
their Compose configuration.

Licensed under [GNU GPL version 3 only](LICENSE), `GPL-3.0-only`.
