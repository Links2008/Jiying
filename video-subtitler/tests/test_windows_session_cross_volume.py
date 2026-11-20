import json
import os

import pytest

from app.api import local_session


@pytest.mark.skipif(os.name != "nt", reason="Windows 会话登记")
@pytest.mark.parametrize("code", [17, 5])
def test_session_registration_when_replace_fails(tmp_path, monkeypatch, code):
    import ctypes
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    target = local_session._session_file(tmp_path / "engine")
    target.parent.mkdir(parents=True)
    previous = b'{"pid":1}'
    target.write_bytes(previous)

    def rejected(source, destination):
        assert source.parent == destination.parent
        raise ctypes.WinError(code)

    monkeypatch.setattr(local_session.os, "replace", rejected)
    if code == 5:
        with pytest.raises(OSError) as raised:
            with local_session.windows_session(tmp_path / "engine", 43129):
                pytest.fail("权限错误应传回调用方")
        assert raised.value.winerror == 5
        assert target.read_bytes() == previous
    else:
        with local_session.windows_session(tmp_path / "engine", 43129) as token:
            record = json.loads(target.read_bytes())
            assert (record["pid"], record["port"]) == (os.getpid(), 43129)
            assert local_session.windows_data.unprotect(record["token_dpapi"]) == token
            assert token not in target.read_text()
        assert not target.exists()
    assert not list(target.parent.glob(".session-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows 真实跨卷登记")
def test_real_second_volume(tmp_path, monkeypatch):
    import tempfile
    from pathlib import Path

    other = os.environ.get("JIYING_TEST_SECOND_VOLUME")
    if not other or Path(other).resolve().drive.lower() == tmp_path.resolve().drive.lower():
        pytest.skip("指定另一卷的测试目录以验证真实 WinError 17")
    create = tempfile.NamedTemporaryFile
    with tempfile.TemporaryDirectory(dir=other) as staging:
        def cross_volume_temp(**kwargs):
            return create(**(kwargs | {"dir": staging}))

        monkeypatch.setattr(local_session.tempfile, "NamedTemporaryFile", cross_volume_temp)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "中文 用户"))
        engine = tmp_path / "安装 路径" / "engine"
        for port in (41981, 41982):
            target = local_session._session_file(engine)
            with local_session.windows_session(engine, port) as token:
                record = json.loads(target.read_bytes())
                assert record["port"] == port
                assert local_session.windows_data.unprotect(record["token_dpapi"]) == token
            assert not target.exists()
            assert not list(Path(staging).iterdir())
