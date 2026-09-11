import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mediabot.ui.local_ai import FollowupModal, LocalChatView, append_prompt


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
  async def answer(*args):started.set();await release.wait();return {'text':'Late private response'}
  view.service.chat.side_effect=answer
  task=asyncio.create_task(view.generate('Private question'));await started.wait()
  identity=view.active_request
  await view.on_timeout();release.set();await task
  self.assertEqual(view.history,[]);self.assertIsNone(view.active_request);self.assertTrue(view.is_finished())
  view.service.cancel.assert_awaited_once_with(identity)
  self.assertIsNone(view.message.edit.await_args.kwargs['view'])
  self.assertFalse(any(call.kwargs.get('embed') for call in view.message.edit.await_args_list))
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
  async def answer(*args):started.set();await release.wait();return {'text':'First response'}
  view.service.chat.side_effect=answer
  first=asyncio.create_task(view.generate('First'));await started.wait()
  event=interaction();await view.generate('Second',event)
  view.service.chat.assert_awaited_once();self.assertTrue(event.followup.send.await_args.kwargs['ephemeral'])
  release.set();await first
 async def test_requested_cancel_discards_even_a_late_success_response(self):
  view=self.view();started=asyncio.Event();release=asyncio.Event()
  async def answer(*args):started.set();await release.wait();return {'text':'Should not appear'}
  view.service.chat.side_effect=answer
  task=asyncio.create_task(view.generate('Question'));await started.wait()
  identity=view.active_request;event=interaction();await view.cancel(event)
  release.set();await task
  view.service.cancel.assert_awaited_once_with(identity)
  self.assertEqual(view.history,[])
  self.assertFalse(any(call.kwargs.get('embed') for call in view.message.edit.await_args_list))
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

if __name__=='__main__':unittest.main()
