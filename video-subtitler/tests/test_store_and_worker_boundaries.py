"""真实数据库迁移、并发快照与后台容量边界。"""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from app.core.workers import WorkerPool
from app.core.config import Config
from app.models import db
from app.pipeline.runner import PipelineRunner


def test_legacy_rows_migrate_without_reissuing_artifact_ids(tmp_path):
    path = tmp_path / "legacy.db"
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fixture")
    with sqlite3.connect(path) as connection:
        connection.executescript(db._SCHEMA.replace(" REFERENCES task(id) ON DELETE CASCADE", ""))
        connection.execute("INSERT INTO task(id,url,url_key,created_at) VALUES ('old','local','local','fixture')")
        connection.execute("INSERT INTO artifact(id,task_id,kind,path,created_at) VALUES (13,'old','video',?,'fixture')",
                           (str(media),))
        connection.execute("INSERT INTO artifact(id,task_id,kind,path,created_at) VALUES (31,'missing','video',?,'fixture')",
                           (str(media),))
        connection.execute("INSERT INTO task_stage(task_id,stage,status) VALUES ('old','download','done')")
    store = db.Store(path)
    try:
        assert store.task_snapshot(include_artifacts=True)[0]["artifacts"][0]["id"] == 13
        assert store._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "missing" in store._conn.execute("SELECT previous_record FROM legacy_orphan").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            store.add_artifact("absent", "video", str(media))
        other = store.create_task("history", "best", [], "none")["id"]
        store.add_artifact(other, "video", str(media))
        assert store.list_artifacts(other)[0]["id"] > 31
        assert store.artifact_owned_by_other(other, str(media))
        store.delete_task("old")
        assert store.stage_status("old", "download") != "done"
        assert not store.artifact_owned_by_other(other, str(media))
        assert media.read_bytes() == b"fixture"
    finally:
        store.close()


def test_wal_reader_serves_last_committed_snapshot_while_writer_is_active(tmp_path):
    store = db.Store(tmp_path / "concurrent.db")
    try:
        if not store.wal_enabled:
            pytest.skip("需要带官方 WAL 修复的 SQLite")
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        task = store.create_task("fixture", "best", [], "none")
        with ThreadPoolExecutor(1) as pool:
            with store._lock, store._conn:
                store._conn.execute("UPDATE task SET title='pending commit' WHERE id=?", (task["id"],))
                seen = pool.submit(store.get_task, task["id"]).result(timeout=3)
                assert seen["title"] == ""
        assert store.get_task(task["id"])["title"] == "pending commit"
    finally:
        store.close()


def test_worker_pool_bounds_queue_and_releases_slots_after_failure():
    pool = WorkerPool("isolated", 1, 1)
    hold = threading.Event()
    try:
        first = pool.submit(hold.wait, 4)
        queued = pool.submit(lambda: "done")
        assert pool.busy
        with pytest.raises(RuntimeError, match="队列已满"):
            pool.submit(lambda: None)
        hold.set()
        assert first.result(timeout=2)
        assert queued.result(timeout=2) == "done"
        assert pool.submit(lambda: 42).result(timeout=2) == 42
    finally:
        hold.set()
        pool.close()


def test_closed_store_rejects_reads_writes_and_snapshots(tmp_path):
    store = db.Store(tmp_path / "closed.db")
    store.create_task("fixture", "best", [], "none")
    store.close()
    store.close()
    for operation in (store.list_tasks, store.task_snapshot, store.recover_stale_running):
        with pytest.raises(sqlite3.ProgrammingError, match="存储已关闭"):
            operation()


def test_wal_reader_reuses_connection_without_pinning_checkpoint(tmp_path):
    store = db.Store(tmp_path / "reuse.db")
    try:
        if not store.wal_enabled:
            pytest.skip("需要带官方 WAL 修复的 SQLite")
        with store._reader() as reader:
            assert reader.in_transaction
            assert reader.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0
        assert not reader.in_transaction
        store.create_task("fixture", "best", [], "none")
        assert store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        with store._reader() as again:
            assert again is reader
            assert again.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 1
    finally:
        store.close()


def test_wal_close_waits_for_active_snapshot(tmp_path):
    store = db.Store(tmp_path / "shutdown.db")
    if not store.wal_enabled:
        store.close()
        pytest.skip("需要带官方 WAL 修复的 SQLite")
    ready, finish, closing = (threading.Event() for _ in range(3))

    def read():
        with store._reader() as reader:
            ready.set()
            assert finish.wait(3)
            reader.execute("SELECT COUNT(*) FROM task").fetchone()
            return reader

    def close():
        closing.set()
        store.close()

    with ThreadPoolExecutor(2) as pool:
        active = pool.submit(read)
        try:
            assert ready.wait(2)
            shutdown = pool.submit(close)
            assert closing.wait(2)
            with pytest.raises(TimeoutError):
                shutdown.result(timeout=.08)
        finally:
            finish.set()
        reader = active.result(timeout=2)
        shutdown.result(timeout=2)
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            reader.execute("SELECT 1")


def test_wal_existing_reader_reclaims_finished_thread_connection(tmp_path, monkeypatch):
    clock = [10.]
    monkeypatch.setattr(db, "monotonic", lambda: clock[0])
    store = db.Store(tmp_path / "finished-threads.db")
    try:
        if not store.wal_enabled:
            pytest.skip("需要带官方 WAL 修复的 SQLite")
        store.list_tasks()
        worker = threading.Thread(target=store.list_tasks)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        old_connection = store._readers[worker]
        clock[0] += 6
        store.list_tasks()
        assert worker not in store._readers
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            old_connection.execute("SELECT 1")
    finally:
        store.close()


def test_standalone_render_resources_follow_runner_lifetime():
    store = db.Store(":memory:")
    runner = PipelineRunner(Config({}), store)
    try:
        assert runner._standalone_renders is None
        assert runner.submit_render(lambda: "done").result(timeout=2) == "done"
        runner.close()
        assert runner._standalone_renders is None
        with pytest.raises(RuntimeError, match="队列已关闭"):
            runner.submit_render(lambda: None)
    finally:
        runner.close()
        store.close()
