"""语境和译名机制回归；使用模拟服务回复，不代表真实模型翻译质量。"""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.core.config import Config
from app.services.translator import LLMTranslator


def response(number, text, source=None, target=None, kind='person', sense='OpenAI负责人'):
    terms = [] if source is None else [{
        'source': source, 'target': target, 'kind': kind, 'sense': sense,
        'ids': [number], 'confidence': 'high'}]
    return json.dumps({'translations': [{'id': number, 'text': text}], 'terms': terms}, ensure_ascii=False)


def row(prompt):
    number, text = prompt.splitlines()[-1].split('. ', 1)
    return int(number), text


def worker(**settings):
    return LLMTranslator(Config({'translator': {'llm': {'batch_size': 1, **settings}}}))


@pytest.mark.parametrize('context, expected', [
    ('They are watching the NBA finals.', '让他发挥。'),
    ('The chef is preparing dinner.', '让他做饭。'),
])
def test_meme_context_crosses_batch_boundary(monkeypatch, context, expected):
    translator = worker()
    captured = []
    def reply(prompt):
        captured.append(prompt)
        number, text = row(prompt)
        if number == 2:
            assert context in prompt and '前文（只参考）' in prompt
            assert '区分玩梗和字面意思' in prompt
        return response(number, '背景字幕' if number == 1 else expected)
    monkeypatch.setattr(translator, '_call_api', reply)
    assert translator.translate([context, 'Let him cook.'], 'en', 'zh') == ['背景字幕', expected]
    assert len(captured) == 2


def test_title_domain_notes_and_only_target_language_glossary_are_sent(monkeypatch):
    translator = worker(domain_context='大语言模型技术访谈', terminology_notes='RAG 是检索增强生成',
                        glossary={'zh': {'RAG': '检索增强生成'}, 'en': {'RAG': 'retrieval-augmented generation'}})
    translator.set_context('AI 研究访谈')
    captured = []
    monkeypatch.setattr(translator, '_call_api', lambda prompt: captured.append(prompt) or response(
        1, '使用增强检索。', 'RAG', '增强检索', 'technical', '检索与生成结合'))
    assert translator.translate(['Use RAG.'], 'en', 'zh') == ['使用检索增强生成。']
    assert all(text in captured[0] for text in ['AI 研究访谈', '大语言模型技术访谈', 'RAG 是检索增强生成'])
    assert 'retrieval-augmented generation' not in captured[0]


def test_seeded_name_memory_is_reused_without_extra_analysis_call(monkeypatch):
    translator = worker()
    calls = []
    def reply(prompt):
        number, text = row(prompt)
        calls.append(number)
        if number > 1:
            assert '山姆·奥特曼' in prompt
        name = '山姆·奥特曼' if number == 1 else '山姆·奥尔特曼'
        return response(number, name + '表示同意。', 'Sam Altman', name)
    monkeypatch.setattr(translator, '_call_api', reply)
    result = translator.translate(['Sam Altman agrees.'] * 5, 'en', 'zh')
    assert result == ['山姆·奥特曼表示同意。'] * 5
    assert sorted(calls) == [1, 2, 3, 4, 5]


def test_fixed_name_overrides_automatic_memory_before_next_prompt(monkeypatch):
    translator = worker(glossary={'zh': {'Sam Altman': '山姆·奥特曼'}})
    def reply(prompt):
        number, _ = row(prompt)
        if number == 2:
            assert '山姆·奥尔特曼' not in prompt
            assert '山姆·奥特曼' in prompt
        return response(number, '山姆·奥尔特曼发言。', 'Sam Altman', '山姆·奥尔特曼')
    monkeypatch.setattr(translator, '_call_api', reply)
    assert translator.translate(['Sam Altman speaks.'] * 2, 'en', 'zh') == ['山姆·奥特曼发言。'] * 2


def test_parallel_later_discovery_uses_earliest_subtitle_not_completion_order(monkeypatch):
    translator = worker()
    later_recorded = threading.Event()
    original = translator._remember_terms
    def remember(terms, chunk, translated, offset, generation):
        original(terms, chunk, translated, offset, generation)
        if offset == 2:
            later_recorded.set()
    monkeypatch.setattr(translator, '_remember_terms', remember)
    def reply(prompt):
        number, _ = row(prompt)
        if number == 2:
            assert later_recorded.wait(2)
            return response(number, '玛丽发言。', 'Mary', '玛丽', sense='当前访谈嘉宾')
        if number == 3:
            return response(number, '马丽继续。', 'Mary', '马丽', sense='当前访谈嘉宾')
        return response(number, '开场。')
    monkeypatch.setattr(translator, '_call_api', reply)
    assert translator.translate(['intro', 'Mary speaks.', 'Mary continues.'], 'en', 'zh') == [
        '开场。', '玛丽发言。', '玛丽继续。']


