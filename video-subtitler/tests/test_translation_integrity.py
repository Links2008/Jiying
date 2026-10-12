"""Offline regressions for missing translations and bilingual time coverage."""
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.pipeline.runner import PipelineRunner
from app.services.subtitler import parse_srt, save_srt, save_srt_bilingual, to_srt
from app.services.transcriber import Segment
from app.services.translator import LLMTranslator


@pytest.mark.parametrize("response,offset", [
    ("1. first\n99. second", 0),
    ("1. first\n2. second", 20),
    ("1. first\n2. second\n1. duplicate", 0),
    ("1. first\n2.   ", 0),
    (None, 0),
])
def test_translation_rejects_wrong_duplicate_or_empty_numbered_lines(response, offset):
    assert LLMTranslator._parse_numbered(response, 2, offset) is None


def test_translation_retries_invalid_batch_before_reporting_progress(monkeypatch):
    translator = LLMTranslator(Config({"translator": {"llm": {"batch_size": 2}}}))
    responses = iter(["1. first\n99. wrong", "1. first\n2. second"])
    calls = []
    monkeypatch.setattr(translator, "_call_api", lambda prompt: calls.append(prompt) or next(responses))
    monkeypatch.setattr("app.services.translator.time.sleep", lambda _: None)
    progress = []
    assert translator.translate(["a", "b"], "en", "zh", progress.append) == ["first", "second"]
    assert len(calls) == 2
    assert progress == [1.0]


def test_translation_invalid_batch_fails_after_three_attempts(monkeypatch):
    translator = LLMTranslator(Config({}))
    calls = []
    monkeypatch.setattr(translator, "_call_api", lambda prompt: calls.append(prompt) or "1. first\n99. wrong")
    monkeypatch.setattr("app.services.translator.time.sleep", lambda _: None)
    progress = []
    with pytest.raises(AppError) as error:
        translator.translate(["a", "b"], "en", "zh", progress.append)
    assert error.value.code == Codes.TRANS_FAILED
    assert len(calls) == 3
    assert not progress


def translation_runner(tmp_path, monkeypatch, translated):
    runner = PipelineRunner(Config({}), Store(tmp_path / "tasks.db"))
    runner._data = tmp_path
    task = runner.create_task("https://example.test/clip", "best", ["zh"], "burn", title="clip")
    transcript = SimpleNamespace(language="en", segments=[Segment(0, 1, "one"), Segment(1, 2, "two")])
    calls = []
    translator = SimpleNamespace(translate=lambda *_args, **_kwargs: calls.append(True) or translated)
    monkeypatch.setattr("app.pipeline.runner.build_translator", lambda *_: translator)
    return runner, task, transcript, calls


@pytest.mark.parametrize("translated", [["one"], ["one", " "], ["one", None]])
def test_translation_stage_rejects_truncated_or_empty_provider_output(tmp_path, monkeypatch, translated):
    runner, task, transcript, _ = translation_runner(tmp_path, monkeypatch, translated)
    with pytest.raises(AppError) as error:
        runner._stage_translate(task, transcript, ["zh"])
    assert error.value.code == Codes.TRANS_FAILED
    assert not runner._srt_path(task, "clip", "zh").exists()


def test_incomplete_translation_cache_is_backed_up_and_retranslated(tmp_path, monkeypatch):
    runner, task, transcript, calls = translation_runner(tmp_path, monkeypatch, ["first", "second"])
    cached = runner._srt_path(task, "clip", "zh")
    save_srt([Segment(0, 1, "first"), Segment(1, 2, "")], cached)
    old_bytes = cached.read_bytes()
    runner._store.add_artifact(task["id"], "subtitle", str(cached), lang="zh")
    result = runner._stage_translate(task, transcript, ["zh"])
    assert len(calls) == 1
    assert [s.text for s in parse_srt(result["zh"].read_text(encoding="utf-8-sig"))] == ["first", "second"]
    backups = list(runner._output_dir(task).glob("*.incomplete-*.srt"))
    assert len(backups) == 1 and backups[0].read_bytes() == old_bytes
    runner._store.upsert_stage(task["id"], "download", "done")
    runner._cleanup_tmp(task["id"])
    assert backups[0].read_bytes() == old_bytes
    assert len([a for a in runner._store.list_artifacts(task["id"]) if a["kind"] == "subtitle"]) == 1


def test_complete_reflowed_translation_cache_is_preserved(tmp_path, monkeypatch):
    runner, task, transcript, calls = translation_runner(tmp_path, monkeypatch, [])
    transcript.segments = [Segment(0, 6, "source sentence")]
    cached = runner._srt_path(task, "clip", "zh")
    save_srt([Segment(0, 3, "first part"), Segment(3, 6, "second part")], cached)
    old_bytes = cached.read_bytes()
    assert runner._stage_translate(task, transcript, ["zh"]) == {"zh": cached}
    assert not calls and cached.read_bytes() == old_bytes
    assert runner._store.artifact_exists(task["id"], str(cached))


@pytest.mark.parametrize("short_on_top", [True, False])
def test_bilingual_short_language_covers_every_long_language_cue(tmp_path, short_on_top):
    top, bottom, out = (tmp_path / name for name in ("top.srt", "bottom.srt", "merged.srt"))
    short = "谢谢你的解释。"
    long = ("The first idea reaches a conclusion. The second idea starts from that result. "
            "It takes another careful step to complete the proof.")
    save_srt([Segment(0, 6, short if short_on_top else long)], top)
    save_srt([Segment(0, 6, long if short_on_top else short)], bottom)
    save_srt_bilingual(top, bottom, out)
    cues = parse_srt(out.read_text(encoding="utf-8-sig"))
    assert cues[0].start == 0 and cues[-1].end == 6
    assert all(a.end == b.start for a, b in zip(cues, cues[1:]))
    assert all(len(cue.text.splitlines()) == 2 for cue in cues)
    assert all(short in cue.text for cue in cues)


