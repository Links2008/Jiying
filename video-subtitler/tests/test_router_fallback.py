"""引擎路由器测试：FakeEngine 验证降级链与探测缓存（不依赖外网）。"""
from pathlib import Path

import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.models.db import Store
from app.services.engines import router as router_mod
from app.services.engines.base import DownloadProgress, EngineProvider, FormatInfo, ProbeResult
from app.services.engines.router import EngineRouter


class FakeSkipEngine(EngineProvider):
    """探测不支持 + 下载失败的引擎（模拟主引擎挂掉）。"""
    name = "a"

    def __init__(self, config):
        self._cfg = config

    def probe(self, url):
        return None

    def download(self, url, resolution, dest_dir, on_progress=None, task_id=None):
        raise AppError(Codes.ENG_FAILED, "引擎 a 下载失败")


class FakeGoodEngine(EngineProvider):
    name = "b"

    def __init__(self, config):
        self._cfg = config

    def probe(self, url):
        return ProbeResult(video_id="1", title="t", formats=[
            FormatInfo(format_id="f1", label="720p", height=720)])

    def download(self, url, resolution, dest_dir, on_progress=None, task_id=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / "out.mp4"
        out.write_bytes(b"fake")
        return [out]


@pytest.fixture()
def make_router(tmp_path, monkeypatch):
    monkeypatch.setattr(router_mod, "_FACTORY",
                        {"a": FakeSkipEngine, "b": FakeGoodEngine})
    cfg = Config({"engines": {"order": ["a", "b"]}})
    return EngineRouter(cfg, Store(tmp_path / "t.db"))


def test_probe_falls_through(make_router):
    engine, result = make_router.probe("https://example.com/v")
    assert engine.name == "b"
    assert result.formats[0].label == "720p"


def test_probe_cache_hit(make_router):
    make_router.probe("https://example.com/v")  # 首次探测写缓存
    engine, _ = make_router.probe("https://example.com/v", use_cache=True)
    assert engine.name == "b"


def test_force_refresh_replaces_cached_quality(make_router, monkeypatch):
    url = "https://example.com/refresh"
    make_router.probe(url)
    fresh = ProbeResult(video_id="1", title="new", auth_status="loaded",
                        quality_notice="4K 可下载", formats=[
                            FormatInfo(format_id="4k", label="4K / 2160p", height=2160)])
    monkeypatch.setattr(make_router._engines[1], "probe", lambda _: fresh)
    assert make_router.probe(url)[1].title == "t"
    assert make_router.probe(url, use_cache=False)[1] == fresh
    assert make_router.probe(url)[1] == fresh


@pytest.mark.parametrize("fallback_first", [False, True])
def test_exact_format_never_switches_to_unmapped_engine(make_router, tmp_path, fallback_first):
    make_router._engines[0].name = "ytdlp"
    if fallback_first:
        make_router._engines.reverse()
    dest = tmp_path / "exact"
    with pytest.raises(AppError, match="引擎 a 下载失败"):
        make_router.download("https://example.com/v", "120-7", dest)
    assert not (dest / "out.mp4").exists()


def test_download_fallback(make_router, tmp_path):
    engine_name, files = make_router.download(
        "https://example.com/v", "best", tmp_path / "dl")
    assert engine_name == "b"  # 引擎 a 失败 → 自动降级到 b
    assert files[0].read_bytes() == b"fake"


def test_download_does_not_restart_after_bytes_transferred(make_router, tmp_path):
    class FakePartialEngine(FakeSkipEngine):
        def download(self, url, resolution, dest_dir, on_progress=None, task_id=None):
            on_progress(DownloadProgress(fraction=0.6))
            raise AppError(Codes.ENG_FAILED, "传输中断")

    make_router._engines[0] = FakePartialEngine(Config({}))
    dest = tmp_path / "partial"
    with pytest.raises(AppError, match="传输中断"):
        make_router.download("https://example.com/v", "best", dest, on_progress=lambda _: None)
    assert not (dest / "out.mp4").exists()


def test_download_does_not_restart_partial_file_without_progress(make_router, tmp_path):
    class FakePartialEngine(FakeSkipEngine):
        def download(self, url, resolution, dest_dir, on_progress=None, task_id=None):
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "video.mp4.part").write_bytes(b"partial")
            raise AppError(Codes.ENG_FAILED, "传输中断")

    make_router._engines[0] = FakePartialEngine(Config({}))
    dest = tmp_path / "partial-no-progress"
    with pytest.raises(AppError, match="传输中断"):
        make_router.download("https://example.com/v", "best", dest)
    assert not (dest / "out.mp4").exists()


def test_probe_all_fail(tmp_path, monkeypatch):
    class FakeBadEngine(EngineProvider):
        name = "bad"

        def __init__(self, config):
            self._cfg = config

        def probe(self, url):
            return None

        def download(self, url, resolution, dest_dir, on_progress=None):
            raise AppError(Codes.ENG_FAILED, "bad")

    monkeypatch.setattr(router_mod, "_FACTORY", {"bad": FakeBadEngine})
    r = EngineRouter(Config({"engines": {"order": ["bad"]}}),
                     Store(tmp_path / "t2.db"))
    with pytest.raises(AppError) as ei:
        r.probe("https://x.example/v")
    assert ei.value.code == Codes.ENG_UNSUPPORTED
