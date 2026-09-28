import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('finish_gateway', Path(__file__).resolve().parents[1] / 'bin/gateway.py')
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)
ID = '12345678-1234-4abc-8def-123456789abc'


class FinishTests(unittest.TestCase):
    def test_finish_restarts_only_requested_pass_without_reasoning_or_backend_change(self):
        first, unload1, second, unload2 = [MagicMock() for _ in range(4)]
        job = G.Job(ID)
        first.getresponse.return_value.status = 200
        def long_stream():
            yield json.dumps({'message': {'thinking': 'Never publish or persist this reasoning'}}).encode()
            job.finish_requested.set()
            yield json.dumps({'message': {'content': 'unfinished draft'}}).encode()
        first.getresponse.return_value.__iter__.side_effect = long_stream
        second.getresponse.return_value.status = 200
        second.getresponse.return_value.__iter__.return_value = iter([json.dumps({
            'message': {'content': 'A complete compact answer.'}, 'done': True, 'done_reason': 'stop'}).encode()])
        with patch.object(G, 'CONVERSATION_MODEL', G.QUALITY_MODEL), \
                patch.object(G, 'guard_state', return_value=(True, 'ready')), \
                patch.object(G.http.client, 'HTTPConnection', side_effect=[first, unload1, second, unload2]) as factory:
            result = G.chat(job, [{'role': 'user', 'content': 'Explain indexes'}], profile='conversation')
        original = json.loads(first.request.call_args.args[2])
        shortened = json.loads(second.request.call_args.args[2])
        self.assertTrue(original['think'])
        self.assertFalse(shortened['think'])
        self.assertEqual(original['model'], shortened['model'])
        self.assertEqual(shortened['options']['num_gpu'], 24)
        self.assertEqual(shortened['options']['num_ctx'], 8192)
        self.assertEqual(shortened['options']['num_predict'], 1536)
        self.assertLessEqual(factory.call_args_list[2].kwargs['timeout'], factory.call_args_list[0].kwargs['timeout'])
        self.assertEqual(result['text'], 'A complete compact answer.')
        self.assertTrue(result['shortened'])
        self.assertNotIn('Never publish', json.dumps(result))
        self.assertNotIn('unfinished draft', json.dumps(shortened))
        self.assertEqual([call.args[0] for call in factory.call_args_list], ['ollama'] * 4)
        for unload in (unload1, unload2):
            self.assertEqual(json.loads(unload.request.call_args.args[2])['keep_alive'], 0)

    def test_finish_api_is_identity_bound_and_never_shortens_structured_or_desktop(self):
        for backend, profile, identity, expected in (
            ('server', 'conversation', ID, True), ('server', 'structured', ID, False),
            ('desktop', 'conversation', ID, False),
            ('server', 'conversation', '22222222-2222-4222-8222-222222222222', False)):
            job = G.Job(ID)
            job.backend, job.profile = backend, profile
            handler = object.__new__(G.Handler)
            handler.path = '/v1/finish'
            raw = json.dumps({'request_id': identity}).encode()
            handler.headers = {'Content-Length': str(len(raw))}
            handler.rfile = io.BytesIO(raw)
            handler.connection = MagicMock()
            handler.authorized = MagicMock(return_value=True)
            handler.send = MagicMock()
            with patch.object(G, 'ACTIVE', job):
                handler.do_POST()
            self.assertEqual(handler.send.call_args.args[0], 200)
            self.assertEqual(handler.send.call_args.args[1]['finish_requested'], expected)
            self.assertEqual(job.finish_requested.is_set(), expected)
            self.assertFalse(job.cancelled.is_set())

    def test_resource_abort_or_cancel_never_launches_shorter_pass(self):
        for reason in ('cancelled', 'foreign_gpu_workload', 'request_timeout'):
            job = G.Job(ID)
            job.finish_requested.set()
            with patch.object(G, 'chat_pass', side_effect=RuntimeError(reason)) as infer:
                with self.assertRaisesRegex(RuntimeError, reason):
                    G.chat(job, [{'role': 'user', 'content': 'Question'}], profile='conversation')
            infer.assert_called_once()

    def test_unload_handoff_waits_for_fresh_admission_without_overriding_guard(self):
        observed = [
            {'observed_at': 99, 'healthy': True, 'admit': False},
            {'observed_at': 101, 'healthy': True, 'admit': False},
            {'observed_at': 102, 'healthy': True, 'admit': True},
        ]
        state = MagicMock()
        state.read_text.side_effect = [json.dumps(item) for item in observed]
        with patch.object(G, 'STATE', state), patch.object(G.time, 'sleep') as sleep:
            G.wait_for_release_observation(100)
        self.assertEqual(sleep.call_count, 2)
        for item in ({'observed_at': 102, 'healthy': False, 'admit': False},
                     {'observed_at': 102, 'healthy': True, 'admit': True}):
            state.read_text.side_effect = None
            state.read_text.return_value = json.dumps(item)
            with patch.object(G, 'STATE', state), patch.object(G.time, 'sleep') as sleep:
                G.wait_for_release_observation(100)
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
