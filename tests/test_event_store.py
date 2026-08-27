import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mediabot.core.event_store import (
    EVENT_SCHEMA_VERSION,
    EventStore,
    OpenEventExistsError,
)


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "mediabot.db")
        self.store = EventStore(self.db_path)
        self.store.init_schema()

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_event(self, *, guild_id=1, name="Friday Movie Night"):
        return self.store.create_event(
            discord_guild_id=guild_id,
            discord_channel_id=20,
            name=name,
            created_by_discord_id=30,
            timezone_name="America/Denver",
            vote_limit=1,
        )

    def test_schema_migration_is_idempotent_and_foreign_keys_are_enforced(self):
        self.store.init_schema()
        self.assertEqual(self.store.schema_version(), EVENT_SCHEMA_VERSION)

        conn = self.store._connect()
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO event_votes (
                        event_id, nomination_id, discord_user_id
                    ) VALUES (999, 999, 1)
                    """
                )
        finally:
            conn.close()

    def test_one_open_event_per_guild_is_safe_under_concurrency(self):
        def create(name):
            store = EventStore(self.db_path)
            try:
                row = store.create_event(
                    discord_guild_id=55,
                    discord_channel_id=20,
                    name=name,
                    created_by_discord_id=30,
                    timezone_name="America/Denver",
                    vote_limit=1,
                )
                return ("created", row["event_id"])
            except OpenEventExistsError as exc:
                return ("existing", exc.existing_event_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, ("One", "Two")))

        self.assertEqual([kind for kind, _ in results].count("created"), 1)
        self.assertEqual([kind for kind, _ in results].count("existing"), 1)
        self.assertEqual(results[0][1], results[1][1])

    def test_nomination_uniqueness_returns_the_original_row(self):
        event = self.create_event()
        first, created = self.store.add_nomination(
            event_id=event["event_id"],
            media_type="movie",
            tmdb_id=348,
            title="Alien",
            year="1979",
            nominated_by_discord_id=1,
        )
        duplicate, duplicate_created = self.store.add_nomination(
            event_id=event["event_id"],
            media_type="movie",
            tmdb_id=348,
            title="Alien changed upstream",
            year="1979",
            nominated_by_discord_id=2,
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate["nomination_id"], first["nomination_id"])
        self.assertEqual(duplicate["title"], "Alien")
        self.assertEqual(duplicate["nominated_by_discord_id"], 1)

    def test_cancelling_an_open_event_releases_the_guild(self):
        first = self.create_event(guild_id=9)
        self.store.cancel_event(first["event_id"])
        second = self.create_event(guild_id=9, name="Next Event")

        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["status"], "open")


if __name__ == "__main__":
    unittest.main()
