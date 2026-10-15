"""双语字幕漏显示：重叠时间轴、损坏块和毫秒分片不能静默丢句。"""
import json
import os
from pathlib import Path
import subprocess

import pytest

from app.core.config import Config
from app.core.errors import AppError
from app.services.subtitler import parse_srt, save_srt, save_srt_bilingual, to_srt, validate_srt
from app.services.transcriber import Segment


def merge(tmp_path, top, bottom):
    upper, lower, output = (tmp_path / name for name in ('upper.srt', 'lower.srt', 'bilingual.srt'))
    upper.write_text(to_srt(top), encoding='utf-8-sig')
    lower.write_text(to_srt(bottom), encoding='utf-8-sig')
    save_srt_bilingual(upper, lower, output)
    text = output.read_text(encoding='utf-8-sig')
    validate_srt(text)
    return parse_srt(text)


@pytest.mark.parametrize('windows', [((0, 2), (0, 2)), ((0, 3), (1, 2)), ((0, 2), (1, 3))])
def test_overlapping_dialogue_retains_both_languages_with_positive_times(tmp_path, windows):
    top = [Segment(*window, word) for window, word in zip(windows, ['第一位说话。', '第二位回答。'])]
    bottom = [Segment(*window, word) for window, word in zip(windows, ['SpeakerOne.', 'SpeakerTwo.'])]
    cues = merge(tmp_path, top, bottom)
    assert cues[0].start == 0 and cues[-1].end == max(end for _, end in windows)
    assert all(cue.end > cue.start and len(cue.text.splitlines()) == 2 for cue in cues)
    assert all(a.end <= b.start for a, b in zip(cues, cues[1:]))
    text = '\n'.join(cue.text for cue in cues)
    for phrase in ('第一位说话。', '第二位回答。', 'SpeakerOne.', 'SpeakerTwo.'):
        assert text.count(phrase) == 1


def test_malformed_overlapping_srt_cannot_be_silently_skipped(tmp_path):
    upper, lower, output = (tmp_path / name for name in ('upper.srt', 'lower.srt', 'bilingual.srt'))
    upper.write_text(to_srt([Segment(0, 2, '正确块。')]) + '\n2\n损坏时间轴\n这句不能丢。\n')
    lower.write_text(to_srt([Segment(0, 2, 'Complete caption.')]))
    output.write_bytes(b'previous-valid-file')
    with pytest.raises(AppError):
        save_srt_bilingual(upper, lower, output)
    assert output.read_bytes() == b'previous-valid-file'


def test_submillisecond_speech_is_rejected_instead_of_written_as_invisible_srt(tmp_path):
    output = tmp_path / 'captions.srt'
    output.write_bytes(b'previous-valid-file')
    with pytest.raises(AppError):
        save_srt([Segment(1, 1.0001, '不能生成零时长字幕。')], output)
    assert output.read_bytes() == b'previous-valid-file'


def test_many_wrapped_lines_in_a_short_window_keep_every_character_and_positive_cues(tmp_path):
    text = '长句不能因为时间切分精度不足而漏掉字幕文字。' * 30
    output = tmp_path / 'captions.srt'
    save_srt([Segment(0, .003, text)], output)
    body = output.read_text(encoding='utf-8-sig')
    validate_srt(body)
    cues = parse_srt(body)
    assert len(cues) <= 3
    assert ''.join(cue.text.replace('\n', '') for cue in cues) == text
    assert all(cue.end > cue.start for cue in cues)
    assert cues[-1].end == .003


def test_single_language_overlaps_are_not_hidden_by_player_or_mov_text(tmp_path):
    output = tmp_path / 'captions.srt'
    save_srt([Segment(0, 2, 'First speaker.'), Segment(0, 2, 'Second speaker.')], output)
    cues = parse_srt(output.read_text(encoding='utf-8-sig'))
    assert cues == [Segment(0, 2, 'First speaker. Second speaker.')]


