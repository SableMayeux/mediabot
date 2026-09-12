import asyncio
import itertools
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mediabot.services.local_ai import LocalAIError
from mediabot.services.web_search import WebSearchError
from mediabot.ui.local_ai import LocalChatView, conversation_embeds, parse_ask_options, source_embed, source_embeds


SOURCE = {"id": "S1", "title": "Example evidence", "url": "https://example.com/source",
          "text": "Retrieved fixture", "retrieved_at": "2026-09-12T12:00:00Z", "kind": "page"}


class WebAIUITests(unittest.IsolatedAsyncioTestCase):
    def view(self, *, web=True, backend_preference="auto"):
        view = LocalChatView(bot=SimpleNamespace(is_owner=AsyncMock(return_value=True)),
            service=SimpleNamespace(chat=AsyncMock(return_value={"text": "Answer [S1]", "backend": "server"}),
                                    cancel=AsyncMock()),
            web_search=SimpleNamespace(enabled=True, search=AsyncMock(return_value=[dict(SOURCE)])),
            web=web, access_policy=AsyncMock(return_value={"server": True, "desktop": False, "web": True}),
            actor_id=42, guild_id=None, backend_preference=backend_preference)
        view.message = SimpleNamespace(guild=None, edit=AsyncMock(), reply=AsyncMock())
        self.addCleanup(view.stop)
        return view

    def event(self):
        return SimpleNamespace(user=SimpleNamespace(id=42), guild_id=None,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    def test_flags_are_order_independent_and_preserve_literal_question(self):
        for value in ("--web --private Question", "--private --web Question"):
            self.assertEqual(parse_ask_options(value), ("Question", True, True, "auto"))
        self.assertEqual(parse_ask_options("Ask about --web flags"), ("Ask about --web flags", False, False, "auto"))
        self.assertEqual(parse_ask_options("-- --web is literal"), ("--web is literal", False, False, "auto"))
        for backend in ("desktop", "server"):
            for options in itertools.permutations(("--web", "--private", "--" + backend)):
                self.assertEqual(parse_ask_options(" ".join(options) + " Question"), ("Question", True, True, backend))
            self.assertEqual(parse_ask_options("--" + backend + " -- --server literal"),
                             ("--server literal", False, False, backend))
        for value in ("--foo Question", "--web --web Question", "--desktop --desktop Question",
                      "--server --server Question", "--desktop --web --server Question", "--server --desktop Question"):
            with self.assertRaises(LocalAIError):
                parse_ask_options(value)

    async def test_forced_backend_uses_singleton_despite_access_to_both_and_shows_selection(self):
        for backend, model in (("desktop", "gemma4:12b-it-qat"), ("server", "llama3.2:3b-instruct-q4_K_M")):
            view = self.view(backend_preference=backend)
            view.access_policy.return_value = {"desktop": True, "server": True, "web": True}
            view.service.chat.return_value = {"text": "Selected answer", "backend": backend, "model": model}
            await view.generate("Question")
            self.assertEqual(view.service.chat.await_args.kwargs["allowed_backends"], (backend,))
            cards = [call.kwargs["embed"] for call in view.message.edit.await_args_list if call.kwargs.get("embed")]
            self.assertTrue(all(backend.capitalize() + " only" in card.footer.text for card in cards))
            self.assertIn(backend.capitalize() + " GPU", cards[-1].footer.text)

    async def test_forced_backend_does_not_grant_permission_or_search(self):
        for backend in ("desktop", "server"):
            view = self.view(backend_preference=backend)
            view.access_policy.return_value = {"desktop": True, "server": True, "web": True, backend: False}
            await view.generate("Denied question")
            view.web_search.search.assert_not_awaited()
            view.service.chat.assert_not_awaited()
            self.assertEqual(view.history, [])
            card = [call.kwargs["embed"] for call in view.message.edit.await_args_list if call.kwargs.get("embed")][-1]
            self.assertIn(backend.capitalize() + " AI access is disabled", card.description)

    async def test_forced_desktop_permission_revoked_during_search_never_falls_back(self):
        view = self.view(backend_preference="desktop")
        view.access_policy.side_effect = [{"desktop": True, "server": True, "web": True},
                                         {"desktop": False, "server": True, "web": True}]
        await view.generate("Search question")
        view.web_search.search.assert_awaited_once()
        view.service.chat.assert_not_awaited()

    async def test_forced_desktop_late_revocation_withholds_answer_despite_server_permission(self):
        view = self.view(web=False, backend_preference="desktop")
        view.access_policy.side_effect = [{"desktop": True, "server": True}, {"desktop": False, "server": True}]
        view.service.chat.return_value = {"text": "Must not publish", "backend": "desktop", "model": "gemma4:12b-it-qat"}
        await view.generate("Question")
        self.assertEqual(view.service.chat.await_args.kwargs["allowed_backends"], ("desktop",))
        self.assertEqual(view.history, [])
        self.assertFalse(any(call.kwargs.get("embed") and call.kwargs["embed"].description == "Must not publish"
                             for call in view.message.edit.await_args_list))

    async def test_forced_desktop_refuses_unexpected_server_answer(self):
        view = self.view(web=False, backend_preference="desktop")
        view.access_policy.return_value = {"desktop": True, "server": True}
        view.service.chat.return_value = {"text": "Wrong backend", "backend": "server"}
        await view.generate("Question")
        self.assertEqual(view.history, [])
        self.assertFalse(any(call.kwargs.get("embed") and call.kwargs["embed"].description == "Wrong backend"
                             for call in view.message.edit.await_args_list))

    async def test_followup_retains_forced_desktop_and_history(self):
        view = self.view(web=False, backend_preference="desktop")
        view.access_policy.return_value = {"desktop": True, "server": True}
        view.service.chat.return_value = {"text": "Desktop answer", "backend": "desktop", "model": "gemma4:12b-it-qat"}
        following = SimpleNamespace(guild=None, edit=AsyncMock(), reply=AsyncMock())
        view.message.reply.return_value = following
        await view.generate("Original question")
        await view.generate("Followup question")
        self.assertEqual(view.service.chat.await_count, 2)
        self.assertTrue(all(call.kwargs["allowed_backends"] == ("desktop",) for call in view.service.chat.await_args_list))
        self.assertEqual(view.service.chat.await_args.args[1][0]["content"], "Original question")
        self.assertIs(view.message, following)

    async def test_default_routing_still_allows_both_permitted_backends(self):
        view = self.view(web=False)
        view.access_policy.return_value = {"desktop": True, "server": True}
        await view.generate("Automatic question")
        self.assertEqual(view.service.chat.await_args.kwargs["allowed_backends"], ("server", "desktop"))

    def test_fallback_footer_uses_controlled_reason_and_never_raw_remote_text(self):
        fallback = conversation_embeds("Question", "Answer", backend="server", fallback_reason="gpu_vram_low")[0]
        self.assertIn("Server fallback:", fallback.footer.text)
        self.assertNotIn("gpu_vram_low", fallback.footer.text)
        malicious = "@everyone secret remote diagnostic"
        fallback = conversation_embeds("Question", "Answer", backend="server", fallback_reason=malicious)[0]
        self.assertNotIn(malicious, fallback.footer.text)
        for backend, selection in (("desktop", "desktop"), ("server", "server")):
            card = conversation_embeds("Question", "Answer", backend=backend, backend_preference=selection,
                                       fallback_reason="desktop_offline")[0]
            self.assertNotIn("Server fallback:", card.footer.text)

    async def test_searches_only_current_question_and_shows_verified_sources_and_backend(self):
        view = self.view()
        view.history = [{"role": "user", "content": "Earlier private context"}]
        await view.generate("What is current?")
        view.web_search.search.assert_awaited_once_with("What is current?")
        kwargs = view.service.chat.await_args.kwargs
        self.assertEqual(kwargs["evidence"], [SOURCE])
        self.assertEqual(kwargs["allowed_backends"], ("server",))
        final = [call.kwargs["embed"] for call in view.message.edit.await_args_list if call.kwargs.get("embed")][-1]
        self.assertEqual(final.title, "Web-assisted conversation")
        self.assertIn("Server GPU", final.footer.text)
        self.assertIn("Web sources searched", final.footer.text)
        self.assertEqual(final.fields[0].value, "What is current?")
        sources = view.message.reply.await_args.kwargs["embed"]
        self.assertIn("https://example.com/source", sources.fields[0].value)
        self.assertIn(SOURCE["retrieved_at"], sources.fields[0].value)

    async def test_web_failure_and_empty_results_never_run_inference_or_claim_search_success(self):
        for result in ([], WebSearchError("Search is unavailable")):
            view = self.view()
            if isinstance(result, Exception):
                view.web_search.search.side_effect = result
            else:
                view.web_search.search.return_value = result
            await view.generate("Search fixture")
            view.service.chat.assert_not_awaited()
            view.message.reply.assert_not_awaited()
            self.assertFalse(any("Web sources searched" in call.kwargs["embed"].footer.text
                for call in view.message.edit.await_args_list if call.kwargs.get("embed")))

    async def test_disabled_or_oversized_web_search_never_leaves_bot(self):
        for prompt, enabled in (("q" * 801, True), ("fixture", False)):
            view = self.view()
            view.web_search.enabled = enabled
            await view.generate(prompt)
            view.web_search.search.assert_not_awaited()
            view.service.chat.assert_not_awaited()

    async def test_revoked_web_or_all_backends_rejected_before_search(self):
        for permissions in ({"server": True, "desktop": False, "web": False},
                            {"server": False, "desktop": False, "web": True}):
            view = self.view()
            view.access_policy.return_value = permissions
            await view.generate("Blocked")
            view.web_search.search.assert_not_awaited()
            view.service.chat.assert_not_awaited()

    async def test_permissions_rechecked_after_search_before_inference(self):
        view = self.view()
        view.access_policy.side_effect = [{"server": True, "web": True}, {"server": True, "web": False}]
        await view.generate("Revoked while searching")
        view.web_search.search.assert_awaited_once()
        view.service.chat.assert_not_awaited()

    async def test_desktop_only_eligibility_and_revocation_discard_late_response(self):
        view = self.view(web=False)
        view.service.chat.return_value = {"text": "Withheld", "backend": "desktop"}
        view.access_policy.side_effect = [{"server": False, "desktop": True}, {"server": True, "desktop": False}]
        await view.generate("Question")
        self.assertEqual(view.service.chat.await_args.kwargs["allowed_backends"], ("desktop",))
        self.assertEqual(view.history, [])
        self.assertFalse(any(call.kwargs.get("embed") and call.kwargs["embed"].description == "Withheld"
                             for call in view.message.edit.await_args_list))

    async def test_cancel_during_search_cancels_retrieval_and_never_infers(self):
        view = self.view()
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def search(query):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        view.web_search.search.side_effect = search
        task = asyncio.create_task(view.generate("Slow search"))
        await started.wait()
        await view.cancel(self.event())
        await asyncio.wait_for(task, timeout=2)
        self.assertTrue(cancelled.is_set())
        view.service.chat.assert_not_awaited()
        view.service.cancel.assert_not_awaited()
        self.assertIsNone(view.active_request)

    async def test_cancel_during_permission_lookup_does_not_start_search(self):
        view = self.view()
        started, release = asyncio.Event(), asyncio.Event()
        async def permissions():
            started.set()
            await release.wait()
            return {"server": True, "web": True}
        view.access_policy.side_effect = permissions
        task = asyncio.create_task(view.generate("Not sent"))
        await started.wait()
        await view.cancel(self.event())
        release.set()
        await asyncio.wait_for(task, timeout=2)
        view.web_search.search.assert_not_awaited()
        view.service.chat.assert_not_awaited()
        view.service.cancel.assert_not_awaited()

    def test_source_cards_reject_nonweb_links_and_label_snippets(self):
        with self.assertRaises(LocalAIError):
            source_embed([{**SOURCE, "url": "file:///etc/passwd"}])
        embed = source_embed([{**SOURCE, "kind": "snippet"}])
        self.assertIn("Search excerpt", embed.fields[0].value)
        self.assertLessEqual(len(embed), 6000)
        long_url = "https://example.com/source?q=" + "x" * 1400
        pages = source_embeds([{**SOURCE, "url": long_url}])
        self.assertEqual(pages[0].url, long_url)
        self.assertLessEqual(len(pages[0]), 6000)


if __name__ == "__main__":
    unittest.main()
