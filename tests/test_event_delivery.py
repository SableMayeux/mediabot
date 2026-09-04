import asyncio
import inspect
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from mediabot.core.event_store import EventStore
from mediabot.services.events import EventService, EventStatus, ScheduleAssignment


def load_app():
    os.environ.setdefault("DISCORD_TOKEN", "test-token")
    os.environ.setdefault("SEERR_API_KEY", "test-key")
    os.environ.setdefault(
        "LOG_PATH", os.path.join(tempfile.gettempdir(), "mediabot-event-delivery.log")
    )
    import app

    return app


class FakeScheduledEvent:
    def __init__(self, event_id, **values):
        self.id = int(event_id)
        self.name = values.get("name", "")
        self.start_time = values.get("start_time")
        self.end_time = values.get("end_time")
        self.description = values.get("description", "")
        self.location = values.get("location", "")
        self.entity_type = values.get("entity_type", discord.EntityType.external)
        self.privacy_level = values.get(
            "privacy_level", discord.PrivacyLevel.guild_only
        )
        self.status = values.get("status", discord.EventStatus.scheduled)
        self.creator_id = int(values.get("creator_id", 777))
        self.creator = SimpleNamespace(id=self.creator_id)
        self.edit_calls = 0
        self.edit_values = []
        self.deleted = False

    async def edit(self, **values):
        self._validate(values)
        self.edit_calls += 1
        self.edit_values.append(dict(values))
        for key, value in values.items():
            if key not in {"reason", "channel"}:
                setattr(self, key, value)
        return self

    async def delete(self, **_values):
        self.deleted = True

    @staticmethod
    def _validate(values):
        if "name" in values and not 1 <= len(str(values["name"])) <= 100:
            raise ValueError("Discord scheduled-event names must be 1-100 characters")
        if "description" in values and not 1 <= len(str(values["description"])) <= 1000:
            raise ValueError(
                "Discord scheduled-event descriptions must be 1-1000 characters"
            )
        if "location" in values and not 1 <= len(str(values["location"])) <= 100:
            raise ValueError("Discord external-event locations must be 1-100 characters")
        for key in ("start_time", "end_time"):
            value = values.get(key)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{key} must be timezone-aware")


class FakeGuild:
    def __init__(self, guild_id):
        self.id = int(guild_id)
        self.me = SimpleNamespace(
            id=777,
            guild_permissions=SimpleNamespace(
                administrator=True,
                create_events=True,
                manage_events=True,
            )
        )
        self.remote_events = []
        self.create_calls = 0
        self.create_values = []
        self.fetch_calls = 0

    async def fetch_scheduled_events(self, **_values):
        self.fetch_calls += 1
        return list(self.remote_events)

    async def create_scheduled_event(self, **values):
        FakeScheduledEvent._validate(values)
        self.create_calls += 1
        self.create_values.append(dict(values))
        remote = FakeScheduledEvent(
            500 + self.create_calls,
            creator_id=self.me.id,
            **values,
        )
        self.remote_events.append(remote)
        return remote


class EventDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = load_app()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = EventService(
            EventStore(os.path.join(self.temp_dir.name, "events.db"))
        )
        self.service.initialize()
        self.previous_events = self.app.events
        self.previous_guild_ids = self.app.ALLOWED_GUILD_IDS
        self.previous_get_guild = self.app.bot.get_guild
        self.previous_get_channel = self.app.bot.get_channel
        self.previous_bot_user = self.app.bot._connection.user
        self.previous_reminder_mention = self.app.EVENT_REMINDER_MENTION
        self.previous_reminder_role_id = self.app.EVENT_REMINDER_ROLE_ID
        self.previous_jellyfin_public_url = self.app.jellyfin.public_url
        self.app.events = self.service
        self.app.ALLOWED_GUILD_IDS = frozenset({20})
        self.app.EVENT_REMINDER_MENTION = ""
        self.app.bot._connection.user = SimpleNamespace(id=777)
        self.guild = FakeGuild(20)
        self.app.bot.get_guild = lambda guild_id: (
            self.guild if int(guild_id) == self.guild.id else None
        )

    async def asyncTearDown(self):
        self.app.events = self.previous_events
        self.app.ALLOWED_GUILD_IDS = self.previous_guild_ids
        self.app.bot.get_guild = self.previous_get_guild
        self.app.bot.get_channel = self.previous_get_channel
        self.app.bot._connection.user = self.previous_bot_user
        self.app.EVENT_REMINDER_MENTION = self.previous_reminder_mention
        self.app.EVENT_REMINDER_ROLE_ID = self.previous_reminder_role_id
        self.app.jellyfin.public_url = self.previous_jellyfin_public_url
        self.temp_dir.cleanup()

    def create_schedule(
        self,
        starts_at,
        *,
        name="Friday Movie Night",
        title="Alien",
        tmdb_id=348,
    ):
        event = self.service.create_event(
            discord_guild_id=20,
            discord_channel_id=30,
            created_by_discord_id=10,
            name=name,
        )
        nomination = self.service.nominate(
            event_id=event.event_id,
            media_type="movie",
            tmdb_id=tmdb_id,
            title=title,
            year="1979",
            nominated_by_discord_id=10,
            genres=("Horror",),
        ).nomination
        scheduled, slots = self.service.schedule_event(
            event_id=event.event_id,
            assignments=(ScheduleAssignment(starts_at, nomination.nomination_id),),
        )
        return scheduled, slots[0]

    def create_multi_slot_schedule(self, starts_at):
        event = self.service.create_event(
            discord_guild_id=20,
            discord_channel_id=30,
            created_by_discord_id=10,
            name="Movie Marathon",
        )
        nominations = []
        for index, _starts in enumerate(starts_at, start=1):
            nominations.append(
                self.service.nominate(
                    event_id=event.event_id,
                    media_type="movie",
                    tmdb_id=1000 + index,
                    title=f"Movie {index}",
                    year="1979",
                    nominated_by_discord_id=10,
                    genres=("Horror",),
                ).nomination
            )
        scheduled, slots = self.service.schedule_event(
            event_id=event.event_id,
            assignments=tuple(
                ScheduleAssignment(starts, nomination.nomination_id)
                for starts, nomination in zip(starts_at, nominations)
            ),
        )
        return scheduled, slots

    def test_reminder_config_accepts_only_blank_or_role_mentions(self):
        self.assertIsNone(self.app.parse_event_reminder_role_id(""))
        self.assertEqual(self.app.parse_event_reminder_role_id("12345"), 12345)
        self.assertEqual(self.app.parse_event_reminder_role_id("<@&12345>"), 12345)
        for unsafe in ("@everyone", "<@12345>", "hello @everyone"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ValueError):
                    self.app.parse_event_reminder_role_id(unsafe)

    def test_reminder_role_requires_exactly_one_allowed_guild(self):
        self.assertEqual(
            self.app.validate_event_reminder_role_scope(12345, frozenset({20})),
            12345,
        )
        self.assertIsNone(
            self.app.validate_event_reminder_role_scope(
                None,
                frozenset({20, 21}),
            )
        )
        for guild_ids in (frozenset(), frozenset({20, 21})):
            with self.subTest(guild_ids=guild_ids):
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    self.app.validate_event_reminder_role_scope(12345, guild_ids)

    async def test_native_reconciliation_is_idempotent(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )

        self.assertTrue(await self.app.sync_native_event_slot(event, slot))
        persisted = self.service.slots(event.event_id)[0]
        self.assertIsNotNone(persisted.native_scheduled_event_id)
        self.assertEqual(self.guild.create_calls, 1)

        self.assertFalse(
            await self.app.sync_native_event_slot(
                event,
                persisted,
                remote_events=self.guild.remote_events,
            )
        )
        self.assertEqual(self.guild.create_calls, 1)
        self.assertEqual(self.guild.remote_events[0].edit_calls, 0)

    async def test_remote_success_before_local_save_is_reconciled_without_duplicate(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        marker = self.app.native_event_marker(event.event_id, slot.slot_id)
        remote = FakeScheduledEvent(
            900,
            name=f"{event.name}: Alien (1979)",
            start_time=slot.starts_at,
            end_time=slot.starts_at + timedelta(hours=4),
            description=f"Alien\n\nPlanned by Dogginator MediaBot. {marker}. The MediaBot schedule remains authoritative.",
            location=self.app.jellyfin.public_url,
            creator_id=777,
        )
        self.guild.remote_events.append(remote)

        self.assertTrue(
            await self.app.sync_native_event_slot(
                event,
                slot,
                remote_events=self.guild.remote_events,
            )
        )
        self.assertEqual(self.guild.create_calls, 0)
        self.assertEqual(
            self.service.slots(event.event_id)[0].native_scheduled_event_id,
            900,
        )

    async def test_native_create_is_discarded_if_event_is_cancelled_during_await(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        create_started = asyncio.Event()
        release_create = asyncio.Event()
        created = []
        original_create = self.guild.create_scheduled_event

        async def delayed_create(**values):
            create_started.set()
            await release_create.wait()
            remote = await original_create(**values)
            created.append(remote)
            return remote

        self.guild.create_scheduled_event = delayed_create
        task = asyncio.create_task(
            self.app.sync_native_event_slot(
                event,
                slot,
                remote_events=self.guild.remote_events,
            )
        )
        try:
            await asyncio.wait_for(create_started.wait(), timeout=2)
            self.service.cancel(event.event_id)
            release_create.set()
            self.assertFalse(await asyncio.wait_for(task, timeout=2))
        finally:
            release_create.set()
            self.guild.create_scheduled_event = original_create

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].deleted)
        self.assertEqual(self.guild.remote_events, [])

    async def test_native_create_is_discarded_if_event_is_reopened_during_await(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        create_started = asyncio.Event()
        release_create = asyncio.Event()
        created = []
        original_create = self.guild.create_scheduled_event

        async def delayed_create(**values):
            create_started.set()
            await release_create.wait()
            remote = await original_create(**values)
            created.append(remote)
            return remote

        self.guild.create_scheduled_event = delayed_create
        task = asyncio.create_task(
            self.app.sync_native_event_slot(
                event,
                slot,
                remote_events=self.guild.remote_events,
            )
        )
        try:
            await asyncio.wait_for(create_started.wait(), timeout=2)
            self.service.reopen(event.event_id)
            release_create.set()
            self.assertFalse(await asyncio.wait_for(task, timeout=2))
        finally:
            release_create.set()
            self.guild.create_scheduled_event = original_create

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].deleted)
        self.assertEqual(self.guild.remote_events, [])
        self.assertEqual(self.service.slots(event.event_id), ())

    async def test_reopen_cleanup_does_not_delete_a_new_schedule_generation(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        await self.app.sync_native_event_slot(
            event,
            slot,
            remote_events=self.guild.remote_events,
        )
        retired_slot = self.service.slots(event.event_id)[0]
        old_remote = self.guild.remote_events[0]

        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()
        original_fetch = self.guild.fetch_scheduled_events

        async def delayed_fetch(**values):
            fetch_started.set()
            await release_fetch.wait()
            return await original_fetch(**values)

        self.guild.fetch_scheduled_events = delayed_fetch
        reopened = self.service.reopen(event.event_id)
        cleanup = asyncio.create_task(
            self.app.remove_native_events(
                event,
                expected_slot_ids={retired_slot.slot_id},
                expected_native_event_ids={retired_slot.native_scheduled_event_id},
            )
        )
        try:
            await asyncio.wait_for(fetch_started.wait(), timeout=2)
            nomination = self.service.nominations(reopened.event_id)[0]
            new_time = retired_slot.starts_at + timedelta(hours=1)
            scheduled, slots = self.service.schedule_event(
                event_id=reopened.event_id,
                assignments=(
                    ScheduleAssignment(new_time, nomination.nomination_id),
                ),
            )
            await self.app.sync_native_event_slot(
                scheduled,
                slots[0],
                remote_events=self.guild.remote_events,
            )
            new_remote = next(
                remote for remote in self.guild.remote_events if remote is not old_remote
            )
            release_fetch.set()
            self.assertEqual(await asyncio.wait_for(cleanup, timeout=2), 1)
        finally:
            release_fetch.set()
            self.guild.fetch_scheduled_events = original_fetch

        self.assertTrue(old_remote.deleted)
        self.assertFalse(new_remote.deleted)
        self.assertIn(new_remote, self.guild.remote_events)
        self.assertEqual(new_remote.start_time, new_time)

    async def test_stale_native_edit_is_discarded_and_repaired_after_reschedule(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        await self.app.sync_native_event_slot(
            event,
            slot,
            remote_events=self.guild.remote_events,
        )
        persisted = self.service.slots(event.event_id)[0]
        remote = self.guild.remote_events[0]
        remote.description = "force a stale edit"
        edit_started = asyncio.Event()
        release_edit = asyncio.Event()
        original_edit = remote.edit

        async def delayed_edit(**values):
            edit_started.set()
            await release_edit.wait()
            return await original_edit(**values)

        remote.edit = delayed_edit
        task = asyncio.create_task(
            self.app.sync_native_event_slot(
                event,
                persisted,
                remote_events=self.guild.remote_events,
            )
        )
        new_time = persisted.starts_at + timedelta(hours=1)
        try:
            await asyncio.wait_for(edit_started.wait(), timeout=2)
            self.service.reschedule_event(
                event.event_id,
                (ScheduleAssignment(new_time, persisted.nomination_id),),
                expected_revision=event.revision,
            )
            release_edit.set()
            self.assertFalse(await asyncio.wait_for(task, timeout=2))
        finally:
            release_edit.set()
            remote.edit = original_edit

        self.assertTrue(remote.deleted)
        self.assertEqual(self.guild.remote_events, [])
        current_event = self.service.event(event.event_id)
        current_slot = self.service.slots(event.event_id)[0]
        self.assertEqual(current_slot.starts_at, new_time)

        self.assertTrue(
            await self.app.sync_native_event_slot(
                current_event,
                current_slot,
                remote_events=self.guild.remote_events,
            )
        )
        self.assertEqual(len(self.guild.remote_events), 1)
        self.assertEqual(self.guild.remote_events[0].start_time, new_time)

    async def test_native_payload_obeys_discord_field_limits(self):
        self.app.jellyfin.public_url = "https://media.example/" + ("x" * 120)
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2),
            name="E" * 100,
            title="T" * 150,
        )

        self.assertTrue(await self.app.sync_native_event_slot(event, slot))

        payload = self.guild.create_values[0]
        self.assertLessEqual(len(payload["name"]), 100)
        self.assertLessEqual(len(payload["description"]), 1000)
        self.assertGreaterEqual(len(payload["location"]), 1)
        self.assertLessEqual(len(payload["location"]), 100)
        self.assertIsNotNone(payload["start_time"].tzinfo)
        self.assertIsNotNone(payload["end_time"].tzinfo)

    async def test_marker_for_slot_one_does_not_match_slot_ten(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        self.assertEqual(slot.slot_id, 1)
        marker = self.app.native_event_marker(event.event_id, slot.slot_id)
        near_match = FakeScheduledEvent(
            901,
            name="Not MediaBot's slot",
            start_time=slot.starts_at,
            end_time=slot.starts_at + timedelta(hours=4),
            description=f"Planned elsewhere. {marker}0.",
            location="Elsewhere",
            creator_id=777,
        )
        self.guild.remote_events.append(near_match)

        self.assertTrue(
            await self.app.sync_native_event_slot(
                event, slot, remote_events=self.guild.remote_events
            )
        )

        self.assertEqual(self.guild.create_calls, 1)
        self.assertEqual(near_match.edit_calls, 0)
        self.assertNotEqual(
            self.service.slots(event.event_id)[0].native_scheduled_event_id,
            near_match.id,
        )

    async def test_marker_recovery_rejects_event_owned_by_another_user(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        marker = self.app.native_event_marker(event.event_id, slot.slot_id)
        foreign = FakeScheduledEvent(
            902,
            name="Foreign event",
            start_time=slot.starts_at,
            end_time=slot.starts_at + timedelta(hours=4),
            description=f"Foreign event. {marker}.",
            location="Elsewhere",
            creator_id=999,
        )
        self.guild.remote_events.append(foreign)

        self.assertTrue(
            await self.app.sync_native_event_slot(
                event, slot, remote_events=self.guild.remote_events
            )
        )

        self.assertEqual(self.guild.create_calls, 1)
        self.assertEqual(foreign.edit_calls, 0)
        self.assertFalse(foreign.deleted)

    async def test_native_sync_without_event_permissions_is_an_error(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        permissions = self.guild.me.guild_permissions
        permissions.administrator = False
        permissions.create_events = False
        permissions.manage_events = False

        with self.assertRaises(RuntimeError):
            await self.app.sync_native_event_slot(event, slot)

    async def test_reconciliation_fetches_native_events_once_per_guild(self):
        now = datetime.now(timezone.utc)
        self.create_schedule(now + timedelta(days=2), tmdb_id=349)
        self.create_schedule(
            now + timedelta(days=3), name="Saturday Movie Night", tmdb_id=350
        )

        await self.app.run_event_reconciliation_once(reference=now)

        self.assertEqual(self.guild.fetch_calls, 1)
        self.assertEqual(self.guild.create_calls, 2)

    async def test_rollover_cannot_complete_an_event_rescheduled_during_fetch(self):
        reference = datetime.now(timezone.utc)
        event, slot = self.create_schedule(
            reference - timedelta(hours=self.app.EVENT_COMPLETION_GRACE_HOURS + 1)
        )
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()
        original_fetch = self.guild.fetch_scheduled_events

        async def delayed_fetch(**values):
            fetch_started.set()
            await release_fetch.wait()
            return await original_fetch(**values)

        self.guild.fetch_scheduled_events = delayed_fetch
        worker = asyncio.create_task(
            self.app.run_event_reconciliation_once(reference=reference)
        )
        new_time = reference + timedelta(days=2)
        try:
            await asyncio.wait_for(fetch_started.wait(), timeout=2)
            self.service.reschedule_event(
                event.event_id,
                (ScheduleAssignment(new_time, slot.nomination_id),),
                expected_revision=event.revision,
            )
            release_fetch.set()
            result = await asyncio.wait_for(worker, timeout=2)
        finally:
            release_fetch.set()
            self.guild.fetch_scheduled_events = original_fetch

        current = self.service.event(event.event_id)
        current_slot = self.service.slots(event.event_id)[0]
        self.assertEqual(current.status, EventStatus.SCHEDULED)
        self.assertEqual(current_slot.starts_at, new_time)
        self.assertEqual(result["completed"], 0)
        self.assertFalse(result["failed"])

    async def test_past_unsynced_slots_do_not_starve_future_native_events(self):
        now = datetime.now(timezone.utc)
        event, slots = self.create_multi_slot_schedule(
            tuple(now - timedelta(days=value) for value in (5, 4, 3, 2))
            + (now + timedelta(days=2),)
        )

        await self.app.run_event_reconciliation_once(reference=now)

        future = next(slot for slot in slots if slot.starts_at > now)
        persisted = {
            slot.slot_id: slot for slot in self.service.slots(event.event_id)
        }
        self.assertEqual(self.guild.create_calls, 1)
        self.assertIsNotNone(persisted[future.slot_id].native_scheduled_event_id)

    async def test_synced_native_slots_rotate_through_bounded_reconciliation_batch(self):
        reference = datetime.now(timezone.utc)
        event, slots = self.create_multi_slot_schedule(
            tuple(reference + timedelta(days=value) for value in range(2, 7))
        )
        for slot in slots:
            await self.app.sync_native_event_slot(
                event,
                slot,
                remote_events=self.guild.remote_events,
            )
        for remote in self.guild.remote_events:
            remote.description = "drifted"

        await self.app.run_event_reconciliation_once(reference=reference)
        await self.app.run_event_reconciliation_once(
            reference=reference + timedelta(seconds=self.app.EVENT_RECONCILE_SECONDS)
        )

        self.assertEqual(len(self.guild.remote_events), 5)
        self.assertTrue(all(remote.edit_calls == 1 for remote in self.guild.remote_events))
        self.assertTrue(
            all(
                self.app.native_event_marker(event.event_id, slot.slot_id)
                in remote.description
                for slot, remote in zip(slots, self.guild.remote_events)
            )
        )

    async def test_deleted_remote_is_prioritized_before_near_start_slot_ages_out(self):
        reference = datetime.now(timezone.utc)
        event, slots = self.create_multi_slot_schedule(
            tuple(reference + timedelta(seconds=30 + value) for value in range(5))
        )
        for slot in slots:
            await self.app.sync_native_event_slot(
                event,
                slot,
                remote_events=self.guild.remote_events,
            )
        persisted = self.service.slots(event.event_id)
        ordinary_batch = self.app.native_event_reconciliation_batch(
            event,
            persisted,
            guild=self.guild,
            remote_events=self.guild.remote_events,
            reference=reference,
        )
        ordinary_ids = {slot.slot_id for slot in ordinary_batch}
        missing_slot = next(slot for slot in persisted if slot.slot_id not in ordinary_ids)
        missing_remote_id = missing_slot.native_scheduled_event_id
        self.guild.remote_events[:] = [
            remote
            for remote in self.guild.remote_events
            if int(remote.id) != int(missing_remote_id)
        ]
        channel = SimpleNamespace(send=AsyncMock())
        self.app.bot.get_channel = lambda channel_id: (
            channel if int(channel_id) == 30 else None
        )

        await self.app.run_event_reconciliation_once(reference=reference)

        repaired = {
            slot.slot_id: slot for slot in self.service.slots(event.event_id)
        }[missing_slot.slot_id]
        self.assertNotEqual(repaired.native_scheduled_event_id, missing_remote_id)
        self.assertTrue(
            any(
                self.app.native_event_marker(event.event_id, missing_slot.slot_id)
                in remote.description
                for remote in self.guild.remote_events
            )
        )

    async def test_native_reconciliation_repairs_all_authoritative_fields(self):
        event, slot = self.create_schedule(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        await self.app.sync_native_event_slot(event, slot)
        persisted = self.service.slots(event.event_id)[0]
        remote = self.guild.remote_events[0]
        marker = self.app.native_event_marker(event.event_id, persisted.slot_id)
        remote.end_time = persisted.starts_at + timedelta(minutes=5)
        remote.location = "Wrong location"
        remote.description = f"Wrong description, but still contains {marker}."

        self.assertTrue(
            await self.app.sync_native_event_slot(
                event, persisted, remote_events=self.guild.remote_events
            )
        )

        self.assertEqual(remote.edit_calls, 1)
        self.assertEqual(remote.end_time, persisted.starts_at + timedelta(hours=4))
        self.assertEqual(remote.location, self.app.jellyfin.public_url or "Discord")
        self.assertEqual(
            remote.description,
            self.app.native_event_description(event, persisted),
        )

    async def test_worker_retires_native_event_left_after_local_completion(self):
        now = datetime.now(timezone.utc)
        event, slot = self.create_schedule(now + timedelta(days=2))
        await self.app.sync_native_event_slot(event, slot)
        remote = self.guild.remote_events[0]
        self.service.complete(event.event_id)

        await self.app.run_event_reconciliation_once(reference=now)

        self.assertTrue(remote.deleted)

    async def test_worker_collapses_duplicate_native_events_for_one_slot(self):
        now = datetime.now(timezone.utc)
        event, slot = self.create_schedule(now + timedelta(days=2))
        marker = self.app.native_event_marker(event.event_id, slot.slot_id)
        values = {
            "name": "Duplicate",
            "start_time": slot.starts_at,
            "end_time": slot.starts_at + timedelta(hours=4),
            "description": f"Duplicate {marker}.",
            "location": "Discord",
            "creator_id": self.guild.me.id,
        }
        first = FakeScheduledEvent(901, **values)
        second = FakeScheduledEvent(902, **values)
        self.guild.remote_events.extend((first, second))

        await self.app.run_event_reconciliation_once(reference=now)

        self.assertFalse(first.deleted)
        self.assertTrue(second.deleted)
        self.assertEqual(self.guild.create_calls, 0)
        self.assertEqual(
            self.service.slots(event.event_id)[0].native_scheduled_event_id,
            first.id,
        )

    async def test_reminder_is_sent_once_across_worker_runs(self):
        reference = datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc)
        self.create_schedule(reference + timedelta(minutes=30))
        channel = SimpleNamespace(send=AsyncMock())
        self.app.bot.get_channel = lambda channel_id: channel if int(channel_id) == 30 else None

        first = await self.app.run_event_reconciliation_once(reference=reference)
        second = await self.app.run_event_reconciliation_once(
            reference=reference + timedelta(minutes=1)
        )

        self.assertEqual(first["reminders"], 1)
        self.assertEqual(second["reminders"], 0)
        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), {"parse": []})

    async def test_concurrent_workers_claim_a_reminder_before_sending(self):
        reference = datetime.now(timezone.utc)
        self.create_schedule(reference + timedelta(minutes=30))
        send_started = asyncio.Event()
        release_send = asyncio.Event()
        send_calls = 0

        async def send(**_kwargs):
            nonlocal send_calls
            send_calls += 1
            send_started.set()
            await release_send.wait()

        channel = SimpleNamespace(send=send)
        self.app.bot.get_channel = lambda channel_id: (
            channel if int(channel_id) == 30 else None
        )

        first = asyncio.create_task(
            self.app.run_event_reconciliation_once(reference=reference)
        )
        await asyncio.wait_for(send_started.wait(), timeout=2)
        second = asyncio.create_task(
            self.app.run_event_reconciliation_once(reference=reference)
        )
        await asyncio.sleep(0.05)
        release_send.set()
        await asyncio.gather(first, second)

        self.assertEqual(send_calls, 1)

    async def test_reschedule_during_channel_lookup_cannot_claim_stale_reminder(self):
        reference = datetime.now(timezone.utc)
        event, slot = self.create_schedule(reference + timedelta(minutes=30))
        channel = SimpleNamespace(send=AsyncMock())
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        async def fetch_channel(_channel_id):
            fetch_started.set()
            await release_fetch.wait()
            return channel

        previous_fetch_channel = self.app.bot.fetch_channel
        self.app.bot.get_channel = lambda _channel_id: None
        self.app.bot.fetch_channel = fetch_channel
        worker = asyncio.create_task(
            self.app.run_event_reconciliation_once(reference=reference)
        )
        try:
            await asyncio.wait_for(fetch_started.wait(), timeout=2)
            new_time = reference + timedelta(minutes=45)
            self.service.reschedule_event(
                event.event_id,
                (ScheduleAssignment(new_time, slot.nomination_id),),
                expected_revision=event.revision,
            )
            release_fetch.set()
            result = await asyncio.wait_for(worker, timeout=2)
        finally:
            release_fetch.set()
            self.app.bot.fetch_channel = previous_fetch_channel

        self.assertEqual(result["reminders"], 0)
        channel.send.assert_not_awaited()
        due = self.service.due_reminders(reference=reference)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].slot_id, slot.slot_id)
        self.assertEqual(due[0].starts_at, new_time)

    async def test_unmentionable_role_does_not_burn_reminder_claim(self):
        reference = datetime.now(timezone.utc)
        _event, slot = self.create_schedule(reference + timedelta(minutes=30))
        channel = SimpleNamespace(send=AsyncMock())
        self.app.bot.get_channel = lambda channel_id: (
            channel if int(channel_id) == 30 else None
        )
        self.app.EVENT_REMINDER_ROLE_ID = 888
        self.app.EVENT_REMINDER_MENTION = "<@&888>"
        self.guild.me.guild_permissions.mention_everyone = False
        self.guild.get_role = lambda role_id: SimpleNamespace(
            id=int(role_id),
            mentionable=False,
            is_default=lambda: False,
        )

        result = await self.app.run_event_reconciliation_once(reference=reference)

        self.assertTrue(result["failed"])
        channel.send.assert_not_awaited()
        self.assertEqual(
            [reminder.slot_id for reminder in self.service.due_reminders(reference)],
            [slot.slot_id],
        )

    async def test_clear_keeps_running_event_until_completion_grace(self):
        now = datetime.now(timezone.utc)
        running, _ = self.create_schedule(
            now - timedelta(minutes=1),
            name="Still Running",
            tmdb_id=351,
        )
        stale, _ = self.create_schedule(
            now - timedelta(hours=self.app.EVENT_COMPLETION_GRACE_HOURS + 1),
            name="Actually Finished",
            tmdb_id=352,
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=20),
            reply=AsyncMock(),
        )

        await self.app.event_clear.callback(ctx)

        running_after = self.service.event(running.event_id)
        stale_after = self.service.event(stale.event_id)
        self.assertEqual(running_after.status, EventStatus.SCHEDULED)
        self.assertIsNone(running_after.archived_at)
        self.assertEqual(stale_after.status, EventStatus.COMPLETED)
        self.assertIsNotNone(stale_after.archived_at)

    def test_main_shutdown_manages_event_lifecycle_watcher(self):
        self.assertIn("event_lifecycle_watcher", inspect.getsource(self.app.main))


if __name__ == "__main__":
    unittest.main()
