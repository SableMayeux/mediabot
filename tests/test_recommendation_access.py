import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.test_admin_dm import app
from tests.test_recommendation_auto import entries
from mediabot.core import database
from mediabot.services.ai_access import PermissionBoundServerAI
from mediabot.services.recommendation_auto import rank_candidates


@asynccontextmanager
async def typing():
    yield


class RecommendationAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.membership = AsyncMock(return_value=True)
        self.model = SimpleNamespace(enabled=True, chat=AsyncMock(return_value={"text": '{"order":[2,1,0]}'}))
        self.recommend = AsyncMock(return_value=None)
        self.deliver = AsyncMock()
        patches = [
            patch.object(database, "DB_PATH", str(Path(temporary.name) / "access.db")),
            patch.object(app, "allowed_chat_user", self.membership),
            patch.object(app.bot, "is_owner", AsyncMock(return_value=False)),
            patch.object(app, "local_ai", self.model),
            patch.object(app, "ratings_for_user", Mock(return_value=[])),
            patch.object(app, "configured_taste_user", AsyncMock(return_value=None)),
            patch.object(app.recommendations, "recommend", self.recommend),
            patch.object(app, "send_recommendation_batch", self.deliver),
        ]
        for replacement in patches:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.ctx = SimpleNamespace(author=SimpleNamespace(id=99), guild=SimpleNamespace(id=10),
            invoked_with="recommend", typing=typing, reply=AsyncMock())

    async def test_initial_denial_skips_provider_lookups_and_suggests_plain_recommendations(self):
        app.ai_access.set_access(99, "server", False, actor_id=42)
        await app.recommend_media.callback(self.ctx, filters="--auto")
        self.recommend.assert_not_awaited()
        app.configured_taste_user.assert_not_awaited()
        self.model.chat.assert_not_awaited()
        self.assertIn("ordinary `$recommend`", self.ctx.reply.await_args.args[0])

    async def test_plain_recommendations_still_work_without_server_ai_access(self):
        app.ai_access.set_access(99, "server", False, actor_id=42)
        await app.recommend_media.callback(self.ctx, filters="movie")
        self.recommend.assert_awaited_once()
        self.assertNotIn("ai_model", self.recommend.await_args.kwargs)
        self.membership.assert_not_awaited()
        self.model.chat.assert_not_awaited()

    async def test_revocation_during_provider_lookup_keeps_standard_ranking_without_inference(self):
        source = entries()
        async def lookup(*args, **kwargs):
            app.ai_access.set_access(99, "server", False, actor_id=42)
            ranked, status = await rank_candidates(kwargs["ai_model"], source)
            self.assertIs(ranked, source)
            self.assertIn("standard ranking retained", status)
            return SimpleNamespace(signals={"local_ai": status}, reasons={}, items=ranked)
        self.recommend.side_effect = lookup
        await app.recommend_media.callback(self.ctx, filters="--auto")
        self.model.chat.assert_not_awaited()
        self.deliver.assert_awaited_once()
        self.assertEqual(self.membership.await_count, 2)

    async def test_revocation_during_inference_discards_model_ranking(self):
        source = entries()
        async def response(*args, **kwargs):
            app.ai_access.set_access(99, "server", False, actor_id=42)
            return {"text": '{"order":[2,1,0]}'}
        self.model.chat.side_effect = response
        async def lookup(*args, **kwargs):
            ranked, status = await rank_candidates(kwargs["ai_model"], source)
            self.assertIs(ranked, source)
            self.assertIn("standard ranking retained", status)
            return SimpleNamespace(signals={"local_ai": status}, reasons={}, items=ranked)
        self.recommend.side_effect = lookup
        await app.recommend_media.callback(self.ctx, filters="--auto")
        self.model.chat.assert_awaited_once()
        self.assertEqual(self.model.chat.await_args.kwargs,
                         {"profile": "structured", "allowed_backends": ("server",)})
        self.assertEqual(self.membership.await_count, 3)
        self.deliver.assert_awaited_once()

    async def test_membership_revocation_prevents_inference_even_when_capability_remains(self):
        self.membership.side_effect = [True, False]
        source = entries()
        async def lookup(*args, **kwargs):
            ranked, status = await rank_candidates(kwargs["ai_model"], source)
            self.assertIs(ranked, source)
            return None
        self.recommend.side_effect = lookup
        await app.recommend_media.callback(self.ctx, filters="--auto")
        self.model.chat.assert_not_awaited()

    async def test_adapter_accepts_valid_ranking_only_after_both_fresh_checks(self):
        source = entries()
        authorize = AsyncMock(return_value=True)
        adapter = PermissionBoundServerAI(self.model, authorize)
        ranked, status = await rank_candidates(adapter, source)
        self.assertEqual(ranked, list(reversed(source)))
        self.assertEqual(status, "ranked 3 provider candidates")
        self.assertEqual(authorize.await_count, 2)
        self.model.enabled = False
        authorize.reset_mock()
        ranked, status = await rank_candidates(adapter, source)
        self.assertIs(ranked, source)
        authorize.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
