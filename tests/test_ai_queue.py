import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock

from mediabot.services.ai_queue import InferenceQueue, QueueError
from mediabot.services.local_ai import LocalAIService, LocalAIError, _NotAdmitted


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_positions_and_cancelled_waiter_never_runs(self):
        queue = InferenceQueue()
        entered, release = asyncio.Event(), asyncio.Event()
        order, positions = [], []
        async def work(name):
            async def progress(phase, position):
                positions.append((name, phase, position))
            async with queue.slot(name, on_progress=progress):
                order.append(name)
                if name == 'first':
                    entered.set()
                    await release.wait()
        first = asyncio.create_task(work('first'))
        await entered.wait()
        second = asyncio.create_task(work('second'))
        third = asyncio.create_task(work('third'))
        await asyncio.sleep(0)
        self.assertEqual(order, ['first'])
        self.assertIn(('second', 'queued', 1), positions)
        self.assertIn(('third', 'queued', 2), positions)
        self.assertTrue(queue.cancel_waiting('second'))
        with self.assertRaisesRegex(QueueError, 'cancelled'):
            await second
        release.set()
        await asyncio.gather(first, third)
        self.assertEqual(order, ['first', 'third'])
        self.assertEqual(queue.tickets, [])

    async def test_limits_expiry_and_task_cancellation_release_tickets(self):
        queue = InferenceQueue(capacity=3, per_user=1, wait_seconds=.01)
        async with queue.slot('active', owner_id=1):
            with self.assertRaisesRegex(QueueError, 'two AI'):
                async with queue.slot('duplicate-owner', owner_id=1):
                    self.fail('should not run')
            with self.assertRaisesRegex(QueueError, 'expired'):
                async with queue.slot('expired', owner_id=2):
                    self.fail('should not run')
            async def wait():
                async with queue.slot('cancelled-task'):
                    self.fail('should not run')
            task = asyncio.create_task(wait())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual([t.request_id for t in queue.tickets], ['active'])
        self.assertEqual(queue.tickets, [])

    async def test_permissions_refreshed_only_when_slot_is_acquired(self):
        service = LocalAIService()
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        release, entered = asyncio.Event(), asyncio.Event()
        async def infer(identity, *args, **kwargs):
            if identity == first:
                entered.set()
                await release.wait()
            return {'text': 'synthetic'}
        service._chat = AsyncMock(side_effect=infer)
        authorize = AsyncMock(side_effect=LocalAIError('Access revoked'))
        a = asyncio.create_task(service.chat(first, []))
        await entered.wait()
        b = asyncio.create_task(service.chat(second, [], authorize=authorize))
        await asyncio.sleep(0)
        authorize.assert_not_awaited()
        release.set()
        await a
        with self.assertRaisesRegex(LocalAIError, 'Access revoked'):
            await b
        self.assertEqual(service._chat.await_count, 1)
        self.assertFalse(service.queue.tickets)

    async def test_queued_cancel_is_local_and_active_cancel_keeps_slot_until_return(self):
        service = LocalAIService()
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        entered, release = asyncio.Event(), asyncio.Event()
        async def infer(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'text': 'synthetic'}
        service._chat = AsyncMock(side_effect=infer)
        service.request = AsyncMock(return_value={'request_id': first, 'cancel_requested': True, 'active': True})
        a = asyncio.create_task(service.chat(first, []))
        await entered.wait()
        b = asyncio.create_task(service.chat(second, []))
        await asyncio.sleep(0)
        await service.cancel(second)
        service.request.assert_not_awaited()
        with self.assertRaisesRegex(LocalAIError, 'cancelled'):
            await b
        await service.cancel(first)
        self.assertEqual([t.request_id for t in service.queue.tickets], [first])
        release.set()
        await a
        self.assertFalse(service.queue.tickets)

    async def test_only_definite_busy_retries_and_cancel_during_busy_does_not_retry(self):
        service = LocalAIService()
        identity = str(uuid.uuid4())
        service._chat = AsyncMock(side_effect=[_NotAdmitted('busy'), {'text': 'done'}])
        async def wake(phase, position):
            if phase == 'queued':
                asyncio.get_running_loop().call_soon(service.queue.notify)
        result = await service.chat(identity, [], on_progress=wake)
        self.assertEqual(result['text'], 'done')
        self.assertEqual(service._chat.await_count, 2)
        service._chat = AsyncMock(side_effect=LocalAIError('disconnected'))
        with self.assertRaisesRegex(LocalAIError, 'disconnected'):
            await service.chat(identity, [])
        service._chat.assert_awaited_once()
        async def cancel(phase, position):
            if phase == 'queued':
                await service.cancel(identity)
        service._chat = AsyncMock(side_effect=_NotAdmitted('busy'))
        with self.assertRaisesRegex(LocalAIError, 'cancelled'):
            await service.chat(identity, [], on_progress=cancel)
        service._chat.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
