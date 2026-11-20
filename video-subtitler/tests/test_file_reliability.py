import ctypes
import json
import os
from pathlib import Path

import pytest

from app.api import local_session
from app.core import config
from app.services.downloader import native_ranges


@pytest.mark.skipif(os.name != "nt", reason="Windows 文件占用")
@pytest.mark.parametrize("code", [32, 33])
@pytest.mark.parametrize("operation", ["session", "config"])
def test_transient_lock(tmp_path, monkeypatch, code, operation):
    previous = os.replace
    calls = []

    def locked_once(source, target):
        calls.append(target)
        if len(calls) == 1:
            raise ctypes.WinError(code)
        return previous(source, target)

    monkeypatch.setattr(os, "replace", locked_once)
    if operation == "session":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        with local_session.windows_session(tmp_path, 41311) as token:
            record = json.loads(local_session._session_file(tmp_path).read_bytes())
            assert local_session.windows_data.unprotect(record["token_dpapi"]) == token
    else:
        target = tmp_path / "config.yaml"
        config._write_mapping(target, {"model": "tiny"})
        assert config.yaml.safe_load(target.read_text()) == {"model": "tiny"}
    assert len(calls) == 2


def test_checkpoint_io_error_keeps_downloaded_bytes(tmp_path, monkeypatch):
    attempt = tmp_path / "video"
    partial = attempt.with_suffix(".part")
    checkpoint = attempt.with_suffix(".ytdl")
    partial.write_bytes(b"data")
    checkpoint.write_bytes(b'{"downloader":{"current_fragment":{"index":1}}}')
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise PermissionError("读取暂时失败")
        patch.setattr(Path, "read_text", unavailable)
        with pytest.raises(PermissionError):
            native_ranges._prepare_resume(attempt, 4)
    assert partial.read_bytes() == b"data"
    assert checkpoint.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows 持续占用")
def test_persistent_lock_keeps_previous_config(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    target.write_bytes(b"model: tiny")
    def locked(*args):
        raise ctypes.WinError(32)
    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(OSError) as error:
        config._write_mapping(target, {"model": "base"})
    assert error.value.winerror == 32
    assert target.read_bytes() == b"model: tiny"


@pytest.mark.skipif(os.name != "nt", reason="真实 Windows 文件句柄")
def test_replace_recovers_after_reader_releases_target(tmp_path, monkeypatch):
    from threading import Event, Thread

    target = tmp_path / "config.yaml"
    target.write_bytes(b"model: old")
    ready, failed = Event(), Event()
    def reader():
        with target.open("rb"):
            ready.set()
            failed.wait(3)
    thread = Thread(target=reader)
    original = os.replace
    def observe(source, destination):
        try:
            return original(source, destination)
        except PermissionError:
            failed.set()
            raise
    monkeypatch.setattr(os, "replace", observe)
    thread.start()
    try:
        assert ready.wait(2)
        config._write_mapping(target, {"model": "tiny"})
        assert failed.is_set()
        assert config.yaml.safe_load(target.read_text()) == {"model": "tiny"}
    finally:
        failed.set()
        thread.join()


@pytest.mark.skipif(os.name != "nt", reason="Windows 下载文件占用")
def test_cancel_while_output_locked_keeps_both_files(tmp_path):
    from app.core.paths import replace_with_retry

    current, candidate = tmp_path / "video.mp4", tmp_path / "video.part"
    current.write_bytes(b"previous completed video")
    candidate.write_bytes(b"new download")

    class Cancelled(Exception):
        pass

    def cancel():
        raise Cancelled()

    with current.open("rb"), pytest.raises(Cancelled):
        replace_with_retry(candidate, current, before_retry=cancel)
    assert current.read_bytes() == b"previous completed video"
    assert candidate.read_bytes() == b"new download"
