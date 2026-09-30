import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext.commands import CheckFailure

from mediabot.services.media_access import media_scope
from mediabot.services.group_picks import rank_movies
from mediabot.ui.personal_home import HomeView, QueryModal
from mediabot.ui.torrent_review import TorrentReviewLauncher


class PersonalAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(id=42, bot=False)
        self.guild = SimpleNamespace(id=10, fetch_member=AsyncMock(return_value=self.member))
        self.bot = SimpleNamespace(get_guild=Mock(return_value=self.guild))

    async def test_membership_is_fetched_on_every_action_and_removal_denies(self):
        user = SimpleNamespace(id=42)
        await media_scope(self.bot, user, {10}, {})
        self.guild.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'removed')
        with self.assertRaises(CheckFailure):
            await media_scope(self.bot, user, {10}, {})
        self.assertEqual(self.guild.fetch_member.await_count, 2)

    async def test_multiple_servers_require_selection_and_selected_removal_does_not_switch(self):
        user = SimpleNamespace(id=42)
        with self.assertRaisesRegex(CheckFailure, 'several'):
            await media_scope(self.bot, user, {10, 20}, {})
        self.guild.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'removed')
        with self.assertRaises(CheckFailure):
            await media_scope(self.bot, user, {10, 20}, {42: 20})

    async def test_home_rechecks_authorization_and_rejects_other_actor(self):
        authorize, dispatch = AsyncMock(), AsyncMock()
        home = HomeView(42, authorize, dispatch)
        self.addCleanup(home.stop)
        event = SimpleNamespace(user=SimpleNamespace(id=99), response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await home.interaction_check(event))
        authorize.assert_not_awaited()
        event.user.id = 42
        self.assertTrue(await home.interaction_check(event))
        authorize.side_effect = CheckFailure('Membership lost')
        self.assertFalse(await home.interaction_check(event))
        dispatch.assert_not_awaited()

    async def test_modal_rechecks_actor_before_command_dispatch(self):
        home = HomeView(42, AsyncMock(), AsyncMock())
        self.addCleanup(home.stop)
        modal = QueryModal(home, 'request', 'Request title')
        event = SimpleNamespace(user=SimpleNamespace(id=99), response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
        await modal.on_submit(event)
        home.dispatch.assert_not_awaited()
        event.response.defer.assert_not_awaited()

    async def test_persistent_review_launcher_rechecks_membership_after_restart(self):
        service = SimpleNamespace(review_request=AsyncMock())
        authorize = AsyncMock(side_effect=CheckFailure('Membership lost'))
        launcher = TorrentReviewLauncher(bot=self.bot, service=service, guild_id=None, info_hash='a'*40, authorize=authorize)
        self.addCleanup(launcher.stop)
        self.assertTrue(launcher.is_persistent())
        event = SimpleNamespace(user=SimpleNamespace(id=42), guild_id=None, response=SimpleNamespace(send_message=AsyncMock()))
        await launcher.children[0].callback(event)
        service.review_request.assert_not_awaited()


class GroupPickTests(unittest.TestCase):
    def movie(self, identity, minutes=100, genres=('Comedy',), **changes):
        return {'Type':'Movie', 'Id':identity, 'Genres':list(genres), 'RunTimeTicks':minutes*600000000, 'CommunityRating':7, **changes}

    def test_missing_virtual_unknown_runtime_and_overlong_movies_are_excluded(self):
        items = [self.movie('ok'), self.movie('missing', IsMissing=True), self.movie('virtual', IsVirtualItem=True), self.movie('unknown', minutes=0), self.movie('long', minutes=200)]
        self.assertEqual([item['Id'] for item in rank_movies(items, [], max_minutes=120)], ['ok'])

    def test_disliked_genre_penalizes_group_even_when_another_member_loves_it(self):
        profiles = [[{'genres':'Comedy', 'rating':10}], [{'genres':'Comedy', 'rating':1}]]
        result = rank_movies([self.movie('comedy'), self.movie('neutral', genres=('Drama',))], profiles)
        self.assertEqual(result[0]['Id'], 'neutral')

    def test_rankings_accept_json_genres_and_ties_are_stable(self):
        profiles = [[{'genres':'["Comedy"]','rating':10}]]
        result = rank_movies([self.movie('b'), self.movie('a'), self.movie('c', genres=('Drama',))], profiles)
        self.assertEqual([item['Id'] for item in result], ['a','b','c'])
