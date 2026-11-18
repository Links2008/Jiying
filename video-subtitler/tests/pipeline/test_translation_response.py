"""模型编号回复出现未归属文字时，不能静默漏译并保存成功检查点。"""
import hashlib
import json

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.translator import LLMTranslator


def _worker(tmp_path):
    worker = LLMTranslator(Config({}))
    worker.set_checkpoint_dir(tmp_path / 'parts')
    return worker


@pytest.mark.parametrize('wrapper', ['{}', '```text\n{}\n```'])
def test_unassigned_leading_translation_is_retried_without_losing_sentence(
        tmp_path, monkeypatch, wrapper):
    worker = _worker(tmp_path)
    truncated = wrapper.format('别走。\n1. 等等我。\n2. 我会等。')
    complete = json.dumps({'translations': [
        {'id': 1, 'text': '别走，等等我。'}, {'id': 2, 'text': '我会等。'}]})
    responses = iter([truncated, complete])
    calls, progress = [], []
    monkeypatch.setattr(worker, '_call_api', lambda prompt: calls.append(prompt) or next(responses))
    monkeypatch.setattr('app.services.translator.time.sleep', lambda _: None)

    assert worker.translate(["Don't go. Wait for me.", 'I will wait.'], 'en', 'zh',
                            progress.append) == ['别走，等等我。', '我会等。']
    assert len(calls) == 2 and progress == [1.0]
    checkpoint = json.loads((tmp_path / 'parts' / '000000.json').read_text())
    assert checkpoint['response']['translations'][0]['text'] == '别走，等等我。'


def test_persistent_unassigned_translation_does_not_create_completed_checkpoint(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    calls, progress = [], []
    monkeypatch.setattr(worker, '_call_api', lambda prompt: calls.append(prompt) or
                        '别走。\n1. 等等我。\n2. 我会等。')
    monkeypatch.setattr('app.services.translator.time.sleep', lambda _: None)
    with pytest.raises(AppError) as error:
        worker.translate(["Don't go. Wait for me.", 'I will wait.'], 'en', 'zh', progress.append)
    assert error.value.code == Codes.TRANS_FAILED
    assert len(calls) == 3 and not progress
    assert not list((tmp_path / 'parts').glob('*.json'))


@pytest.mark.parametrize('wrapper', ['{}', '```\n{}\n```', '```text\n{}\n```'])
def test_valid_numbered_reply_keeps_outer_fence_compatibility(tmp_path, monkeypatch, wrapper):
    worker = _worker(tmp_path)
    response = wrapper.format('1. 别走，等等我。\n第二行继续保留。\n2. 我会等。')
    monkeypatch.setattr(worker, '_call_api', lambda _: response)
    assert worker.translate(["Don't go. Wait for me.", 'I will wait.'], 'en', 'zh') == [
        '别走，等等我。 第二行继续保留。', '我会等。']


def test_numbered_reply_does_not_delete_literal_backtick_text_inside_cue(tmp_path, monkeypatch):
    worker = _worker(tmp_path)
    monkeypatch.setattr(worker, '_call_api', lambda _: '1. 输入下面的字符。\n```python\n2. 完成。')
    assert worker.translate(['Type the characters: ```python', 'Done.'], 'en', 'zh') == [
        '输入下面的字符。 ```python', '完成。']


def test_legacy_checkpoint_that_may_have_lost_unassigned_text_is_not_reused(tmp_path, monkeypatch):
    texts = ["Don't go. Wait for me.", 'I will wait.']
    # 旧版本只存解析后的文字，丢掉的前缀已经无法从检查点里还原。
    fields = ('vendor', 'base_url', 'model', 'domain_context', 'terminology_notes',
              'glossary', 'context_lines', 'batch_size', 'batch_char_limit')
    old_fingerprint = hashlib.sha256(json.dumps({
        'version': 2, 'source': 'en', 'target': 'zh', 'texts': texts, 'title': '',
        'video': None, 'settings': dict.fromkeys(fields)},
        ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    cache = tmp_path / 'parts' / '000000.json'
    cache.parent.mkdir()
    cache.write_text(json.dumps({'fingerprint': old_fingerprint, 'response': {
        'translations': [{'id': 1, 'text': '等等我。'}, {'id': 2, 'text': '我会等。'}],
        'terms': [], 'context': {}}, 'frame_ids': []}))
    worker, calls = _worker(tmp_path), []
    monkeypatch.setattr(worker, '_call_api', lambda prompt: calls.append(prompt) or
                        '1. 别走，等等我。\n2. 我会等。')
    assert worker.translate(texts, 'en', 'zh') == ['别走，等等我。', '我会等。']
    assert len(calls) == 1
