import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from mediabot.ui.torrent_review import TorrentReviewLauncher, TorrentReviewView, size_text


REVIEW_ID = "1" * 32


def status(**changes):
    return {
        "id": REVIEW_ID, "actor_id": "42", "info_hash": "2" * 40,
        "name": "Test download", "category": "games", "phase": "ready",
        "manifest_sha256": "a" * 64, "selected_indexes": [0, 1],
        "manifest": [
            {"index": 0, "name": "small.txt", "size": 20, "selectable": True},
            {"index": 1, "name": "large.zip", "size": 100 * 1024 * 1024, "selectable": True},
        ],
        "scan_limit_bytes": 95 * 1024 * 1024,
        **changes,
    }


def interaction(user_id=42, guild_id=10, admin=False):
    return SimpleNamespace(
        guild_id=guild_id,
        user=SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(administrator=admin), send=AsyncMock()),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(), delete_original_response=AsyncMock(),
    )


class TorrentReviewUITests(unittest.IsolatedAsyncioTestCase):
    def view(self, role="owner", owner=True):
        view = TorrentReviewView(bot=SimpleNamespace(is_owner=AsyncMock(return_value=owner)),
                                 service=SimpleNamespace(review_request=AsyncMock()),
                                 actor_id=42, guild_id=10, role=role)
        view.interaction = interaction()
        view.absorb(status())
        view.rebuild()
        self.addCleanup(view.stop)
        return view

    async def test_private_review_is_bound_to_actor_guild_and_current_role(self):
        for user, guild, owner, admin in [(99, 10, True, False), (42, 99, True, False), (42, 10, False, False), (42, 10, False, True)]:
            with self.subTest(user=user, guild=guild, owner=owner, admin=admin):
                view = self.view(owner=owner)
                event = interaction(user, guild, admin)
                self.assertFalse(await view.interaction_check(event))
                self.assertTrue(event.response.send_message.await_args.kwargs["ephemeral"])
                view.service.review_request.assert_not_awaited()

    async def test_admin_can_review_but_gets_no_hold_recovery_button(self):
        view = self.view(role="admin", owner=False)
        self.assertTrue(await view.interaction_check(interaction(admin=True)))
        view.absorb(status(phase="held", recoverable_hold=True))
        view.rebuild()
        labels = [getattr(item, "label", "") for item in view.children]
        self.assertNotIn("Recover hold", labels)
        approval = next(item for item in view.children if getattr(item, "label", "") == "Approve selected download")
        self.assertTrue(approval.disabled)

    async def test_launcher_denies_unprivileged_user_before_gateway_access(self):
        service = SimpleNamespace(review_request=AsyncMock())
        launcher = TorrentReviewLauncher(bot=SimpleNamespace(is_owner=AsyncMock(return_value=False)), service=service, guild_id=10)
        self.addCleanup(launcher.stop)
        event = interaction()
        await launcher.children[0].callback(event)
        service.review_request.assert_not_awaited()
        self.assertTrue(event.response.send_message.await_args.kwargs["ephemeral"])

    async def test_pending_and_partial_scan_text_is_truthful(self):
        for value in (-1, None, float("nan"), float("inf"), "unknown"):
            self.assertEqual(size_text(value), "metadata pending")
        view = self.view()
        fields = {field.name: field.value for field in view.embed().fields}
        self.assertIn("100.0 MiB selected bytes", fields["Scan coverage"])
        self.assertIn("does not establish", fields["Scan coverage"])
        self.assertNotIn("clean", fields["Scan coverage"].lower())

    async def test_pagination_never_makes_padding_selectable(self):
        view = self.view()
        rows = [{"index": i, "name": f"file-{i}", "size": i, "selectable": i != 20} for i in range(42)]
        view.absorb(status(manifest=rows, selected_indexes=[0, 21, 41], manifest_sha256="b" * 64))
        view.page = 1
        view.rebuild()
        menu = next(item for item in view.children if isinstance(item, discord.ui.Select))
        self.assertNotIn("20", [option.value for option in menu.options])
        self.assertIn("21", [option.value for option in menu.options])
        self.assertEqual(view.selected, {0, 21, 41})

    async def test_manifest_change_resets_selection_to_new_server_selection(self):
        view = self.view()
        view.selected = {1}
        view.page = 3
        view.absorb(status(manifest_sha256="b" * 64, selected_indexes=[0]))
        self.assertEqual(view.selected, {0})
        self.assertEqual(view.page, 0)

    async def test_old_menu_cannot_change_a_new_manifest_selection(self):
        view = self.view()
        old_menu = next(item for item in view.children if isinstance(item, discord.ui.Select))
        old_menu._values = ["0"]
        view.absorb(status(manifest_sha256="b" * 64, selected_indexes=[4],
                           manifest=[{"index": 4, "name": "changed.txt", "size": 20, "selectable": True}]))
        view.rebuild()
        await old_menu.callback(interaction())
        self.assertEqual(view.selected, {4})

    async def test_old_approve_button_cannot_approve_a_new_manifest(self):
        view = self.view()
        old_button = next(item for item in view.children if getattr(item, "label", "") == "Approve selected download")
        view.absorb(status(manifest_sha256="b" * 64, selected_indexes=[4],
                           manifest=[{"index": 4, "name": "changed.txt", "size": 20, "selectable": True}]))
        view.rebuild()
        event = interaction()
        await old_button.callback(event)
        view.service.review_request.assert_not_awaited()
        self.assertIn("changed", event.followup.send.await_args.args[0])

    async def test_selection_waits_for_pending_approval_and_receipt_uses_approved_files(self):
        view = self.view()
        started, release = asyncio.Event(), asyncio.Event()
        async def gateway(action, **fields):
            self.assertEqual(action, "approve")
            self.assertEqual(fields["selected_indexes"], [0, 1])
            started.set()
            await release.wait()
            return status(phase="approved", receipt={"selected_indexes": [0, 1]})
        view.service.review_request.side_effect = gateway
        menu = next(item for item in view.children if isinstance(item, discord.ui.Select))
        menu._values = ["0"]
        event = interaction()
        approving = asyncio.create_task(view.approve(event))
        await started.wait()
        selecting = asyncio.create_task(menu.callback(interaction()))
        await asyncio.sleep(0)
        try:
            self.assertFalse(selecting.done(), "Selection changed while approval was in flight")
        finally:
            release.set()
            await asyncio.gather(approving, selecting)
        self.assertEqual(view.selected, {0, 1})
        self.assertIn("2 selected files", event.user.send.await_args.args[0])

    async def test_timeout_does_not_cancel_an_approval_in_flight(self):
        view = self.view()
        started, release = asyncio.Event(), asyncio.Event()
        actions = []
        async def gateway(action, **fields):
            actions.append(action)
            if action == "approve":
                started.set()
                await release.wait()
                return status(phase="approved", receipt={"selected_indexes": [0, 1]})
            return status(phase="cancelled")
        view.service.review_request.side_effect = gateway
        approving = asyncio.create_task(view.approve(interaction()))
        await started.wait()
        expiring = asyncio.create_task(view.on_timeout())
        await asyncio.sleep(0)
        try:
            self.assertEqual(actions, ["approve"], "Timeout cancelled before approval result was known")
        finally:
            release.set()
            await asyncio.gather(approving, expiring)
        self.assertEqual(actions, ["approve"])

    async def test_failed_dm_does_not_discard_server_approval(self):
        view = self.view()
        view.service.review_request.return_value = status(phase="approved", receipt={"selected_indexes": [0, 1]})
        event = interaction()
        event.user.send.side_effect = discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "DM closed")
        await view.approve(event)
        self.assertTrue(view.status["receipt"])
        self.assertIn("Approval is saved", event.followup.send.await_args.args[0])
        self.assertEqual(view.service.review_request.await_count, 1)


if __name__ == "__main__":
    unittest.main()
