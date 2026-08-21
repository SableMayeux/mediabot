# Dogginator MediaBot

## Rule

Commands do not directly own service-specific HTTP logic.

Discord Command
    |
    v
Service Layer
    |
    +-- Seerr Provider
    +-- Jellyfin Provider
    +-- SoulSync Provider
    +-- Future Providers

## Target Layout

mediabot/
├── bot.py
├── core/
│   ├── config.py
│   ├── database.py
│   └── logging.py
├── providers/
│   ├── base.py
│   ├── seerr.py
│   ├── jellyfin.py
│   └── soulsync.py
├── services/
│   ├── requests.py
│   ├── discovery.py
│   ├── recommendations.py
│   ├── library.py
│   ├── notifications.py
│   └── events.py
├── commands/
│   ├── help.py
│   ├── request.py
│   ├── status.py
│   ├── random.py
│   ├── discover.py
│   ├── recommend.py
│   ├── music.py
│   ├── report.py
│   ├── events.py
│   ├── spooktober.py
│   └── admin.py
└── ui/
    ├── media.py
    ├── search.py
    ├── pagination.py
    └── confirmation.py

## Authorities

Seerr:
- movie/TV search
- requests
- request status
- TMDB discovery metadata

Jellyfin:
- actual library contents
- watch state
- play history
- media item IDs
- Watch links
- recent additions

SoulSync:
- music discovery
- music acquisition
- music recommendations
- music automation

MediaBot:
- Discord UX
- orchestration
- account mappings
- event system
- user-facing state
- cross-provider reconciliation

MediaBot must NOT become Radarr, Sonarr, Seerr, Jellyfin or SoulSync.
