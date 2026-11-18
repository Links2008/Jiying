"""双流并发、连接预算、失败收尾与进度的行为验收。"""
import threading
import pytest
from app.core.errors import AppError, Codes
from app.services.downloader import native_ranges
from app.services.engines.base import DownloadProgress
from test_bilibili_native_download import prepare_engine


def test_audio_and_video_overlap_with_bounded_total_connections(monkeypatch, tmp_path):
    fixture = prepare_engine(monkeypatch, tmp_path)
    rendezvous = threading.Barrier(2, timeout=1)
    budgets, events = [], []
    def download(ydl, target, directory, on_progress=None, **kwargs):
        budgets.append(kwargs['concurrency'])
        rendezvous.wait()
        on_progress(DownloadProgress(fraction=.5, speed_bps=1000, backend='native-range'))
        path = directory / target.filename
        path.write_bytes(b'isolated stream')
        return path
    monkeypatch.setattr(native_ranges, 'download_ranges', download)
    result = fixture.engine.download('https://www.bilibili.com/video/BVsample', '30116', tmp_path,
                                     on_progress=events.append)
    assert result == [tmp_path / 'clip.mp4']
    assert len(budgets) == 2 and sum(budgets) <= 16
    fractions = [event.fraction for event in events]
    assert fractions == sorted(fractions) and fractions[-1] < 1


def test_failed_stream_stops_sibling_and_never_muxes(monkeypatch, tmp_path):
    fixture = prepare_engine(monkeypatch, tmp_path)
    rendezvous = threading.Barrier(2, timeout=1)
    stopped = threading.Event()
    failure = AppError(Codes.NET_FAILED, 'isolated primary failure')
    def download(ydl, target, directory, on_progress=None, **kwargs):
        rendezvous.wait()
        if target.kind == 'video':
            raise failure
        event = kwargs.get('stop_event')
        if event and event.wait(2):
            stopped.set()
        raise AppError(Codes.NET_FAILED, 'isolated sibling stopped')
    monkeypatch.setattr(native_ranges, 'download_ranges', download)
    with pytest.raises(AppError) as caught:
        fixture.engine.download('https://www.bilibili.com/video/BVsample', '30116', tmp_path)
    assert caught.value is failure and stopped.is_set() and not fixture.mux
