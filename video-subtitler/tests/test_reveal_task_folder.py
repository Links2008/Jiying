"""记录能选中路径含中文和空格的历史任务文件。"""
import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.api import server


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 资源管理器")
def test_reveal_uses_argv_for_registered_windows_path(tmp_path, monkeypatch):
    folder = tmp_path / "即影 输出"
    folder.mkdir()
    sample = folder / "原素材 中文 示例.mp4"
    sample.write_bytes(b"demo")
    client = TestClient(server.create_app(), base_url="http://127.0.0.1")
    task = client.app.state.runtime.runner.create_task("https://example.test/sample", "mixkit-40913-4k", [], "none")
    client.app.state.runtime.store.add_artifact(task["id"], "video", str(sample), "mixkit-40913-4k")
    client.app.state.runtime.store.finish_task(task["id"], status="completed")
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append(args))
    details = client.get(f"/api/v1/tasks/{task['id']}")
    assert details.status_code == 200
    assert details.json()["artifacts"][0]["path"] == str(sample)
    response = client.post(f"/api/v1/tasks/{task['id']}/reveal")
    assert response.status_code == 200
    assert len(calls) == 1 and isinstance(calls[0], list)
    assert Path(calls[0][0]).name.lower() == "explorer.exe"
    assert calls[0][1:] == ["/select,", str(sample)]
    assert sample.read_bytes() == b"demo"
