"""Classifier usage summaries consume offline driver evidence only."""
import unittest
from unittest.mock import patch

from mizu import classification
from mizu.classification import _model_summary


class ClassificationUsageTests(unittest.TestCase):
    def test_raw_driver_usage(self):
        shapes = {
            'pi': [{'input': 100, 'output': 20, 'cacheRead': 5, 'cacheWrite': 1}],
            'pi-durable': [{'input': 100, 'output': 20, 'cacheRead': 5, 'cacheWrite': 1}],
            'codex': [{'input_tokens': 100, 'output_tokens': 20, 'cached_input_tokens': 5,
                       'reasoning_output_tokens': 2}],
            'claude': [{'model': 'example', 'inputTokens': 100, 'outputTokens': 20,
                        'cacheReadInputTokens': 5, 'cacheCreationInputTokens': 1}],
        }
        for engine, fragments in shapes.items():
            with self.subTest(engine=engine):
                model = {'engine': engine, 'profile': 'classifier', 'requests': 1,
                         'usage_known': True, 'usage': fragments}
                self.assertEqual(_model_summary(model), {'engine': engine, 'profile': 'classifier',
                    'requests': 1, 'usage_known': True, 'input_tokens': 100, 'output_tokens': 20})

    def test_multiple_fragments_and_zero_counts(self):
        model = {'engine': 'pi', 'usage_known': True,
                 'usage': [{'input': 0, 'output': 4}, {'input': 8, 'output': 0}]}
        self.assertEqual(_model_summary(model), {'engine': 'pi', 'usage_known': True,
                                                 'input_tokens': 8, 'output_tokens': 4})

    def test_oversized_engine_is_unknown_before_usage_normalization(self):
        model = {'engine': 'pi' + 'x' * 256, 'profile': 'classifier', 'usage_known': True,
                 'usage': [{'input': 1, 'output': 2}]}
        with patch('mizu.classification.attempt_usage', wraps=classification.attempt_usage) as attempt, \
             patch('mizu.classification.normalize', wraps=classification.normalize) as normalize:
            self.assertEqual(_model_summary(model), {'profile': 'classifier'})
            attempt.assert_called_once_with(model['usage'], 'unknown')
            normalize.assert_not_called()
            model['usage'] = [{'input_tokens': 1, 'output_tokens': 2}]
            self.assertEqual(_model_summary(model), {'profile': 'classifier', 'usage_known': True,
                                                     'input_tokens': 1, 'output_tokens': 2})
            self.assertEqual(attempt.call_args.args[1], 'unknown')
            normalize.assert_called_once_with(model['usage'][0], 'unknown')

    def test_incomplete_or_invalid_usage_is_not_known(self):
        bad = [None, [], {}, 'bad', [{'input': 1}], [{'output': 1}],
               [{'input': 1, 'output': 2}, {}], [{'input': 1, 'output': True}],
               [{'input': -1, 'output': 2}], [{'input': 1.0, 'output': 2}],
               [{'input': 1, 'output': 2, 'cacheRead': 'bad'}],
               [{'input': 2**53, 'output': 1}, {'input': 1, 'output': 1}],
               [{'usage': {'input': 1, 'output': 2}}],
               [{'input': 1, 'output': 2, 3: 4}]]
        for usage in bad:
            with self.subTest(usage=usage):
                self.assertEqual(_model_summary({'engine': 'pi', 'usage_known': True, 'usage': usage}),
                                 {'engine': 'pi'})
        self.assertEqual(_model_summary({'engine': 'pi', 'usage_known': False,
                                        'usage': [{'input': 1, 'output': 2}]}), {'engine': 'pi'})


if __name__ == '__main__':
    unittest.main()
