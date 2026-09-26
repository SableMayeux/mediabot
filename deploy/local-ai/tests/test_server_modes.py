"""Persistent selection, media priority, and CPU/GPU execution boundaries."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('mode_gateway', Path(__file__).resolve().parents[1] / 'bin/gateway.py')
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)
ID = '12345678-1234-4abc-8def-123456789abc'


class ServerModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for replacement in (
            patch.object(G, 'MODE_FILE', self.directory / 'mode.json'),
            patch.object(G, 'CPU_ENABLED', True),
            patch.object(G, 'ACTIVE', None),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_mode_defaults_on_and_survives_reload_without_affecting_other_files(self):
        unrelated = self.directory / 'unrelated'
        unrelated.write_text('preserve')
        self.assertTrue(G.quality_enabled())
        receipt = G.save_quality_mode(False)
        self.assertFalse(receipt['quality_enabled'])
        self.assertFalse(G.quality_enabled())
        G.save_quality_mode(True)
        self.assertTrue(G.quality_enabled())
        self.assertEqual(unrelated.read_text(), 'preserve')
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['mode.json', 'unrelated'])
        for value in (None, 1, 'off'):
            with self.assertRaises(ValueError):
                G.save_quality_mode(value)

    def test_corrupt_state_fails_closed_instead_of_reenabling_gpu(self):
        for raw in ('{broken', '{}', '[]', '{"quality_enabled": "false"}'):
            G.MODE_FILE.write_text(raw)
            with self.assertRaisesRegex(RuntimeError, 'server_mode_unavailable'):
                G.select_server()

    def test_off_cancels_only_active_server_gpu_job(self):
        for backend, execution, cancelled in (('server', 'gpu', True),
                                              ('server', 'cpu', False),
                                              ('desktop', 'gpu', False)):
            job = G.Job(ID)
            job.backend, job.server_execution = backend, execution
            with patch.object(G, 'ACTIVE', job):
                receipt = G.save_quality_mode(False)
            self.assertEqual(receipt['active_cancel_requested'], cancelled)
            self.assertEqual(job.cancelled.is_set(), cancelled)
        job = G.Job(ID)
        with patch.object(G, 'ACTIVE', job):
            G.save_quality_mode(True)
        self.assertFalse(job.cancelled.is_set())

    def test_quality_uses_gpu_until_media_reserves_it_and_off_always_uses_cpu(self):
        with patch.object(G, 'guard_state', return_value=(True, 'ready')), \
                patch.object(G, 'cpu_runtime_ready', return_value=(True, 'ready')):
            self.assertEqual(G.select_server()['execution'], 'gpu')
            G.save_quality_mode(False)
            chosen = G.select_server()
            self.assertEqual((chosen['execution'], chosen['mode'], chosen['model']),
                             ('cpu', 'playback', G.LIGHTWEIGHT_MODEL))
        G.save_quality_mode(True)
        def media_guard(admission=False, *, cpu=False):
            return (True, 'ready') if cpu else (False, 'foreign_gpu_workload')
        with patch.object(G, 'guard_state', side_effect=media_guard), \
                patch.object(G, 'cpu_runtime_ready', return_value=(True, 'ready')):
            chosen = G.select_server()
            self.assertTrue(chosen['ready'])
            self.assertEqual(chosen['execution'], 'cpu')
            self.assertEqual(chosen['fallback_reason'], 'foreign_gpu_workload')

    def test_cpu_still_requires_fresh_monitor_and_available_ram(self):
        state = self.directory / 'state.json'
        with patch.object(G, 'STATE', state):
            for age, ram, expected in ((0, 10000, True), (0, 7000, False), (4, 10000, False)):
                state.write_text(json.dumps({'observed_at': time.time() - age,
                    'host_available_mib': ram, 'healthy': False, 'reason': 'foreign_gpu_workload'}))
                self.assertEqual(G.guard_state(admission=True, cpu=True)[0], expected)
            state.write_text('{}')
            self.assertFalse(G.guard_state(cpu=True)[0])

    def test_cpu_readiness_verifies_runtime_and_model_pin(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        entry = {'name': G.LIGHTWEIGHT_MODEL,
                 'digest': G.CONVERSATION_MODELS[G.LIGHTWEIGHT_MODEL]['digest'].removeprefix('sha256:')}
        with patch.object(G.http.client, 'HTTPConnection', return_value=connection):
            for digest, expected in ((entry['digest'], True), ('wrong', False)):
                response.read.return_value = json.dumps({'models': [{**entry, 'digest': digest}]}).encode()
                self.assertEqual(G.cpu_runtime_ready()[0], expected)
            connection.request.side_effect = ConnectionRefusedError()
            self.assertEqual(G.cpu_runtime_ready(), (False, 'cpu_runtime_unavailable'))

    def test_cpu_chat_uses_separate_runtime_zero_gpu_layers_and_unloads(self):
        connection, unload = MagicMock(), MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.__iter__.return_value = iter([json.dumps({'message': {'content': 'Answer'}, 'done': True}).encode()])
        job = G.Job(ID)
        job.server_execution, job.server_mode = 'cpu', 'playback'
        with patch.object(G.http.client, 'HTTPConnection', side_effect=[connection, unload]) as factory:
            answer = G.chat(job, [{'role': 'user', 'content': 'Question'}], profile='conversation')
        payload = json.loads(connection.request.call_args.args[2])
        self.assertEqual(payload['model'], G.LIGHTWEIGHT_MODEL)
        self.assertEqual(payload['options']['num_gpu'], 0)
        self.assertEqual(payload['options']['num_thread'], 4)
        self.assertTrue(payload['messages'][0]['content'].startswith(G.PLAYBACK_SYSTEM))
        self.assertEqual([call.args[0] for call in factory.call_args_list], ['ollama-cpu', 'ollama-cpu'])
        self.assertEqual(json.loads(unload.request.call_args.args[2])['keep_alive'], 0)
        self.assertEqual(answer['server_execution'], 'cpu')

    def test_mode_api_rejects_invalid_payload_and_confirms_persisted_change(self):
        for value, expected in (({'quality_enabled': False}, 200), ({'quality_enabled': 'off'}, 400),
                                ({'quality_enabled': False, 'model': 'arbitrary'}, 400)):
            handler = object.__new__(G.Handler)
            handler.path = '/v1/server-mode'
            raw = json.dumps(value).encode()
            handler.headers = {'Content-Length': str(len(raw))}
            handler.rfile = io.BytesIO(raw)
            handler.connection = MagicMock()
            handler.authorized = MagicMock(return_value=True)
            handler.send = MagicMock()
            handler.do_POST()
            self.assertEqual(handler.send.call_args.args[0], expected)
        self.assertFalse(G.quality_enabled())

    def test_reasoning_only_budget_exhaustion_never_publishes_an_empty_answer(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.__iter__.return_value = iter([json.dumps({
            'message': {'thinking': 'Private internal reasoning', 'content': ''},
            'done': True, 'done_reason': 'length'}).encode()])
        with patch.object(G.http.client, 'HTTPConnection', side_effect=[connection, MagicMock()]):
            with self.assertRaisesRegex(RuntimeError, '^reasoning_budget_exhausted$'):
                G.chat(G.Job(ID), [{'role': 'user', 'content': 'Question'}], profile='conversation')


if __name__ == '__main__':
    unittest.main()
