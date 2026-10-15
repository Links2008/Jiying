"""识别输出的对话分段回归；使用模拟解码结果，不下载模型或调用云服务。"""
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.services.transcriber import CloudAsrTranscriber, FasterWhisperTranscriber, Segment
from app.services.subtitler import parse_srt, save_srt


def word(text, start, end):
    return SimpleNamespace(word=text, start=start, end=end)


def decode(text, words, start=0, end=10):
    worker = FasterWhisperTranscriber(Config({"transcriber": {"idle_unload_minutes": 0}}))
    chunk = SimpleNamespace(text=text, words=words, start=start, end=end)
    model = SimpleNamespace(transcribe=lambda *a, **kw: (
        iter([chunk]), SimpleNamespace(duration=end, language="zh")))
    return worker._run_model(model, None, None, None, vad=True)[0]


def test_local_dialogue_uses_sentence_and_word_times(tmp_path):
    cues = decode("你吃饭了吗？吃过了。", [
        word("你", .2, .4), word("吃饭", .4, .8), word("了吗？", .8, 1.2),
        word("吃过", 1.3, 1.8), word("了。", 1.8, 2.1),
    ])
    assert cues == [Segment(.2, 1.2, "你吃饭了吗？"), Segment(1.3, 2.1, "吃过了。")]
    path = tmp_path / "dialogue.srt"
    save_srt(cues, path)
    assert parse_srt(path.read_text(encoding="utf-8-sig")) == cues


def test_pause_without_punctuation_starts_a_new_cue():
    cues = decode("先等等再继续", [
        word("先", .2, .4), word("等等", .4, .9),
        word("再", 1.8, 2), word("继续", 2, 2.4),
    ])
    assert cues == [Segment(.2, .9, "先等等"), Segment(1.8, 2.4, "再继续")]


def test_english_sentences_keep_abbreviations_decimals_and_quotes():
    text = 'Dr. Li paid 3.14 dollars. "Then he left."'
    tokens = ['Dr.', ' Li', ' paid', ' 3.14', ' dollars.', ' "Then', ' he', ' left."']
    cues = decode(text, [word(token, i * .3, (i + 1) * .3) for i, token in enumerate(tokens)])
    assert [cue.text for cue in cues] == ['Dr. Li paid 3.14 dollars.', '"Then he left."']
    assert cues[0].end == pytest.approx(1.5)
    assert cues[1].start == pytest.approx(1.5)


def test_long_unpunctuated_speech_uses_real_word_boundaries():
    tokens = [' one'] * 12
    cues = decode(''.join(tokens), [word(token, i, i + .3) for i, token in enumerate(tokens)], end=12)
    assert len(cues) > 1
    assert ' '.join(cue.text for cue in cues) == ' '.join(['one'] * 12)
    assert all(cue.end - cue.start <= 6 for cue in cues)
    assert all(cue.start in range(12) and (cue.end - .3) == pytest.approx(round(cue.end - .3))
               for cue in cues)


@pytest.mark.parametrize("words", [None, [word("different", 0, 1)],
                                  [word("hello", float('nan'), 1)]])
def test_unusable_word_alignment_keeps_original_text_and_segment_times(words):
    assert decode("hello", words, start=.5, end=2) == [Segment(.5, 2, "hello")]


def test_cloud_word_timestamps_keep_sentence_punctuation(tmp_path):
    worker = CloudAsrTranscriber(Config({}))
    payload = {"language": "zh", "text": "你好！请继续。", "words": [
        {"word": "你好", "start": .2, "end": .8},
        {"word": "请", "start": 1.5, "end": 1.8},
        {"word": "继续", "start": 1.8, "end": 2.2},
    ], "segments": [{"start": 0, "end": 8, "text": "你好！请继续。"}]}
    cues, language = worker._parse(payload, tmp_path / "unused.wav")
    assert language == "zh"
    assert cues == [Segment(.2, .8, "你好！"), Segment(1.5, 2.2, "请继续。")]


def test_cloud_words_only_result_is_not_reported_as_empty(tmp_path):
    payload = {"words": [{"word": "Hello", "start": .1, "end": .4},
                         {"word": "world", "start": .4, "end": .9}]}
    cues, _ = CloudAsrTranscriber(Config({}))._parse(payload, tmp_path / "unused.wav")
    assert cues == [Segment(.1, .9, "Hello world")]


@pytest.mark.parametrize('tokens, expected', [
    (['Hello,', 'world'], 'Hello, world'), (['今天', '天气', '真好。'], '今天天气真好。'),
])
def test_cloud_words_only_spacing_does_not_join_english_or_separate_chinese(tmp_path, tokens, expected):
    payload = {'words': [{'word': token, 'start': i * .2, 'end': (i + 1) * .2}
                         for i, token in enumerate(tokens)]}
    cues, _ = CloudAsrTranscriber(Config({}))._parse(payload, tmp_path / 'unused.wav')
    assert [cue.text for cue in cues] == [expected]


def test_blank_local_decode_retries_without_vad(monkeypatch, tmp_path):
    worker = FasterWhisperTranscriber(Config({"transcriber": {"idle_unload_minutes": 0}}))
    calls = []
    def transcribe(*args, **kwargs):
        calls.append(kwargs['vad_filter'])
        text = '   ' if len(calls) == 1 else 'hello'
        return iter([SimpleNamespace(text=text, words=None, start=0, end=1)]), SimpleNamespace(
            duration=1, language='en')
    monkeypatch.setattr(worker, '_load_model', lambda *a, **kw: SimpleNamespace(transcribe=transcribe))
    transcript = worker.transcribe(tmp_path / 'unused.wav')
    assert calls == [True, False]
    assert transcript.segments == [Segment(0, 1, 'hello')]