@pytest.fixture
def real_ffmpeg():
    executable = os.environ.get('JIYING_TEST_FFMPEG')
    if not executable:
        pytest.skip('设置 JIYING_TEST_FFMPEG 以运行隔离的真实字幕烧录与封装验证')
    return executable


@pytest.mark.parametrize('mode', ['burn', 'softmux'])
def test_real_pipeline_preserves_source_and_translations_in_srt_and_video(tmp_path, monkeypatch, mode, real_ffmpeg):
    from types import SimpleNamespace
    from app.models.db import Store
    from app.pipeline.runner import PipelineRunner
    from app.services.transcriber import Transcript
    source = tmp_path / 'video.mp4'
    subprocess.run([real_ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'color=black:size=640x360:rate=25:duration=3', '-c:v', 'libx264',
                    '-y', str(source)], check=True)
    config = Config({'translator': {'provider': 'llm', 'llm': {
        'base_url': 'https://example.test', 'model': 'test', 'batch_size': 20}},
        'render': {'font_name': 'DejaVu Sans', 'encoder': 'libx264'},
        'paths': {'ffmpeg': real_ffmpeg}})
    runner = PipelineRunner(config, Store(tmp_path / 'tasks.db'))
    runner._data = tmp_path / 'data'
    task = runner.create_task('https://example.test/video', 'best', ['zh'], mode,
                              title='two-speakers', bilingual=True, font_name='DejaVu Sans')
    transcript = Transcript('en', [Segment(0, 2, 'SpeakerOne.'), Segment(0, 2, 'SpeakerTwo.')])
    transcript.save_json(runner._data / 'cache/transcripts' / f"{task['id']}.json")
    runner._store.upsert_stage(task['id'], 'transcribe', 'done')
    original, source_srt = runner._stage_transcribe(task, source, tmp_path / 'unused.wav')
    calls = []
    def request(*_, **kwargs):
        calls.append(kwargs['json'])
        # 首次漏一条，必须重试；只校验请求机制，不调用真实模型。
        entries = [{'id': 1, 'text': '第一位说话。'}]
        if len(calls) > 1:
            entries.append({'id': 2, 'text': '第二位回答。'})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'translations': entries}, ensure_ascii=False)}}]})
    monkeypatch.setattr('requests.post', request)
    monkeypatch.setattr('app.services.translator.time.sleep', lambda _: None)
    translated = runner._stage_translate(task, original, ['zh'])
    outputs = runner._stage_render(task, source, {'en': source_srt, **translated})
    assert len(calls) == 2
    bilingual = next(a for a in runner._store.list_artifacts(task['id']) if a['kind'] == 'bilingual')
    body = Path(bilingual['path']).read_text(encoding='utf-8-sig')
    validate_srt(body)
    for marker in ('第一位说话。', '第二位回答。', 'SpeakerOne.', 'SpeakerTwo.'):
        assert body.count(marker) == 1
    assert all(c.end > c.start and len(c.text.splitlines()) == 2 for c in parse_srt(body))
    assert all(marker in source_srt.read_text(encoding='utf-8-sig') for marker in ('SpeakerOne.', 'SpeakerTwo.'))
    assert all(marker in translated['zh'].read_text(encoding='utf-8-sig') for marker in ('第一位说话。', '第二位回答。'))
    if mode == 'softmux':
        embedded = subprocess.check_output([real_ffmpeg, '-v', 'error', '-i', str(outputs[0]),
                                            '-map', '0:s:0', '-f', 'srt', '-']).decode('utf-8-sig')
        for marker in ('第一位说话。', '第二位回答。', 'SpeakerOne.', 'SpeakerTwo.'):
            assert embedded.count(marker) == 1
    else:
        # 解码真实烧录结果，确认上下两层各有可见文字，而非只验证 ffmpeg 退出码。
        gray = subprocess.check_output([real_ffmpeg, '-v', 'error', '-ss', '1', '-i', str(outputs[0]),
                                        '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'])
        assert len(gray) == 640 * 360
        active_rows = [y for y in range(360) if sum(
            pixel > 150 for pixel in gray[y * 640:(y + 1) * 640]) > 4]
        groups = []
        for y in active_rows:
            if not groups or y > groups[-1][-1] + 1:
                groups.append([y])
            else:
                groups[-1].append(y)
        assert len([g for g in groups if len(g) >= 4]) >= 2


