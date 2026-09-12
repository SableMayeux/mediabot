import unittest
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from mediabot.services.local_ai import CONVERSATION_MODELS, LocalAIService, LocalAIError, MODEL, MODEL_DIGEST

ID = '12345678-1234-4abc-8def-123456789abc'
MESSAGES = [{'role': 'user', 'content': 'What changed?'}]
EVIDENCE = [{'id': 'S1', 'title': 'Example source', 'url': 'https://example.com/', 'text': 'Quoted evidence.',
             'retrieved_at': '2026-09-12T00:00:00Z', 'kind': 'page'}]


class RoutingClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_accumulates_split_json_and_rejects_oversized_stream(self):
        valid = {'model': MODEL, 'model_manifest_sha256': MODEL_DIGEST,
                 'server': {'ready': True}, 'desktop': {'ready': False}}
        for raw in (json.dumps(valid).encode(), b'x' * 16385):
            async def chunks(size):
                for offset in range(0, len(raw), 7):
                    yield raw[offset:offset + 7]
            response = MagicMock()
            response.status = 200
            response.content = SimpleNamespace(iter_chunked=chunks)
            response.__aenter__ = AsyncMock(return_value=response)
            response.__aexit__ = AsyncMock(return_value=False)
            client = LocalAIService(base_url='http://local-ai-gateway:8080')
            client.session = SimpleNamespace(closed=False, get=MagicMock(return_value=response))
            if len(raw) > 16384:
                with self.assertRaisesRegex(LocalAIError, 'oversized'):
                    await client.status()
            else:
                self.assertEqual(await client.status(), valid)
            self.assertFalse(client.session.get.call_args.kwargs['allow_redirects'])

    def client(self, *, backend='server', evidence=False):
        client = LocalAIService(base_url='http://local-ai-gateway:8080')
        model = 'gemma4:12b-it-qat' if backend == 'desktop' else MODEL
        body = {'request_id': ID, 'text': 'Answer', 'profile': 'conversation', 'backend': backend,
                'model': model, 'model_manifest_sha256': CONVERSATION_MODELS[model]['digest'],
                'retrieval_used': evidence, 'tools_used': ['web_search'] if evidence else [],
                'sources': [{k: v for k, v in source.items() if k != 'text'} for source in EVIDENCE] if evidence else []}
        client.request = AsyncMock(return_value=body)
        return client

    async def test_explicit_backend_permissions_and_evidence_cross_gateway_boundary(self):
        client = self.client(backend='desktop', evidence=True)
        result = await client.chat(ID, MESSAGES, profile='conversation', evidence=EVIDENCE,
                                   allowed_backends=('server', 'desktop'))
        payload = client.request.call_args.args[1]
        self.assertEqual(payload['allowed_backends'], ['server', 'desktop'])
        self.assertEqual(payload['evidence'], EVIDENCE)
        self.assertEqual(result['backend'], 'desktop')

    async def test_backend_cannot_ignore_access_or_mislabel_model(self):
        for backend, allowed in (('desktop', ('server',)), ('server', ('desktop',))):
            client = self.client(backend=backend)
            with self.assertRaises(LocalAIError):
                await client.chat(ID, MESSAGES, profile='conversation', allowed_backends=allowed)
        client = self.client(backend='desktop')
        client.request.return_value['backend'] = 'server'
        with self.assertRaises(LocalAIError):
            await client.chat(ID, MESSAGES, profile='conversation', allowed_backends=('server', 'desktop'))

    async def test_fabricated_source_and_unrequested_retrieval_rejected(self):
        client = self.client(evidence=True)
        with self.assertRaises(LocalAIError):
            await client.chat(ID, MESSAGES, profile='conversation')
        client.request.return_value['sources'][0]['url'] = 'https://fabricated.example'
        with self.assertRaises(LocalAIError):
            await client.chat(ID, MESSAGES, profile='conversation', evidence=EVIDENCE)

    async def test_structured_work_cannot_use_web_or_desktop_and_no_access_cannot_infer(self):
        for options in ({'evidence': EVIDENCE}, {'allowed_backends': ('desktop',)}, {'allowed_backends': ()}):
            client = self.client()
            with self.assertRaises(LocalAIError):
                await client.chat(ID, MESSAGES, **options)
            client.request.assert_not_awaited()

    async def test_oversized_or_malformed_evidence_fails_before_network(self):
        for change in ({'text': 'x' * 6001}, {'id': 'S9'}, {'kind': 'tool'}, {'url': 'file:///etc/passwd'}):
            client = self.client()
            with self.assertRaises(LocalAIError):
                await client.chat(ID, MESSAGES, profile='conversation', evidence=[{**EVIDENCE[0], **change}])
            client.request.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
