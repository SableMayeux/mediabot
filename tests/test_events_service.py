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
    TimeOptionNotFoundError,
    VoteLimitExceededError,
)
from mediabot.events.presets.spooktober import build_spooktober_preset
from mediabot.services.events import (
    EventService,
    EventStatus,
    EventUsageError,
    ReminderStage,
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

    def test_time_options_rank_by_votes_then_earliest_and_drive_generic_schedule(self):
        event = self.create_event()
        alien = self.nominate(event.event_id, 348, "Alien")
        early = datetime(2026, 10, 15, 18, tzinfo=DENVER)
        late = datetime(2026, 10, 15, 20, tzinfo=DENVER)
        options = self.service.add_time_options(
            event.event_id,
            (late, early),
            created_by_discord_id=3,
        )
        by_time = {option.starts_at: option for option in options}
        early_id = by_time[early].time_option_id
        late_id = by_time[late].time_option_id

        first_vote = self.service.replace_time_votes(
            event.event_id,
            11,
            (early_id, late_id),
        )
        self.assertEqual(
            set(first_vote.selected_time_option_ids),
            {early_id, late_id},
        )
        tied = self.service.ranked_time_options(event.event_id)
        self.assertEqual([value.time_option_id for value in tied], [early_id, late_id])

        self.service.replace_time_votes(event.event_id, 12, (late_id,))
        ranked = self.service.ranked_time_options(event.event_id)
        plan = self.service.build_ranked_schedule(event.event_id)
        self.assertEqual(ranked[0].time_option_id, late_id)
        self.assertEqual(plan.assignments[0].starts_at, late)
        self.assertEqual(plan.assignments[0].nomination_id, alien.nomination_id)

        with self.assertRaises(TimeOptionNotFoundError):
            self.service.replace_time_votes(event.event_id, 13, (99999,))

    def test_replacing_time_options_preserves_only_unchanged_option_votes(self):
        event = self.create_event()
        first = datetime(2026, 10, 15, 18, tzinfo=DENVER)
        retained = datetime(2026, 10, 15, 20, tzinfo=DENVER)
        replacement = datetime(2026, 10, 16, 20, tzinfo=DENVER)
        original = self.service.add_time_options(event.event_id, (first, retained), 3)
        ids = {option.starts_at: option.time_option_id for option in original}
        self.service.replace_time_votes(
            event.event_id,
            11,
            (ids[first], ids[retained]),
        )

        current = self.service.replace_time_options(
            event.event_id,
            (retained, replacement),
            3,
        )
        current_by_time = {option.starts_at: option for option in current}
        self.assertEqual(current_by_time[retained].time_option_id, ids[retained])
        self.assertEqual(current_by_time[retained].vote_count, 1)
        self.assertEqual(current_by_time[replacement].vote_count, 0)
        self.assertEqual(
            self.service.user_time_vote_ids(event.event_id, 11),
            (ids[retained],),
        )

    def test_candidate_times_fit_one_discord_select(self):
        event = self.create_event()
        options = tuple(
            datetime(2026, 10, 1, 19, tzinfo=DENVER) + timedelta(days=index)
            for index in range(25)
        )
        self.assertEqual(
            len(self.service.replace_time_options(event.event_id, options, 3)),
            25,
        )
        with self.assertRaisesRegex(EventUsageError, "at most 25"):
            self.service.add_time_options(
                event.event_id,
                (datetime(2026, 11, 1, 19, tzinfo=DENVER),),
                3,
            )
        with self.assertRaisesRegex(EventUsageError, "at most 25"):
            self.service.replace_time_options(
                event.event_id,
                options + (datetime(2026, 11, 1, 19, tzinfo=DENVER),),
                3,
            )

    def test_expired_time_winner_does_not_strand_future_runner_up(self):
        event = self.create_event()
        self.nominate(event.event_id, 348, "Alien")
        reference = datetime(2026, 10, 15, 19, tzinfo=DENVER)
        expired = reference - timedelta(minutes=1)
        upcoming = reference + timedelta(hours=1)
        options = self.service.add_time_options(
            event.event_id,
            (expired, upcoming),
            3,
        )
        by_time = {option.starts_at: option for option in options}
        expired_id = by_time[expired.astimezone(timezone.utc)].time_option_id
        upcoming_id = by_time[upcoming.astimezone(timezone.utc)].time_option_id
        self.service.store.replace_time_votes(
            event_id=event.event_id,
            discord_user_id=10,
            time_option_ids=(expired_id,),
        )
        self.service.store.replace_time_votes(
            event_id=event.event_id,
            discord_user_id=11,
            time_option_ids=(expired_id,),
        )
        self.service.store.replace_time_votes(
            event_id=event.event_id,
            discord_user_id=12,
            time_option_ids=(upcoming_id,),
        )

        future = self.service.future_time_options(
            event.event_id,
            reference=reference,
            ranked=True,
        )
        plan = self.service.build_ranked_schedule(
            event.event_id,
            reference=reference,
        )

        self.assertEqual([option.time_option_id for option in future], [upcoming_id])
        self.assertEqual(plan.assignments[0].starts_at, upcoming.astimezone(timezone.utc))
        with self.assertRaisesRegex(EventUsageError, "already passed"):
            self.service.replace_time_votes(
                event.event_id,
                13,
                (expired_id,),
                reference=reference,
            )

    def test_expired_candidates_do_not_consume_the_actionable_option_cap(self):
        event = self.create_event()
        reference = datetime.now(timezone.utc)
        expired = tuple(
            reference - timedelta(days=index + 1)
            for index in range(25)
        )
        self.service.add_time_options(event.event_id, expired, 3)
        upcoming = reference + timedelta(days=1)

        self.service.add_time_options(event.event_id, (upcoming,), 3)

        self.assertEqual(len(self.service.time_options(event.event_id)), 26)
        self.assertEqual(
            [option.starts_at for option in self.service.future_time_options(event.event_id)],
            [upcoming],
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

    def test_preset_reschedule_moves_existing_slots_without_rewriting_lineup(self):
        preset = build_spooktober_preset(
            year=2026,
            nights=(15, 16),
            vote_limit=2,
        )
        event = self.service.create_event(
            discord_guild_id=89,
            discord_channel_id=2,
            created_by_discord_id=3,
            preset=preset,
        )
        alien = self.nominate(event.event_id, 348, "Alien")
        thing = self.nominate(event.event_id, 1091, "The Thing")

        with self.assertRaisesRegex(EventUsageError, "preset's dates changed"):
            self.service.schedule_event(
                event_id=event.event_id,
                assignments=(
                    ScheduleAssignment(
                        datetime(2026, 10, 15, 20, tzinfo=DENVER),
                        alien.nomination_id,
                    ),
                    ScheduleAssignment(
                        datetime(2026, 10, 16, 20, tzinfo=DENVER),
                        thing.nomination_id,
                    ),
                ),
            )

        _, original = self.service.schedule_ranked(
            self.service.build_ranked_schedule(event.event_id)
        )
        self.service.set_native_scheduled_event_id(original[0].slot_id, 8101)
        self.service.set_native_scheduled_event_id(original[1].slot_id, 8102)
        moved_times = tuple(slot.starts_at + timedelta(hours=1) for slot in original)

        rescheduled, changed = self.service.reschedule_event(
            event.event_id,
            tuple(
                ScheduleAssignment(starts_at, slot.nomination_id)
                for starts_at, slot in zip(moved_times, original)
            ),
        )

        self.assertEqual(rescheduled.status, EventStatus.SCHEDULED)
        self.assertEqual([slot.slot_id for slot in changed], [slot.slot_id for slot in original])
        self.assertEqual(
            [slot.nomination_id for slot in changed],
            [alien.nomination_id, thing.nomination_id],
        )
        self.assertEqual(
            [slot.native_scheduled_event_id for slot in changed],
            [8101, 8102],
        )
        self.assertEqual(tuple(slot.starts_at for slot in changed), moved_times)

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
        archived = self.service.archive(completed.event_id)
        self.assertIsNotNone(archived.archived_at)

    def test_reschedule_preserves_slot_and_native_ids_then_reopen_discards_slots(self):
        event = self.create_event(guild_id=93)
        alien = self.nominate(event.event_id, 348, "Alien")
        thing = self.nominate(event.event_id, 1091, "The Thing")
        scheduled, slots = self.service.schedule_event(
            event_id=event.event_id,
            assignments=(
                ScheduleAssignment(
                    datetime(2026, 10, 15, 19, tzinfo=DENVER),
                    alien.nomination_id,
                ),
                ScheduleAssignment(
                    datetime(2026, 10, 16, 19, tzinfo=DENVER),
                    thing.nomination_id,
                ),
            ),
        )
        self.service.set_native_scheduled_event_id(slots[0].slot_id, 8001)
        self.service.set_native_scheduled_event_id(slots[1].slot_id, 8002)

        rescheduled, changed = self.service.reschedule_event(
            event.event_id,
            (
                ScheduleAssignment(
                    datetime(2026, 10, 15, 20, tzinfo=DENVER),
                    alien.nomination_id,
                ),
                ScheduleAssignment(
                    datetime(2026, 10, 16, 20, tzinfo=DENVER),
                    thing.nomination_id,
                ),
            ),
        )
        self.assertEqual(rescheduled.status, EventStatus.SCHEDULED)
        self.assertEqual([slot.slot_id for slot in changed], [slot.slot_id for slot in slots])
        self.assertEqual(
            [slot.native_scheduled_event_id for slot in changed],
            [8001, 8002],
        )

        with self.assertRaisesRegex(StaleEventRevisionError, "schedule changed"):
            self.service.reschedule_event(
                event.event_id,
                (
                    ScheduleAssignment(
                        datetime(2026, 10, 15, 21, tzinfo=DENVER),
                        alien.nomination_id,
                    ),
                    ScheduleAssignment(
                        datetime(2026, 10, 16, 21, tzinfo=DENVER),
                        thing.nomination_id,
                    ),
                ),
                expected_revision=scheduled.revision,
            )
        self.assertEqual(
            tuple(slot.starts_at for slot in self.service.slots(event.event_id)),
            (
                datetime(2026, 10, 15, 20, tzinfo=DENVER).astimezone(timezone.utc),
                datetime(2026, 10, 16, 20, tzinfo=DENVER).astimezone(timezone.utc),
            ),
        )

        reopened = self.service.reopen(event.event_id)
        self.assertEqual(reopened.status, EventStatus.OPEN)
        self.assertEqual(self.service.slots(event.event_id), ())

    def test_archive_and_clear_old_hide_terminal_and_past_schedules(self):
        terminal = self.create_event(guild_id=94, name="Cancelled")
        self.service.cancel(terminal.event_id)

        stale = self.create_event(guild_id=94, name="Yesterday")
        alien = self.nominate(stale.event_id, 348, "Alien")
        self.service.schedule_event(
            event_id=stale.event_id,
            assignments=(
                ScheduleAssignment(
                    datetime(2026, 10, 14, 19, tzinfo=DENVER),
                    alien.nomination_id,
                ),
            ),
        )
        future = self.create_event(guild_id=94, name="Tomorrow")
        thing = self.nominate(future.event_id, 1091, "The Thing")
        self.service.schedule_event(
            event_id=future.event_id,
            assignments=(
                ScheduleAssignment(
                    datetime(2026, 10, 16, 19, tzinfo=DENVER),
                    thing.nomination_id,
                ),
            ),
        )

        cleared = self.service.clear_old(
            94,
            datetime(2026, 10, 15, 19, tzinfo=DENVER),
        )
        self.assertEqual(
            {event.event_id for event in cleared},
            {terminal.event_id, stale.event_id},
        )
        self.assertTrue(all(event.archived_at is not None for event in cleared))
        self.assertEqual(
            [event.event_id for event in self.service.list_events(discord_guild_id=94)],
            [future.event_id],
        )
        all_events = self.service.list_events(
            discord_guild_id=94,
            include_archived=True,
        )
        by_id = {event.event_id: event for event in all_events}
        self.assertEqual(by_id[stale.event_id].status, EventStatus.COMPLETED)
        self.assertEqual(by_id[future.event_id].status, EventStatus.SCHEDULED)

    def test_reminder_stages_are_durable_and_restart_safe(self):
        event = self.create_event(guild_id=95)
        alien = self.nominate(event.event_id, 348, "Alien")
        starts_at = datetime(2026, 10, 15, 19, tzinfo=DENVER)
        _, slots = self.service.schedule_event(
            event_id=event.event_id,
            assignments=(ScheduleAssignment(starts_at, alien.nomination_id),),
        )
        slot = slots[0]

        day = self.service.due_reminders(starts_at - timedelta(hours=23))
        self.assertEqual(len(day), 1)
        self.assertEqual(day[0].stage, int(ReminderStage.DAY))
        self.assertEqual(day[0].title, "Alien")
        self.service.advance_reminder(slot.slot_id, day[0].stage)

        restarted = EventService(EventStore(self.db_path))
        restarted.initialize()
        self.assertEqual(
            restarted.due_reminders(starts_at - timedelta(hours=22)),
            (),
        )
        hour = restarted.due_reminders(starts_at - timedelta(minutes=30))
        self.assertEqual(hour[0].stage, int(ReminderStage.HOUR))
        self.assertEqual(
            restarted.advance_reminder(slot.slot_id, hour[0].stage),
            (1, 2),
        )
        start = restarted.due_reminders(starts_at + timedelta(seconds=1))
        self.assertEqual(start[0].stage, int(ReminderStage.START))
        self.assertEqual(
            restarted.due_reminders(starts_at + timedelta(minutes=16)),
            (),
        )
        self.assertEqual(restarted.advance_reminder(slot.slot_id, 3), (1, 2, 3))
        self.assertEqual(
            restarted.due_reminders(starts_at + timedelta(minutes=1)),
            (),
        )


if __name__ == "__main__":
    unittest.main()