def test_bilingual_missing_language_window_fails_before_writing(tmp_path):
    top, bottom, out = (tmp_path / name for name in ("top.srt", "bottom.srt", "merged.srt"))
    top.write_text(to_srt([Segment(0, 1, "第一句。")]), encoding="utf-8")
    bottom.write_text(to_srt([Segment(0, 1, "First."), Segment(1, 2, "Second.")]), encoding="utf-8")
    with pytest.raises(AppError) as error:
        save_srt_bilingual(top, bottom, out)
    assert error.value.code == Codes.TRANS_FAILED
    assert not out.exists()


def test_translation_keeps_wrapped_response_text():
    response = "1. 第一行。\n补充内容不能丢。\n2. 第二行。"
    assert LLMTranslator._parse_numbered(response, 2, 0) == [
        "第一行。 补充内容不能丢。", "第二行。"]


def test_translation_batches_overlap_and_keep_input_order(monkeypatch):
    import threading
    gate = threading.Barrier(4, timeout=2)
    translator = LLMTranslator(Config({"translator": {"llm": {"batch_size": 1}}}))
    def respond(prompt):
        gate.wait()
        return prompt.splitlines()[-1]
    monkeypatch.setattr(translator, "_call_api", respond)
    assert translator.translate(["甲", "乙", "丙", "丁"], "en", "zh") == ["甲", "乙", "丙", "丁"]


def test_source_subtitle_cache_is_checked_before_reuse(tmp_path, monkeypatch):
    from app.services.transcriber import Transcript
    runner, task, transcript, _ = translation_runner(tmp_path, monkeypatch, [])
    cache = tmp_path / "cache" / "transcripts" / f"{task['id']}.json"
    Transcript(transcript.language, transcript.segments).save_json(cache)
    runner._store.upsert_stage(task["id"], "transcribe", "done")
    original = runner._srt_path(task, "clip", "en")
    save_srt(transcript.segments[:1], original)
    saved = original.read_bytes()
    _, result = runner._stage_transcribe(task, tmp_path / "video.mp4", tmp_path / "audio.wav")
    assert [s.text for s in parse_srt(result.read_text(encoding="utf-8-sig"))] == ["one", "two"]
    backups = list(runner._output_dir(task).glob("*.incomplete-*.srt"))
    assert len(backups) == 1 and backups[0].read_bytes() == saved


def test_bilingual_softmux_embeds_merged_subtitle_first(tmp_path, monkeypatch):
    runner, task, _, _ = translation_runner(tmp_path, monkeypatch, [])
    task.update(subtitle_mode="softmux", bilingual=1)
    subs = {lang: tmp_path / f"{lang}.srt" for lang in ("en", "zh")}
    save_srt([Segment(0, 2, "Hello.")], subs["en"])
    save_srt([Segment(0, 2, "你好。")], subs["zh"])
    captured = []
    def mux(_cfg, _video, tracks, out, **_kwargs):
        captured.extend((lang, path.read_text(encoding="utf-8-sig")) for path, lang in tracks)
        out.write_bytes(b"video")
        return out
    monkeypatch.setattr("app.pipeline.runner.softmux", mux)
    runner._stage_render(task, tmp_path / "video.mp4", subs)
    assert [lang for lang, _ in captured] == ["zh+en", "zh", "en"]
    assert parse_srt(captured[0][1])[0].text == "你好。\nHello."


@pytest.mark.parametrize("base,expected", [
    ("https://api.deepseek.com/v1", {"type": "disabled"}),
    ("https://example.test/v1", None),
])
def test_translation_disables_thinking_only_for_deepseek_endpoint(monkeypatch, base, expected):
    captured = []
    def post(*_args, **kwargs):
        captured.append(kwargs["json"])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "choices": [{"finish_reason": "stop", "message": {"content": "1. 你好。"}}]})
    monkeypatch.setattr("app.services.translator.requests.post", post)
    t = LLMTranslator(Config({"translator": {"llm": {"base_url": base, "model": "demo"}}}))
    assert t._call_api("hello") == "1. 你好。"
    assert captured[0].get("thinking") == expected


def test_translation_rejects_length_truncated_response(monkeypatch):
    import requests
    monkeypatch.setattr("app.services.translator.requests.post", lambda *_a, **_k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {
            "choices": [{"finish_reason": "length", "message": {"content": "1. 截断内容"}}]}))
    t = LLMTranslator(Config({"translator": {"llm": {"base_url": "https://example.test", "model": "demo"}}}))
    with pytest.raises(requests.RequestException):
        t._call_api("hello")


def test_cancelled_translation_does_not_submit_more_batches(monkeypatch):
    import threading
    gate, finished = threading.Event(), threading.Event()
    calls, done = [], []
    t = LLMTranslator(Config({"translator": {"llm": {"batch_size": 1}}}))
    def respond(prompt):
        calls.append(prompt)
        gate.wait(3)
        done.append(True)
        if len(done) == 4:
            finished.set()
        return prompt.splitlines()[-1]
    def cancel(_fraction):
        raise AppError(Codes.TASK_CANCELLED, "cancelled")
    monkeypatch.setattr(t, "_call_api", respond)
    try:
        with pytest.raises(AppError) as error:
            t.translate(["hello"] * 40, "en", "zh", on_progress=cancel)
        assert error.value.code == Codes.TASK_CANCELLED
    finally:
        gate.set()
    assert finished.wait(2)
    assert len(calls) == 4
