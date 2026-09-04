import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from mediabot.core.event_store import EventStore
from mediabot.events.presets import build_spooktober_preset
from mediabot.services.events import EventService, ScheduleAssignment


def load_app():
    os.environ.setdefault("DISCORD_TOKEN", "test-token")
    os.environ.setdefault("SEERR_API_KEY", "test-key")
    os.environ.setdefault(
        "LOG_PATH",
        os.path.join(tempfile.gettempdir(), "mediabot-event-ui.log"),
    )
    os.environ.setdefault(
        "DB_PATH",
        os.path.join(tempfile.gettempdir(), "mediabot-event-ui.db"),
    )
    import app

    return app


class EventAppTestCase(unittest.TestCase):
    def setUp(self):
        self.app = load_app()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = EventService(
            EventStore(os.path.join(self.temp_dir.name, "events.db"))
        )
        self.service.initialize()
        self.previous_events = self.app.events
        self.app.events = self.service
        self.app._event_dashboard_locks.clear()

    def tearDown(self):
        self.app.events = self.previous_events
        self.app._event_dashboard_locks.clear()
        self.temp_dir.cleanup()

    def create_event(self, **kwargs):
        values = {
            "discord_guild_id": 20,
            "discord_channel_id": 30,
            "created_by_discord_id": 10,
            "name": "Friday Movie Night",
        }
        values.update(kwargs)
        return self.service.create_event(**values)


class EventCreateParserTests(EventAppTestCase):
    def test_generic_name_and_trailing_vote_limit(self):
        spec = self.app.parse_event_create_input(
            "Friday Movie Night --votes 3"
        )
        self.assertEqual(spec.name, "Friday Movie Night")
        self.assertEqual(spec.vote_limit, 3)
        self.assertIsNone(spec.preset)

    def test_spooktober_defaults_year_and_snapshots_versioned_preset(self):
        now = datetime(2031, 2, 4, 12, 0, tzinfo=ZoneInfo("America/Denver"))
        spec = self.app.parse_event_create_input(
            "spooktober --votes 2",
            now=now,
        )
        self.assertEqual(spec.name, "Spooktober 2031")
        self.assertEqual(spec.vote_limit, 2)
        self.assertEqual(spec.preset.key, "spooktober")
        self.assertEqual(spec.preset.version, "1")
        self.assertEqual(len(spec.preset.slot_times_utc), 31)

    def test_vote_modifier_must_be_trailing(self):
        with self.assertRaises(self.app.EventUsageError):
            self.app.parse_event_create_input(
                "Friday --votes 2 Movie Night"
            )


class EventCommandSurfaceTests(EventAppTestCase):
    def test_event_group_is_complete_without_duplicate_spooktober_command(self):
        group = self.app.bot.get_command("event")
        self.assertIsNotNone(group)
        self.assertEqual(
            {command.name for command in group.commands},
            {
                "create",
                "nominate",
                "vote",
                "time",
                "schedule",
                "reschedule",
                "reopen",
                "tonight",
                "history",
                "archive",
                "clear",
                "complete",
                "cancel",
            },
        )
        self.assertIsNone(self.app.bot.get_command("spooktober"))
        self.assertEqual(self.app.BOT_VERSION, "1.0.0")

    def test_admin_children_have_both_guild_and_administrator_checks(self):
        group = self.app.bot.get_command("event")
        for name in (
            "create",
            "schedule",
            "reschedule",
            "reopen",
            "archive",
            "clear",
            "complete",
            "cancel",
        ):
            self.assertGreaterEqual(len(group.get_command(name).checks), 2, name)
        for name in ("nominate", "vote", "time", "tonight", "history"):
            self.assertGreaterEqual(len(group.get_command(name).checks), 1, name)


