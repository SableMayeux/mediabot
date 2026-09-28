import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mediabot.services.local_ai import LocalAIService, LocalAIError
from mediabot.ui.local_ai import LocalChatView


class QueueUITests(unittest.IsolatedAsyncioTestCase):
    def view(self, service, actor):
        view = LocalChatView(bot=SimpleNamespace(is_owner=AsyncMock(return_value=True)),
            service=service, actor_id=actor, guild_id=None)
        view.message = SimpleNamespace(guild=None, edit=AsyncMock(), reply=AsyncMock())
        self.addCleanup(view.stop)
        return view

    async def test_real_service_queue_updates_cards_and_keeps_active_conversation_alive(self):
        service = LocalAIService()
        release, started = asyncio.Event(), asyncio.Event()
        async def infer(*args, **kwargs):
            started.set()
            await release.wait()
            return {'text': 'Complete answer.'}
        service._chat = AsyncMock(side_effect=infer)
        first, second = self.view(service, 1), self.view(service, 2)
        a = asyncio.create_task(first.generate('First'))
        await started.wait()
        b = asyncio.create_task(second.generate('Second'))
        for _ in range(20):
            await asyncio.sleep(0)
            if second.phase == 'queued':
                break
        self.assertEqual(second.phase, 'queued')
        self.assertIsNone(first.timeout)
        self.assertIsNone(first._idle_task)
        self.assertIsNone(second._idle_task)
        card = [c.kwargs['embed'] for c in second.message.edit.await_args_list if c.kwargs.get('embed')][-1]
        self.assertIn('position 1', card.description)
        self.assertEqual(card.fields[0].value, 'Second')
        self.assertTrue(next(c for c in second.children if c.label == 'Finish sooner').disabled)
        self.assertFalse(next(c for c in first.children if c.label == 'Finish sooner').disabled)
        release.set()
        await asyncio.gather(a, b)
        self.assertEqual(service._chat.await_count, 2)
        for view in (first, second):
            self.assertIsNotNone(view._idle_task)
            self.assertIsNone(view.active_request)
            self.assertEqual(view.history[-1]['content'], 'Complete answer.')

    async def test_finish_button_uses_bound_identity_and_preserves_cancel(self):
        service = SimpleNamespace(finish=AsyncMock(return_value={'finish_requested': True}))
        view = self.view(service, 42)
        view.active_request, view.phase = 'first', 'model'
        view.rebuild()
        stale = next(c for c in view.children if c.label == 'Finish sooner')
        view.active_request = 'current'
        view.rebuild()
        event = SimpleNamespace(user=SimpleNamespace(id=42), guild_id=None,
            response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))
        await stale.callback(event)
        service.finish.assert_not_awaited()
        await next(c for c in view.children if c.label == 'Finish sooner').callback(event)
        service.finish.assert_awaited_once_with('current')
        self.assertTrue(next(c for c in view.children if c.label == 'Finish sooner').disabled)
        self.assertFalse(next(c for c in view.children if c.label == 'Cancel generation').disabled)
        self.assertIsNone(view.cancel_requested)

    async def test_failed_progress_delivery_does_not_change_destination(self):
        service = LocalAIService()
        service._chat = AsyncMock(side_effect=LocalAIError('offline'))
        view = self.view(service, 42)
        await view.generate('Question')
        self.assertIsNone(view.active_request)
        self.assertIsNotNone(view._idle_task)
        self.assertFalse(service.queue.tickets)


if __name__ == '__main__':
    unittest.main()
