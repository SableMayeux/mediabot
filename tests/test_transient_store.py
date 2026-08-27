import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from mediabot.core import database
from mediabot.core.transient_store import TransientUIStore


class TransientUIStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "mediabot.db")
        self.store = TransientUIStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def register(
        self,
        entry_id="entry-1",
        *,
        batch_id=None,
        card_message_id=1001,
        command_message_id=501,
        expected_batch_size=1,
        expires_at=1300,
        now=1000,
    ):
        return self.store.register(
            entry_id=entry_id,
            batch_id=batch_id,
            expected_batch_size=expected_batch_size,
            kind="request_search",
            guild_id=11,
            channel_id=22,
            card_message_id=card_message_id,
            command_channel_id=22,
            command_message_id=command_message_id,
            expires_at=expires_at,
            now=now,
        )

    def test_initialization_is_idempotent_and_can_follow_shared_db_path(self):
        previous_path = database.DB_PATH
        database.DB_PATH = self.db_path
        try:
            default_store = TransientUIStore()
            default_store.initialize()
            row = default_store.register(
                entry_id="shared",
                kind="music_search",
                channel_id=22,
                card_message_id=1999,
                command_message_id=999,
                expires_at=1300,
                now=1000,
            )
        finally:
            database.DB_PATH = previous_path

        self.assertEqual(row.entry_id, "shared")
        with sqlite3.connect(self.db_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM transient_ui_entries"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_register_and_reset_extend_only_an_active_entry(self):
        record = self.register()
        self.assertEqual(record.state, "active")
        self.assertEqual(record.expires_at, 1300)

        self.assertTrue(self.store.reset("entry-1", expires_at=1600, now=1010))
        self.assertEqual(self.store.get("entry-1").expires_at, 1600)

        self.assertTrue(self.store.mark_terminal("entry-1", "accepted", now=1020))
        self.assertFalse(self.store.reset("entry-1", expires_at=1900, now=1030))
        with self.assertRaisesRegex(ValueError, "terminal transient entry"):
            self.register(expires_at=1900, now=1030)

    def test_register_rejects_inconsistent_batch_metadata(self):
        self.register(
            "one",
            batch_id="batch",
            card_message_id=1001,
            expected_batch_size=2,
        )
        with self.assertRaisesRegex(ValueError, "metadata or expected size"):
            self.register(
                "two",
                batch_id="batch",
                card_message_id=1002,
                command_message_id=502,
                expected_batch_size=2,
            )

    def test_same_card_can_enter_a_new_stage_only_after_old_stage_is_terminal(self):
        self.register("rating-search", card_message_id=1001)
        with self.assertRaisesRegex(ValueError, "already has an active"):
            self.register(
                "rating-request",
                batch_id="rating-request",
                card_message_id=1001,
            )

        self.store.mark_terminal("rating-search", "accepted", now=1001)
        request_stage = self.register(
            "rating-request",
            batch_id="rating-request",
            card_message_id=1001,
            expires_at=1400,
            now=1002,
        )
        self.assertEqual(request_stage.state, "active")
        self.assertEqual(self.store.get("rating-search").state, "accepted")
        self.assertEqual(
            self.store.get_for_card(channel_id=22, card_message_id=1001).entry_id,
            "rating-request",
        )
        self.assertTrue(
            self.store.reset_card(
                channel_id=22,
                card_message_id=1001,
                expires_at=1700,
                now=1003,
            )
        )
        self.assertEqual(self.store.get("rating-request").expires_at, 1700)

    def test_card_lookup_can_distinguish_active_from_terminal_history(self):
        self.register("completed", card_message_id=1001)
        self.store.mark_terminal("completed", "kept", now=1001)
        self.assertIsNone(
            self.store.get_for_card(
                channel_id=22,
                card_message_id=1001,
                active_only=True,
            )
        )
        self.assertEqual(
            self.store.get_for_card(channel_id=22, card_message_id=1001).state,
            "kept",
        )
        self.assertFalse(
            self.store.reset_card(
                channel_id=22,
                card_message_id=1001,
                expires_at=1800,
                now=1002,
            )
        )

    def test_expiry_claim_exclusively_blocks_stage_transition(self):
        self.register("rating", card_message_id=1001, expires_at=1100)
        self.store.claim_expired("expiry-worker", now=1200)

        self.assertFalse(
            self.store.transition_card(
                channel_id=22,
                card_message_id=1001,
                kind="rating_saved_actions",
                expires_at=1500,
                now=1201,
            )
        )
        record = self.store.get("rating")
        self.assertEqual(record.kind, "request_search")
        self.assertEqual(record.expires_at, 1100)
        self.assertEqual(record.claim_token, "expiry-worker")
        reclaimed = self.store.claim_expired("other", now=1499)
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0].claim_token, "other")

    def test_list_and_claim_expired_are_ordered_and_lease_aware(self):
        self.register("later", card_message_id=1002, expires_at=1200)
        self.register("earlier", card_message_id=1001, expires_at=1100)
        self.register("future", card_message_id=1003, expires_at=1400)

        listed = self.store.list_expired(now=1300)
        self.assertEqual([row.entry_id for row in listed], ["earlier", "later"])

        first_claim = self.store.claim_expired("worker-a", now=1300, limit=1)
        self.assertEqual([row.entry_id for row in first_claim], ["earlier"])
        self.assertEqual(first_claim[0].claim_token, "worker-a")
        second_claim = self.store.claim_expired("worker-b", now=1300)
        self.assertEqual([row.entry_id for row in second_claim], ["later"])

        # A dead worker's lease can be reclaimed, but not before it expires.
        self.assertEqual(self.store.claim_expired("worker-c", now=1359), [])
        reclaimed = self.store.claim_expired("worker-c", now=1361, limit=1)
        self.assertEqual([row.entry_id for row in reclaimed], ["earlier"])

    def test_release_claim_can_schedule_a_safe_retry(self):
        self.register(expires_at=1100)
        self.store.claim_expired("worker-a", now=1200)
        self.assertFalse(
            self.store.release_claim(
                "entry-1", "wrong-worker", retry_at=1500, now=1201
            )
        )
        self.assertTrue(
            self.store.release_claim(
                "entry-1", "worker-a", retry_at=1500, now=1201
            )
        )
        self.assertEqual(self.store.list_expired(now=1499), [])
        self.assertEqual(
            [row.entry_id for row in self.store.list_expired(now=1500)],
            ["entry-1"],
        )

    def test_user_acceptance_wins_over_an_expiry_claim(self):
        self.register(expires_at=1100)
        self.store.claim_expired("expiry-worker", now=1200)

        self.assertTrue(self.store.mark_terminal("entry-1", "accepted", now=1201))
        self.assertFalse(
            self.store.mark_terminal(
                "entry-1",
                "expired",
                claim_token="expiry-worker",
                now=1202,
            )
        )
        self.assertEqual(self.store.get("entry-1").state, "accepted")
        self.assertIsNone(
            self.store.claim_batch_command_deletion(
                "entry-1", "command-worker", now=1203
            )
        )

    def test_expiry_worker_must_own_the_entry_claim(self):
        self.register(expires_at=1100)
        self.store.claim_expired("worker-a", now=1200)
        self.assertFalse(
            self.store.mark_terminal(
                "entry-1", "expired", claim_token="worker-b", now=1201
            )
        )
        self.assertTrue(
            self.store.mark_terminal(
                "entry-1", "expired", claim_token="worker-a", now=1202
            )
        )

    def test_batch_command_delete_is_claimed_only_after_every_card_is_disposable(self):
        self.register(
            "one",
            batch_id="batch",
            card_message_id=1001,
            expected_batch_size=3,
        )
        self.register(
            "two",
            batch_id="batch",
            card_message_id=1002,
            expected_batch_size=3,
        )
        self.register(
            "three",
            batch_id="batch",
            card_message_id=1003,
            expected_batch_size=3,
        )

        self.store.mark_terminal("one", "dismissed", now=1100)
        self.store.mark_terminal("two", "expired", now=1101)
        self.assertIsNone(
            self.store.claim_batch_command_deletion("batch", "worker-a", now=1102)
        )
        self.store.mark_terminal("three", "dismissed", now=1103)

        claim = self.store.claim_batch_command_deletion(
            "batch", "worker-a", now=1104
        )
        self.assertEqual(claim.command_message_id, 501)
        self.assertEqual(claim.channel_id, 22)
        self.assertIsNone(
            self.store.claim_batch_command_deletion("batch", "worker-b", now=1105)
        )
        self.assertFalse(
            self.store.mark_batch_command_deleted("batch", "worker-b", now=1106)
        )
        self.assertTrue(
            self.store.mark_batch_command_deleted("batch", "worker-a", now=1107)
        )
        self.assertIsNone(
            self.store.claim_batch_command_deletion("batch", "worker-c", now=1200)
        )

    def test_expected_count_prevents_partial_batch_from_deleting_command(self):
        self.register(
            "one",
            batch_id="batch",
            expected_batch_size=2,
        )
        self.store.mark_terminal("one", "expired", now=1100)
        self.assertIsNone(
            self.store.claim_batch_command_deletion("batch", "worker", now=1101)
        )

    def test_kept_or_accepted_card_permanently_preserves_batch_command(self):
        for preserved_state in ("kept", "accepted"):
            with self.subTest(state=preserved_state):
                path = str(Path(self.temp_dir.name) / f"{preserved_state}.db")
                store = TransientUIStore(path)
                for index in range(2):
                    store.register(
                        entry_id=f"{preserved_state}-{index}",
                        batch_id=preserved_state,
                        expected_batch_size=2,
                        kind="recommendation",
                        channel_id=22,
                        card_message_id=2000 + index,
                        command_message_id=700,
                        expires_at=1300,
                        now=1000,
                    )
                store.mark_terminal(
                    f"{preserved_state}-0", preserved_state, now=1100
                )
                store.mark_terminal(
                    f"{preserved_state}-1", "expired", now=1101
                )
                self.assertIsNone(
                    store.claim_batch_command_deletion(
                        preserved_state, "worker", now=1102
                    )
                )

    def test_remaining_batch_cards_can_register_after_an_early_acceptance(self):
        self.register(
            "one",
            batch_id="batch",
            card_message_id=1001,
            expected_batch_size=2,
        )
        self.store.mark_terminal("one", "accepted", now=1001)
        second = self.register(
            "two",
            batch_id="batch",
            card_message_id=1002,
            expected_batch_size=2,
            now=1002,
        )
        self.assertEqual(second.state, "active")
        self.store.mark_terminal("two", "expired", now=1003)
        self.assertIsNone(
            self.store.claim_batch_command_deletion("batch", "worker", now=1004)
        )

    def test_command_claim_can_be_released_or_reclaimed_after_a_dead_worker(self):
        self.register()
        self.store.mark_terminal("entry-1", "expired", now=1100)
        self.assertIsNotNone(
            self.store.claim_batch_command_deletion(
                "entry-1", "worker-a", now=1101
            )
        )
        self.assertTrue(
            self.store.release_batch_command_claim(
                "entry-1", "worker-a", now=1102
            )
        )
        self.assertIsNotNone(
            self.store.claim_batch_command_deletion(
                "entry-1", "worker-b", now=1103
            )
        )
        self.assertIsNone(
            self.store.claim_batch_command_deletion(
                "entry-1", "worker-c", now=1162
            )
        )
        reclaimed = self.store.claim_batch_command_deletion(
            "entry-1", "worker-c", now=1164
        )
        self.assertEqual(reclaimed.claim_token, "worker-c")

    def test_pending_command_scan_retries_terminal_batches_and_stale_claims(self):
        self.register("released", batch_id="released", card_message_id=1001)
        self.store.mark_terminal("released", "expired", now=1100)
        first = self.store.claim_batch_command_deletion(
            "released", "worker-a", now=1101
        )
        self.assertIsNotNone(first)
        self.store.release_batch_command_claim("released", "worker-a", now=1102)

        self.register("stale", batch_id="stale", card_message_id=1002)
        self.store.mark_terminal("stale", "dismissed", now=1100)
        self.assertIsNotNone(
            self.store.claim_batch_command_deletion(
                "stale", "dead-worker", now=1101
            )
        )

        claims = self.store.claim_deletable_batch_commands(
            "sweeper", now=1200, lease_seconds=60
        )
        self.assertEqual(
            {claim.batch_id for claim in claims},
            {"released", "stale"},
        )
        self.assertTrue(
            all(claim.claim_token == "sweeper" for claim in claims)
        )

    def test_pending_command_scan_never_claims_incomplete_or_preserved_batch(self):
        self.register(
            "partial",
            batch_id="partial",
            expected_batch_size=2,
            card_message_id=1001,
        )
        self.store.mark_terminal("partial", "expired", now=1100)

        self.register("kept", batch_id="kept", card_message_id=1002)
        self.store.mark_terminal("kept", "kept", now=1100)

        self.assertEqual(
            self.store.claim_deletable_batch_commands("sweeper", now=1200),
            [],
        )

    def test_missing_command_is_finalized_without_returning_a_delete_target(self):
        self.register(command_message_id=None)
        self.store.mark_terminal("entry-1", "expired", now=1100)
        self.assertIsNone(
            self.store.claim_batch_command_deletion(
                "entry-1", "worker", now=1101
            )
        )
        self.assertEqual(self.store.purge_terminal(before=1200), 1)

    def test_retention_purges_only_fully_terminal_finalized_batches(self):
        self.register("active", card_message_id=1001, now=1000)

        self.register("pending", card_message_id=1002, now=1000)
        self.store.mark_terminal("pending", "expired", now=1010)

        self.register("preserved", card_message_id=1003, now=1000)
        self.store.mark_terminal("preserved", "accepted", now=1010)

        self.register("deleted", card_message_id=1004, now=1000)
        self.store.mark_terminal("deleted", "expired", now=1010)
        self.store.claim_batch_command_deletion("deleted", "worker", now=1011)
        self.store.mark_batch_command_deleted("deleted", "worker", now=1012)

        self.assertEqual(self.store.purge_terminal(before=1100), 2)
        self.assertIsNotNone(self.store.get("active"))
        self.assertIsNotNone(self.store.get("pending"))
        self.assertIsNone(self.store.get("preserved"))
        self.assertIsNone(self.store.get("deleted"))

    def test_two_store_instances_cannot_claim_the_same_expired_row(self):
        self.register(expires_at=1100)
        stores = [TransientUIStore(self.db_path), TransientUIStore(self.db_path)]
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def claim(store, token):
            try:
                barrier.wait(timeout=5)
                results.append(store.claim_expired(token, now=1200))
            except Exception as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = [
            threading.Thread(target=claim, args=(stores[0], "worker-a")),
            threading.Thread(target=claim, args=(stores[1], "worker-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertEqual(sorted(len(result) for result in results), [0, 1])

    def test_two_store_instances_cannot_claim_the_same_batch_command(self):
        self.register()
        self.store.mark_terminal("entry-1", "expired", now=1100)
        stores = [TransientUIStore(self.db_path), TransientUIStore(self.db_path)]
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def claim(store, token):
            try:
                barrier.wait(timeout=5)
                results.append(
                    store.claim_batch_command_deletion(
                        "entry-1", token, now=1200
                    )
                )
            except Exception as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = [
            threading.Thread(target=claim, args=(stores[0], "worker-a")),
            threading.Thread(target=claim, args=(stores[1], "worker-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertEqual(sum(result is not None for result in results), 1)


if __name__ == "__main__":
    unittest.main()