class EventNominationViewTests(EventAppTestCase):
    def test_nomination_search_pages_five_results_at_a_time(self):
        event_record = self.create_event()
        results = [
            {
                "id": index + 1,
                "mediaType": "movie",
                "title": f"Movie {index}",
                "releaseDate": f"20{index:02d}-01-01",
            }
            for index in range(7)
        ]
        view = self.app.EventNominationSearchView(
            guild_id=20,
            event_record=event_record,
            requester_id=10,
            query="Movie",
            results=results,
            seerr_page=1,
            total_seerr_pages=1,
            command_message=SimpleNamespace(),
        )
        self.assertEqual(len(view.page_items()), 5)
        view.display_page = 1
        view.refresh_controls()
        self.assertEqual(len(view.page_items()), 2)
        self.assertEqual(
            [button.disabled for button in view.result_buttons],
            [False, False, True, True, True],
        )

    def test_nomination_search_title_respects_embed_limit(self):
        event_record = self.create_event(name="E" * 100)
        view = self.app.EventNominationSearchView(
            guild_id=20,
            event_record=event_record,
            requester_id=10,
            query="Q" * 500,
            results=[],
            seerr_page=1,
            total_seerr_pages=1,
            command_message=SimpleNamespace(),
        )
        self.assertLessEqual(len(view.build_embed().title), 256)


class EventScheduleShapeTests(EventAppTestCase):
    def test_spooktober_preview_receipt_and_dashboard_show_all_31_slots(self):
        event_record = self.create_event(
            name=None,
            preset=build_spooktober_preset(year=2032),
        )
        self.service.nominate(
            event_id=event_record.event_id,
            media_type="movie",
            tmdb_id=99,
            title="The Thing",
            year="1982",
            nominated_by_discord_id=10,
            genres=("Horror",),
        )
        plan = self.service.build_ranked_schedule(event_record.event_id)
        preview = self.app.build_event_schedule_preview(event_record, plan)
        preview_text = "\n".join(
            field.value
            for field in preview.fields
            if field.name.startswith("Ranked schedule")
        )
        self.assertEqual(preview_text.count("<t:"), 31)
        self.assertTrue(
            all(len(field.value) <= 1024 for field in preview.fields)
        )

        scheduled, slots = self.service.schedule_ranked(plan)
        receipt = self.app.build_event_schedule_receipt(scheduled, slots)
        receipt_text = "\n".join(
            field.value
            for field in receipt.fields
            if field.name.startswith("Schedule")
        )
        self.assertEqual(receipt_text.count("<t:"), 31)

        dashboard = self.app.build_event_dashboard_embed(scheduled)
        dashboard_text = "\n".join(
            field.value
            for field in dashboard.fields
            if field.name.startswith("Schedule")
        )
        self.assertEqual(dashboard_text.count("<t:"), 31)


