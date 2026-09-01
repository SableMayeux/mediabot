import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from mediabot.core.event_store import EventStore
from mediabot.events.presets import build_spooktober_preset
from mediabot.services.events import EventService


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
                "schedule",
                "tonight",
                "complete",
                "cancel",
            },
        )
        self.assertIsNone(self.app.bot.get_command("spooktober"))
        self.assertEqual(self.app.BOT_VERSION, "0.9.1")

    def test_admin_children_have_both_guild_and_administrator_checks(self):
        group = self.app.bot.get_command("event")
        for name in ("create", "schedule", "complete", "cancel"):
            self.assertGreaterEqual(len(group.get_command(name).checks), 2, name)
        for name in ("nominate", "vote", "tonight"):
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
