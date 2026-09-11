import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mediabot.services.local_ai import LocalAIError
from mediabot.services.recommendation_auto import (
    decode_order, extract_auto_option, rank_candidates, ranking_prompt,
)


def entries(count=3):
    return [(10-i, {'id': i+1, 'mediaType': 'movie', 'title': f'Provider title {i}'},
             f'Trakt suggestion #{i+1}', ()) for i in range(count)]


class RankingValidationTests(unittest.TestCase):
    def test_accepts_only_an_exact_permutation_without_added_fields(self):
        self.assertEqual(decode_order('{"order":[2,0,1]}', 3), [2,0,1])
        for output in ('{"order":[0,0,1]}', '{"order":[0,1,9]}',
                       '{"order":[false,1,2]}', '{"order":[0,1]}',
                       '{"order":[0,1,2],"title":"invented"}',
                       '{"order":[0,1,2],"order":[2,1,0]}', 'null', '[]'):
            with self.subTest(output=output), self.assertRaises(LocalAIError):
                decode_order(output, 3)

    def test_auto_flag_can_follow_filters_without_consuming_other_options(self):
        self.assertEqual(extract_auto_option('movie horror --auto --count 3'),
                         ('movie horror  --count 3', True))
        self.assertEqual(extract_auto_option('movie --automatic'), ('movie --automatic', False))

    def test_prompt_is_bounded_and_contains_provider_evidence_only(self):
        prompt = ranking_prompt(entries(6))
        self.assertLessEqual(len(prompt.encode()), 3000)
        self.assertIn('Trakt suggestion #1', prompt)
        excessive = [(1, {'title':'\U0001f600'*100,'mediaType':'movie'}, '\U0001f600'*260, ())]*6
        with self.assertRaises(LocalAIError): ranking_prompt(excessive)


class RankingExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reorders_original_objects_and_never_expands_shortlist(self):
        source = entries(8)
        model = SimpleNamespace(enabled=True, chat=AsyncMock(return_value={'text':'{"order":[5,4,3,2,1,0]}'}))
        ranked, status = await rank_candidates(model, source)
        self.assertIs(ranked[0], source[5])
        self.assertEqual(ranked[6:], source[6:])
        self.assertEqual(status, 'ranked 6 provider candidates')
        sent = model.chat.call_args.args[1][0]['content']
        self.assertNotIn('Provider title 6', sent)

    async def test_unavailable_or_hallucinated_output_preserves_standard_order(self):
        source = entries()
        for error in (False, True):
            model = SimpleNamespace(enabled=True, chat=AsyncMock(return_value={'text':'{"order":[0,1,999]}'}))
            if error: model.chat.side_effect=LocalAIError('GPU busy')
            ranked, status = await rank_candidates(model, source)
            self.assertIs(ranked, source)
            self.assertIn('standard ranking retained', status)
        model.enabled=False; model.chat.reset_mock()
        self.assertIs((await rank_candidates(model, source))[0], source)
        model.chat.assert_not_awaited()
