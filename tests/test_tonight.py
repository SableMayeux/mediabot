import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext.commands import CheckFailure

from mediabot.services.group_picks import (
    library_genres, load_tonight_catalog, parse_runtime, parse_tonight_options,
    rank_movies, resolve_tonight_genres,
)
from mediabot.ui.tonight import TonightView, TonightRuntimeModal, TonightFiltersModal


def movie(identity, *, genres=('Horror',), minutes=90, score=7, **extra):
    return {'Id': identity, 'Name': str(identity), 'Type': 'Movie', 'Genres': list(genres),
            'RunTimeTicks': minutes * 600000000, 'CommunityRating': score, 'ProductionYear':2000, **extra}


def interaction(identity=42, guild_id=10):
    return SimpleNamespace(user=SimpleNamespace(id=identity), guild_id=guild_id,
                           response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(), is_done=Mock(return_value=True)),
                           followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())


class TonightInputTests(unittest.TestCase):
    def test_original_horror_reproduction_and_time_aliases(self):
        for raw in ('horror --time 90', 'horror --under 90', '--runtime=1h30m horror'):
            parsed = parse_tonight_options(raw)
            self.assertEqual(parsed.max_minutes, 90)
            self.assertEqual(resolve_tonight_genres(parsed.genre_query), ('Horror',))
        self.assertEqual(resolve_tonight_genres(parse_tonight_options('horror').genre_query), ('Horror',))

    def test_mentions_multiword_genres_and_deduplication(self):
        parsed = parse_tonight_options('science fiction, thriller <@42> <@!43> <@43> --time 2h')
        self.assertEqual(parsed.participant_ids, (42, 43))
        self.assertEqual(parsed.max_minutes, 120)
        self.assertEqual(resolve_tonight_genres(parsed.genre_query), ('Science Fiction', 'Thriller'))

    def test_explicit_genre_flags_and_sci_fi_alias(self):
        parsed = parse_tonight_options('--genre "sci-fi" --genres horror,thriller')
        self.assertEqual(resolve_tonight_genres(parsed.genre_query), ('Science Fiction', 'Horror', 'Thriller'))
        self.assertEqual(resolve_tonight_genres('sci fi', ('Sci-Fi',)), ('Sci-Fi',))

    def test_library_specific_genre_names_are_resolved_without_invention(self):
        self.assertEqual(resolve_tonight_genres('dark comedy', ('Dark Comedy',)), ('Dark Comedy',))
        with self.assertRaisesRegex(ValueError, 'Unknown genre'):
            resolve_tonight_genres('horro')

    def test_invalid_flags_duplicate_times_and_missing_values_do_not_fall_back(self):
        for raw in ('--time', '--time --under 90', '--time 90 --under 120', '--time nope',
                    '--speed 90', '--time 29', '--time 301', '--time 1:99', '"horror', '--genre='):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_tonight_options(raw)

    def test_duration_forms_and_inclusive_bounds(self):
        for raw, minutes in (('30', 30), ('300m', 300), ('90 min', 90), ('2h', 120), ('1h30m', 90), ('1:30', 90)):
            self.assertEqual(parse_runtime(raw), minutes)

    def test_genre_and_runtime_are_hard_constraints(self):
        rows = [movie('comedy', genres=('Comedy',), score=10), movie('horror', minutes=90),
                movie('long', minutes=91), movie('missing', IsMissing=True),
                movie('virtual', IsVirtualItem=True), movie('unknown', minutes=0)]
        result = rank_movies(rows, [[{'genres':'Comedy', 'rating':10}]], genres=('Horror',), max_minutes=90)
        self.assertEqual([item['Id'] for item in result], ['horror'])

    def test_multiple_genres_match_any_without_widening_to_unrelated_movies(self):
        result = rank_movies([movie('h'), movie('t', genres=('Thriller',)), movie('c', genres=('Comedy',))], [], genres=('horror', 'thriller'))
        self.assertEqual({item['Id'] for item in result}, {'h', 't'})

    def test_empty_matches_stay_empty_and_genre_menu_excludes_virtual_titles(self):
        rows = [movie('c', genres=('Comedy',)), movie('v', genres=('Horror',), IsVirtualItem=True)]
        self.assertEqual(rank_movies(rows, [], genres=('Horror',)), [])
        self.assertEqual(library_genres(rows), ('Comedy',))


class TonightCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_catalog_page_is_included(self):
        provider = SimpleNamespace(catalog=AsyncMock(side_effect=[
            {'Items':[movie('a', genres=('Comedy',)),movie('b', genres=('Comedy',))], 'TotalRecordCount':3},
            {'Items':[movie('h')], 'TotalRecordCount':3}]))
        rows = await load_tonight_catalog(provider, page_size=2)
        self.assertEqual([item['Id'] for item in rank_movies(rows, [], genres=('Horror',))], ['h'])
        self.assertEqual(provider.catalog.await_args.kwargs['start_index'], 2)

    async def test_repeated_page_is_an_error_instead_of_infinite_loop_or_false_complete(self):
        provider = SimpleNamespace(catalog=AsyncMock(return_value={'Items':[movie('a')], 'TotalRecordCount':10}))
        with self.assertRaisesRegex(ValueError, 'same catalog page'):
            await load_tonight_catalog(provider, page_size=1)


class TonightUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.authorize, self.render, self.consent = AsyncMock(), AsyncMock(return_value=({'content':'Updated','embeds':[]}, [])), AsyncMock()
        self.view = TonightView(actor_id=42, guild_id=10, available_genres=('Comedy','Horror'),
                                genres=(), max_minutes=150, authorize=self.authorize, render=self.render,
                                set_consent=self.consent, watch_url=lambda identity: 'https://media.example/watch/'+identity)
        self.addCleanup(self.view.stop)

    def button(self, label):
        return next(item for item in self.view.children if getattr(item, 'label', None) == label)

    async def test_other_current_member_can_only_change_their_own_consent(self):
        event = interaction(99)
        self.view.message = SimpleNamespace(edit=AsyncMock())
        await self.button('Include my ratings').callback(event)
        self.assertEqual(self.authorize.await_count, 2)
        self.authorize.assert_awaited_with(event.user)
        self.consent.assert_awaited_once_with(99, True)
        self.render.assert_awaited_once_with((), 150)
        event.followup.send.assert_awaited_once()
        self.assertTrue(event.followup.send.await_args.kwargs['ephemeral'])
        self.view.message.edit.assert_awaited_once()

    async def test_optout_button_changes_only_clickers_preference(self):
        await self.button('Keep my ratings private').callback(interaction(99))
        self.consent.assert_awaited_once_with(99, False)

    async def test_committed_consent_is_reported_even_if_card_update_fails(self):
        self.render.side_effect = CheckFailure('A participant left')
        event = interaction(99)
        await self.button('Keep my ratings private').callback(event)
        self.consent.assert_awaited_once_with(99, False)
        self.assertIn('was saved', event.followup.send.await_args.args[0])

    async def test_other_actor_cannot_change_filters(self):
        event = interaction(99)
        await self.button('Update picks').callback(event)
        self.render.assert_not_awaited()
        self.assertIn('requester', event.response.send_message.await_args.args[0])

    async def test_revoked_membership_prevents_consent_and_refresh(self):
        self.authorize.side_effect = CheckFailure('Membership lost')
        await self.button('Include my ratings').callback(interaction())
        await self.button('Update picks').callback(interaction())
        self.consent.assert_not_awaited()
        self.render.assert_not_awaited()

    async def test_revocation_after_waiting_for_action_lock_prevents_preference_write(self):
        self.authorize.side_effect = [None, CheckFailure('Membership lost while waiting')]
        with self.assertRaises(CheckFailure):
            await self.button('Include my ratings').callback(interaction())
        self.consent.assert_not_awaited()

    async def test_wrong_transport_does_not_reuse_a_guild_card_in_a_dm(self):
        await self.button('Include my ratings').callback(interaction(guild_id=None))
        self.authorize.assert_not_awaited()
        self.consent.assert_not_awaited()

    async def test_genre_selection_updates_the_existing_message(self):
        selector = self.view.children[0]
        selector._values = ['1']  # Any, Comedy, Horror.
        event = interaction()
        await selector.callback(event)
        self.render.assert_awaited_once_with(('Horror',), 150)
        event.edit_original_response.assert_awaited_once()
        event.followup.send.assert_not_awaited()
        self.assertEqual(self.view.genres, ('Horror',))

    async def test_runtime_selection_and_custom_modal(self):
        selector = self.view.children[1]
        selector._values = ['90']
        await selector.callback(interaction())
        self.render.assert_awaited_once_with((), 90)
        custom = self.view.children[1]
        custom._values = ['custom']
        event = interaction()
        await custom.callback(event)
        self.assertIsInstance(event.response.send_modal.await_args.args[0], TonightRuntimeModal)

    async def test_open_modal_rechecks_membership_and_expiration(self):
        modal = TonightRuntimeModal(self.view)
        modal.runtime._value = '2h'
        self.authorize.side_effect = CheckFailure('Membership lost')
        await modal.on_submit(interaction())
        self.render.assert_not_awaited()
        self.view.stop()
        await modal.on_submit(interaction())
        self.render.assert_not_awaited()

    async def test_custom_filters_can_select_genres_beyond_dropdown_capacity(self):
        self.view.available_genres = tuple(f'Genre {index}' for index in range(30))
        modal = TonightFiltersModal(self.view)
        modal.genres._value, modal.runtime._value = 'Genre 29, horror', '2h'
        await modal.on_submit(interaction())
        self.render.assert_awaited_once_with(('Genre 29','Horror'), 120)

    async def test_invalid_custom_genre_never_applies_or_widens_filters(self):
        modal = TonightFiltersModal(self.view)
        modal.genres._value, modal.runtime._value = 'horro', '90'
        event = interaction()
        await modal.on_submit(event)
        self.render.assert_not_awaited()
        self.assertIn('Unknown genre', event.response.send_message.await_args.args[0])

    async def test_timeout_disables_actions_but_preserves_watch_links(self):
        self.view.rebuild([movie('h')])
        self.view.message = SimpleNamespace(edit=AsyncMock())
        await self.view.on_timeout()
        self.assertTrue(all(item.disabled for item in self.view.children if not getattr(item,'url',None)))
        self.assertFalse(self.button('Watch pick 1').disabled)
        self.view.message.edit.assert_awaited_once()


class TonightConsentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import app
        self.app = app
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        replacement = patch.object(app, 'DB_PATH', str(Path(temporary.name)/'consent.db'))
        replacement.start()
        self.addCleanup(replacement.stop)
        self.ratings = Mock(side_effect=lambda identity: [{'genres':'Horror','rating':identity}])
        replacement = patch.object(app, 'ratings_for_user', self.ratings)
        replacement.start()
        self.addCleanup(replacement.stop)

    async def test_revocation_is_observed_on_next_refresh_without_cached_profiles(self):
        await self.app.set_tonight_consent(99, True)
        self.app.tonight_profiles({42,99}, 42)
        self.assertEqual([call.args[0] for call in self.ratings.call_args_list], [42,99])
        self.ratings.reset_mock()
        await self.app.set_tonight_consent(99, False)
        self.app.tonight_profiles({42,99}, 42)
        self.ratings.assert_called_once_with(42)

    async def test_unmentioned_opted_in_users_are_never_read(self):
        await self.app.set_tonight_consent(123, True)
        self.app.tonight_profiles({42,99}, 42)
        self.ratings.assert_called_once_with(42)


class TonightCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import app
        self.app = app
        self.catalog = [movie('h'), movie('c', genres=('Comedy',), score=10)]
        self.guild = SimpleNamespace(id=10, fetch_member=AsyncMock(side_effect=lambda identity: SimpleNamespace(id=identity,bot=False)))
        self.provider = SimpleNamespace(enabled=True, catalog=AsyncMock(return_value={'Items':self.catalog, 'TotalRecordCount':2}),
                                        watch_url=lambda identity:'https://media.example/watch/'+identity, _tmdb_id=lambda _:None)
        @asynccontextmanager
        async def typing():
            yield
        self.ctx = SimpleNamespace(author=SimpleNamespace(id=42), guild=self.guild, reply=AsyncMock(), typing=typing)
        for replacement in (patch.object(app, 'media_scope', AsyncMock(return_value=(self.guild,SimpleNamespace(id=42)))),
                            patch.object(app, 'jellyfin', self.provider), patch.object(app, 'tonight_profiles', Mock(return_value=[]))):
            replacement.start()
            self.addCleanup(replacement.stop)

    async def run_command(self, options):
        await self.app.tonight.callback(self.ctx, options=options)
        view = self.ctx.reply.await_args.kwargs.get('view')
        if view:
            self.addCleanup(view.stop)
        return self.ctx.reply.await_args

    async def test_original_user_command_produces_horror_card_and_controls(self):
        result = await self.run_command('horror --time 90')
        self.assertIn('Horror', result.kwargs['content'])
        self.assertEqual([embed.title for embed in result.kwargs['embeds']], ['h (2000)'])
        self.assertIsInstance(result.kwargs['view'], TonightView)
        self.assertIn('Include my ratings', [getattr(item,'label',None) for item in result.kwargs['view'].children])

    async def test_invalid_time_does_not_query_provider(self):
        await self.run_command('horror --time nope')
        self.provider.catalog.assert_not_awaited()

    async def test_absent_participant_prevents_library_lookup(self):
        self.guild.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404,reason='missing'), 'removed')
        with self.assertRaises(CheckFailure):
            await self.run_command('horror <@99>')
        self.provider.catalog.assert_not_awaited()

    async def test_no_matches_still_returns_filter_controls_without_unrelated_picks(self):
        result = await self.run_command('horror --time 30')
        self.assertEqual(result.kwargs['embeds'], [])
        self.assertIn('No playable movies', result.kwargs['content'])
        self.assertIsInstance(result.kwargs['view'], TonightView)

    async def test_large_provider_text_stays_inside_discord_message_limits(self):
        rows = [movie(str(index), Overview='x'*10000, Name='n'*1000, genres=('Horror', 'g'*3000)) for index in range(3)]
        payload, picks = self.app.tonight_payload(rows, [], genres=('Horror',), max_minutes=150)
        self.assertEqual(len(picks), 3)
        self.assertLessEqual(sum(len(embed) for embed in payload['embeds']), 6000)
        self.assertLessEqual(len(payload['content']), 2000)

    async def test_gui_join_and_leave_refresh_the_current_participant_set(self):
        result = await self.run_command('horror <@99>')
        view = result.kwargs['view']
        with patch.object(self.app, 'set_tonight_consent', AsyncMock()) as save:
            await view.set_consent(100, True)
            payload, _ = await view.render(('Horror',), 150)
            self.assertIn('Group: 3/8', payload['content'])
            self.assertEqual(self.app.tonight_profiles.call_args.args[0], {42,99,100})
            await view.set_consent(100, False)
            payload, _ = await view.render(('Horror',), 150)
            self.assertIn('Group: 2/8', payload['content'])
            self.assertEqual([call.args for call in save.await_args_list], [(100,True),(100,False)])

    async def test_full_group_cannot_add_a_ninth_person_or_save_their_consent(self):
        result = await self.run_command('horror ' + ' '.join(f'<@{identity}>' for identity in range(1,8)))
        with patch.object(self.app, 'set_tonight_consent', AsyncMock()) as save:
            with self.assertRaisesRegex(CheckFailure, 'eight people'):
                await result.kwargs['view'].set_consent(100, True)
            save.assert_not_awaited()

    async def test_dm_controls_require_current_account_link(self):
        self.ctx.guild = None
        result = await self.run_command('horror')
        with patch.object(self.app.bot, 'is_owner', AsyncMock(return_value=False)), patch.object(self.app, 'get_link', return_value=None):
            with self.assertRaisesRegex(CheckFailure, 'Seerr link'):
                await result.kwargs['view'].authorize(SimpleNamespace(id=42))
