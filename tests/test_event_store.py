import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mediabot.core.event_store import (
    EVENT_SCHEMA_VERSION,
    EventStore,
    EventStoreError,
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

    def test_v1_database_migrates_in_place_without_an_event_level_native_id(self):
        legacy_path = str(Path(self.temp_dir.name) / "legacy.db")
        legacy = EventStore(legacy_path)
        with legacy._transaction(immediate=True) as conn:
            conn.execute(
                """
                CREATE TABLE event_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT (
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
                )
                """
            )
            legacy._apply_v1(conn)
            conn.execute(
                "INSERT INTO event_schema_migrations (version, name) VALUES (1, 'v1')"
            )
        event = legacy.create_event(
            discord_guild_id=91,
            discord_channel_id=20,
            name="Legacy Night",
            created_by_discord_id=30,
            timezone_name="America/Denver",
            vote_limit=1,
        )
        nomination, _ = legacy.add_nomination(
            event_id=event["event_id"],
            media_type="movie",
            tmdb_id=348,
            title="Alien",
            year="1979",
            nominated_by_discord_id=1,
        )
        legacy.freeze_schedule(
            event_id=event["event_id"],
            assignments=(("2026-10-16T01:00:00.000000Z", nomination["nomination_id"]),),
        )

        legacy.init_schema()
        legacy.init_schema()

        conn = legacy._connect()
        try:
            event_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(events)")
            }
            slot_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(event_slots)")
            }
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("archived_at", event_columns)
            self.assertNotIn("native_scheduled_event_id", event_columns)
            self.assertIn("native_scheduled_event_id", slot_columns)
            self.assertTrue(
                {
                    "event_time_options",
                    "event_time_votes",
                    "event_reminder_state",
                }.issubset(tables)
            )
            self.assertEqual(
                conn.execute("SELECT name FROM events").fetchone()["name"],
                "Legacy Night",
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM event_slots").fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_v2_migration_rejects_a_same_named_table_without_foreign_keys(self):
        malformed_path = str(Path(self.temp_dir.name) / "malformed-v2.db")
        malformed = EventStore(malformed_path)
        with malformed._transaction(immediate=True) as conn:
            conn.execute(
                """
                CREATE TABLE event_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT (
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
                )
                """
            )
            malformed._apply_v1(conn)
            conn.execute(
                "INSERT INTO event_schema_migrations (version, name) VALUES (1, 'v1')"
            )
            conn.execute(
                """
                CREATE TABLE event_reminder_state (
                    slot_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    advanced_at TEXT NOT NULL DEFAULT (
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    ),
                    PRIMARY KEY (slot_id, stage)
                )
                """
            )
            conn.execute(
                "INSERT INTO event_schema_migrations (version, name) "
                "VALUES (2, 'partial v2')"
            )

        with self.assertRaisesRegex(EventStoreError, "malformed foreign keys"):
            malformed.init_schema()
        with self.assertRaisesRegex(EventStoreError, "malformed foreign keys"):
            malformed.init_schema()

    def test_v2_migration_rejects_a_same_named_index_with_wrong_columns(self):
        malformed_path = str(Path(self.temp_dir.name) / "malformed-index.db")
        malformed = EventStore(malformed_path)
        malformed.init_schema()
        with malformed._transaction(immediate=True) as conn:
            conn.execute("DROP INDEX idx_event_reminder_state_event")
            conn.execute(
                "CREATE INDEX idx_event_reminder_state_event "
                "ON event_reminder_state (slot_id, event_id, stage)"
            )

        with self.assertRaisesRegex(EventStoreError, "malformed index"):
            malformed.init_schema()

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

    def test_time_ballots_replace_atomically_under_competing_writers(self):
        event = self.create_event(guild_id=92)
        options = self.store.add_time_options(
            event_id=event["event_id"],
            starts_at_utc=(
                "2026-10-16T01:00:00.000000Z",
                "2026-10-17T01:00:00.000000Z",
                "2026-10-18T01:00:00.000000Z",
            ),
            created_by_discord_id=30,
        )
        option_ids = tuple(int(row["time_option_id"]) for row in options)
        requested = ((option_ids[0], option_ids[1]), (option_ids[2],))

        def replace(choice):
            store = EventStore(self.db_path)
            return store.replace_time_votes(
                event_id=event["event_id"],
                discord_user_id=501,
                time_option_ids=choice,
            )[0]

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(replace, requested))

        final = self.store.user_time_vote_ids(
            event_id=event["event_id"],
            discord_user_id=501,
        )
        self.assertIn(final, requested)
        self.assertIn(results[-1], requested)
        self.assertNotEqual(set(final), set(option_ids))


if __name__ == "__main__":
    unittest.main()
