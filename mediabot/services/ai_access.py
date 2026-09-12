"""Persistent AI capability overrides, independent of backend availability."""
from __future__ import annotations

from mediabot.core import database

CAPABILITIES = ("server", "desktop", "web")
DEFAULTS = {"server": True, "desktop": False, "web": True}


def _identity(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        raise ValueError("Use a valid Discord user ID.")
    return value


class AIAccessService:
    def initialize(self):
        with database.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS ai_access (
                discord_user_id INTEGER NOT NULL,
                capability TEXT NOT NULL CHECK (capability IN ('server', 'desktop', 'web')),
                allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
                changed_by INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (discord_user_id, capability))""")

    def access(self, user_id, *, owner=False):
        identity = _identity(user_id)
        if owner:
            return {key: {"allowed": True, "source": "owner"} for key in CAPABILITIES}
        self.initialize()
        with database.connection() as conn:
            rows = conn.execute("SELECT capability, allowed FROM ai_access WHERE discord_user_id = ?",
                                (identity,)).fetchall()
        overrides = {row["capability"]: bool(row["allowed"]) for row in rows}
        return {key: {"allowed": overrides.get(key, DEFAULTS[key]),
                      "source": "override" if key in overrides else "default"} for key in CAPABILITIES}

    def set_access(self, user_id, capability, allowed, *, actor_id, owner=False):
        identity, actor = _identity(user_id), _identity(actor_id)
        if capability not in CAPABILITIES or allowed is not None and not isinstance(allowed, bool):
            raise ValueError("Choose server, desktop, or web and allow, deny, or reset.")
        if owner:
            raise ValueError("The bot owner always retains every AI capability; no override was changed.")
        self.initialize()
        with database.connection() as conn:
            if allowed is None:
                conn.execute("DELETE FROM ai_access WHERE discord_user_id = ? AND capability = ?",
                             (identity, capability))
            else:
                conn.execute("""INSERT INTO ai_access (discord_user_id, capability, allowed, changed_by)
                    VALUES (?, ?, ?, ?) ON CONFLICT(discord_user_id, capability)
                    DO UPDATE SET allowed=excluded.allowed, changed_by=excluded.changed_by,
                    updated_at=CURRENT_TIMESTAMP""", (identity, capability, int(allowed), actor))
        return self.access(identity)
