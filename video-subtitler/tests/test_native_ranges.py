"""Real loopback acceptance for yt-dlp's ordered native Range adapter."""
import hashlib
import random
import re
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app.core import cancel
from app.core.errors import AppError, Codes
from app.services.downloader import native_ranges
from app.services.engines.base import DirectTarget


@pytest.fixture
def range_server(monkeypatch):
    monkeypatch.setattr(native_ranges, "CHUNK_BYTES", 512 * 1024)
    payload = random.Random(17).randbytes(8 * 512 * 1024 + 123)
    state = {"mode": "ok", "active": 0, "peak": 0, "sent": 0, "starts": [], "ends": []}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            if state["mode"] == "head405":
                self.send_error(405)
                return
            self.send_response(200)
            if state["mode"] != "unknown-size":
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()

        def do_GET(self):
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
            if not match:
                with lock:
                    state["starts"].append("unbounded")
                self.send_error(400)
                return
            start, end = map(int, match.groups())
            with lock:
                state["starts"].append(start)
                occurrence = state["starts"].count(start)
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                if state["mode"] == "always503" or (
                        state["mode"] == "retry" and start == 0 and occurrence <= 2):
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, lo, hi, total = 206, start, end, len(payload)
                if state["mode"] == "ignore":
                    status, lo, hi = 200, 0, len(payload) - 1
                elif state["mode"] == "wrong-start":
                    lo += 1
                elif state["mode"] == "wrong-end":
                    hi -= 1
                elif state["mode"] == "wrong-total":
                    total += 1
                self.send_response(status)
                if state["mode"] not in {"missing-range", "ignore"}:
                    self.send_header("Content-Range", f"bytes {lo}-{hi}/{total}")
                self.send_header("Content-Length", str(hi - lo + 1))
                self.end_headers()
                time.sleep(.04 if start == 0 else .005)
                for pos in range(lo, hi + 1, 65536):
                    time.sleep(.02 if state["mode"] == "slow" else .004)
                    chunk = payload[pos:min(pos + 65536, hi + 1)]
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    with lock:
                        state["sent"] += len(chunk)
                    if state["mode"] == "disconnect" and start == 0 and pos >= 65536:
                        self.close_connection = True
                        return  # EOF 后只能续当前片段的实际已写偏移。
            except (ConnectionError, OSError):
                pass
            finally:
                with lock:
                    state["active"] -= 1
                    state["ends"].append(start)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.request_queue_size = 32
    server.handle_error = lambda *_: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/source.mp4"
    try:
        yield DirectTarget(url, "source.mp4"), state, payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def ydl():
    with YoutubeDL({"proxy": "", "quiet": True, "no_warnings": True,
                    "socket_timeout": 2, "noprogress": True,
                    "buffersize": 65536, "noresizebuffer": True}) as downloader:
        yield downloader


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.02)
    assert predicate()


def test_parallel_native_ranges_preserve_source_sha_and_progress(range_server, ydl, tmp_path):
    target, state, payload = range_server
    progress = []
    output = native_ranges.download_ranges(ydl, target, tmp_path, progress.append, None, 4)
    assert output == tmp_path / target.filename and output.parent == tmp_path
    assert hashlib.sha256(output.read_bytes()).digest() == hashlib.sha256(payload).digest()
    assert output.stat().st_size == len(payload)
    assert state["peak"] > 1 and state["ends"] != sorted(state["ends"])
    assert "unbounded" not in state["starts"]
    assert len(state["starts"]) == 9
    fractions = [event.fraction for event in progress]
    assert fractions and fractions == sorted(fractions)


@pytest.mark.parametrize("mode", ["missing-range", "wrong-start", "wrong-end", "wrong-total", "ignore"])
def test_invalid_range_response_never_becomes_success(range_server, ydl, tmp_path, mode):
    target, state, _ = range_server
    state["mode"] = mode
    with pytest.raises(DownloadError, match="Range"):
        native_ranges.download_ranges(ydl, target, tmp_path, None, None, 4)
    assert not (tmp_path / target.filename).exists()
    assert "unbounded" not in state["starts"]


