# Music and Home Assistant

MediaBot's `$home` is your private Discord front door. Home Assistant is the
device-control dashboard. Music Assistant provides music search, queues and
speaker playback. These remain separate accounts and permission systems.

## The current homelab

- Home: `http://10.0.0.55:8123/homelab-dashboard/start`
- Music: `http://10.0.0.53:8095`, your Music Assistant account
- Music library: existing NAS music mounted read-only at `/music`
- Denny's music controls: `media_player.dennys_2`, from Music Assistant
- Denny's existing Cast entity: `media_player.dennys`

Open **Play music**, select **Denny's**, then choose a track or album. Use the
queue to add more songs. The Home dashboard provides playback, volume and stop
controls, plus Jellyfin, Seerr, Proton Calendar and a shopping list. Music
Assistant manages its own queue; it does not move your music or change your
Navidrome playlists. Its library import discovers new music automatically.

Create separate Music Assistant accounts for other people in its user
management screen. Discord linking does not grant access to the Music
Assistant administrator account. Avoid sharing an administrator password.

**Proton Calendar remains your calendar.** The dashboard opens it directly.
Life/Nextcloud tasks remain available through their existing screens.

## Notifications

The bot can deliver owner download-review, security-hold and collection-ready
notifications to a local-only HA webhook. It holds that scoped webhook URL,
not an HA administrator token. The HA automation only creates notifications;
it cannot approve torrents, run model-generated actions or control devices.
Completion is distinguished from a full scan and partial scan coverage.

These messages appear in HA's **Notifications** drawer. Phone push and phone
action buttons require enrollment of the intended phone in the HA Companion
app and a selected notification target. A dashboard notification is not phone
push. The browser and music player work without that enrollment.

## Denny's display and voice

Music playback uses Google Cast and is separate from casting an HA dashboard.
The latter needs a reachable, trusted HTTPS HA URL. The prepared private TLS
proxy still needs a scoped DNS credential before it can be activated. The HA
dashboard has not been made public and the Nest Hub firmware has not changed.
Its microphone continues to use Google's voice service. Local Assist/voice
requires its own hardware or integration setup.

## External monitoring

The optional `homelab-watchdog.yml` workflow runs on GitHub's external runner
every fifteen minutes, subject to scheduler delays. Set repository variables
`HOMELAB_MONITOR_ENABLED=true`, `HOMELAB_MEDIA_HEALTH_URL` and
`HOMELAB_REQUESTS_HEALTH_URL` to the public HTTPS health endpoints. It validates
the response body, opens one GitHub issue on failure and closes that issue on
recovery. Subscribe to that issue or repository notifications for alerts.

The checks transmit no API keys, private LAN addresses or torrent names. A
failed probe establishes endpoint failure, not which WAN/tunnel/app layer
failed. This workflow does not measure streaming performance and is not an
instant outage detector.

## Deploy elsewhere

Provision Music Assistant using its [official installation guide](https://www.music-assistant.io/installation/),
with persistent `/data`, a read-only music source and LAN discovery support.
Create its first account in the browser. Add the Music Assistant integration
from HA's Devices & services page and finish authorization against the correct
HA instance. Set the optional home-link variables in MediaBot's `.env` and
recreate the bot. Music Assistant and Home Assistant are not installed by the
portable MediaBot installer.
