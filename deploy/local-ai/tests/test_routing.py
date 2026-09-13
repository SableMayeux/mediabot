import importlib.util
import io
import json
from pathlib import Path
import unittest
import time
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location('routing_gateway', Path(__file__).resolve().parents[1] / 'bin/gateway.py')
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)
ID = '12345678-1234-4abc-8def-123456789abc'
MESSAGES = [{'role': 'user', 'content': 'What changed?'}]
EVIDENCE = [{'id': 'S1', 'title': 'Example source', 'url': 'https://example.com/',
             'text': 'A bounded piece of quoted evidence.', 'retrieved_at': '2026-09-12T00:00:00Z', 'kind': 'page'}]


class RoutingTests(unittest.TestCase):
    def test_dispatched_worker_failure_keeps_safe_reason_without_server_replay(self):
        for reason in ('request_timeout', 'gpu_vram_low', 'host_memory_low', 'gpu_monitor_failed',
                       'runtime_error', 'runtime_unavailable', 'runtime_response_invalid', 'desktop_disabled'):
            with self.subTest(reason=reason), patch.object(G, 'desktop_status', return_value={'ready': True}), \
                    patch.object(G, 'desktop_request', return_value=(503, {'error': reason})) as request, \
                    patch.object(G, 'chat') as server:
                with self.assertRaises(G.DesktopRequestFailure) as caught:
                    G.route_chat(G.Job(ID), MESSAGES, profile='conversation', allowed_backends=['server', 'desktop'])
                self.assertEqual(str(caught.exception), 'desktop_request_failed')
                self.assertEqual(caught.exception.reason, reason)
                request.assert_called_once()
                server.assert_not_called()

    def test_unknown_worker_details_are_not_forwarded_and_auth_is_distinct(self):
        for status, reason, expected in ((503, 'private prompt or credential', 'desktop_unknown_failure'),
                                         (503, {'private': 'detail'}, 'desktop_unknown_failure'),
                                         (503, None, 'desktop_unknown_failure'),
                                         (401, 'private auth detail', 'desktop_auth_failed')):
            with self.subTest(status=status, reason=reason), \
                    patch.object(G, 'desktop_request', return_value=(status, {'error': reason, 'detail': 'secret'})):
                with self.assertRaises(G.DesktopRequestFailure) as caught:
                    G.forward_desktop(G.Job(ID), MESSAGES, [])
                self.assertEqual(caught.exception.reason, expected)
                self.assertNotIn('private', str(caught.exception))

    def test_transport_timeout_tls_disconnect_and_invalid_json_have_distinct_safe_reasons(self):
        for error, category, reason in ((TimeoutError('secret'), 'desktop_request_interrupted', 'desktop_transport_timeout'),
                                        (G.ssl.SSLError('secret'), 'desktop_request_interrupted', 'desktop_tls_failed'),
                                        (ConnectionResetError('secret'), 'desktop_request_interrupted', 'desktop_transport_failed'),
                                        (G.http.client.RemoteDisconnected('secret'), 'desktop_request_interrupted', 'desktop_transport_failed'),
                                        (ValueError('private malformed response'), 'desktop_response_mismatch', 'desktop_response_mismatch')):
            with self.subTest(reason=reason), patch.object(G, 'desktop_request', side_effect=error):
                with self.assertRaises(G.DesktopRequestFailure) as caught:
                    G.forward_desktop(G.Job(ID), MESSAGES, [])
                self.assertEqual((str(caught.exception), caught.exception.reason), (category, reason))

    def test_forwarded_cancellation_keeps_cancellation_semantics(self):
        with patch.object(G, 'desktop_request', return_value=(409, {'error': 'cancelled'})):
            with self.assertRaisesRegex(RuntimeError, '^cancelled$'):
                G.forward_desktop(G.Job(ID), MESSAGES, [])
        job = G.Job(ID)
        def interrupted(*args, **kwargs):
            job.cancelled.set()
            raise TimeoutError('private transport detail')
        with patch.object(G, 'desktop_request', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, '^cancelled$'):
                G.forward_desktop(job, MESSAGES, [])

    def test_handler_returns_request_identity_and_allowlisted_failure_reason(self):
        handler = object.__new__(G.Handler)
        handler.path = '/v1/chat'
        body = json.dumps({'request_id': ID, 'messages': MESSAGES, 'profile': 'conversation', 'allowed_backends': ['desktop']}).encode()
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.connection = MagicMock()
        handler.authorized = MagicMock(return_value=True)
        handler.send = MagicMock()
        with patch.object(G, 'ACTIVE', None), patch.object(G, 'CANCELLED', {}), \
                patch.object(G, 'route_chat', side_effect=G.DesktopRequestFailure('desktop_request_failed', 'request_timeout')):
            handler.do_POST()
            self.assertIsNone(G.ACTIVE)
        handler.send.assert_called_once_with(503, {'error': 'desktop_request_failed', 'request_id': ID,
                                                   'desktop_reason': 'request_timeout'})

    def test_web_clock_is_trusted_context_and_evidence_stays_quoted(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.__iter__.return_value = iter([json.dumps({'message': {'content': 'Answer'}, 'done': True}).encode()])
        with patch.object(G.http.client, 'HTTPConnection', return_value=connection), \
                patch.object(G, 'guard_state', return_value=(True, 'ready')), \
                patch.object(G.time, 'strftime', return_value='2026-09-12'):
            G.chat(G.Job(ID), MESSAGES, profile='conversation', evidence=EVIDENCE)
        request = next(call for call in connection.request.call_args_list if call.args[1] == '/api/chat')
        sent = json.loads(request.args[2])['messages']
        self.assertTrue(sent[0]['content'].endswith('Current date (UTC): 2026-09-12.'))
        self.assertEqual([message['role'] for message in sent], ['system', 'user', 'user'])
        self.assertIn('Quoted web evidence', sent[1]['content'])

    def test_readiness_retains_only_known_desktop_reasons(self):
        valid = {'ready': False, 'model': G.DESKTOP_MODEL,
                 'model_manifest_sha256': G.DESKTOP_DIGEST, 'backend': 'desktop'}
        for reason in ('gpu_vram_low', 'foreign_gpu_workload', 'busy', 'gpu_monitor_failed'):
            with self.subTest(reason=reason), patch.object(G, 'DESKTOP_URL', 'https://x.ts.net:8445'), \
                    patch.object(G, 'desktop_request', return_value=(200, {**valid, 'reason': reason})):
                self.assertEqual(G.desktop_status()['reason'], reason)
        for reason in ('sensitive untrusted text', {}, None):
            with self.subTest(reason=reason), patch.object(G, 'DESKTOP_URL', 'https://x.ts.net:8445'), \
                    patch.object(G, 'desktop_request', return_value=(200, {**valid, 'reason': reason})):
                self.assertEqual(G.desktop_status()['reason'], 'desktop_unavailable')

    def test_desktop_auth_and_identity_failures_have_safe_reasons(self):
        for code, body, expected in ((401, {}, 'desktop_auth_failed'),
                                      (200, {'ready': True}, 'desktop_response_mismatch')):
            with self.subTest(code=code), patch.object(G, 'DESKTOP_URL', 'https://x.ts.net:8445'), \
                    patch.object(G, 'desktop_request', return_value=(code, body)):
                self.assertEqual(G.desktop_status()['reason'], expected)

    def test_auto_fallback_explains_reason_and_strict_desktop_never_runs_server(self):
        with patch.object(G, 'desktop_status', return_value={'ready': False, 'reason': 'gpu_vram_low'}), \
                patch.object(G, 'guard_state', return_value=(True, 'ready')), \
                patch.object(G, 'chat', return_value={'backend': 'server'}) as server:
            result = G.route_chat(G.Job(ID), MESSAGES, profile='conversation', allowed_backends=['server', 'desktop'])
            self.assertEqual(result['fallback_reason'], 'gpu_vram_low')
            server.reset_mock()
            with self.assertRaises(G.DesktopUnavailable) as caught:
                G.route_chat(G.Job(ID), MESSAGES, profile='conversation', allowed_backends=['desktop'])
            self.assertEqual(caught.exception.reason, 'gpu_vram_low')
            server.assert_not_called()

    def test_cancel_monitor_interrupts_socket_created_after_initial_cancel(self):
        job = G.Job(ID)
        connection = MagicMock()
        connection.sock = None
        late_socket = MagicMock()
        def delayed_dispatch(*args):
            job.cancel()
            connection.sock = late_socket
            deadline = time.monotonic() + 1
            while not late_socket.shutdown.called and time.monotonic() < deadline:
                time.sleep(0.01)
            raise ConnectionResetError()
        connection.request.side_effect = delayed_dispatch
        with patch.object(G.http.client, 'HTTPConnection', side_effect=[connection, MagicMock()]):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                G.chat(job, MESSAGES)
        late_socket.shutdown.assert_called()

    def test_healthcheck_never_waits_for_optional_desktop(self):
        handler = object.__new__(G.Handler)
        handler.path = '/v1/health'
        handler.authorized = MagicMock(return_value=True)
        handler.send = MagicMock()
        with patch.object(G, 'guard_state', return_value=(True, 'ready')), patch.object(G, 'desktop_status') as desktop:
            handler.do_GET()
        desktop.assert_not_called()
        self.assertEqual(handler.send.call_args.args[0], 200)

    def test_cancellation_between_route_selection_and_chat_never_opens_runtime(self):
        job = G.Job(ID)
        job.cancelled.set()
        with patch.object(G.http.client, 'HTTPConnection') as connection:
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                G.chat(job, MESSAGES)
        connection.assert_not_called()

    def test_allowed_desktop_precedes_server_without_requiring_server_gpu(self):
        job = G.Job(ID)
        with patch.object(G, 'desktop_status', return_value={'ready': True}), \
                patch.object(G, 'forward_desktop', return_value={'backend': 'desktop'}) as desktop, \
                patch.object(G, 'chat') as server, patch.object(G, 'guard_state') as guard:
            result = G.route_chat(job, MESSAGES, profile='conversation', evidence=EVIDENCE,
                                  allowed_backends=['server', 'desktop'])
        self.assertEqual(result['backend'], 'desktop')
        desktop.assert_called_once_with(job, MESSAGES, EVIDENCE)
        server.assert_not_called()
        guard.assert_not_called()

    def test_offline_desktop_falls_back_only_with_server_permission(self):
        for backends in (['server', 'desktop'], ['desktop']):
            with self.subTest(backends=backends), patch.object(G, 'desktop_status', return_value={'ready': False}), \
                    patch.object(G, 'guard_state', return_value=(True, 'ready')), \
                    patch.object(G, 'chat', return_value={'backend': 'server'}) as server:
                if 'server' in backends:
                    self.assertEqual(G.route_chat(G.Job(ID), MESSAGES, profile='conversation',
                                                 allowed_backends=backends)['backend'], 'server')
                    server.assert_called_once()
                else:
                    with self.assertRaisesRegex(RuntimeError, 'desktop_unavailable_no_fallback'):
                        G.route_chat(G.Job(ID), MESSAGES, profile='conversation', allowed_backends=backends)
                    server.assert_not_called()

    def test_server_only_never_even_probes_desktop(self):
        with patch.object(G, 'desktop_status') as desktop, patch.object(G, 'guard_state', return_value=(True, 'ready')), \
                patch.object(G, 'chat') as server:
            G.route_chat(G.Job(ID), MESSAGES, profile='conversation', allowed_backends=['server'])
        desktop.assert_not_called()
        server.assert_called_once()

    def test_forwarded_timeout_or_rejection_never_starts_server(self):
        with patch.object(G, 'desktop_status', return_value={'ready': True}), \
                patch.object(G, 'forward_desktop', side_effect=RuntimeError('desktop_request_interrupted')), \
                patch.object(G, 'chat') as server:
            with self.assertRaisesRegex(RuntimeError, 'desktop_request_interrupted'):
                G.route_chat(G.Job(ID), MESSAGES, profile='conversation', allowed_backends=['server', 'desktop'])
        server.assert_not_called()

    def test_server_fallback_preserves_gpu_guard(self):
        with patch.object(G, 'guard_state', return_value=(False, 'foreign_gpu_workload')), patch.object(G, 'chat') as server:
            with self.assertRaisesRegex(RuntimeError, 'foreign_gpu_workload'):
                G.route_chat(G.Job(ID), MESSAGES)
        server.assert_not_called()

    def test_cancelled_job_is_never_admitted_or_forwarded(self):
        job = G.Job(ID)
        job.cancelled.set()
        with patch.object(G, 'desktop_status') as desktop, patch.object(G, 'chat') as server:
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                G.route_chat(job, MESSAGES, profile='conversation', allowed_backends=['desktop'])
        desktop.assert_not_called()
        server.assert_not_called()

    def test_structured_contract_rejects_evidence_and_desktop(self):
        base = {'request_id': ID, 'messages': MESSAGES}
        for extra in ({'evidence': EVIDENCE}, {'allowed_backends': ['desktop']}, {'allowed_backends': []},
                      {'allowed_backends': ['server', 'server']}, {'allowed_backends': ['remote']},
                      {'allowed_backends': [{}]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                G.validate({**base, **extra})
        self.assertEqual(G.validate({**base, 'profile': 'conversation', 'evidence': EVIDENCE,
                                     'allowed_backends': ['desktop']}), (ID, MESSAGES))

    def test_source_budget_schema_and_provenance(self):
        for change in ({'text': 'x' * 6001}, {'id': 'S99'}, {'kind': 'instruction'},
                       {'url': 'http://user:password@example.com'}, {'text': ''}, {'extra': 'x'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                G.validate_evidence([{**EVIDENCE[0], **change}])
        self.assertEqual(G.source_metadata(EVIDENCE)[0]['id'], 'S1')
        self.assertNotIn('text', G.source_metadata(EVIDENCE)[0])

    def test_desktop_response_must_match_supplied_sources_and_pinned_model(self):
        valid = {'request_id': ID, 'text': 'Answer [S1]', 'model': G.DESKTOP_MODEL,
                 'model_manifest_sha256': G.DESKTOP_DIGEST, 'profile': 'conversation', 'backend': 'desktop',
                 'retrieval_used': True, 'sources': G.source_metadata(EVIDENCE), 'tools_used': ['web_search']}
        with patch.object(G, 'desktop_request', return_value=(200, valid)):
            self.assertEqual(G.forward_desktop(G.Job(ID), MESSAGES, EVIDENCE), valid)
        for change in ({'model_manifest_sha256': 'other'}, {'sources': []}, {'backend': 'server'},
                       {'tools_used': ['shell']}, {'retrieval_used': False}):
            with self.subTest(change=change), patch.object(G, 'desktop_request', return_value=(200, {**valid, **change})):
                with self.assertRaisesRegex(RuntimeError, 'desktop_response_mismatch'):
                    G.forward_desktop(G.Job(ID), MESSAGES, EVIDENCE)

    def test_desktop_endpoint_cannot_be_plaintext_or_arbitrary_public_host(self):
        for value in ('http://example.ts.net:8445', 'https://example.org', 'https://x.ts.net@evil.org',
                      'https://x.ts.net/path', 'https://x.ts.net?token=x'):
            with self.subTest(value=value), patch.object(G, 'DESKTOP_URL', value), self.assertRaises(ValueError):
                G.desktop_connection(2)


if __name__ == '__main__':
    unittest.main()
