"""评测算法以已知错字、缺词和独立的短序列距离作校验。"""
import importlib.util
import random
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('eval_transcript', Path(__file__).resolve().parents[1]
                                            / 'scripts' / 'eval_transcript.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def test_chinese_metric_normalizes_script_but_counts_wrong_words():
    result = metrics.evaluate('今天天氣真好。', '今天天气很好', 'zh')
    assert result['character_errors'] == 1
    assert result['cer'] == pytest.approx(1 / 6)
    assert result['wer'] is None


def test_english_metric_counts_word_deletion_and_insertions():
    assert metrics.evaluate('One two three.', 'One three.', 'en')['wer'] == pytest.approx(1 / 3)
    assert metrics.evaluate('one', 'one two three', 'en')['wer'] == 2


def test_metric_is_defined_for_empty_hypothesis_but_not_empty_reference():
    assert metrics.evaluate('你好', '', 'zh')['cer'] == 1
    with pytest.raises(ValueError):
        metrics.evaluate('', '你好', 'zh')


def test_bit_distance_matches_independent_short_sequence_dynamic_programming():
    rng = random.Random(17)
    for _ in range(100):
        left, right = [''.join(rng.choices('abc', k=rng.randrange(12))) for _ in range(2)]
        row = list(range(len(left) + 1))
        for i, char in enumerate(right, 1):
            next_row = [i]
            for j, other in enumerate(left, 1):
                next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (char != other)))
            row = next_row
        assert metrics.edit_distance(left, right) == row[-1]


def test_long_reference_has_exact_distance_without_quadratic_matrix():
    text = '真实长视频逐字稿' * 2000
    assert metrics.edit_distance(text, text) == 0
    assert metrics.edit_distance(text, text[:-1]) == 1