@pytest.mark.parametrize('windows', [((0, 2), (0, 2)), ((0, 3), (1, 2)), ((0, 2), (1, 3))])
def test_real_softmux_of_edited_overlapping_captions_retains_every_line(tmp_path, windows, real_ffmpeg):
    from app.services.renderer import softmux
    source = tmp_path / 'video.mp4'
    subprocess.run([real_ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'color=black:size=640x360:rate=25:duration=3', '-c:v', 'libx264',
                    '-y', str(source)], check=True)
    edited = tmp_path / 'edited.srt'
    # 编辑器允许同一时间出现两位说话者，不能要求用户手工合并后才能封装。
    edited.write_text(to_srt([Segment(*windows[0], '第一位。\nSpeakerOne.'),
                              Segment(*windows[1], '第二位。\nSpeakerTwo.')]), encoding='utf-8-sig')
    before = edited.read_bytes()
    config = Config({'paths': {'ffmpeg': real_ffmpeg}})
    output = softmux(config, source, [(edited, 'zh+en')], tmp_path / 'output.mp4')
    embedded = subprocess.check_output([real_ffmpeg, '-v', 'error', '-i', str(output),
                                        '-map', '0:s:0', '-f', 'srt', '-']).decode('utf-8-sig')
    for marker in ('第一位。', 'SpeakerOne.', '第二位。', 'SpeakerTwo.'):
        assert embedded.count(marker) == 1
    validate_srt(embedded)
    cues = parse_srt(embedded)
    assert len(cues) == 1
    assert cues[0].start == 0 and cues[0].end == max(end for _, end in windows)
    assert edited.read_bytes() == before


@pytest.mark.parametrize('offset', [0, 30])
def test_real_burn_keeps_caption_on_its_video_frame_after_moving_position(tmp_path, offset, real_ffmpeg):
    from app.services.renderer import burn
    source = tmp_path / 'video.mp4'
    subprocess.run([real_ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'color=black:size=640x360:rate=100:duration=0.3', '-c:v', 'libx264',
                    '-y', str(source)], check=True)
    captions = tmp_path / 'edited.srt'
    captions.write_text(to_srt([Segment(.1, .104, 'Visible on this frame.')]), encoding='utf-8-sig')
    config = Config({'paths': {'ffmpeg': real_ffmpeg},
                     'render': {'font_name': 'DejaVu Sans', 'encoder': 'cpu'}})
    output = burn(config, source, captions, tmp_path / 'output.mp4', vertical_offset=offset)
    gray = subprocess.check_output([real_ffmpeg, '-v', 'error', '-ss', '0.1', '-i', str(output),
                                    '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'])
    assert len(gray) == 640 * 360
    assert sum(pixel > 150 for pixel in gray) > 30


@pytest.mark.parametrize('start,end', [(0, .004), (.1, .104), (3599.999, 3600.002)])
def test_positioned_ass_has_visible_duration_and_covers_original_millisecond_window(tmp_path, start, end):
    from app.services.renderer import _write_positioned_ass
    captions, output = tmp_path / 'edited.srt', tmp_path / 'position.ass'
    captions.write_text(to_srt([Segment(start, end, 'Very short cue.')]), encoding='utf-8-sig')
    _write_positioned_ass(captions, output, 'bottom', -20)
    event = next(line for line in output.read_text().splitlines() if line.startswith('Dialogue:'))
    def milliseconds(value):
        h, m, s = value.split(':')
        seconds, cs = s.split('.')
        return (int(h) * 3600 + int(m) * 60 + int(seconds)) * 1000 + int(cs) * 10
    actual_start, actual_end = map(milliseconds, event.split(',')[1:3])
    assert actual_end > actual_start
    assert 0 <= round(start * 1000) - actual_start < 10
    assert 0 <= actual_end - round(end * 1000) < 10


