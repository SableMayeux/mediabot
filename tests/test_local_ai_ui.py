import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mediabot.ui.local_ai import FollowupModal, LocalChatView, append_prompt, conversation_embed, conversation_embeds


def interaction(user=42,guild=None):
 return SimpleNamespace(user=SimpleNamespace(id=user),guild_id=guild,response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock(),send_modal=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))

class LocalAIUITests(unittest.IsolatedAsyncioTestCase):
 def view(self,owner=True,guild=None):
  result=LocalChatView(bot=SimpleNamespace(is_owner=AsyncMock(return_value=owner)),service=SimpleNamespace(chat=AsyncMock(return_value={'text':'Synthetic answer'}),cancel=AsyncMock(return_value={'cancel_requested':True,'active':True})),actor_id=42,guild_id=guild)
  result.message=SimpleNamespace(guild=None,edit=AsyncMock())
  self.addCleanup(result.stop)
  return result
 async def test_denies_foreign_actor_guild_lost_owner_and_expired_controls(self):
  for user,guild,owner in ((99,None,True),(42,10,True),(42,None,False)):
   view=self.view(owner);event=interaction(user,guild)
   await view.follow(event);await view.cancel(event)
   view.service.chat.assert_not_awaited();view.service.cancel.assert_not_awaited()
   self.assertTrue(event.response.send_message.await_args.kwargs['ephemeral'])
  view=self.view();await view.on_timeout();await view.follow(interaction())
  view.service.chat.assert_not_awaited()
 async def test_public_or_missing_destination_never_infers_or_falls_back(self):
  for guild,message in ((None,None),(None,SimpleNamespace(guild=SimpleNamespace(id=10),edit=AsyncMock())),(10,SimpleNamespace(guild=None,edit=AsyncMock()))):
   view=self.view(guild=guild);view.message=message
   await view.generate('Private synthetic question')
   view.service.chat.assert_not_awaited()
 async def test_success_only_edits_private_message_with_mentions_disabled(self):
  view=self.view();await view.generate('Hello')
  self.assertEqual(view.history,[{'role':'user','content':'Hello'},{'role':'assistant','content':'Synthetic answer'}])
  edits=view.message.edit.await_args_list
  self.assertTrue(all(call.kwargs['allowed_mentions'].everyone is False for call in edits))
  self.assertTrue(any(call.kwargs.get('embed') and call.kwargs['embed'].description=='Synthetic answer' for call in edits))
 async def test_timeout_discards_late_answer_and_never_repopulates_history(self):
  view=self.view();started=asyncio.Event();release=asyncio.Event()
  async def answer(*args,**kwargs):started.set();await release.wait();return {'text':'Late private response'}
  view.service.chat.side_effect=answer
  task=asyncio.create_task(view.generate('Private question'));await started.wait()
  identity=view.active_request
  await view.on_timeout();release.set();await task
  self.assertEqual(view.history,[]);self.assertIsNone(view.active_request);self.assertTrue(view.is_finished())
  view.service.cancel.assert_awaited_once_with(identity)
  self.assertIsNone(view.message.edit.await_args.kwargs['view'])
  self.assertFalse(any(call.kwargs.get('embed') and call.kwargs['embed'].description in ('Late private response', 'Should not appear') for call in view.message.edit.await_args_list))
 async def test_public_generation_is_bound_to_actor_guild_and_channel(self):
  view=self.view(owner=False,guild=10);view.private=False;view.channel_id=20;view.authorize=AsyncMock(return_value=True)
  view.message=SimpleNamespace(guild=SimpleNamespace(id=10),channel=SimpleNamespace(id=20),edit=AsyncMock())
  await view.generate('Public question')
  view.service.chat.assert_awaited_once()
  embed=next(call.kwargs['embed'] for call in view.message.edit.await_args_list if call.kwargs.get('embed'))
  self.assertEqual(embed.fields[0].value,'Public question')
  for actor,guild,channel in ((99,10,20),(42,11,20),(42,10,21)):
   event=interaction(actor,guild);event.channel_id=channel
   self.assertFalse(await view.interaction_check(event))
  event=interaction(42,10);event.channel_id=20
  self.assertTrue(await view.interaction_check(event))
  view.authorize.return_value=False
  self.assertFalse(await view.interaction_check(event))
 async def test_public_route_refuses_a_different_channel_before_inference(self):
  view=self.view(guild=10);view.private=False;view.channel_id=20
  view.message=SimpleNamespace(guild=SimpleNamespace(id=10),channel=SimpleNamespace(id=21),edit=AsyncMock())
  await view.generate('Must not route here')
  view.service.chat.assert_not_awaited()
 async def test_separate_requesters_have_separate_history(self):
  first=self.view();second=self.view();second.actor_id=99
  await first.generate('First user text');await second.generate('Second user text')
  self.assertNotIn('First user text',str(second.service.chat.call_args))
  self.assertNotIn('Second user text',str(first.history))
 async def test_overlapping_generations_submit_once(self):
  view=self.view();started=asyncio.Event();release=asyncio.Event()
  async def answer(*args,**kwargs):started.set();await release.wait();return {'text':'First response'}
  view.service.chat.side_effect=answer
  first=asyncio.create_task(view.generate('First'));await started.wait()
  event=interaction();await view.generate('Second',event)
  view.service.chat.assert_awaited_once();self.assertTrue(event.followup.send.await_args.kwargs['ephemeral'])
  release.set();await first
 async def test_requested_cancel_discards_even_a_late_success_response(self):
  view=self.view();started=asyncio.Event();release=asyncio.Event()
  async def answer(*args,**kwargs):started.set();await release.wait();return {'text':'Should not appear'}
  view.service.chat.side_effect=answer
  task=asyncio.create_task(view.generate('Question'));await started.wait()
  identity=view.active_request;event=interaction();await view.cancel(event)
  release.set();await task
  view.service.cancel.assert_awaited_once_with(identity)
  self.assertEqual(view.history,[])
  self.assertFalse(any(call.kwargs.get('embed') and call.kwargs['embed'].description in ('Late private response', 'Should not appear') for call in view.message.edit.await_args_list))
  self.assertTrue(event.followup.send.await_args.kwargs['ephemeral'])
 async def test_old_cancel_button_cannot_cancel_new_request(self):
  view=self.view();view.active_request='old';view.rebuild()
  old_button=next(item for item in view.children if item.label=='Cancel generation')
  view.active_request='new';view.rebuild()
  await old_button.callback(interaction())
  view.service.cancel.assert_not_awaited();self.assertEqual(view.active_request,'new')
 async def test_old_followup_modal_cannot_submit_after_conversation_changed(self):
  view=self.view();modal=FollowupModal(view);self.addCleanup(modal.stop);modal.question._value='Stale question'
  await view.generate('A newer question')
  view.service.chat.reset_mock();event=interaction();await modal.on_submit(event)
  view.service.chat.assert_not_awaited();self.assertTrue(event.followup.send.await_args.kwargs['ephemeral'])
 async def test_new_topic_cannot_clear_history_while_generating(self):
  view=self.view();view.history=[{'role':'user','content':'Keep'}];view.active_request='current';view.rebuild()
  event=interaction();await view.fresh(event)
  self.assertEqual(view.history,[{'role':'user','content':'Keep'}]);event.response.send_modal.assert_not_awaited()
 async def test_modal_rechecks_owner_before_private_generation(self):
  view=self.view();modal=FollowupModal(view);self.addCleanup(modal.stop);modal.question._value='Private text'
  event=interaction(99);await modal.on_submit(event)
  view.service.chat.assert_not_awaited();self.assertTrue(event.response.send_message.await_args.kwargs['ephemeral'])
 def test_history_trim_preserves_current_question_and_original_list(self):
  history=[{'role':role,'content':'x'*500} for role in ('user','assistant')*8]
  original=[dict(item) for item in history]
  messages=append_prompt(history,'Current question')
  self.assertEqual(history,original);self.assertEqual(messages[0]['role'],'user');self.assertEqual(messages[-1]['content'],'Current question')
  self.assertLessEqual(len(messages),12);self.assertLessEqual(sum(len(item['content'].encode()) for item in messages),3000)

 async def test_full_private_question_and_embed_limit(self):
  prompt='a'*2990+' last words'
  prompt=prompt[:3000]
  embeds=conversation_embeds(prompt,'b'*3900);embed=embeds[0]
  self.assertEqual(''.join(f.value for f in embed.fields),prompt)
  self.assertEqual(''.join(page.description for page in embeds),'b'*3900)
  self.assertTrue(all(len(page)<=6000 for page in embeds))
  view=self.view();await view.generate(prompt)
  final=[c.kwargs['embed'] for c in view.message.edit.await_args_list if c.kwargs.get('embed')][-1]
  self.assertEqual(''.join(f.value for f in final.fields),prompt)

 async def test_long_answer_is_delivered_losslessly_without_mentions_and_keeps_controls(self):
  view=self.view();view.message.reply=AsyncMock()
  answer='Opening explanation.\n'+('Detailed evidence and reasoning. '*350)+'\nFinal conclusion.'
  view.service.chat.return_value={'text':answer,'metrics':{'done_reason':'stop'}}
  await view.generate('Explain the tradeoff')
  view.service.chat.assert_awaited_once()
  self.assertEqual(view.service.chat.await_args.kwargs,{'profile':'conversation'})
  first=[call.kwargs['embed'] for call in view.message.edit.await_args_list if call.kwargs.get('embed')][-1]
  pages=[first]+[call.kwargs['embed'] for call in view.message.reply.await_args_list]
  self.assertEqual(''.join(page.description for page in pages),answer)
  self.assertTrue(all(len(page)<=6000 and len(page.description)<=4096 for page in pages))
  self.assertEqual(first.fields[0].value,'Explain the tradeoff')
  self.assertTrue(all(not page.fields for page in pages[1:]))
  self.assertTrue(all(call.kwargs['allowed_mentions'].everyone is False and call.kwargs['mention_author'] is False for call in view.message.reply.await_args_list))
  self.assertIs(view.message.edit.await_args.kwargs['view'],view)
  self.assertEqual(view.history[-1]['content'],answer)

 async def test_output_limit_is_visible_on_last_page_and_legacy_limits_are_disclosed(self):
  pages=conversation_embeds('Question','x'*9000,limit_reached=True,legacy_profile=True)
  self.assertIn('Output limit reached',pages[-1].footer.text)
  self.assertNotIn('Output limit reached',pages[0].footer.text)
  self.assertTrue(all('Older gateway response limits' in page.footer.text for page in pages))
  view=self.view();view.service.chat.return_value={'text':'Generated portion','metrics':{'done_reason':'length'}}
  await view.generate('Question')
  page=[call.kwargs['embed'] for call in view.message.edit.await_args_list if call.kwargs.get('embed')][-1]
  self.assertIn('Output limit reached',page.footer.text)

 async def test_display_uses_actual_approved_response_model(self):
  view=self.view();view.service.chat.return_value={'text':'Answer','model':'qwen3.5:4b'}
  await view.generate('Question')
  page=[call.kwargs['embed'] for call in view.message.edit.await_args_list if call.kwargs.get('embed')][-1]
  self.assertTrue(page.footer.text.startswith('Qwen 3.5 4B.'))
  self.assertNotIn('Llama',page.footer.text)
  self.assertTrue(conversation_embed('Question').footer.text.startswith('Local model.'))

 async def test_malformed_optional_metrics_do_not_break_answer_delivery(self):
  for metrics in (None,[], 'unexpected', 42):
   view=self.view();view.service.chat.return_value={'text':'Answer','metrics':metrics}
   await view.generate('Question')
   page=[call.kwargs['embed'] for call in view.message.edit.await_args_list if call.kwargs.get('embed')][-1]
   self.assertEqual(page.description,'Answer')
   self.assertNotIn('Output limit reached',page.footer.text)

 async def test_failed_continuation_delivery_is_explicit_and_does_not_regenerate(self):
  import discord
  view=self.view();view.message.reply=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'closed'))
  view.service.chat.return_value={'text':'x'*8000}
  await view.generate('Question')
  view.service.chat.assert_awaited_once()
  self.assertTrue(any('displayed answer is incomplete' in (call.kwargs.get('content') or '') for call in view.message.edit.await_args_list))
  self.assertIsNone(view.active_request)

 def test_single_page_helper_rejects_silent_truncation(self):
  with self.assertRaisesRegex(ValueError,'multi-page'):
   conversation_embed('Question','x'*5000)

 async def test_followups_preserve_previous_question_and_answer(self):
  view=self.view();old=view.message
  newer=SimpleNamespace(guild=None,edit=AsyncMock())
  old.reply=AsyncMock(return_value=newer)
  await view.generate('Original question')
  count=len(old.edit.await_args_list)
  await view.generate('Followup')
  self.assertIs(view.message,newer)
  self.assertEqual([c.kwargs for c in old.edit.await_args_list[count:]],[{'view':None}])
  self.assertEqual(view.history[0]['content'],'Original question')

 async def test_failed_followup_delivery_keeps_old_turn_and_does_not_infer(self):
  import discord
  view=self.view();old=view.message
  await view.generate('Keep this')
  old.reply=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'closed'))
  view.service.chat.reset_mock();count=len(old.edit.await_args_list)
  await view.generate('Do not overwrite',interaction())
  view.service.chat.assert_not_awaited()
  self.assertTrue(all('embed' not in c.kwargs and 'content' not in c.kwargs for c in old.edit.await_args_list[count:]))

 async def test_new_topic_keeps_old_transcript_but_clears_model_context(self):
  view=self.view();old=view.message
  old.reply=AsyncMock(return_value=SimpleNamespace(guild=None,edit=AsyncMock()))
  await view.generate('Old topic')
  await view.fresh(interaction())
  await view.generate('New topic')
  self.assertEqual(view.service.chat.await_args.args[1],[{'role':'user','content':'New topic'}])
  self.assertIsNot(view.message,old)

if __name__=='__main__':unittest.main()
