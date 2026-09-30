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
- Denny's touch dashboard: `homelab-dashboard/dennys`

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
The private HTTPS proxy is live at `https://ha.2-msb.com:8444`, listening only
on the MediaServer LAN address. Its trusted certificate renews through a
Cloudflare token restricted to this zone's DNS controls. Native HA login and
authenticated WebSockets are required. The proxy also rejects connections
whose actual TCP peer is outside the LAN; forged forwarding headers do not
grant access. The active Cloudflare tunnel has no HA route. The hostname's
public DNS record resolves to a private LAN address. This is not a public
Home Assistant URL. The Nest Hub firmware has not changed.

Denny's has displayed the dashboard, confirmed on the physical screen. Its
separate touch view provides **Shuffle music**, **Quiet volume**, **Normal
volume** and the shopping list. Shuffle starts a fresh random queue of twenty
tracks from the existing music library. For a particular song or album, open
Music Assistant on a phone or computer, select Denny's, and choose the music.

The device runs one Cast application at a time. Playing music replaces the
dashboard with Music Assistant's player. The **Return Home when music stops**
automation waits for idle playback, then restores the touch view. It skips
active and paused music. The browser dashboard includes a **Show Home on
Denny's** button and a toggle for that automation. Showing Home manually
replaces the current Cast application.

You can restore the screen without opening Home Assistant: DM MediaBot
`$ha home`, or run `$home` and press **Show HA on Denny's**. Both controls
are owner-only and work in the trusted Discord server as well. They explicitly
stop Denny's music, close its current Cast application and launch a fresh HA
view. Swiping back to Google's home screen does not stop music, so the idle
automation may correctly leave playback alone. The bot reports that HA
accepted the request; check the physical screen to confirm it appeared.

The restore capability uses a separate local-only POST webhook. Its only
action is the fixed Denny's restore script. MediaBot does not receive an HA
administrator token or an arbitrary device/service control API. Other
deployments can leave `HOME_ASSISTANT_CAST_WEBHOOK` unset. To enable it,
create a local-only POST webhook automation for a fixed restore script, use
a random 48-96 character webhook ID, and set its private-IP URL in the bot's
environment. Never publish that URL or put it in a command argument.

Shuffle has started real playback from a physical touchscreen tap. Volume,
pause and resume have passed direct HA action checks. The Cast touch view
uses the compatible `call-service` button format. Physical volume taps remain
separate from the API checks. If a button appears but does nothing on the
Nest, test its actual action from the browser and check the Cast frontend's
support for that card and action format.

The AmpliFi Alien currently returns an empty DNS answer for the private HA
hostname, while Google and Cloudflare resolve it correctly. Denny's can reach
the HTTPS origin through public DNS. Continue using the LAN Home link above
for ordinary browsers while the router DNS issue remains unresolved.
AmpliFi's [DNS troubleshooting guide](https://help.amplifi.com/hc/en-us/articles/360015273534-Troubleshooting-DNS-Issues)
documents **Bypass DNS Cache** in its web interface. This affects router DNS
handling across the network; it is not a hostname-specific exception.

The microphone continues to use Google's voice service. Local Assist/voice
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

The separate **Manual private HA boundary check** workflow can probe an
operator's explicitly configured public WAN IP on ports 443, 8123 and 8444.
Put a JSON list of those public IPs in the temporary repository secret
`PRIVATE_HA_AUDIT_TARGETS`, run the workflow manually, inspect its result, then
delete the secret. Logs omit the target addresses. A passing result only
establishes that those ports were unreachable from that external runner at
that time; it is not an audit of every router rule or every WAN port.

## Deploy elsewhere

Provision Music Assistant using its [official installation guide](https://www.music-assistant.io/installation/),
with persistent `/data`, a read-only music source and LAN discovery support.
Create its first account in the browser. Add the Music Assistant integration
from HA's Devices & services page and finish authorization against the correct
HA instance. Set the optional home-link variables in MediaBot's `.env` and
recreate the bot. Music Assistant and Home Assistant are not installed by the
portable MediaBot installer.