def test_same_spelling_with_different_technical_meanings_is_not_replaced(monkeypatch):
    translator = worker()
    def reply(prompt):
        number, _ = row(prompt)
        if number == 1:
            return response(number, 'Transformer模型使用注意力。', 'transformer', 'Transformer模型',
                            'technical', '神经网络架构')
        return response(number, '变压器连接电网。', 'transformer', '变压器', 'technical', '电气设备')
    monkeypatch.setattr(translator, '_call_api', reply)
    assert translator.translate(['A transformer uses attention.', 'A transformer connects the power grid.'],
                                'en', 'zh') == ['Transformer模型使用注意力。', '变压器连接电网。']


def test_name_replacement_respects_lexical_boundary(monkeypatch):
    translator = worker(glossary={'zh': {'Ann': '安'}})
    monkeypatch.setattr(translator, '_call_api', lambda prompt: response(
        1, 'Anna met Ann.', 'Ann', 'Ann', sense='访谈嘉宾Ann'))
    assert translator.translate(['Anna met Ann.'], 'en', 'zh') == ['Anna met 安.']


def test_two_name_edits_use_original_spans_without_cascading(monkeypatch):
    translator = worker(glossary={'zh': {'Alex': 'Betty', 'Blake': 'Carol'}})
    terms = [{'source': source, 'target': target, 'kind': 'person', 'sense': source,
              'ids': [1], 'confidence': 'high'} for source, target in [('Alex', 'Ann'), ('Blake', 'Betty')]]
    monkeypatch.setattr(translator, '_call_api', lambda _: json.dumps({
        'translations': [{'id': 1, 'text': 'Ann spoke to Betty.'}], 'terms': terms}))
    assert translator.translate(['Alex spoke to Blake.'], 'en', 'zh') == ['Betty spoke to Carol.']


def test_unsupported_or_context_only_name_metadata_is_ignored(monkeypatch):
    translator = worker()
    bad = {'source': 'Sam Altman', 'target': '奥特曼', 'kind': 'person', 'sense': '猜测身份',
           'confidence': 'high', 'ids': [1, 99, True]}
    monkeypatch.setattr(translator, '_call_api', lambda _: json.dumps({
        'translations': [{'id': 1, 'text': '这个人表示同意。'}], 'terms': [bad]}))
    assert translator.translate(['Someone agrees.'], 'en', 'zh') == ['这个人表示同意。']
    assert translator._terms == {}


def test_memory_is_not_shared_between_recordings(monkeypatch):
    translator = worker()
    monkeypatch.setattr(translator, '_call_api', lambda _: response(1, '奥特曼发言。', 'Sam Altman', '奥特曼'))
    translator.translate(['Sam Altman speaks.'], 'en', 'zh')
    assert translator._terms
    captured = []
    monkeypatch.setattr(translator, '_call_api', lambda prompt: captured.append(prompt) or response(1, '第二个视频。'))
    translator.translate(['Different recording.'], 'en', 'zh')
    assert translator._terms == {}
    assert '奥特曼' not in captured[0]


@pytest.mark.parametrize('entries', [
    [{'id': 1, 'text': '甲'}, {'id': 1, 'text': '乙'}],
    [{'id': 1, 'text': '甲'}, {'id': 99, 'text': '乙'}],
    [{'id': 1, 'text': '甲'}, {'id': 2, 'text': ''}],
])
def test_structured_translation_rejects_duplicate_missing_or_empty_rows(entries):
    assert LLMTranslator._parse_response(json.dumps({'translations': entries}), 2, 0)[0] is None


def test_structured_translation_preserves_number_order_and_accepts_json_fence():
    raw = '```json\n' + json.dumps({'translations': [{'id': 22, 'text': '第二条'},
                                                    {'id': 21, 'text': '第一条'}]}) + '\n```'
    assert LLMTranslator._parse_response(raw, 2, 20)[0] == ['第一条', '第二条']


def test_character_budget_splits_batches_without_truncating_source(monkeypatch):
    translator = worker(batch_size=20, batch_char_limit=1000)
    captured = []
    def reply(prompt):
        number, text = row(prompt)
        captured.append((number, prompt))
        return response(number, text[0] + '的完整译文')
    monkeypatch.setattr(translator, '_call_api', reply)
    assert translator.translate(['甲' * 600, '乙' * 600, '丙' * 600], 'en', 'zh') == ['甲的完整译文', '乙的完整译文', '丙的完整译文']
    assert len(captured) == 3
    # 并行请求可以乱序到达，但每条原文必须完整，返回的译文必须回到正确位置。
    assert all(text in dict(captured)[number] for number, text in enumerate(['甲' * 600, '乙' * 600, '丙' * 600], 1))


