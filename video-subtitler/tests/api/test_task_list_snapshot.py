"""列表快照必须在固定查询次数内返回阶段、进度和产物。"""
from contextlib import contextmanager
from fastapi.testclient import TestClient

from app.api import server
from app.core.events import AppEvent, EventBus


def test_history_snapshot_batches_queries_and_includes_artifacts(tmp_path, monkeypatch):
    with TestClient(server.create_app(), base_url="http://127.0.0.1") as client:
        media = tmp_path / "视频 样本.mp4"
        media.write_bytes(b"fixture")
        done_ids = set()
        for index in range(180):
            task = client.app.state.runtime.store.create_task(f"https://example.com/{index}", "best", [], "none")
            tid = task["id"]
            client.app.state.runtime.store.upsert_stage(tid, "download", "done")
            client.app.state.runtime.store.add_artifact(tid, "video", str(media))
            client.app.state.runtime.store.finish_task(tid, status="completed")
            done_ids.add(tid)
        active = client.app.state.runtime.store.create_task("https://example.com/active", "best", [], "none")["id"]
        client.app.state.runtime.store.update_task(active, status="running")
        client.app.state.runtime.store.upsert_stage(active, "download", "running")
        client.app.state.runtime.runner._set_live(active, stage="download", fraction=.64, speed_bps=1000)

        reads = []
        store = client.app.state.runtime.store
        original = store._reader

        @contextmanager
        def traced():
            with original() as reader:
                reader.set_trace_callback(reads.append)
                try:
                    yield reader
                finally:
                    reader.set_trace_callback(None)

        monkeypatch.setattr(store, "_reader", traced)
        response = client.get("/api/v1/tasks", params={"include_artifacts": True})
        assert response.status_code == 200
        cards = {item["id"]: item for item in response.json()}
        assert cards.keys() == done_ids | {active}
        assert len([sql for sql in reads if sql.startswith("SELECT")]) == 3
        assert response.headers["X-Jiying-Event-Sequence"].isdigit()
        assert all(cards[tid]["artifacts"][0]["size"] == 7 for tid in done_ids)
        assert cards[active]["progress"]["fraction"] == .64
        assert cards[active]["stages"][0]["status"] == "running"

        target = next(iter(done_ids))
        client.app.state.runtime.store.delete_task(target)
        changed = client.get("/api/v1/tasks", params={"include_artifacts": True, "ids": f"{target},{active}"})
        assert [item["id"] for item in changed.json()] == [active]
        assert changed.json()[0]["artifacts"] == []
        assert "artifacts" not in client.get("/api/v1/tasks").json()[0]


def test_event_sequence_detects_events_lost_at_queue_limit():
    bus = EventBus(max_queue=1)
    bus.publish(AppEvent(type="task", task_id="old"))
    subscription, cursor = bus.subscribe_with_sequence()
    bus.publish(AppEvent(type="progress", task_id="old"))
    bus.publish(AppEvent(type="task", task_id="old", status="completed"))
    queued = subscription.get_nowait()
    assert queued.sequence == cursor + 1
    assert bus.sequence == cursor + 2
