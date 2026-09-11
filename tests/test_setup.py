"""Installer boundaries that protect existing data and private credentials."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import warnings
from unittest.mock import patch

from scripts import setup


class ConfigurationTests(unittest.TestCase):
    def test_new_configuration_is_private_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            setup.write_environment(path, {'TOKEN': 'synthetic-secret'})
            original = path.read_bytes()
            with self.assertRaises(setup.SetupError):
                setup.write_environment(path, {'TOKEN': 'replacement'})
            self.assertEqual(path.read_bytes(), original)
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == 'nt', 'Unprivileged Windows symlink creation varies by host')
    def test_existing_symlink_target_is_not_touched(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'private'
            target.write_text('retained')
            path = Path(directory) / '.env'
            path.symlink_to(target)
            with self.assertRaises(setup.SetupError):
                setup.write_environment(path, {'TOKEN': 'replacement'})
            self.assertEqual(target.read_text(), 'retained')
            self.assertTrue(path.is_symlink())

    def test_injected_lines_and_invalid_names_do_not_create_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            for values in ({'TOKEN': 'key\nOTHER=bad'}, {'TOKEN': 'key\rsecret'},
                           {'TOKEN': 'key\x1bhidden'}, {'INVALID NAME': 'key'}):
                with self.subTest(values=list(values)):
                    with self.assertRaises(setup.SetupError):
                        setup.write_environment(path, values)
                    self.assertFalse(path.exists())

    def test_urls_reject_embedded_credentials_queries_and_container_loopback(self):
        for value in ('http://user:secret@example.test', 'http://example.test?token=secret',
                      'http://example.test#secret', 'ftp://example.test', 'http://localhost:5055',
                      'http://127.0.0.1', 'http://[::1]:5055', 'http://example.test:70000',
                      'http://example.test/path with whitespace'):
            with self.subTest(value=value):
                with self.assertRaises(setup.SetupError) as captured:
                    setup.validate_url(value)
                self.assertNotIn('secret', str(captured.exception))
        self.assertEqual(setup.validate_url('https://example.test/seerr/'), 'https://example.test/seerr')
        self.assertEqual(setup.validate_url('', required=False), '')

    def test_server_ids_deduplicate_and_reject_channel_mentions(self):
        self.assertEqual(setup.guild_ids('900000000000000001, 900000000000000001'), '900000000000000001')
        for value in ('', '0', '1e18', '<#900000000000000001>', '900000000000000001,'):
            with self.assertRaises(setup.SetupError):
                setup.guild_ids(value)

    def test_invalid_cli_project_has_argparse_error_without_traceback(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as captured:
            setup.main(['--project-name', '../../wrong'])
        self.assertEqual(captured.exception.code, 2)
        self.assertNotIn('Traceback', output.getvalue())

    def test_existing_env_is_not_read_or_prompted_by_wizard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('existing')
            with patch('builtins.input', side_effect=AssertionError('must not prompt')):
                with self.assertRaises(setup.SetupError):
                    setup.configure(root, None)
            self.assertEqual((root / '.env').read_text(), 'existing')

    def test_hidden_prompt_refuses_getpass_echo_fallback(self):
        def unavailable_terminal(*args):
            warnings.warn('Cannot control echo on the terminal.', setup.getpass.GetPassWarning)
            raise AssertionError('Echo fallback must never be reached')
        with patch.object(setup.getpass, 'getpass', side_effect=unavailable_terminal):
            with self.assertRaisesRegex(setup.SetupError, 'cannot hide credentials'):
                setup.prompt('Private token', secret=True)


class ComposeSafetyTests(unittest.TestCase):
    def test_parent_compose_selection_and_credentials_cannot_override_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup.write_environment(root / '.env', {'DISCORD_TOKEN': 'configured'})
            with patch.object(setup, 'ROOT', root), patch.dict(os.environ, {
                'DISCORD_TOKEN': 'unrelated-secret', 'COMPOSE_FILE': 'other.yaml',
                'COMPOSE_PROJECT_NAME': 'other', 'DOCKER_CONTEXT': 'selected-daemon',
            }):
                command, environment = setup.compose_context('new-instance')
            self.assertNotIn('DISCORD_TOKEN', environment)
            self.assertNotIn('COMPOSE_FILE', environment)
            self.assertNotIn('COMPOSE_PROJECT_NAME', environment)
            self.assertEqual(environment['DOCKER_CONTEXT'], 'selected-daemon')
            self.assertIn(str(root / '.env'), command)
            self.assertIn(str(root / 'compose.yaml'), command)
            self.assertEqual(command[-2:], ['--project-name', 'new-instance'])
            self.assertNotIn('configured', ' '.join(command))

    def test_old_bind_mount_is_refused_while_expected_named_volume_is_accepted(self):
        model = json.dumps({'volumes': {'mediabot_data': {'name': 'example_mediabot_data'}}})
        for mount, refused in (
                ({'Destination': '/app/data', 'Type': 'bind', 'Source': '/private/data'}, True),
                ({'Destination': '/app/data', 'Type': 'volume', 'Name': 'other_data'}, True),
                ({'Destination': '/app/data', 'Type': 'volume', 'Name': 'example_mediabot_data'}, False)):
            with self.subTest(mount=mount), patch.object(setup, 'run', side_effect=[
                'a' * 64, model, json.dumps([mount])]):
                if refused:
                    with self.assertRaises(setup.SetupError):
                        setup.reject_legacy_data_mount(['docker', 'compose'], {})
                else:
                    setup.reject_legacy_data_mount(['docker', 'compose'], {})

    def test_compose_error_never_prints_invalid_env_line(self):
        with patch.object(setup.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 1, 'TOKEN=synthetic-secret', 'invalid line TOKEN=synthetic-secret')):
            with self.assertRaises(setup.SetupError) as captured:
                setup.run(['docker', 'compose', 'config'], 'Configuration validation')
            self.assertNotIn('synthetic-secret', str(captured.exception))
            self.assertIn('Configuration validation failed', str(captured.exception))

    def test_start_builds_both_images_before_waiting_for_health(self):
        with patch.object(setup, 'prerequisites'), patch.object(setup, 'compose_context', return_value=(['docker', 'compose'], {})), \
             patch.object(setup, 'reject_legacy_data_mount'), patch.object(setup, 'run', return_value='') as command, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(['--start']), 0)
        arguments = [item.args[0] for item in command.call_args_list]
        self.assertIn(['docker', 'compose', 'build'], arguments)
        self.assertEqual(arguments[-1], ['docker', 'compose', 'up', '-d', '--wait', '--wait-timeout', '150'])

    def test_invalid_check_report_is_rejected_before_any_rows_are_printed(self):
        report = json.dumps([{'check': 'test', 'ok': True, 'detail': 'untrusted-first-row'},
                             {'check': 'invalid', 'ok': 'yes', 'detail': 'invalid'}])
        output = io.StringIO()
        with patch.object(setup, 'prerequisites'), patch.object(setup, 'compose_context', return_value=(['docker', 'compose'], {})), \
             patch.object(setup, 'reject_legacy_data_mount'), patch.object(setup, 'run', return_value=''), \
             patch.object(setup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, report, 'secret')), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(setup.main(['--check']), 1)
        self.assertNotIn('untrusted-first-row', output.getvalue())
        self.assertNotIn('secret', output.getvalue())


if __name__ == '__main__':
    unittest.main()