class EventTimeUITests(EventAppTestCase):
    def test_picker_stays_within_discord_component_limits(self):
        event_record = self.create_event()
        view = self.app.EventTimePickerView(
            requester_id=10,
            guild_id=20,
            event_record=event_record,
        )

        self.assertLessEqual(len(view.date_select.options), 25)
        self.assertLessEqual(len(view.time_select.options), 25)
        self.assertEqual(len(view.time_select.options), 24)
        self.assertIsNotNone(view.selected_at.tzinfo)
        self.assertIn("Ready to add", [field.name for field in view.build_embed().fields])

    def test_dashboard_exposes_title_time_and_admin_time_controls(self):
        event_record = self.create_event()
        view = self.app.EventDashboardView(event_record)
        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())
        self.assertEqual(
            {child.label for child in view.children},
            {"Vote titles", "Vote times", "Add times"},
        )

    def test_equal_availability_requires_explicit_time_choice(self):
        event_record = self.create_event()
        starts = (
            datetime.now(timezone.utc) + timedelta(days=1),
            datetime.now(timezone.utc) + timedelta(days=2),
        )
        options = self.service.add_time_options(
            event_record.event_id,
            starts,
            10,
        )
        view = self.app.EventScheduleTimeTieView(
            requester_id=10,
            guild_id=20,
            event_record=event_record,
            options=options,
            command_message=SimpleNamespace(),
        )
        self.assertIn("will not silently pick", view.build_embed().description)
        self.assertEqual(len(view.children[0].options), 2)

    def test_reschedule_picker_keeps_current_out_of_window_time_and_custom(self):
        event_record = self.create_event()
        nomination = self.service.nominate(
            event_id=event_record.event_id,
            media_type="movie",
            tmdb_id=99,
            title="The Thing",
            year="1982",
            nominated_by_discord_id=10,
            genres=("Horror",),
        ).nomination
        starts_at = datetime.now(timezone.utc) + timedelta(days=40, minutes=17)
        scheduled, slots = self.service.schedule_event(
            event_id=event_record.event_id,
            assignments=(ScheduleAssignment(starts_at, nomination.nomination_id),),
        )
        view = self.app.EventReschedulePickerView(
            requester_id=10,
            guild_id=20,
            event_record=scheduled,
            slot=slots[0],
            command_message=SimpleNamespace(),
        )

        self.assertIn(
            "Custom",
            {getattr(child, "label", None) for child in view.children},
        )
        self.assertEqual(
            [option.value for option in view.date_select.options if option.default],
            [view.selected_date],
        )
        self.assertEqual(
            [option.value for option in view.time_select.options if option.default],
            [view.selected_time],
        )

    def test_dashboard_and_availability_ballot_hide_expired_candidates(self):
        event_record = self.create_event()
        reference = datetime.now(timezone.utc)
        options = self.service.add_time_options(
            event_record.event_id,
            (
                reference - timedelta(minutes=1),
                reference + timedelta(days=1),
            ),
            10,
        )
        future = self.service.future_time_options(event_record.event_id)
        dashboard = self.app.build_event_dashboard_embed(event_record)
        dashboard_text = "\n".join(field.value for field in dashboard.fields)
        vote_view = self.app.EventTimeVoteView(
            requester_id=10,
            guild_id=20,
            event_record=event_record,
            options=future,
            selected_ids=tuple(option.time_option_id for option in options),
        )

        self.assertNotIn(
            self.app.discord.utils.format_dt(options[0].starts_at, style="F"),
            dashboard_text,
        )
        self.assertIn(
            self.app.discord.utils.format_dt(options[1].starts_at, style="F"),
            dashboard_text,
        )
        self.assertEqual(
            vote_view.selected_ids,
            {options[1].time_option_id},
        )


class EventVoteInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = load_app()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = EventService(
            EventStore(os.path.join(self.temp_dir.name, "events.db"))
        )
        self.service.initialize()
        self.previous_events = self.app.events
        self.app.events = self.service
        self.app._event_dashboard_locks.clear()
        self.event = self.service.create_event(
            discord_guild_id=20,
            discord_channel_id=30,
            created_by_discord_id=10,
            name="Friday Movie Night",
        )
        for tmdb_id, title in ((1, "Alien"), (2, "The Thing")):
            self.service.nominate(
                event_id=self.event.event_id,
                media_type="movie",
                tmdb_id=tmdb_id,
                title=title,
                year="1982",
                nominated_by_discord_id=10,
                genres=("Horror",),
            )

    async def asyncTearDown(self):
        self.app.events = self.previous_events
        self.app._event_dashboard_locks.clear()
        self.temp_dir.cleanup()

    def make_view(self):
        return self.app.EventVoteView(
            requester_id=10,
            guild_id=20,
            event_record=self.event,
            rankings=self.service.rankings(self.event.event_id),
            selected_ids=(),
            command_message=SimpleNamespace(id=70),
        )

    async def test_controls_are_originating_user_and_guild_only(self):
        view = self.make_view()
        wrong_user = SimpleNamespace(
            guild_id=20,
            user=SimpleNamespace(id=11),
            response=SimpleNamespace(send_message=AsyncMock()),
            message=None,
        )
        self.assertFalse(await view.interaction_check(wrong_user))
        wrong_user.response.send_message.assert_awaited_once()

        wrong_guild = SimpleNamespace(
            guild_id=999,
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(send_message=AsyncMock()),
            message=None,
        )
        self.assertFalse(await view.interaction_check(wrong_guild))
        wrong_guild.response.send_message.assert_awaited_once()

    async def test_bare_event_reply_is_transient_not_an_extra_persistent_dashboard(self):
        command_message = SimpleNamespace(
            id=70,
            channel=SimpleNamespace(id=30),
        )
        reply_message = SimpleNamespace(
            id=71,
            channel=SimpleNamespace(id=30),
            guild=SimpleNamespace(id=20),
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=20),
            message=command_message,
            reply=AsyncMock(return_value=reply_message),
        )
        previous_register = self.app.register_transient_card
        register = Mock(return_value=True)
        self.app.register_transient_card = register
        try:
            await self.app.event_group.callback(ctx)
        finally:
            self.app.register_transient_card = previous_register

        view = ctx.reply.await_args.kwargs["view"]
        self.assertEqual(view.timeout, self.app.REQUEST_UI_TIMEOUT)
        self.assertFalse(view.is_persistent())
        self.assertIs(view.message, reply_message)
        register.assert_called_once_with(
            message=reply_message,
            command_message=command_message,
            kind="event_dashboard_snapshot",
        )

    async def test_time_vote_card_retires_if_admin_removed_all_candidate_times(self):
        starts_at = datetime.now(timezone.utc) + timedelta(days=1)
        options = self.service.add_time_options(
            self.event.event_id,
            (starts_at,),
            10,
        )
        view = self.app.EventTimeVoteView(
            requester_id=10,
            guild_id=20,
            event_record=self.service.event(self.event.event_id),
            options=options,
            selected_ids=(),
            command_message=SimpleNamespace(id=70),
        )
        message = SimpleNamespace(id=50, channel=SimpleNamespace(id=30))
        view.message = message
        interaction = SimpleNamespace(
            guild_id=20,
            user=SimpleNamespace(id=10),
            message=message,
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        self.service.replace_time_options(self.event.event_id, (), 10)
        previous_refresh = self.app.refresh_event_dashboard
        self.app.refresh_event_dashboard = AsyncMock(return_value=True)
        try:
            await view.clear(interaction)
        finally:
            self.app.refresh_event_dashboard = previous_refresh

        self.assertTrue(view.finished)
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn("Run `$event time` again", kwargs["content"])
        self.assertIsNone(kwargs["embed"])
        self.assertIsNone(kwargs["view"])

    async def test_admin_gets_time_picker_when_only_expired_candidates_remain(self):
        self.service.add_time_options(
            self.event.event_id,
            (datetime.now(timezone.utc) - timedelta(minutes=1),),
            10,
        )
        message = SimpleNamespace(id=71, channel=SimpleNamespace(id=30))
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=20),
            author=SimpleNamespace(
                id=10,
                guild_permissions=SimpleNamespace(administrator=True),
            ),
            message=SimpleNamespace(id=70),
            reply=AsyncMock(return_value=message),
        )
        previous_register = self.app.register_transient_card
        self.app.register_transient_card = Mock(return_value=True)
        try:
            await self.app.event_time.callback(ctx, value="")
        finally:
            self.app.register_transient_card = previous_register

        view = ctx.reply.await_args.kwargs["view"]
        self.assertIsInstance(view, self.app.EventTimePickerView)
        self.assertIs(view.message, message)

    async def test_stale_reschedule_picker_cannot_overwrite_newer_admin_change(self):
        nomination = self.service.rankings(self.event.event_id)[0].nomination
        first_time = datetime.now(timezone.utc) + timedelta(days=2)
        scheduled, slots = self.service.schedule_event(
            event_id=self.event.event_id,
            assignments=(ScheduleAssignment(first_time, nomination.nomination_id),),
        )
        view = self.app.EventReschedulePickerView(
            requester_id=10,
            guild_id=20,
            event_record=scheduled,
            slot=slots[0],
            command_message=SimpleNamespace(id=70),
        )
        newer_time = first_time + timedelta(hours=1)
        self.service.reschedule_event(
            scheduled.event_id,
            (ScheduleAssignment(newer_time, nomination.nomination_id),),
            expected_revision=scheduled.revision,
        )
        view.selected_at = first_time + timedelta(hours=2)
        interaction = SimpleNamespace(
            guild_id=20,
            user=SimpleNamespace(id=10),
            message=SimpleNamespace(id=50),
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )

        await view.add_candidate(interaction)

        self.assertTrue(view.finished)
        interaction.response.edit_message.assert_awaited_once()
        self.assertIn(
            "Run `$event reschedule",
            interaction.response.edit_message.await_args.kwargs["content"],
        )
        interaction.response.send_message.assert_not_awaited()
        self.assertEqual(
            self.service.slots(scheduled.event_id)[0].starts_at,
            newer_time,
        )

    async def test_successful_vote_becomes_static_durable_receipt_on_timeout(self):
        view = self.make_view()
        message = SimpleNamespace(
            id=50,
            channel=SimpleNamespace(id=30),
            edit=AsyncMock(),
        )
        view.message = message
        interaction = SimpleNamespace(
            guild_id=20,
            user=SimpleNamespace(id=10),
            message=message,
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                send_message=AsyncMock(),
            ),
        )
        previous_transition = self.app.transition_transient_message
        previous_touch = self.app.touch_transient_message
        previous_terminal = self.app.mark_transient_message_terminal
        previous_refresh = self.app.refresh_event_dashboard
        self.app.transition_transient_message = Mock(return_value=True)
        self.app.touch_transient_message = Mock(return_value=True)
        self.app.mark_transient_message_terminal = Mock(return_value=True)
        self.app.refresh_event_dashboard = AsyncMock(return_value=True)
        try:
            await view.toggle_slot(interaction, 0)
            self.assertEqual(
                len(
                    self.service.user_vote_ids(
                        event_id=self.event.event_id,
                        discord_user_id=10,
                    )
                ),
                1,
            )
            self.app.transition_transient_message.assert_called_once_with(
                message,
                "event_vote_saved_actions",
            )
            await view.on_timeout()
            message.edit.assert_awaited_once()
            self.assertIsNone(message.edit.await_args.kwargs["view"])
            self.app.mark_transient_message_terminal.assert_called_with(
                message,
                "kept",
            )
        finally:
            self.app.transition_transient_message = previous_transition
            self.app.touch_transient_message = previous_touch
            self.app.mark_transient_message_terminal = previous_terminal
            self.app.refresh_event_dashboard = previous_refresh

    async def test_time_vote_becomes_static_if_durable_transition_fails(self):
        options = self.service.add_time_options(
            self.event.event_id,
            (datetime.now(timezone.utc) + timedelta(days=1),),
            10,
        )
        view = self.app.EventTimeVoteView(
            requester_id=10,
            guild_id=20,
            event_record=self.service.event(self.event.event_id),
            options=options,
            selected_ids=(),
            command_message=SimpleNamespace(id=70),
        )
        message = SimpleNamespace(id=50, channel=SimpleNamespace(id=30))
        view.message = message
        interaction = SimpleNamespace(
            guild_id=20,
            user=SimpleNamespace(id=10),
            message=message,
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        previous_transition = self.app.transition_transient_message
        previous_terminal = self.app.mark_transient_message_terminal
        previous_refresh = self.app.refresh_event_dashboard
        self.app.transition_transient_message = Mock(return_value=False)
        self.app.mark_transient_message_terminal = Mock(return_value=True)
        self.app.refresh_event_dashboard = AsyncMock(return_value=True)
        try:
            await view.save_values(
                interaction,
                (str(options[0].time_option_id),),
            )
        finally:
            self.app.transition_transient_message = previous_transition
            self.app.mark_transient_message_terminal = previous_terminal
            self.app.refresh_event_dashboard = previous_refresh

        self.assertTrue(view.saved)
        self.assertTrue(view.finished)
        self.assertFalse(view.durable)
        self.assertEqual(
            self.service.user_time_vote_ids(self.event.event_id, 10),
            (options[0].time_option_id,),
        )
        self.assertIsNone(interaction.response.edit_message.await_args.kwargs["view"])
        self.assertIn(
            "Availability saved",
            interaction.response.edit_message.await_args.kwargs["embed"].footer.text,
        )

    async def test_queued_click_cannot_vote_for_a_title_that_moved_into_the_slot(self):
        view = self.make_view()
        message = SimpleNamespace(id=50, channel=SimpleNamespace(id=30))
        view.message = message
        response = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild_id=20,
            user=SimpleNamespace(id=10),
            message=message,
            response=response,
        )
        old_slot_one_id = view.slot_custom_id(1)
        previous_transition = self.app.transition_transient_message
        previous_refresh = self.app.refresh_event_dashboard
        self.app.transition_transient_message = Mock(return_value=True)
        self.app.refresh_event_dashboard = AsyncMock(return_value=True)
        try:
            await view.toggle_slot(interaction, 1, clicked_id=old_slot_one_id)
            selected_before = self.service.user_vote_ids(
                event_id=self.event.event_id,
                discord_user_id=10,
            )
            await view.toggle_slot(interaction, 1, clicked_id=old_slot_one_id)
            selected_after = self.service.user_vote_ids(
                event_id=self.event.event_id,
                discord_user_id=10,
            )
        finally:
            self.app.transition_transient_message = previous_transition
            self.app.refresh_event_dashboard = previous_refresh

        self.assertEqual(selected_after, selected_before)
        response.send_message.assert_awaited_once()

    async def test_dashboard_refresh_serialization_leaves_newest_revision_visible(self):
        current = self.service.set_dashboard_message(
            event_id=self.event.event_id,
            discord_channel_id=30,
            dashboard_message_id=50,
        )
        first_edit_entered = asyncio.Event()
        release_first_edit = asyncio.Event()
        rendered_footers = []

        async def edit_message(*, embed, view):
            rendered_footers.append(embed.footer.text)
            if len(rendered_footers) == 1:
                first_edit_entered.set()
                await release_first_edit.wait()

        message = SimpleNamespace(edit=edit_message)
        previous_lookup = self.app.discord_message_by_id
        self.app.discord_message_by_id = AsyncMock(return_value=message)
        try:
            older_refresh = asyncio.create_task(
                self.app.refresh_event_dashboard(current.event_id)
            )
            await first_edit_entered.wait()
            nomination_id = self.service.rankings(current.event_id)[0].nomination.nomination_id
            self.service.toggle_vote(
                event_id=current.event_id,
                nomination_id=nomination_id,
                discord_user_id=10,
            )
            newer_refresh = asyncio.create_task(
                self.app.refresh_event_dashboard(current.event_id)
            )
            await asyncio.sleep(0)
            release_first_edit.set()
            self.assertEqual(await asyncio.gather(older_refresh, newer_refresh), [True, True])
        finally:
            release_first_edit.set()
            self.app.discord_message_by_id = previous_lookup

        self.assertEqual(len(rendered_footers), 2)
        self.assertNotEqual(rendered_footers[0], rendered_footers[1])
        self.assertTrue(
            rendered_footers[-1].endswith(
                f"revision {self.service.event(current.event_id).revision}"
            )
        )


if __name__ == "__main__":
    unittest.main()
