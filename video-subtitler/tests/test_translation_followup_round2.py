"""语境证据与嵌套译名回归；模拟模型回复，不验证真实模型质量。"""
import json

import pytest

from app.core.config import Config
from app.services.translator import LLMTranslator


def _translator(**options):
    return LLMTranslator(Config({'translator': {'llm': options}}))


def _reply(prompt, context=None):
    rows = prompt.split('待翻译台词：\n', 1)[1].splitlines()
    return json.dumps({'translations': [
        {'id': int(row.split('. ', 1)[0]), 'text': '有效译文'} for row in rows],
        'context': context}, ensure_ascii=False)


@pytest.mark.parametrize('batch_char_limit, evidence_id', [(6000, 8), (1000, 5)])
def test_background_accepts_evidence_from_actual_first_batch_and_restores_it(
        tmp_path, monkeypatch, batch_char_limit, evidence_id):
    texts = [f'Subtitle {i}: ' + 'x' * 150 for i in range(1000)]
    texts[evidence_id - 1] = 'I am Dr. Ada and today I will discuss quantum computing.'
    options = {'batch_size': 20, 'batch_char_limit': batch_char_limit, 'context_lines': 0}
    worker = _translator(**options)
    worker.set_checkpoint_dir(tmp_path)
    context = {'summary': 'Ada 博士讨论量子计算。', 'evidence_ids': [evidence_id],
               'frame_ids': [], 'confidence': 'high'}
    # 编号刻意不在全片稀疏采样中，但确实出现在首批完整台词内。
    assert evidence_id not in {row['id'] for row in worker._sample_overview(texts)}
    later_prompts = []

    def request(prompt):
        first_id = int(prompt.split('待翻译台词：\n', 1)[1].split('.', 1)[0])
        if first_id == 1:
            assert f'{evidence_id}. {texts[evidence_id - 1]}' in prompt
        else:
            later_prompts.append(prompt)
        return _reply(prompt, context if first_id == 1 else None)

    monkeypatch.setattr(worker, '_call_api', request)
    assert len(worker.translate(texts, 'en', 'zh')) == len(texts)
    assert later_prompts and all(context['summary'] in prompt for prompt in later_prompts)
    assert worker._background == context

    resumed = _translator(**options)
    resumed.set_checkpoint_dir(tmp_path)
    monkeypatch.setattr(resumed, '_call_api', lambda _: pytest.fail('完整检查点不应重新请求'))
    assert len(resumed.translate(texts, 'en', 'zh')) == len(texts)
    assert resumed._background == context


def test_background_still_rejects_unprovided_first_batch_evidence(monkeypatch):
    texts = [f'Subtitle {i}' for i in range(1000)]
    worker = _translator(batch_size=2, context_lines=0)
    assert 8 not in {row['id'] for row in worker._sample_overview(texts)}
    context = {'summary': '未提供的身份信息', 'evidence_ids': [8],
               'frame_ids': [], 'confidence': 'high'}
    monkeypatch.setattr(worker, '_call_api', lambda prompt: _reply(prompt, context))
    worker.translate(texts, 'en', 'zh')
    assert worker._background == {}


@pytest.mark.parametrize('long_name, translated', [
    ('Apple Pencil', '她用苹果铅笔。'),
    ('Apple Pencil', '她用苹果铅笔，桌上还有一支苹果铅笔。'),
])
def test_company_name_unification_cannot_corrupt_recorded_product_name(
        monkeypatch, long_name, translated):
    worker = _translator(glossary={'zh': {'Apple': '苹果公司', long_name: '苹果铅笔'}})
    terms = [{'source': source, 'target': target, 'kind': 'proper', 'sense': sense,
              'ids': [1], 'confidence': 'high'} for source, target, sense in [
                  ('Apple', '苹果', '公司'), (long_name, '苹果铅笔', '产品')]]
    monkeypatch.setattr(worker, '_call_api', lambda _: json.dumps({
        'translations': [{'id': 1, 'text': translated}], 'terms': terms}))
    assert worker.translate(['She uses an Apple Pencil.'], 'en', 'zh') == [translated]
