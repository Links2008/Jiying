"""编辑译文时提供同一任务的真实原文，不凭目标语言猜测源语言。"""
import json

from app.api import server
from app.core.paths import data_dir


def test_editor_reference_uses_detected_language_and_current_source(tmp_path):
    app = server.create_app()
    task = server._runner.create_task('https://example.test/editor', 'best', ['zh'], 'translate')
    source = tmp_path / 'source.srt'
    source.write_text('1\n00:00:00,000 --> 00:00:02,000\nCorrected original\n', encoding='utf-8')
    target = tmp_path / 'translated.srt'
    target.write_text('1\n00:00:00,000 --> 00:00:01,000\n译文第一句\n\n2\n00:00:01,000 --> 00:00:02,000\n译文第二句\n', encoding='utf-8')
    server._store.add_artifact(task['id'], 'subtitle', str(source), lang='en')
    server._store.add_artifact(task['id'], 'subtitle', str(target), lang='zh')
    cached = data_dir() / 'cache/transcripts' / (task['id'] + '.json')
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps({'language': 'en', 'segments': []}), encoding='utf-8')
    endpoint = next(r.endpoint for r in app.routes if r.path == '/api/v1/artifacts/{aid}/content' and 'GET' in r.methods)
    artifacts = server._store.list_artifacts(task['id'])
    translated = endpoint(next(a['id'] for a in artifacts if a['lang'] == 'zh'))
    assert translated['source_text'] == source.read_text(encoding='utf-8')
    assert translated['source_language'] == 'en' and not translated['is_source']
    original = endpoint(next(a['id'] for a in artifacts if a['lang'] == 'en'))
    assert original['is_source']
    source.unlink()
    assert endpoint(next(a['id'] for a in artifacts if a['lang'] == 'zh'))['source_text'] == ''


def test_editor_does_not_guess_missing_original_language(tmp_path):
    app = server.create_app()
    task = server._runner.create_task('https://example.test/no-original', 'best', ['zh'], 'translate')
    target = tmp_path / 'translated.srt'
    target.write_text('1\n00:00:00,000 --> 00:00:01,000\n译文\n', encoding='utf-8')
    server._store.add_artifact(task['id'], 'subtitle', str(target), lang='zh')
    artifact = server._store.list_artifacts(task['id'])[0]
    endpoint = next(r.endpoint for r in app.routes if r.path == '/api/v1/artifacts/{aid}/content' and 'GET' in r.methods)
    result = endpoint(artifact['id'])
    assert result['source_text'] == '' and result['source_language'] == ''
