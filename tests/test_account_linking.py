import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mediabot.providers.seerr import SeerrError, SeerrProvider, matching_users, user_display_name


class SeerrDirectoryTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, pages):
        provider = SeerrProvider(base_url='http://seerr.test', api_key='fixture-key')
        provider.request = AsyncMock(side_effect=pages)
        return provider

    async def test_user_on_second_page_is_included_even_when_server_caps_page_size(self):
        provider = self.provider([
            {'results': [{'id': 1}, {'id': 2}], 'pageInfo': {'results': 3}},
            {'results': [{'id': 3, 'jellyfinUsername': 'NewMember'}], 'pageInfo': {'results': 3}},
        ])
        users = await provider.users()
        self.assertEqual([u['id'] for u in users], [1, 2, 3])
        self.assertEqual(provider.request.await_args_list[1].kwargs['params']['skip'], 2)
        self.assertEqual(matching_users(users, 'newmember')[0]['id'], 3)

    async def test_incomplete_or_repeating_pages_fail_without_partial_directory(self):
        first = {'results': [{'id': 1}], 'pageInfo': {'results': 2}}
        for second in ({'results': [{'id': 1}], 'pageInfo': {'results': 2}},
                       {'results': [], 'pageInfo': {'results': 2}}):
            provider = self.provider([first, second])
            with self.assertRaises(SeerrError):
                await provider.users()

    async def test_legacy_list_and_empty_paginated_response_are_supported(self):
        self.assertEqual(await self.provider([[{'id': 1}]]).users(), [{'id': 1}])
        self.assertEqual(await self.provider([{'results': [], 'pageInfo': {'results': 0}}]).users(), [])

    async def test_malformed_directory_is_not_reported_as_no_accounts(self):
        for response in ({}, None, {'results': [{'id': True}]}, {'results': [{'username': 'name'}]}):
            with self.subTest(response=response), self.assertRaises(SeerrError):
                await self.provider([response]).users()

    def test_jellyfin_identity_has_correct_label_and_all_aliases_match(self):
        user = {'id': 6, 'username': None, 'plexUsername': None,
                'jellyfinUsername': 'MediaFriend', 'email': 'different@example.test'}
        self.assertEqual(user_display_name(user), 'MediaFriend')
        for value in ('mediafriend', ' MEDIAFRIEND ', 'different@example.test', '6'):
            self.assertEqual(matching_users([user], value), [user])
        self.assertEqual(matching_users([user], 'media'), [])
        self.assertEqual(matching_users([user], ''), [])

    def test_duplicate_aliases_are_returned_as_ambiguous_not_guessed(self):
        users = [{'id': 1, 'username': 'same'}, {'id': 2, 'jellyfinUsername': 'SAME'}]
        self.assertEqual(len(matching_users(users, 'same')), 2)


class AdminAccountOutputTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('DISCORD_TOKEN', 'fixture-token')
        os.environ.setdefault('SEERR_API_KEY', 'fixture-key')
        os.environ.setdefault('DB_PATH', str(Path(tempfile.gettempdir()) / 'mediabot-link-tests.db'))
        os.environ.setdefault('LOG_PATH', str(Path(tempfile.gettempdir()) / 'mediabot-link-tests.log'))
        import app
        cls.app = app

    async def test_full_directory_is_private_and_no_user_disappears_at_discord_limit(self):
        users = [{'id': i, 'jellyfinUsername': 'Friend-' + str(i) + 'x' * 80} for i in range(1, 101)]
        ctx = SimpleNamespace(guild=SimpleNamespace(id=10), reply=AsyncMock())
        private = AsyncMock(return_value=True)
        with patch.object(self.app.seerr, 'users', AsyncMock(return_value=users)), patch.object(self.app, 'send_private_output', private):
            await self.app.admin_users.callback(ctx)
        texts = [call.kwargs['content'] for call in private.await_args_list]
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(text) <= 2000 for text in texts))
        combined = '\n'.join(texts)
        for user in users:
            self.assertIn(f"`{user['id']}` - {user['jellyfinUsername']}", combined)
        ctx.reply.assert_awaited_once()
        self.assertNotIn('Friend-', ctx.reply.await_args.args[0])

    async def test_missing_seerr_identity_gives_import_instruction_without_writing_link(self):
        ctx = SimpleNamespace(guild=None, reply=AsyncMock())
        private = AsyncMock(return_value=True)
        with patch.object(self.app, 'resolve_admin_member', AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch.object(self.app.seerr, 'users', AsyncMock(return_value=[])), \
             patch.object(self.app, 'set_link') as persist, patch.object(self.app, 'send_private_output', private):
            await self.app.admin_link.callback(ctx, '99', seerr_username='MissingMember')
        persist.assert_not_called()
        self.assertIn('Import Jellyfin Users', private.await_args.kwargs['content'])
        self.assertIn('No link was changed', private.await_args.kwargs['content'])

    async def test_jellyfin_alias_links_canonical_identity(self):
        ctx = SimpleNamespace(guild=None, reply=AsyncMock())
        user = {'id': 6, 'jellyfinUsername': 'MediaFriend', 'email': 'other@example.test'}
        member = SimpleNamespace(id=99)
        with patch.object(self.app, 'resolve_admin_member', AsyncMock(return_value=member)), \
             patch.object(self.app.seerr, 'users', AsyncMock(return_value=[user])), \
             patch.object(self.app, 'set_link') as persist, \
             patch.object(self.app, 'send_private_output', AsyncMock(return_value=True)):
            await self.app.admin_link.callback(ctx, '99', seerr_username='mediafriend')
        persist.assert_called_once_with(99, str(member), 6, 'MediaFriend')

    async def test_ambiguous_seerr_names_never_write_a_link(self):
        ctx = SimpleNamespace(guild=None, reply=AsyncMock())
        users = [{'id': 1, 'username': 'same'}, {'id': 2, 'jellyfinUsername': 'SAME'}]
        with patch.object(self.app, 'resolve_admin_member', AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch.object(self.app.seerr, 'users', AsyncMock(return_value=users)), \
             patch.object(self.app, 'set_link') as persist:
            await self.app.admin_link.callback(ctx, '99', seerr_username='same')
        persist.assert_not_called()
        self.assertIn('refuse to guess', ctx.reply.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
