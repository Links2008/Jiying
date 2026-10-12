"""aria2c 后端测试：FakeClient 驱动，不依赖真实 RPC 服务。"""
import pytest

from app.core.config import Config
from app.core.errors import AppError, Codes
from app.services.downloader.aria2c import Aria2cBackend, Aria2cRpcClient
from app.services.engines.base import DirectTarget


class FakeClient(Aria2cRpcClient):
    """按预设脚本回放 tellStatus，无需真实 aria2c。"""

    def __init__(self, statuses: list[dict]):
        self.statuses = list(statuses)
        self.added: list[tuple[str, dict]] = []
        self.removed: list[str] = []

    def add_uri(self, uri: str, options: dict) -> str:
        self.added.append((uri, options))
        return "gid-test"

    def tell_status(self, gid: str) -> dict:
        return self.statuses.pop(0) if self.statuses else {"status": "complete"}

    def remove(self, gid: str) -> None:
        self.removed.append(gid)


def make_cfg() -> Config:
    return Config({
        "download": {"aria2c": {
            "rpc_url": "http://127.0.0.1:6800/jsonrpc", "secret": "",
            "poll_interval": 0.01, "max_conn_per_server": 16,
            "split": 16, "min_split_size": "1M"}},
        "proxy": "",
    })


def test_backend_completes_with_progress(tmp_path):
    out = tmp_path / "v.mp4"
    out.write_bytes(b"data")  # 预置产物，模拟 aria2c 落盘完成
    statuses = [
        {"status": "active", "totalLength": "100", "completedLength": "50",
         "downloadSpeed": "2048"},
        {"status": "complete", "totalLength": "100", "completedLength": "100",
         "downloadSpeed": "0"},
    ]
    client = FakeClient(statuses)
    backend = Aria2cBackend(make_cfg(), client=client)
    target = DirectTarget(url="http://example.com/v.mp4", filename="v.mp4",
                          headers={"User-Agent": "test-agent"})

    events = []
    got = backend.download(target, tmp_path, on_progress=events.append)
    assert got == out
    assert events[0].backend == "aria2c" and events[0].fraction == 0.5
    assert events[0].speed_bps == 2048.0
    # RPC 参数：多连接分段 + 请求头透传
    uri, options = client.added[0]
    assert uri == "http://example.com/v.mp4"
    assert options["max-connection-per-server"] == "16"
    assert options["split"] == "16"
    assert options["continue"] == "true"
    assert "User-Agent: test-agent" in options["header"]


def test_backend_parallel_streams(tmp_path):
    """双 Job 并行：两条流各自提交、聚合进度、全部完成后返回。"""
    v = tmp_path / "v.f137.mp4"
    a = tmp_path / "a.f140.m4a"
    v.write_bytes(b"v")
    a.write_bytes(b"a")
    statuses = [
        {"status": "active", "totalLength": "200", "completedLength": "60",
         "downloadSpeed": "4096"},
        {"status": "active", "totalLength": "200", "completedLength": "100",
         "downloadSpeed": "1024"},
        {"status": "complete", "totalLength": "200", "completedLength": "200",
         "downloadSpeed": "0"},
        {"status": "complete", "totalLength": "200", "completedLength": "200",
         "downloadSpeed": "0"},
    ]
    client = FakeClient(statuses)
    backend = Aria2cBackend(make_cfg(), client=client)
    targets = [DirectTarget(url="http://x/v", filename="v.f137.mp4"),
               DirectTarget(url="http://x/a", filename="a.f140.m4a")]
    events = []
    outs = backend.download_many(targets, tmp_path, on_progress=events.append)
    assert [o.name for o in outs] == ["v.f137.mp4", "a.f140.m4a"]
    assert len(client.added) == 2                      # 两条流各自提交
    assert events[0].fraction == pytest.approx(160 / 400)  # 聚合进度
    assert events[0].speed_bps == 4096 + 1024          # 速度聚合
    assert events[0].backend == "aria2c"


def test_backend_cancel_removes_jobs(tmp_path):
    """取消：aria2c 里的任务被撤回，抛出统一取消错误码。"""
    class ActiveForever(FakeClient):
        def tell_status(self, gid):
            return {"status": "active", "totalLength": "100",
                    "completedLength": "10", "downloadSpeed": "1"}

        def remove(self, gid):
            self.removed = getattr(self, "removed", []) + [gid]

    client = ActiveForever([])
    backend = Aria2cBackend(make_cfg(), client=client)
    with pytest.raises(AppError) as ei:
        backend.download(DirectTarget(url="http://x/v.mp4", filename="v.mp4"),
                         tmp_path, cancel_check=lambda: bool(client.added))
    assert ei.value.code == Codes.TASK_CANCELLED
    assert client.removed == ["gid-test"]  # 已向 aria2c 撤回


def test_backend_error_is_retryable(tmp_path):
    backend = Aria2cBackend(make_cfg(), client=FakeClient(
        [{"status": "error", "errorMessage": "HTTP 403"}]))
    with pytest.raises(AppError) as ei:
        backend.download(DirectTarget(url="http://x/v.mp4", filename="v.mp4"),
                         tmp_path)
    assert ei.value.retryable and "403" in ei.value.message


def test_progress_cancellation_stops_rpc_download(tmp_path):
    client = FakeClient([{"status": "active", "completedLength": "10", "totalLength": "100"}])

    def cancelled(_):
        raise AppError(Codes.TASK_CANCELLED, "cancelled during progress callback")

    with pytest.raises(AppError, match="cancelled during"):
        Aria2cBackend(make_cfg(), client=client).download(
            DirectTarget(url="http://example.test/v", filename="v.mp4"),
            tmp_path, on_progress=cancelled)
    assert client.removed == ["gid-test"]


def test_split_error_stops_other_active_stream(tmp_path):
    class SplitError(FakeClient):
        def __init__(self):
            super().__init__([])
            self.removed = []

        def add_uri(self, uri, options):
            gid = f"gid-{len(self.added)}"
            self.added.append((uri, options))
            return gid

        def tell_status(self, gid):
            return {"status": "error" if gid == "gid-0" else "active",
                    "errorMessage": "HTTP 403"}

        def remove(self, gid):
            self.removed.append(gid)

    client = SplitError()
    backend = Aria2cBackend(make_cfg(), client=client)
    targets = [DirectTarget(url=f"http://x/{i}", filename=f"{i}.mp4") for i in range(2)]
    with pytest.raises(AppError, match="HTTP 403"):
        backend.download_many(targets, tmp_path)
    assert "gid-1" in client.removed  # 错误发生后不得继续传输另一条流


def test_backend_timeout(tmp_path):
    class ActiveForever(FakeClient):
        def __init__(self):
            super().__init__([])
            self.removed = []

        def tell_status(self, gid):
            return {"status": "active", "totalLength": "100",
                    "completedLength": "10", "downloadSpeed": "1"}

        def remove(self, gid):
            self.removed.append(gid)

    client = ActiveForever()
    backend = Aria2cBackend(make_cfg(), client=client)
    with pytest.raises(AppError) as ei:
        backend.download(DirectTarget(url="http://x/v.mp4", filename="v.mp4"),
                         tmp_path, timeout=0.05)
    assert ei.value.code == Codes.NET_TIMEOUT
    assert client.removed == ["gid-test"]