def test_long_chinese_cues_survive_layout_without_guessed_times(tmp_path):
    tokens = ['讨论问题，'] * 8
    cues = decode(''.join(tokens), [word(token, i * .4, i * .4 + .2)
                                  for i, token in enumerate(tokens)])
    assert len(cues) > 1
    path = tmp_path / 'long.srt'
    save_srt(cues, path)
    saved = parse_srt(path.read_text(encoding='utf-8-sig'))
    assert [(s.start, s.end) for s in saved] == [(round(s.start, 3), round(s.end, 3)) for s in cues]
    assert ''.join(s.text for s in saved) == ''.join(tokens)


def test_overlapping_word_times_do_not_create_overlapping_cues():
    cues = decode('你好。再见。', [word('你好。', 0, 1), word('再见。', .95, 2)])
    assert cues == [Segment(0, 2, '你好。再见。')]


def test_cloud_partial_words_cannot_hide_the_remaining_segment_text(tmp_path):
    payload = {'words': [{'word': 'Hello', 'start': 0, 'end': .5}],
               'segments': [{'text': 'Hello world.', 'start': 0, 'end': 1}]}
    cues, _ = CloudAsrTranscriber(Config({}))._parse(payload, tmp_path / 'unused.wav')
    assert cues == [Segment(0, 1, 'Hello world.')]


def test_cloud_segment_text_without_full_text_keeps_chinese_joining(tmp_path):
    payload = {'words': [{'word': '今天', 'start': 0, 'end': .2},
                         {'word': '真好', 'start': .2, 'end': .6}],
               'segments': [{'text': '今天', 'start': 0, 'end': .2},
                            {'text': '真好。', 'start': .2, 'end': .6}]}
    cues, _ = CloudAsrTranscriber(Config({}))._parse(payload, tmp_path / 'unused.wav')
    assert cues == [Segment(0, .6, '今天真好。')]


def test_cloud_nested_words_are_used_and_invalid_global_words_are_ignored(tmp_path):
    payload = {'text': '你好。再见。', 'words': [{'word': '你好', 'start': 0, 'end': float('inf')}],
               'segments': [{'text': '你好。再见。', 'start': 0, 'end': 5, 'words': [
                   {'word': '你好', 'start': .1, 'end': .6},
                   {'word': '再见', 'start': 1, 'end': 1.5}]}]}
    cues, _ = CloudAsrTranscriber(Config({}))._parse(payload, tmp_path / 'unused.wav')
    assert cues == [Segment(.1, .6, '你好。'), Segment(1, 1.5, '再见。')]


@pytest.mark.parametrize('vendor, model, timestamps', [
    ('openai', 'whisper-1', ['word', 'segment']), ('siliconflow', 'SenseVoice', None),
])
def test_cloud_request_only_adds_word_option_for_supported_vendor(monkeypatch, tmp_path,
                                                               vendor, model, timestamps):
    audio = tmp_path / 'input.wav'
    audio.write_bytes('测试音频占位'.encode('utf-8'))
    data = []
    def post(*args, **kwargs):
        data.append(kwargs['data'])
        return SimpleNamespace(status_code=200, json=lambda: {
            'text': 'hello', 'segments': [{'text': 'hello', 'start': .1, 'end': .8}]})
    monkeypatch.setattr('requests.post', post)
    worker = CloudAsrTranscriber(Config({'transcriber': {'cloud': {'vendor': vendor, 'model': model}}}))
    assert worker.transcribe(audio).segments == [Segment(.1, .8, 'hello')]
    assert data[0].get('timestamp_granularities[]') == timestamps


def test_pipeline_cache_subtitles_and_translation_keep_dialogue_times(monkeypatch, tmp_path):
    from app.models.db import Store
    from app.pipeline.runner import PipelineRunner
    from app.services.transcriber import Transcript

    worker = FasterWhisperTranscriber(Config({'transcriber': {'idle_unload_minutes': 0}}))
    chunk = SimpleNamespace(text='你好。再见。', words=[word('你好。', .1, .6), word('再见。', 1, 1.5)],
                            start=0, end=3)
    monkeypatch.setattr(worker, '_load_model', lambda *a, **kw: SimpleNamespace(
        transcribe=lambda *a, **kw: (iter([chunk]), SimpleNamespace(duration=3, language='zh'))))
    runner = PipelineRunner(Config({}), Store(tmp_path / 'tasks.db'))
    runner._data = tmp_path / 'isolated'
    runner._transcriber = worker
    task = runner.create_task('https://example.test/dialogue', 'best', ['en'], 'none', title='对话')
    transcript, path = runner._stage_transcribe(task, tmp_path / 'unused.mp4', tmp_path / 'unused.wav')
    assert parse_srt(path.read_text(encoding='utf-8-sig')) == transcript.segments
    cached = Transcript.load_json(runner._data / 'cache' / 'transcripts' / (task['id'] + '.json'))
    assert cached.segments == [Segment(.1, .6, '你好。'), Segment(1, 1.5, '再见。')]
    monkeypatch.setattr('app.pipeline.runner.build_translator', lambda *_: SimpleNamespace(
        translate=lambda texts, *a, **kw: ['Hello.', 'Goodbye.']))
    translated = runner._stage_translate(task, transcript, ['en'])['en']
    assert parse_srt(translated.read_text(encoding='utf-8-sig')) == [
        Segment(.1, .6, 'Hello.'), Segment(1, 1.5, 'Goodbye.')]
