"""Bilibili range routing regressions; no remote requests or user task data."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services import renderer
from app.services.downloader import native_ranges
from app.services.engines import ytdlp_engine
from app.services.engines.base import DirectTarget, DownloadProgress


def prepare_engine(monkeypatch, tmp_path, native_error=None, partial=False, mux_error=None):
    native_calls, aria_calls, resolved, mux_calls, fallback_calls = [], [], [], [], []
    targets = [DirectTarget("https://example.test/video", "clip.f30116.mp4", kind="video"),
               DirectTarget("https://example.test/audio", "clip.f30280.m4a", kind="audio")]

    class Backend:
        def __init__(self, _): aria_calls.append("backend")
        def ping(self): return True
        def download_many(self, streams, directory, *args, **kwargs):
            files = [directory / t.filename for t in streams]
            for path in files:
                path.write_bytes(b"aria output")
            return files

    def range_download(ydl, target, directory, on_progress=None, task_id=None, concurrency=16):
        native_calls.append((target, directory, task_id, concurrency))
        output = directory / target.filename
        if native_error:
            if partial:
                output.with_name(output.name + ".part").write_bytes(b"partial bytes")
            raise native_error
        output.write_bytes(b"native output")
        if on_progress:
            on_progress(DownloadProgress(fraction=1, backend="native-range"))
        return output

    def merge(config, video, audio, merged, **kwargs):
        mux_calls.append((video, audio))
        merged.write_bytes(b"merged output")
        if mux_error:
            raise mux_error
        return merged

    def extract(*args, **kwargs):
        fallback_calls.append(args)
        raise AssertionError("native range failure must not restart via extract_info")

    @contextmanager
    def youtube_dl(options):
        yield SimpleNamespace(params=options, extract_info=extract)

    def split(url, resolution):
        resolved.append((url, resolution))
        return targets

    monkeypatch.setattr(native_ranges, "download_ranges", range_download)
    monkeypatch.setattr(ytdlp_engine, "Aria2cBackend", Backend)
    monkeypatch.setattr(renderer, "mux_av", merge)
    engine = ytdlp_engine.YtDlpEngine(Config({"download": {"aria2c": {"enabled": True}}}))
    monkeypatch.setattr(engine, "_youtube_dl", youtube_dl)
    monkeypatch.setattr(engine, "_head_direct_file", lambda *_: None)
    monkeypatch.setattr(engine, "_resolve_split_streams", split)
    monkeypatch.setattr(engine, "_resolve_direct_stream", lambda *_: (_ for _ in ()).throw(
        AssertionError("unexpected direct fallback")))
    return SimpleNamespace(engine=engine, targets=targets, native=native_calls, aria=aria_calls,
                           resolved=resolved, mux=mux_calls, fallback=fallback_calls)


@pytest.mark.parametrize("url", ["https://www.bilibili.com/video/BVexample/",
                                 "https://m.bilibili.com/video/BVexample/", "https://b23.tv/example"])
def test_bilibili_exact_split_uses_native_ranges_and_original_mux(monkeypatch, tmp_path, url):
    h = prepare_engine(monkeypatch, tmp_path)
    files = h.engine.download(url, "30116", tmp_path, task_id="route-test")
    assert h.resolved == [(url, "30116")]
    assert [call[0] for call in h.native] == h.targets
    assert all(call[1:] == (tmp_path, "route-test", 16) for call in h.native)
    assert h.aria == [] and h.fallback == []
    assert h.mux == [(tmp_path / h.targets[0].filename, tmp_path / h.targets[1].filename)]
    assert files == [tmp_path / "clip.mp4"]


@pytest.mark.parametrize("url", ["https://youtube.com/watch?v=example",
                                 "https://bilibili.com.example.test/watch"])
def test_other_sites_keep_aria_split_download(monkeypatch, tmp_path, url):
    h = prepare_engine(monkeypatch, tmp_path)
    files = h.engine.download(url, "30116", tmp_path)
    assert h.native == [] and h.aria == ["backend"] and h.fallback == []
    assert len(h.mux) == 1 and files == [tmp_path / "clip.mp4"]


@pytest.mark.parametrize("code,partial", [(Codes.ENG_FAILED, False),
                                         (Codes.NET_FAILED, True), (Codes.TASK_CANCELLED, True)])
def test_native_range_error_never_falls_back_or_redownloads(monkeypatch, tmp_path, code, partial):
    error = AppError(code, "range interrupted")
    h = prepare_engine(monkeypatch, tmp_path, native_error=error, partial=partial)
    with pytest.raises(AppError) as caught:
        h.engine.download("https://www.bilibili.com/video/BVexample/", "30116", tmp_path)
    assert caught.value is error
    assert len(h.native) == 1 and h.aria == [] and h.fallback == [] and h.mux == []
    assert not list(tmp_path.parent.glob(tmp_path.name + ".invalid-*"))
    if partial:
        assert (tmp_path / (h.targets[0].filename + ".part")).read_bytes() == b"partial bytes"


def test_native_range_mux_corruption_keeps_existing_quarantine(monkeypatch, tmp_path):
    error = AppError(Codes.RENDER_FAILED, "ffmpeg 视频数据损坏", detail="Invalid NAL unit size")
    h = prepare_engine(monkeypatch, tmp_path, mux_error=error)
    with pytest.raises(AppError) as caught:
        h.engine.download("https://www.bilibili.com/video/BVexample/", "30116", tmp_path)
    assert caught.value is error
    assert len(h.native) == 2 and h.aria == [] and h.fallback == []
    assert all(not (tmp_path / t.filename).exists() for t in h.targets)
    assert not (tmp_path / "clip.mp4").exists()
    saved = list(tmp_path.parent.glob(tmp_path.name + ".invalid-*"))
    assert len(saved) == 1
    assert {p.name for p in saved[0].iterdir()} == {t.filename for t in h.targets} | {"clip.mp4"}
