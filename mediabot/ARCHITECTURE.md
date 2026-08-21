# Dogginator MediaBot Architecture

## Rule

Discord command handlers must not become service implementations.

## Authority

### Seerr

Movie / TV:

- discovery
- search
- request creation
- request state
- metadata

### Jellyfin

Actual library:

- available media
- recently added media
- item IDs
- provider IDs
- playback links
- future watch state/history

### SoulSync

Music:

- music discovery
- acquisition
- recommendation workflows

### MediaBot

Orchestration:

- Discord UX
- account mappings
- persistent request cards
- cross-provider matching
- event engine
- diagnostics

## Current flow

Discord
   |
   | $request
   v
MediaBot
   |
   v
Seerr
   |
   v
Radarr / Sonarr
   |
   v
Downloader
   |
   v
Jellyfin
   |
   | availability poll
   v
MediaBot
   |
   v
Original Discord message becomes:
WATCH IN JELLYFIN

## Future

MediaBot
├── Video
│   ├── Seerr
│   └── Jellyfin
│
├── Music
│   └── SoulSync
│
├── Discovery
│   ├── Random
│   └── Recommendations
│
└── Events
    └── Spooktober