def test_positioned_ass_coalesces_rounding_overlap_instead_of_painting_text_on_itself(tmp_path):
    from app.services.renderer import _write_positioned_ass
    captions, output = tmp_path / 'edited.srt', tmp_path / 'position.ass'
    captions.write_text(to_srt([Segment(.1, .104, '第一句。\nFirst.'),
                                Segment(.104, .108, '第二句。\nSecond.')]), encoding='utf-8-sig')
    _write_positioned_ass(captions, output, 'bottom', -20)
    events = [line for line in output.read_text().splitlines() if line.startswith('Dialogue:')]
    assert len(events) == 1
    assert events[0].split(',')[1:3] == ['0:00:00.10', '0:00:00.11']
    for marker in ('第一句。', 'First.', '第二句。', 'Second.'):
        assert events[0].count(marker) == 1


@pytest.mark.parametrize('cancelled', [True, False])
def test_softmux_keeps_prepared_tracks_until_fallback_or_cancellation_then_cleans_up(tmp_path, monkeypatch, cancelled):
    from app.core.errors import Codes
    from app.services import renderer
    from app.services.subtitler import read_srt
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'isolated-test-video')
    original = []
    for language in ('zh', 'en'):
        path = tmp_path / f'{language}.srt'
        path.write_text(to_srt([Segment(0, 2, f'{language}-first.'),
                                Segment(0, 2, f'{language}-second.')]), encoding='utf-8-sig')
        original.append((path, language))
    before = [path.read_bytes() for path, _ in original]
    calls = []
    def run(args, **kwargs):
        inputs = [Path(args[i + 1]) for i, item in enumerate(args) if item == '-i'][1:]
        calls.append(inputs)
        for path, (_, language) in zip(inputs, original):
            cues = read_srt(path)
            assert len(cues) == 1 and cues[0].start == 0 and cues[0].end == 2
            assert f'{language}-first.' in cues[0].text and f'{language}-second.' in cues[0].text
        if len(calls) == 1:
            raise AppError(Codes.TASK_CANCELLED if cancelled else Codes.ENG_FAILED, '模拟封装中断')
        Path(args[-1]).write_bytes(b'complete-fallback')
    monkeypatch.setattr(renderer, '_run', run)
    monkeypatch.setattr(renderer, 'find_ffmpeg', lambda _: 'ffmpeg')
    monkeypatch.setattr(renderer, 'audio_args', lambda *_: [])
    if cancelled:
        with pytest.raises(AppError) as error:
            renderer.softmux(Config({}), source, original, tmp_path / 'output.mp4')
        assert error.value.code == Codes.TASK_CANCELLED
        assert len(calls) == 1
    else:
        output = renderer.softmux(Config({}), source, original, tmp_path / 'output.mp4')
        assert output.suffix == '.mkv' and output.read_bytes() == b'complete-fallback'
        assert len(calls) == 2 and calls[0] == calls[1]
    assert all(not path.exists() for path in calls[0])
    assert all(not path.parent.exists() for path in calls[0])
    assert [path.read_bytes() for path, _ in original] == before


@pytest.mark.parametrize('windows', [((0, .001), (.002, .003)), ((.002, .003), (0, .001))])
def test_coverage_rounding_tolerance_cannot_produce_single_language_bilingual_cues(tmp_path, windows):
    upper, lower, output = (tmp_path / name for name in ('upper.srt', 'lower.srt', 'bilingual.srt'))
    upper.write_text(to_srt([Segment(*windows[0], '原文。')]), encoding='utf-8-sig')
    lower.write_text(to_srt([Segment(*windows[1], 'Translation.')]), encoding='utf-8-sig')
    output.write_bytes(b'previous-valid-file')
    with pytest.raises(AppError):
        save_srt_bilingual(upper, lower, output)
    assert output.read_bytes() == b'previous-valid-file'