def test_unknown_head_size_rejects_before_media_get(range_server, ydl, tmp_path):
    target, state, _ = range_server
    state["mode"] = "unknown-size"
    with pytest.raises(AppError, match="长度"):
        native_ranges.download_ranges(ydl, target, tmp_path, None, None, 4)
    assert state["starts"] == []


def test_head_http_error_returns_clear_app_error_without_media_get(range_server, ydl, tmp_path):
    target, state, _ = range_server
    state["mode"] = "head405"
    with pytest.raises(AppError) as caught:
        native_ranges.download_ranges(ydl, target, tmp_path, None, None, 4)
    assert caught.value.code == Codes.NET_FAILED
    assert state["starts"] == []


def test_target_filename_cannot_escape_destination(range_server, ydl, tmp_path):
    target, state, _ = range_server
    target.filename = "../escape.mp4"
    with pytest.raises(AppError) as caught:
        native_ranges.download_ranges(ydl, target, tmp_path, None, None, 4)
    assert caught.value.code == Codes.DISK_FAILED
    assert not (tmp_path.parent / "escape.mp4").exists()
    assert state["starts"] == []


def test_503_retries_only_current_fragment_and_is_bounded(range_server, ydl, tmp_path):
    target, state, payload = range_server
    state["mode"] = "retry"
    result = native_ranges.download_ranges(ydl, target, tmp_path, None, None, 4)
    assert result.read_bytes() == payload
    counts = Counter(state["starts"])
    assert counts[0] == 3 and all(count == 1 for start, count in counts.items() if start != 0)
    assert "unbounded" not in counts


def test_503_exhaustion_does_not_skip_fragment(range_server, ydl, tmp_path):
    target, state, _ = range_server
    state["mode"] = "always503"
    with pytest.raises(DownloadError):
        native_ranges.download_ranges(ydl, target, tmp_path, None, None, 2)
    assert not (tmp_path / target.filename).exists()
    assert all(count == 4 for count in Counter(state["starts"]).values())
    assert "unbounded" not in state["starts"]


def test_interrupted_fragment_resumes_exact_offset_and_preserves_sha(range_server, ydl, tmp_path):
    target, state, payload = range_server
    state["mode"] = "disconnect"
    result = native_ranges.download_ranges(ydl, target, tmp_path, None, None, 4)
    assert hashlib.sha256(result.read_bytes()).digest() == hashlib.sha256(payload).digest()
    counts = Counter(state["starts"])
    assert counts[0] == 1 and counts[131072] == 1
    assert len(state["starts"]) == 10 and "unbounded" not in counts


def test_cancel_closes_connections_and_does_not_start_queued_ranges(range_server, ydl, tmp_path):
    target, state, payload = range_server
    state["mode"] = "slow"
    task_id = "range-test-" + uuid.uuid4().hex
    requested_cancel = threading.Event()

    def progress(_):
        if state["peak"] >= 2 and not requested_cancel.is_set():
            requested_cancel.set()
            cancel.cancel(task_id)

    try:
        with pytest.raises(AppError) as caught:
            native_ranges.download_ranges(ydl, target, tmp_path, progress, task_id, 2)
        assert caught.value.code == Codes.TASK_CANCELLED
        assert requested_cancel.is_set()
        wait_for(lambda: state["active"] == 0)
        sent, starts = state["sent"], list(state["starts"])
        time.sleep(.3)
        assert state["sent"] == sent and state["starts"] == starts
        assert len(starts) <= 2 and sent < len(payload)
        assert not (tmp_path / target.filename).exists()
    finally:
        cancel.clear(task_id)


def test_fresh_attempt_never_appends_untracked_old_partial(range_server, ydl, tmp_path):
    target, _, payload = range_server
    (tmp_path / (target.filename + ".part")).write_bytes(b"stale wrong offset")
    (tmp_path / (target.filename + ".native-old.part")).write_bytes(b"older cancelled attempt")
    result = native_ranges.download_ranges(ydl, target, tmp_path, None, None, 4)
    assert result.read_bytes() == payload
