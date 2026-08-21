"""
SoulSync provider placeholder.

MediaBot's provider boundary is intentionally established before
music commands are implemented.

Future responsibilities:

- music search
- album/track requests
- music recommendations
- acquisition state
- recently acquired music

Video acquisition stays with:

    Seerr -> Radarr/Sonarr

Music acquisition stays with:

    SoulSync -> music acquisition sources
"""


class SoulSyncProvider:

    enabled = False

    async def start(self):
        return

    async def close(self):
        return
