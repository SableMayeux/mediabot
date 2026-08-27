import json
import tempfile
import unittest
from pathlib import Path

from mediabot.core import database


class RequestTrackingMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name) / "mediabot.db")

    def tearDown(self):
        database.DB_PATH = self.previous_path
        self.temp_dir.cleanup()

    def test_connection_enforces_shared_locking_and_integrity_policy(self):
        with database.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
            conn.execute("CREATE TABLE rollback_probe (value INTEGER)")

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with database.connection() as conn:
                conn.execute("INSERT INTO rollback_probe VALUES (1)")
                raise RuntimeError("rollback")

        with database.connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rollback_probe").fetchone()[0],
                0,
            )

    def test_tracks_requested_seasons_and_expected_episode_counts(self):
        database.init_tracking_db()
        database.track_request(
            seerr_request_id=42,
            media_type="tv",
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            requester_discord_id=1,
            discord_guild_id=2,
            discord_channel_id=3,
            discord_message_id=4,
            request_status="Approved",
            requested_seasons=[2, 1, 2],
            requested_episode_counts={1: 11, 2: 10},
            requested_episode_numbers={1: range(1, 12), 2: range(1, 11)},
        )

        row = dict(database.pending_requests()[0])

        self.assertEqual(row["requested_seasons"], "1,2")
        self.assertEqual(
            json.loads(row["requested_episode_counts"]),
            {"1": 11, "2": 10},
        )
        self.assertEqual(
            json.loads(row["requested_episode_numbers"]),
            {"1": list(range(1, 12)), "2": list(range(1, 11))},
        )

    def test_media_request_intent_is_durable_until_tracking_commits(self):
        database.init_tracking_db()
        database.begin_media_request_intent(
            intent_id="intent-1",
            media_type="tv",
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            requester_discord_id=1,
            discord_guild_id=2,
            discord_channel_id=3,
            discord_message_id=4,
            direct_tracking_id=-123,
            requested_seasons=[7],
            requested_episode_counts={7: 10},
            requested_episode_numbers={7: range(1, 11)},
        )
        self.assertEqual(database.media_request_intent_stats()["prepared"], 1)

        database.record_media_request_acceptance(
            intent_id="intent-1",
            seerr_request_id=272,
            accepted_seasons=[7],
            accepted_episode_counts={7: 10},
            accepted_episode_numbers={7: range(1, 11)},
            request_status="Approved",
        )
        recoverable = database.recoverable_media_request_intents()
        self.assertEqual([row["intent_id"] for row in recoverable], ["intent-1"])
        self.assertEqual(json.loads(recoverable[0]["accepted_seasons"]), [7])

        database.mark_media_request_intent_tracked("intent-1")
        self.assertEqual(database.recoverable_media_request_intents(), [])
        self.assertEqual(database.media_request_intent_stats()["tracked"], 1)

    def test_failed_prepared_intent_cannot_be_misreported_as_accepted(self):
        database.init_tracking_db()
        database.begin_media_request_intent(
            intent_id="intent-failed",
            media_type="movie",
            tmdb_id=348,
            title="Alien",
            year="1979",
            requester_discord_id=1,
            discord_guild_id=2,
            discord_channel_id=3,
            discord_message_id=4,
            direct_tracking_id=-124,
        )
        database.fail_media_request_intent(
            intent_id="intent-failed",
            error="provider rejected request",
        )
        with self.assertRaisesRegex(RuntimeError, "not recoverable"):
            database.record_media_request_acceptance(
                intent_id="intent-failed",
                seerr_request_id=273,
                request_status="Approved",
            )
        self.assertEqual(database.media_request_intent_stats()["failed"], 1)

    def test_music_request_duplicate_guard_and_lookup(self):
        database.init_tracking_db()
        database.begin_music_request(
            local_request_id="local-1",
            display_query="Chappell Roan - Pink Pony Club",
            requester_discord_id=1,
            discord_guild_id=2,
            discord_channel_id=3,
            discord_message_id=4,
        )

        duplicate = database.recent_music_request(
            "chappell roan - pink pony club"
        )
        self.assertEqual(duplicate["local_request_id"], "local-1")

        database.update_music_request(
            local_request_id="local-1",
            request_status="downloaded",
            soulsync_request_id="soul-1",
        )
        found = database.latest_music_request(
            "Pink Pony Club",
            requester_discord_id=1,
        )
        self.assertEqual(found["request_status"], "downloaded")

        database.begin_music_request(
            local_request_id="local-other-guild",
            display_query="Chappell Roan - Pink Pony Club",
            requester_discord_id=9,
            discord_guild_id=99,
            discord_channel_id=30,
            discord_message_id=40,
        )
        self.assertEqual(
            database.latest_music_request(
                "Pink Pony Club",
                discord_guild_id=2,
            )["local_request_id"],
            "local-1",
        )
        self.assertEqual(
            database.latest_music_request(
                "Pink Pony Club",
                discord_guild_id=99,
            )["local_request_id"],
            "local-other-guild",
        )
        self.assertIsNone(
            database.recent_music_request(
                "unrelated",
                discord_guild_id=2,
            )
        )

    def test_tracking_and_intent_stats_can_be_guild_scoped(self):
        database.init_tracking_db()
        for guild_id, request_id in ((2, 201), (99, 202)):
            database.track_request(
                seerr_request_id=request_id,
                media_type="movie",
                tmdb_id=request_id,
                title=f"Movie {request_id}",
                year="2026",
                requester_discord_id=1,
                discord_guild_id=guild_id,
                discord_channel_id=3,
                discord_message_id=4,
                request_status="Approved",
            )
            database.begin_media_request_intent(
                intent_id=f"intent-{guild_id}",
                media_type="movie",
                tmdb_id=request_id,
                title=f"Movie {request_id}",
                year="2026",
                requester_discord_id=1,
                discord_guild_id=guild_id,
                discord_channel_id=3,
                discord_message_id=4,
                direct_tracking_id=-request_id,
            )

        self.assertEqual(database.tracking_stats(discord_guild_id=2)["total"], 1)
        self.assertEqual(database.tracking_stats(discord_guild_id=99)["total"], 1)
        self.assertEqual(database.tracking_stats()["total"], 2)
        self.assertEqual(
            database.media_request_intent_stats(discord_guild_id=2)["prepared"],
            1,
        )

    def test_request_id_lookup_and_terminal_status_filter(self):
        database.init_tracking_db()
        database.track_request(
            seerr_request_id=272,
            media_type="tv",
            tmdb_id=1408,
            title="Criminal Minds",
            year="2005",
            requester_discord_id=1,
            discord_guild_id=2,
            discord_channel_id=3,
            discord_message_id=4,
            request_status="Approved",
            requested_seasons=[1],
        )

        self.assertEqual(database.request_by_id(272)["title"], "Criminal Minds")
        self.assertEqual(len(database.pending_requests()), 1)

        database.update_tracked_request_status(
            seerr_request_id=272,
            request_status="Declined",
        )
        self.assertEqual(database.pending_requests(), [])

    def create_report(self, **overrides):
        values = {
            "target_key": "episode:episode-27",
            "jellyfin_item_id": "episode-27",
            "jellyfin_series_id": "series-1",
            "media_type": "episode",
            "title": "Breaking Bad",
            "year": "2008",
            "season_number": 2,
            "episode_number": 7,
            "episode_title": "Negro y Azul",
            "category": "wont_play",
            "details": "",
            "reporter_discord_id": 10,
            "discord_guild_id": 20,
            "discord_channel_id": 30,
            "discord_message_id": 40,
        }
        values.update(overrides)
        return database.create_media_report(**values)

    def test_report_creation_is_idempotent_per_active_reporter_issue(self):
        database.init_tracking_db()
        first, first_created = self.create_report()
        duplicate, duplicate_created = self.create_report(discord_message_id=41)

        self.assertTrue(first_created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first["report_id"], duplicate["report_id"])
        self.assertEqual(database.media_report_stats(20)["active"], 1)

    def test_report_queue_and_transitions_are_guild_scoped(self):
        database.init_tracking_db()
        report, _ = self.create_report()

        self.assertEqual(
            [row["report_id"] for row in database.list_media_reports(
                discord_guild_id=20
            )],
            [report["report_id"]],
        )
        self.assertIsNone(
            database.media_report_by_id(
                report["report_id"],
                discord_guild_id=999,
            )
        )
        self.assertIsNone(
            database.update_media_report_status(
                report_id=report["report_id"],
                discord_guild_id=999,
                status="resolved",
                handler_discord_id=5,
            )
        )

        claimed = database.update_media_report_status(
            report_id=report["report_id"],
            discord_guild_id=20,
            status="in_progress",
            handler_discord_id=5,
        )
        self.assertEqual(claimed["status"], "in_progress")

        stolen = database.update_media_report_status(
            report_id=report["report_id"],
            discord_guild_id=20,
            status="in_progress",
            handler_discord_id=6,
        )
        self.assertIsNone(stolen)
        self.assertEqual(
            database.media_report_by_id(report["report_id"])["handler_discord_id"],
            5,
        )

        resolved = database.update_media_report_status(
            report_id=report["report_id"],
            discord_guild_id=20,
            status="resolved",
            handler_discord_id=5,
            resolution_note="Replaced the file",
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolution_note"], "Replaced the file")
        self.assertEqual(database.list_media_reports(discord_guild_id=20), [])
        self.assertEqual(database.media_report_stats(20)["resolved"], 1)

    def test_closed_report_allows_a_new_ticket_for_the_same_problem(self):
        database.init_tracking_db()
        first, _ = self.create_report()
        database.update_media_report_status(
            report_id=first["report_id"],
            discord_guild_id=20,
            status="dismissed",
            handler_discord_id=5,
        )
        second, created = self.create_report(discord_message_id=99)

        self.assertTrue(created)
        self.assertNotEqual(first["report_id"], second["report_id"])
