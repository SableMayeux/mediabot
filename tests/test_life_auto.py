import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from mediabot.services.life_auto import auto_prompt, decode_decision, task_fields
from mediabot.services.local_ai import LocalAIError
from mediabot.services.life_workflow import LifeWorkflowError
from mediabot.ui.life_auto import promote_automatically

CAPTURE = SimpleNamespace(capture_id='91395e62-b056-4918-890b-81aa3669ca3d',
    title='Call the mechanic', created_at='2026-09-11T00:00:00Z')


class DecisionTests(unittest.TestCase):
    def test_only_exact_source_words_can_be_used(self):
        self.assertEqual(decode_decision('{"kind":"task","title":"call the mechanic"}',
            'I need to Call the mechanic.'), 'Call the mechanic')
        self.assertIsNone(decode_decision('{"kind":"note"}', 'Nice weather.'))
        for response in ('{}', '[]', 'null', '{"kind":"task","title":"Pay $500"}',
            '{"kind":"task","title":"Call the mechanic","due_at":"tomorrow"}',
            '{"kind":"note","kind":"task","title":"Call the mechanic"}',
            '```json\n{"kind":"note"}\n```', '{"kind":"task","title":""}'):
            with self.subTest(response=response), self.assertRaises(LocalAIError):
                decode_decision(response, 'Call the mechanic')

    def test_context_limit_leaves_oversize_thought_for_manual_review(self):
        with self.assertRaises(LocalAIError): auto_prompt('x' * 3000)
        self.assertLessEqual(len(auto_prompt('Call the mechanic').encode()), 3000)

    def test_stable_task_identity_and_no_inferred_scheduling(self):
        first = task_fields(CAPTURE.capture_id, CAPTURE.title)
        self.assertEqual(first, task_fields(CAPTURE.capture_id, CAPTURE.title))
        self.assertIsNone(first['due_at']); self.assertIsNone(first['remind_at'])


class AutoUITests(unittest.IsolatedAsyncioTestCase):
    def setup_case(self, output, guild=None):
        message = SimpleNamespace(edit=AsyncMock())
        ctx = SimpleNamespace(guild=guild, author=SimpleNamespace(id=42, send=AsyncMock(return_value=message)),
            reply=AsyncMock(return_value=message), send=AsyncMock())
        bot = SimpleNamespace(is_owner=AsyncMock(return_value=True))
        model = SimpleNamespace(enabled=True, chat=AsyncMock(return_value={'text':output}))
        service = SimpleNamespace(enabled=True, request=AsyncMock(return_value={}))
        return ctx, message, dict(bot=bot, model=model, service=service, capture=CAPTURE, thought='Call the mechanic')

    async def test_valid_task_is_created_once_with_source_title_and_receipt(self):
        ctx, message, kw = self.setup_case('{"kind":"task","title":"Call the mechanic"}')
        await promote_automatically(ctx, **kw)
        kw['service'].request.assert_awaited_once_with('create_task', actor_id=42, **task_fields(CAPTURE.capture_id,CAPTURE.title))
        self.assertEqual(message.edit.await_args.kwargs['embed'].title,'Task created in Nextcloud')
        message.edit.await_args.kwargs['view'].stop()

    async def test_note_invalid_json_and_model_unavailable_do_not_submit(self):
        for output in ('{"kind":"note"}', '{"kind":"task","title":"Invented task"}', 'Maybe do something'):
            ctx, message, kw = self.setup_case(output)
            await promote_automatically(ctx, **kw)
            kw['service'].request.assert_not_awaited()
            message.edit.await_args.kwargs['view'].stop()

    async def test_disabled_service_or_closed_dm_prevents_inference(self):
        ctx,message,kw=self.setup_case('{"kind":"task","title":"Call the mechanic"}')
        kw['service'].enabled=False
        await promote_automatically(ctx,**kw)
        kw['model'].chat.assert_not_awaited();kw['service'].request.assert_not_awaited()
        ctx,message,kw=self.setup_case('{"kind":"note"}',guild=SimpleNamespace(id=7))
        ctx.author.send.side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'closed')
        await promote_automatically(ctx,**kw)
        kw['model'].chat.assert_not_awaited();kw['service'].request.assert_not_awaited()
        self.assertNotIn(CAPTURE.title,str(ctx.send.call_args))

    async def test_uncertain_creation_retry_keeps_exact_fields_and_request_id(self):
        ctx,message,kw=self.setup_case('{"kind":"task","title":"Call the mechanic"}')
        kw['service'].request.side_effect=LifeWorkflowError('Unconfirmed')
        await promote_automatically(ctx,**kw)
        view=message.edit.await_args.kwargs['view'];self.addCleanup(view.stop)
        self.assertEqual(view.fields,task_fields(CAPTURE.capture_id,CAPTURE.title))
        self.assertEqual(view.guild_id,None);self.assertEqual(view.actor_id,42)
        self.assertEqual(view.children[0].label,'Retry same task')
        self.assertNotIn('Task created',str(message.edit.await_args))


if __name__=='__main__':unittest.main()
