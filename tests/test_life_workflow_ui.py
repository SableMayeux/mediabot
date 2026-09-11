import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from mediabot.services.life_workflow import LifeWorkflowError
from mediabot.ui.life_workflow import LifeLauncher, LifeView, PromotionModal, ProposalView, parse_local_time, time_label


CAPTURE = {"id": "91395e62-b056-4918-890b-81aa3669ca3d", "title": "Private thought", "created_at": "2026-09-10T20:00:00Z"}


def event(user=42, guild=10):
    return SimpleNamespace(user=SimpleNamespace(id=user), guild_id=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())


class LocalTimeTests(unittest.TestCase):
    def setUp(self):
        configured_zone = patch("mediabot.ui.life_workflow.LOCAL_ZONE", "America/Denver")
        configured_zone.start()
        self.addCleanup(configured_zone.stop)

    def test_native_date_and_floating_time_do_not_invent_timezone(self):
        self.assertEqual(time_label("2026-09-12"), "2026-09-12")
        self.assertEqual(time_label("2026-09-12T14:30:00"), "2026-09-12 14:30 (time zone unspecified)")
        self.assertEqual(time_label("2026-09-12T20:30:00Z"), "2026-09-12 14:30 MDT")

    def test_dst_gap_and_fold_require_unambiguous_input(self):
        for value in ("2026-03-08 02:30", "2026-11-01 01:30", "2026-09-12"):
            with self.assertRaises(ValueError):
                parse_local_time(value)
        self.assertTrue(parse_local_time("2026-09-12 14:30").endswith("-06:00"))
        self.assertTrue(parse_local_time("2026-11-01T01:30-07:00").endswith("-07:00"))
        self.assertIsNone(parse_local_time(""))


class LifeUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        configured_zone = patch("mediabot.ui.life_workflow.LOCAL_ZONE", "America/Denver")
        configured_zone.start()
        self.addCleanup(configured_zone.stop)

    def opts(self, owner=True):
        return dict(bot=SimpleNamespace(is_owner=AsyncMock(return_value=owner)),
            service=SimpleNamespace(request=AsyncMock()), actor_id=42, guild_id=10)

    def keep(self, view):
        self.addCleanup(view.stop)
        return view

    async def test_launcher_rejects_foreign_actor_guild_and_lost_ownership_before_read(self):
        for user, guild, owner in ((99, 10, True), (42, 99, True), (42, 10, False)):
            view = self.keep(LifeLauncher(**self.opts(owner)))
            interaction = event(user, guild)
            await view.open(interaction)
            view.service.request.assert_not_awaited()
            self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_capture_launcher_never_fetches_private_content_for_nonowner(self):
        view = self.keep(LifeLauncher(capture=CAPTURE, **self.opts()))
        self.assertNotIn(CAPTURE["title"], str([item.label for item in view.children]))
        await view.open(event(99))
        view.service.request.assert_not_awaited()

    async def test_promotion_modal_only_proposes_until_explicit_confirmation(self):
        view = self.keep(LifeView(capture=CAPTURE, **self.opts()))
        modal = PromotionModal(owner_view=view, capture=CAPTURE)
        self.addCleanup(modal.stop)
        modal.name._value = "Call mechanic"
        modal.due._value = "2026-09-12 14:30"
        interaction = event()
        await modal.on_submit(interaction)
        view.service.request.assert_not_awaited()
        proposal = self.keep(interaction.response.send_message.await_args.kwargs["view"])
        self.assertEqual(proposal.fields["capture_id"], CAPTURE["id"])
        self.assertEqual(proposal.fields["title"], "Call mechanic")
        self.assertEqual(proposal.fields["due_at"], "2026-09-12T14:30:00-06:00")
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.assertIn("not an alert", str(proposal.embed().to_dict()))

    async def test_event_cannot_be_created_with_end_before_start(self):
        view = self.keep(LifeView(capture=CAPTURE, **self.opts()))
        modal = PromotionModal(owner_view=view, capture=CAPTURE, event=True)
        self.addCleanup(modal.stop)
        modal.name._value = "Appointment"
        modal.start._value = "2026-09-12 15:00"
        modal.end._value = "2026-09-12 14:00"
        modal.reminder._value = "30"
        interaction = event()
        await modal.on_submit(interaction)
        self.assertNotIn("view", interaction.response.send_message.await_args.kwargs)
        view.service.request.assert_not_awaited()

    async def test_ambiguous_submission_retry_keeps_exact_request_identity(self):
        view = self.keep(ProposalView(action="create_task", fields={"capture_id": CAPTURE["id"], "title": "Action", "due_at": None}, **self.opts()))
        view.service.request.side_effect = [LifeWorkflowError("Unconfirmed"), {"task": {"title": "Action"}, "receipt": {}}]
        await view.confirm(event())
        self.assertFalse(view.completed)
        await view.confirm(event())
        calls = view.service.request.await_args_list
        self.assertEqual(calls[0], calls[1])
        self.assertTrue(view.completed)

    async def test_concurrent_confirmations_submit_only_once(self):
        view = self.keep(ProposalView(action="create_task", fields={"capture_id": CAPTURE["id"], "title": "Action", "due_at": None}, **self.opts()))
        async def submit(*args, **kwargs):
            await asyncio.sleep(0.01)
            return {"task": {"title": "Action"}, "receipt": {}}
        view.service.request.side_effect = submit
        await asyncio.gather(view.confirm(event()), view.confirm(event()))
        view.service.request.assert_awaited_once()

    async def test_cancel_does_not_submit_an_action(self):
        view = self.keep(ProposalView(action="create_task", fields={"capture_id": CAPTURE["id"], "title": "Action"}, **self.opts()))
        await view.cancel(event())
        view.service.request.assert_not_awaited()
        self.assertTrue(view.is_finished())

    async def test_old_completion_button_cannot_target_new_selection(self):
        view = self.keep(LifeView(**self.opts()))
        view.mode = "tasks"
        view.selected = {"id": "one", "title": "First", "etag": '"1"', "status": "NEEDS-ACTION"}
        view.items = [view.selected]
        view.rebuild()
        button = next(x for x in view.children if getattr(x, "label", "") == "Mark complete")
        view.selected = {"id": "two", "title": "Second", "etag": '"2"', "status": "NEEDS-ACTION"}
        interaction = event()
        await button.callback(interaction)
        self.assertNotIn("view", interaction.response.send_message.await_args.kwargs)
        view.service.request.assert_not_awaited()

    async def test_recurring_tasks_do_not_offer_unsupported_completion(self):
        view = self.keep(LifeView(**self.opts()))
        view.mode = "tasks"
        view.selected = {"id": "one", "title": "Recurring", "etag": '"1"', "status": "NEEDS-ACTION", "recurring": True}
        view.items = [view.selected]
        view.rebuild()
        self.assertNotIn("Mark complete", [getattr(x, "label", "") for x in view.children])

    async def test_item_pages_stay_within_discord_limits(self):
        view = self.keep(LifeView(**self.opts()))
        view.items = [{**CAPTURE, "id": str(index)} for index in range(50)]
        view.rebuild()
        menu = next(x for x in view.children if isinstance(x, discord.ui.Select))
        self.assertEqual(len(menu.options), 20)
        self.assertLessEqual(len(view.children), 25)