def test_semantic_hints_api_roundtrip_omission_and_explicit_clear(tmp_path):
    from app.api import server
    client = TestClient(server.create_app(), base_url='http://127.0.0.1')
    values = {'asr_hotwords': 'Sam Altman, RAG', 'llm_domain_context': 'AI技术访谈',
              'llm_terminology_notes': 'Sam Altman 译为山姆·奥特曼'}
    assert client.post('/api/v1/config', json=values).status_code == 200
    fetched = client.get('/api/v1/config').json()
    assert all(fetched[key] == value for key, value in values.items())
    assert server._config.get('transcriber.hotwords') == values['asr_hotwords']
    assert server._config.get('translator.llm.domain_context') == values['llm_domain_context']
    assert (tmp_path / 'config.yaml').is_file()
    client.post('/api/v1/config', json={'model': 'small'})
    assert client.get('/api/v1/config').json()['llm_terminology_notes'] == values['llm_terminology_notes']
    client.post('/api/v1/config', json={'llm_terminology_notes': ''})
    assert client.get('/api/v1/config').json()['llm_terminology_notes'] == ''
    assert client.post('/api/v1/config', json={'llm_domain_context': '甲' * 2001}).status_code == 422


def test_pipeline_passes_recording_title_to_translator(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.models.db import Store
    from app.pipeline.runner import PipelineRunner
    from app.services.transcriber import Transcript, Segment
    runner = PipelineRunner(Config({}), Store(tmp_path / 'tasks.db'))
    runner._data = tmp_path
    task = runner.create_task('https://example.test/interview', 'best', ['zh'], 'softmux', title='AI访谈')
    titles = []
    fake = SimpleNamespace(set_context=titles.append, translate=lambda *a, **kw: ['译文'])
    monkeypatch.setattr('app.pipeline.runner.build_translator', lambda *_: fake)
    runner._stage_translate(task, Transcript('en', [Segment(0, 1, 'hello')]), ['zh'])
    assert titles == ['AI访谈']


def test_translation_retry_restores_name_memory_and_only_calls_missing_batches(monkeypatch, tmp_path):
    from app.core.errors import AppError, Codes
    translator = worker()
    translator.set_checkpoint_dir(tmp_path / 'parts')
    def first_reply(prompt):
        number, _ = row(prompt)
        if number > 1:
            raise AppError(Codes.TRANS_FAILED, '模拟失败')
        return response(number, '奥特曼发言。', 'Sam Altman', '奥特曼')
    monkeypatch.setattr(translator, '_call_api', first_reply)
    with pytest.raises(AppError):
        translator.translate(['Sam Altman speaks.', 'Sam Altman continues.'], 'en', 'zh')
    assert [p.name for p in (tmp_path / 'parts').glob('*.json')] == ['000000.json']
    restored = worker(api_key='模拟凭据不会落盘')
    restored.set_checkpoint_dir(tmp_path / 'parts')
    calls = []
    def reply(prompt):
        number, _ = row(prompt)
        calls.append(number)
        assert '奥特曼' in prompt
        return response(number, '奥尔特曼继续。', 'Sam Altman', '奥尔特曼')
    monkeypatch.setattr(restored, '_call_api', reply)
    assert restored.translate(['Sam Altman speaks.', 'Sam Altman continues.'], 'en', 'zh') == [
        '奥特曼发言。', '奥特曼继续。']
    assert calls == [2]
    assert all('模拟凭据不会落盘' not in p.read_text(encoding='utf-8') for p in (tmp_path / 'parts').glob('*.json'))
    assert restored.translate(['Sam Altman speaks.', 'Sam Altman continues.'], 'en', 'zh') == [
        '奥特曼发言。', '奥特曼继续。']
    assert calls == [2]


def test_context_change_invalidates_translation_checkpoint(monkeypatch, tmp_path):
    calls = []
    for context in ['AI访谈', '游戏访谈']:
        translator = worker(domain_context=context)
        translator.set_checkpoint_dir(tmp_path / 'parts')
        monkeypatch.setattr(translator, '_call_api', lambda prompt: calls.append(prompt) or response(1, '译文'))
        translator.translate(['source'], 'en', 'zh')
    assert len(calls) == 2


def test_large_manual_glossary_selects_relevant_names_instead_of_first_entries(monkeypatch):
    glossary = {f'UnrelatedName{i}': f'无关名字{i}' for i in range(100)}
    glossary['Sam Altman'] = '山姆·奥特曼'
    translator = worker(glossary={'zh': glossary})
    captured = []
    monkeypatch.setattr(translator, '_call_api', lambda prompt: captured.append(prompt) or response(
        1, '山姆·奥特曼发言。', 'Sam Altman', '山姆·奥特曼'))
    translator.translate(['Sam Altman speaks.'], 'en', 'zh')
    assert '山姆·奥特曼' in captured[0] and 'UnrelatedName0' not in captured[0]


def test_invalid_optional_metadata_cannot_break_checkpoint_serialization(monkeypatch, tmp_path):
    translator = worker()
    translator.set_checkpoint_dir(tmp_path / 'parts')
    monkeypatch.setattr(translator, '_call_api', lambda _: json.dumps({
        'translations': [{'id': 1, 'text': '有效译文'}], 'terms': [{'untrusted': float('nan')}]}))
    assert translator.translate(['source'], 'en', 'zh') == ['有效译文']
    saved = json.loads((tmp_path / 'parts' / '000000.json').read_text(encoding='utf-8'))
    assert saved['response']['terms'] == []
