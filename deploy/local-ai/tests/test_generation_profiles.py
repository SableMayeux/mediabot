"""Profile separation, fixed model provenance, and generation limits."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import uuid


SOURCE = Path(__file__).resolve().parents[1] / 'bin' / 'gateway.py'


def load_gateway():
    spec = importlib.util.spec_from_file_location('profile_gateway', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


G = load_gateway()
QWEN = 'qwen3.5:4b'
MESSAGES = [{'role': 'user', 'content': 'Explain a synthetic tradeoff.'}]


class Stream:
    status = 200

    def __iter__(self):
        yield json.dumps({'message': {'content': 'A complete synthetic answer.'}}).encode()
        yield json.dumps({'done': True, 'done_reason': 'length', 'eval_count': 1024}).encode()


class GenerationProfileTests(unittest.TestCase):
    def infer(self, *, profile='structured', model=None):
        connection = MagicMock()
        connection.getresponse.return_value = Stream()
        unload = MagicMock()
        with patch.object(G, 'CONVERSATION_MODEL', model or G.MODEL), \
                patch.object(G.http.client, 'HTTPConnection', side_effect=[connection, unload]) as factory, \
                patch.object(G, 'guard_state', return_value=(True, 'ready')):
            receipt = G.chat(G.Job(str(uuid.uuid4())), MESSAGES, profile=profile)
        payload = json.loads(connection.request.call_args.args[2])
        unloaded = json.loads(unload.request.call_args.args[2])
        return receipt, payload, unloaded, factory.call_args_list

    def test_legacy_calls_preserve_structured_model_prompt_limits_and_unload(self):
        receipt, payload, unloaded, connections = self.infer(model=QWEN)
        self.assertEqual(payload['model'], G.MODEL)
        self.assertEqual(payload['messages'][0], {'role': 'system', 'content': G.SYSTEM})
        self.assertEqual(payload['options']['num_predict'], 256)
        self.assertEqual(payload['options']['num_ctx'], 4096)
        self.assertNotIn('think', payload)
        self.assertEqual(connections[0].kwargs['timeout'], 65)
        self.assertEqual(unloaded, {'model': G.MODEL, 'keep_alive': 0})
        self.assertEqual(receipt['model_manifest_sha256'], G.DIGEST)
        self.assertEqual(receipt['profile'], 'structured')

    def test_conversation_uses_distinct_prompt_and_budget_without_widening_context(self):
        receipt, payload, unloaded, connections = self.infer(profile='conversation')
        self.assertEqual(payload['messages'][0]['content'], G.CONVERSATION_SYSTEM)
        self.assertNotEqual(payload['messages'][0]['content'], G.SYSTEM)
        self.assertEqual(payload['messages'][1:], MESSAGES)
        self.assertEqual(payload['options']['num_predict'], 1024)
        self.assertEqual(payload['options']['num_ctx'], 4096)
        self.assertEqual(connections[0].kwargs['timeout'], 125)
        self.assertEqual(payload['keep_alive'], 0)
        self.assertNotIn('think', payload)
        self.assertEqual(receipt['profile'], 'conversation')
        self.assertEqual(receipt['metrics']['done_reason'], 'length')
        self.assertEqual(receipt['text'], 'A complete synthetic answer.')

    def test_approved_qwen_is_conversation_only_and_reports_its_actual_pin(self):
        receipt, payload, unloaded, _ = self.infer(profile='conversation', model=QWEN)
        self.assertEqual(payload['model'], QWEN)
        self.assertIs(payload['think'], False)
        self.assertEqual(unloaded, {'model': QWEN, 'keep_alive': 0})
        self.assertEqual(receipt['model'], QWEN)
        self.assertEqual(receipt['model_manifest_sha256'], G.CONVERSATION_MODELS[QWEN]['digest'])

    def test_profile_selection_does_not_allow_options_or_caller_model_overrides(self):
        body = {'request_id': str(uuid.uuid4()), 'messages': MESSAGES}
        for profile in ('structured', 'conversation'):
            self.assertEqual(G.validate({**body, 'profile': profile}), (body['request_id'], MESSAGES))
        for change in ({'profile': None}, {'profile': []}, {'profile': {}}, {'profile': 'unbounded'},
                       {'profile': 'conversation', 'model': QWEN},
                       {'profile': 'conversation', 'options': {'num_predict': -1}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                G.validate({**body, **change})
        with self.assertRaises(ValueError):
            G.validate({**body, 'profile': 'conversation', 'messages': [{'role': 'user', 'content': 'x' * 3001}]})

    def test_unsupported_configured_model_fails_before_serving_requests(self):
        for value in ('unapproved:model', '', 'false', 'qwen3.5:4b ', True, None, []):
            with self.subTest(value=value), patch.object(os, 'environ', {'LOCAL_AI_CONVERSATION_MODEL': value}):
                with self.assertRaisesRegex(ValueError, 'Unsupported configured conversation model'):
                    load_gateway()
        with patch.dict(os.environ, {'LOCAL_AI_CONVERSATION_MODEL': QWEN}):
            self.assertEqual(load_gateway().CONVERSATION_MODEL, QWEN)

    def test_transport_deadline_follows_the_selected_profile(self):
        for profile, elapsed, error in (('structured', 61, 'request_timeout'),
                                        ('conversation', 61, 'model_connection_failed'),
                                        ('conversation', 121, 'request_timeout')):
            connection = MagicMock()
            connection.request.side_effect = TimeoutError()
            with self.subTest(profile=profile, elapsed=elapsed), \
                    patch.object(G.http.client, 'HTTPConnection', return_value=connection), \
                    patch.object(G.time, 'monotonic', side_effect=[0, elapsed]), \
                    patch.object(G, 'guard_state', return_value=(True, 'ready')):
                with self.assertRaisesRegex(RuntimeError, error):
                    G.chat(G.Job(str(uuid.uuid4())), MESSAGES, profile=profile)

    def test_status_keeps_legacy_model_and_reports_configured_conversation_pin(self):
        handler = object.__new__(G.Handler)
        handler.path = '/v1/status'
        handler.authorized = MagicMock(return_value=True)
        handler.send = MagicMock()
        with patch.object(G, 'CONVERSATION_MODEL', QWEN), patch.object(G, 'guard_state', return_value=(True, 'ready')):
            handler.do_GET()
        status, body = handler.send.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(body['model'], G.MODEL)
        self.assertEqual(body['conversation_model'], QWEN)
        self.assertEqual(body['conversation_model_manifest_sha256'], G.CONVERSATION_MODELS[QWEN]['digest'])
        self.assertFalse(body['tools_enabled'])


if __name__ == '__main__':
    unittest.main()
