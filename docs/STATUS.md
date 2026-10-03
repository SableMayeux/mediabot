# Homelab status, October 2, 2026

This records the reference homelab, not a promise that the portable installer
provisions every optional service. MediaBot **v2.12.0** is deployed and
[released](https://github.com/SableMayeux/mediabot/releases/tag/v2.12.0).

## October 2 movie-pick release

`$tonight horror --time 90` now filters the actual Jellyfin movie library.
The result card has genre and maximum-runtime selectors, custom filters,
updates and watch links. Each member can join the group with **Include my
ratings** or revoke sharing with **Keep my ratings private**. These buttons
change only the clicker's preference; filter controls belong to the requester.
Several genres match any selected genre, and `--time` means maximum movie
length, not a calendar start time. The existing `--under` option still works.
See [the usage guide](USAGE.md#music-playback-problems-and-media-nights) for command examples.

704 bot tests passed in the staged Linux deployment. The Windows suite passed
with four platform/optional skips. Python 3.13 CI and
fresh Docker installation checks passed. The live command returned three
Horror movies of 77.75, 80.31 and 85.65 minutes, and Discord accepted its
result card with the new controls. The temporary validation preview was
removed and the owner's sharing preference was unchanged. Physical clicks
by a user were not part of that automated check.

The deployment passed its backup, runtime and release gates. A fresh
before/after comparison confirmed unchanged VPN/qBittorrent, other containers,
Tailscale routes and hardened HA proxy configuration. The live checks below
remain dated September 29; this release did not repeat WAN or disk checks.

## September 29 infrastructure baseline

The following records the earlier infrastructure inspection and its remaining
work. The movie-night row includes the October 2 command update.

### User paths

| Want | What works | Boundary or remaining work |
| --- | --- | --- |
| Personal Discord experience | DM `$home` for requests, watch picks, ratings, reports and downloads | Fresh trusted-server membership is checked for actions; open a fresh screen after a restart or expiration |
| Consistent help and administration | `$help` describes the available DM/server commands; authorized administrators can manage linking privately | Provider accounts and Discord identities remain separate |
| Movie night | `$tonight horror --time 90`, optional members, genre/runtime selectors and personal sharing buttons | Other participants opt in before their ratings are used; each button changes only its clicker's preference |
| Magnet approval and progress | Persistent `$torrent review`, explicit file selection, approval, stage and real path | Existing unapproved jobs still need their intended owner review; full download is separate from full scan coverage |
| Usable Home Assistant | Home dashboard, music controls, shopping list, media links and cost estimator | It is a LAN service with a separate HA login, not the Google firmware home screen |
| Denny's return to HA | Owner `$ha home`, or `$home` then **Show HA on Denny's** | Explicitly stops music and launches a fresh Cast dashboard; physical confirmation is separate from the receiver's acknowledgement |
| Music | Music Assistant is linked to HA and has the existing read-only NAS library; physical Shuffle touch started real playback | Music Assistant accounts are separate; physical volume taps still need confirmation |
| HA notifications | Scoped owner review/hold/ready notifications reach HA's Notifications drawer | Phone push and action buttons await HA Companion phone enrollment and a selected target |
| Local AI quality | Live gateway reports Huihui Qwen3 8B v2 Q4_K_M, quality on, server GPU ready, 16,384 generation tokens and a 25-minute deadline | Budget includes reasoning; factual accuracy is not guaranteed; resource interruptions remain explicit |
| Playback-friendly AI | `$admin ai quality off` uses Qwen3 4B on CPU; GPU guard also reserves resources for media | An interrupted answer is not automatically replayed |
| Web, queuing and access | `$ask --web`, explicit backend selection, queue/cancel/Finish sooner and independent user access controls | Models cannot execute arbitrary host or HA actions; source quality still limits answers |
| Optional desktop AI | Installed On/Off switch and explicit `$ask --desktop` path | Current work did not enable or retest the desktop GPU; it remains a separate runtime |
| Life and task capture | Immutable raw capture, explicit task/event promotion, source-constrained `$think --auto`, Nextcloud tasks/reminders | Life remains owner-only; multi-user isolated storage is not implemented |
| Nextcloud desktop | Earlier live receipt confirms installed client and successful existing-account sync | Off-LAN desktop synchronization was not verified in this pass; Proton Calendar remains in use |
| Independent installation | Portable Compose, setup CLI, installation/member guides, GPLv3 and tagged releases | Advanced gateways and HA/Music Assistant remain separately provisioned services |
| External watchdog | GitHub checks public media/request health every fifteen minutes and reports failure/recovery through an issue | Scheduler delays are possible; this does not measure streaming quality |
| SSH and disks | Fresh key logins to NAS and Proxmox pass; NAS array has both members; current NVMe health passes with zero media errors | NAS SMART baseline is September 10; RAID0 and the single Proxmox NVMe still have no disk redundancy |
| Recovery | Earlier encrypted critical-data restores passed; current bot SQLite backup restored and passed integrity/read/write checks | Complete NAS/VM disaster recovery, off-site coverage and a recurring backup policy are not established by these checks |
| Electricity accounting | HA has an editable watts/rate/subscription estimator | No actual power/energy meter is connected; estimates are not measurements |
| Voice and document feeding | Deferred deliberately | Denny's microphone still uses Google; local Assist and personal-corpus retrieval were not commissioned |

### HA security evidence

- `ha.2-msb.com` resolves through public DNS to a private LAN address. The
  HTTPS proxy binds only to the MediaServer LAN address on TCP 8444.
- Native HA login is required. Anonymous API access returned 401; an
  authenticated WebSocket succeeded with the trusted certificate.
- The proxy now checks the actual TCP peer. A disallowed test source received
  403, including with forged forwarding headers; an allowed source reached
  HA's authentication boundary.
- The active Cloudflare tunnel contains only media and requests routes,
  followed by a 404 catchall. There is no HA route.
- The HA host and MediaServer have no public IPv6 addresses in this inspection.
- An [external GitHub probe](https://github.com/SableMayeux/mediabot/actions/runs/36658266453)
  found the owner's WAN ports 443, 8123 and 8444 unreachable. The temporary
  target secret was deleted afterward. This is a bounded port check, not a
  complete audit of every router rule or WAN port.
- MediaBot's separate local-only Cast webhook has one fixed action. It does
  not carry an HA administrator token or accept arbitrary services/devices.

The AmpliFi Alien still filters the private HA DNS answer. Ordinary browsers
can use the LAN Home link while that resolver issue remains unresolved.

### September 29 validation

670 bot tests passed, with four platform/optional skips. Release CI passed
on Python 3.13, including the optional gateway checks and a fresh Docker
installation. Live deployment gates passed. A separate before/after comparison
verified that the bot deployment left VPN/qBittorrent, other containers,
Tailscale routes and the hardened HA proxy configuration unchanged.

The deployed restore command passed current Discord owner/membership checks;
its webhook triggered HA and Denny's receiver acknowledged **Home Assistant
Lovelace, Homelab: Denny's**. User confirmation of the latest physical display
is still pending. See [the Home guide](HOME.md) for the actual controls.
