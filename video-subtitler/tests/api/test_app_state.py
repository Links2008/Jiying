"""两个应用实例应各自持有配置、任务库和事件水位。"""
from fastapi.testclient import TestClient

from app.api.server import create_app


def test_application_instances_keep_separate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "first"))
    first = create_app()
    first_state = first.state.runtime
    first_state.config.set("pipeline.max_concurrent", 3)
    task = first_state.runner.create_task("https://example.test/first", "best", [], "none")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "second"))
    second = create_app()
    second_state = second.state.runtime
    with TestClient(first, base_url="http://127.0.0.1") as a, TestClient(second, base_url="http://127.0.0.1") as b:
        assert a.get("/api/v1/config").json()["max_concurrent"] == 3
        assert b.get("/api/v1/config").json()["max_concurrent"] == 2
        listing = a.get("/api/v1/tasks")
        assert [item["id"] for item in listing.json()] == [task["id"]]
        assert int(listing.headers["X-Jiying-Event-Sequence"]) > 0
        assert b.get("/api/v1/tasks").json() == []
        assert b.get("/api/v1/tasks").headers["X-Jiying-Event-Sequence"] == "0"
    assert first_state.components is not second_state.components
