"""任务内 Whisper 准备进度与取消：不连接仓库、不载入真实模型。"""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services import transcriber as tc


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", lambda *a, **k: object())
    worker = tc.FasterWhisperTranscriber(Config({"transcriber": {
        "model": "tiny", "device": "cpu", "idle_unload_minutes": 0}}))
    monkeypatch.setattr(worker, "_run_model", lambda *a, **k: (
        [tc.Segment(0, 1, "hello")], SimpleNamespace(duration=1, language="en")))
    return worker, tmp_path


def test_first_transcription_reports_download_and_load(monkeypatch, prepared):
    worker, tmp_path = prepared
    seen = []
    def download(config, model, **kw):
        if kw.get("on_status"):
            kw["on_status"]("正在连接国内模型仓库…")
        if kw.get("tqdm_class"):
            cls = kw["tqdm_class"]
            cls.set_download_total(100)
            bar = cls(total=100, unit="B")
            bar.update(50)
            bar.close()
        return tmp_path
    monkeypatch.setattr(tc, "download_whisper_model", download)
    transcript = worker.transcribe(tmp_path / "audio.wav", on_progress=lambda f, n: seen.append((f, n)))
    assert transcript.segments[0].text == "hello"
    assert any("国内" in note for _, note in seen)
    assert any("50%" in note for _, note in seen)
    assert any("载入" in note for _, note in seen)
    assert all(f == 0 for f, _ in seen[:-1])
    assert seen[-1] == (1, "转写完成")


def test_cancelled_download_is_not_retried(monkeypatch):
    import huggingface_hub
    from faster_whisper import utils
    calls = []
    monkeypatch.setattr(utils, "download_model", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(tc, "_configure_whisper_http", lambda cfg: None)
    def cancelled(*a, **k):
        calls.append(k)
        raise AppError(Codes.TASK_CANCELLED, "任务已取消")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", cancelled)
    with pytest.raises(AppError) as err:
        tc.download_whisper_model(Config({}), "custom/model", check_cancel=lambda: None)
    assert err.value.code == Codes.TASK_CANCELLED
    assert len(calls) == 1
    assert not tc._whisper_download_lock.locked()


def test_progress_cancel_is_not_swallowed():
    def cancel():
        raise AppError(Codes.TASK_CANCELLED, "任务已取消")
    cls = tc.whisper_progress_tqdm(lambda f: None, check_cancel=cancel)
    with pytest.raises(AppError) as err:
        cls(total=100, unit="B")
    assert err.value.code == Codes.TASK_CANCELLED


def test_transcribe_waiting_for_model_lock_can_cancel(monkeypatch, prepared):
    worker, tmp_path = prepared
    finished = threading.Event()
    errors = []
    worker._lock.acquire()
    monkeypatch.setattr(worker, "_transcribe_inner", lambda *a, **k: None)
    def progress(frac, note):
        raise AppError(Codes.TASK_CANCELLED, "任务已取消")
    def call():
        try:
            worker.transcribe(tmp_path / "audio.wav", on_progress=progress)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    thread = threading.Thread(target=call)
    thread.start()
    try:
        assert finished.wait(0.7), "cancelled task remained blocked by another model operation"
    finally:
        worker._lock.release()
        thread.join(timeout=2)
    assert isinstance(errors[0], AppError)
    assert errors[0].code == Codes.TASK_CANCELLED


def test_cancel_after_loading_prevents_decode(monkeypatch, prepared):
    import faster_whisper
    worker, tmp_path = prepared
    loaded = []
    decoded = []
    monkeypatch.setattr(tc, "download_whisper_model", lambda *a, **k: tmp_path)
    monkeypatch.setattr(faster_whisper, "WhisperModel", lambda *a, **k: loaded.append(True) or object())
    monkeypatch.setattr(worker, "_run_model", lambda *a, **k: decoded.append(True))
    def progress(frac, note):
        if loaded:
            raise AppError(Codes.TASK_CANCELLED, "任务已取消")
    with pytest.raises(AppError) as err:
        worker.transcribe(tmp_path / "audio.wav", on_progress=progress)
    assert err.value.code == Codes.TASK_CANCELLED
    assert not decoded
