import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mediabot.core.event_store import (
    EventStateError,
    EventStore,
    OpenEventExistsError,
    StaleEventRevisionError,
    VoteLimitExceededError,
)
from mediabot.events.presets.spooktober import build_spooktober_preset
from mediabot.services.events import (
    EventService,
    EventStatus,
    EventUsageError,
    ScheduleAssignment,
    local_day_utc_bounds,
    parse_schedule_input,
)


DENVER = ZoneInfo("America/Denver")


class EventServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "mediabot.db")
        self.service = EventService(EventStore(self.db_path))
        self.service.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_event(self, *, guild_id=1, vote_limit=1, name="Movie Night"):
        return self.service.create_event(
            discord_guild_id=guild_id,
            discord_channel_id=2,
            created_by_discord_id=3,
            name=name,
            vote_limit=vote_limit,
        )

    def nominate(
        self,
        event_id,
        tmdb_id,
        title,
        *,
        user_id=1,
        genres=("Horror",),
        media_type="movie",
    ):
        return self.service.nominate(
            event_id=event_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            year="1979",
            nominated_by_discord_id=user_id,
            genres=genres,
        ).nomination

    def test_create_enforces_one_open_event_per_guild_but_not_other_guilds(self):
        first = self.create_event(guild_id=10)
        with self.assertRaises(OpenEventExistsError):
            self.create_event(guild_id=10, name="Another")
        other = self.create_event(guild_id=11)

        self.assertEqual(first.status, EventStatus.OPEN)
        self.assertEqual(other.discord_guild_id, 11)

    def test_one_choice_vote_moves_and_toggles_without_duplicate_rows(self):
        event = self.create_event()
        alien = self.nominate(event.event_id, 348, "Alien")
        thing = self.nominate(event.event_id, 1091, "The Thing")

        selected = self.service.toggle_vote(
            event_id=event.event_id,
            nomination_id=alien.nomination_id,
            discord_user_id=99,
        )
        moved = self.service.toggle_vote(
            event_id=event.event_id,
            nomination_id=thing.nomination_id,
            discord_user_id=99,
        )
        removed = self.service.toggle_vote(
            event_id=event.event_id,
            nomination_id=thing.nomination_id,
            discord_user_id=99,
        )

        self.assertTrue(selected.selected)
        self.assertEqual(moved.selected_nomination_ids, (thing.nomination_id,))
        self.assertFalse(removed.selected)
        self.assertEqual(removed.selected_nomination_ids, ())

    def test_configurable_vote_limit_requires_an_explicit_deselection(self):
        event = self.create_event(vote_limit=2)
        nominees = [
            self.nominate(event.event_id, tmdb_id, title)
            for tmdb_id, title in ((1, "One"), (2, "Two"), (3, "Three"))
        ]
        for nominee in nominees[:2]:
            self.service.toggle_vote(
                event_id=event.event_id,
                nomination_id=nominee.nomination_id,
                discord_user_id=9,
            )

        with self.assertRaises(VoteLimitExceededError):
            self.service.toggle_vote(
                event_id=event.event_id,
                nomination_id=nominees[2].nomination_id,
                discord_user_id=9,
            )

        self.service.toggle_vote(
            event_id=event.event_id,
            nomination_id=nominees[0].nomination_id,
            discord_user_id=9,
        )
        final = self.service.toggle_vote(
            event_id=event.event_id,
            nomination_id=nominees[2].nomination_id,
            discord_user_id=9,
        )
        self.assertEqual(
            set(final.selected_nomination_ids),
            {nominees[1].nomination_id, nominees[2].nomination_id},
        )

    def test_schedule_freezes_atomically_and_rejects_a_stale_preview(self):
        event = self.create_event()
        alien = self.nominate(event.event_id, 348, "Alien")
        plan = self.service.build_ranked_schedule(
            event.event_id,
            starts_at=(datetime(2026, 10, 15, 19, 0, tzinfo=DENVER),),
        )
        self.service.toggle_vote(
            event_id=event.event_id,
            nomination_id=alien.nomination_id,
            discord_user_id=55,
        )

        with self.assertRaises(StaleEventRevisionError):
            self.service.schedule_ranked(plan)

        current_plan = self.service.build_ranked_schedule(
            event.event_id,
            starts_at=(datetime(2026, 10, 15, 19, 0, tzinfo=DENVER),),
        )
        scheduled, slots = self.service.schedule_ranked(current_plan)

        self.assertEqual(scheduled.status, EventStatus.SCHEDULED)
        self.assertEqual(slots[0].nomination_id, alien.nomination_id)
        with self.assertRaises(EventStateError):
            self.service.toggle_vote(
                event_id=event.event_id,
                nomination_id=alien.nomination_id,
                discord_user_id=77,
            )
        with self.assertRaises(EventStateError):
            self.service.nominate(
                event_id=event.event_id,
                media_type="movie",
                tmdb_id=2,
                title="Late Nomination",
                year="2026",
                nominated_by_discord_id=77,
            )

    def test_rankings_include_zero_vote_titles_and_expose_ties(self):
        event = self.create_event()
        first = self.nominate(event.event_id, 1, "First")
        second = self.nominate(event.event_id, 2, "Second")
        ranking = self.service.rankings(event.event_id)
        plan = self.service.build_ranked_schedule(
            event.event_id,
            starts_at=(
                datetime(2026, 9, 1, 19, tzinfo=DENVER),
                datetime(2026, 9, 2, 19, tzinfo=DENVER),
            ),
        )

        self.assertEqual([item.vote_count for item in ranking], [0, 0])
        self.assertEqual(plan.tied_vote_counts, (0,))
        self.assertEqual(
            [item.nomination_id for item in plan.assignments],
            [first.nomination_id, second.nomination_id],
        )

    def test_schedule_rejects_duplicate_titles_when_repeats_are_disabled(self):
        event = self.create_event()
        alien = self.nominate(event.event_id, 348, "Alien")
        with self.assertRaises(EventUsageError):
            self.service.schedule_event(
                event_id=event.event_id,
                assignments=(
                    ScheduleAssignment(
                        datetime(2026, 10, 1, 19, tzinfo=DENVER),
                        alien.nomination_id,
                    ),
                    ScheduleAssignment(
                        datetime(2026, 10, 2, 19, tzinfo=DENVER),
                        alien.nomination_id,
                    ),
                ),
            )

    def test_spooktober_is_a_versioned_movie_horror_preset_snapshot(self):
        preset = build_spooktober_preset(
            year=2026,
            nights=(1, 31),
            vote_limit=2,
        )
        event = self.service.create_event(
            discord_guild_id=88,
            discord_channel_id=2,
            created_by_discord_id=3,
            preset=preset,
        )

        self.assertEqual(event.name, "Spooktober 2026")
        self.assertEqual(event.preset_key, "spooktober")
        self.assertEqual(event.preset_version, "1")
        self.assertEqual(event.vote_limit, 2)
        self.assertEqual(len(event.rules["slot_times_utc"]), 2)

        with self.assertRaises(EventUsageError):
            self.service.nominate(
                event_id=event.event_id,
                media_type="tv",
                tmdb_id=1,
                title="Horror Show",
                year="2026",
                nominated_by_discord_id=4,
                genres=("Horror",),
            )
        with self.assertRaises(EventUsageError):
            self.service.nominate(
                event_id=event.event_id,
                media_type="movie",
                tmdb_id=2,
                title="A Comedy",
                year="2026",
                nominated_by_discord_id=4,
                genres=("Comedy",),
            )

        alien = self.nominate(event.event_id, 348, "Alien")
        thing = self.nominate(event.event_id, 1091, "The Thing")
        plan = self.service.build_ranked_schedule(event.event_id)
        scheduled, slots = self.service.schedule_ranked(plan)

        self.assertEqual(scheduled.status, EventStatus.SCHEDULED)
        self.assertEqual(
            [slot.nomination_id for slot in slots],
            [alien.nomination_id, thing.nomination_id],
        )

    def test_denver_tonight_uses_local_day_and_survives_service_restart(self):
        event = self.create_event(guild_id=41)
        alien = self.nominate(event.event_id, 348, "Alien")
        scheduled, _ = self.service.schedule_event(
            event_id=event.event_id,
            assignments=(
                ScheduleAssignment(
                    datetime(2026, 10, 15, 19, 0, tzinfo=DENVER),
                    alien.nomination_id,
                ),
            ),
        )
        self.assertEqual(scheduled.status, EventStatus.SCHEDULED)

        restarted = EventService(EventStore(self.db_path))
        restarted.initialize()
        tonight = restarted.tonight(
            discord_guild_id=41,
            reference=datetime(2026, 10, 15, 23, 55, tzinfo=DENVER),
        )
        tomorrow = restarted.tonight(
            discord_guild_id=41,
            reference=datetime(2026, 10, 16, 0, 1, tzinfo=DENVER),
        )

        self.assertEqual(len(tonight), 1)
        self.assertEqual(tonight[0].title, "Alien")
        self.assertEqual(tomorrow, ())

    def test_denver_day_bounds_respect_the_fall_dst_day(self):
        start, end = local_day_utc_bounds(
            datetime(2026, 11, 1, 12, tzinfo=DENVER)
        )
        self.assertEqual(end - start, timedelta(hours=25))

    def test_schedule_parser_preserves_local_timezone_and_input_order(self):
        parsed = parse_schedule_input(
            "2026-10-10 19:00, 2026-10-03 18:30",
            timezone_name="America/Denver",
        )

        self.assertEqual(
            [(value.day, value.hour, value.minute) for value in parsed],
            [(10, 19, 0), (3, 18, 30)],
        )
        self.assertTrue(all(value.tzinfo == DENVER for value in parsed))
        self.assertTrue(all(value.utcoffset() == timedelta(hours=-6) for value in parsed))

    def test_schedule_parser_rejects_empty_duplicate_and_invalid_values(self):
        invalid_values = (
            "",
            "2026-10-03 19:00,",
            "October 3 at 7pm",
            "2026-02-30 19:00",
            "2026-10-03 7:00",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(EventUsageError):
                    parse_schedule_input(value)

        with self.assertRaisesRegex(EventUsageError, "appears more than once"):
            parse_schedule_input(
                "2026-10-03 19:00, 2026-10-03 19:00"
            )

    def test_schedule_parser_and_service_cap_slots_at_thirty_one(self):
        values = ", ".join(
            f"2026-10-{day:02d} 19:00" for day in range(1, 32)
        )
        self.assertEqual(len(parse_schedule_input(values)), 31)

        with self.assertRaisesRegex(EventUsageError, "at most 31"):
            parse_schedule_input(f"{values}, 2026-11-01 19:00")

        event = self.create_event()
        alien = self.nominate(event.event_id, 348, "Alien")
        with self.assertRaisesRegex(EventUsageError, "at most 31"):
            self.service.build_ranked_schedule(
                event.event_id,
                starts_at=tuple(
                    datetime(2026, 10, 1, 19, tzinfo=DENVER)
                    + timedelta(days=index)
                    for index in range(32)
                ),
            )
        with self.assertRaisesRegex(EventUsageError, "at most 31"):
            self.service.schedule_event(
                event_id=event.event_id,
                assignments=tuple(
                    ScheduleAssignment(
                        datetime(2026, 10, 1, 19, tzinfo=DENVER)
                        + timedelta(days=index),
                        alien.nomination_id if index == 0 else None,
                    )
                    for index in range(32)
                ),
            )

    def test_schedule_parser_rejects_nonexistent_spring_forward_time(self):
        with self.assertRaisesRegex(EventUsageError, "does not exist"):
            parse_schedule_input(
                "2026-03-08 02:30",
                timezone_name="America/Denver",
            )

    def test_schedule_parser_rejects_ambiguous_fall_back_time(self):
        with self.assertRaisesRegex(EventUsageError, "happens twice"):
            parse_schedule_input(
                "2026-11-01 01:30",
                timezone_name="America/Denver",
            )

        accepted = parse_schedule_input(
            "2026-11-01 02:30",
            timezone_name="America/Denver",
        )
        self.assertEqual(accepted[0].utcoffset(), timedelta(hours=-7))

    def test_completed_and_cancelled_events_are_terminal(self):
        event = self.create_event(guild_id=77)
        alien = self.nominate(event.event_id, 348, "Alien")
        scheduled, _ = self.service.schedule_event(
            event_id=event.event_id,
            assignments=(
                ScheduleAssignment(
                    datetime(2026, 10, 15, 19, tzinfo=DENVER),
                    alien.nomination_id,
                ),
            ),
        )
        completed = self.service.complete(scheduled.event_id)
        self.assertEqual(completed.status, EventStatus.COMPLETED)
        with self.assertRaises(EventStateError):
            self.service.cancel(completed.event_id)


if __name__ == "__main__":
    unittest.main()
