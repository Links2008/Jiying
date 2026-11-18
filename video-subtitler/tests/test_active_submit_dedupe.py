from api_test_support import route_endpoint

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest
from fastapi import HTTPException

from app.api import server


@pytest.fixture
def endpoints(monkeypatch):
    app = server.create_app()
    started = []
    monkeypatch.setattr(app.state.runtime, "submit_job", lambda target, *args: started.append(args))
    monkeypatch.setattr(app.state.runtime.router, "probe", lambda *_: ("fake", SimpleNamespace(title="clip")))
    submit = route_endpoint(app, '/api/v1/tasks', 'POST')
    retry = route_endpoint(app, '/api/v1/tasks/{tid}/retry')
    return submit, retry, started, app.state.runtime


def test_identical_active_submit_reuses_task_and_worker(endpoints):
    submit, _, started, _ = endpoints
    body = server.TaskIn(url="https://example.test/clip")
    first, second = submit(body), submit(body)
    assert first["id"] == second["id"]
    assert started == [(first["id"],)]


def test_concurrent_identical_submits_start_only_one_worker(endpoints, monkeypatch):
    submit, _, started, runtime = endpoints
    barrier = threading.Barrier(2)
    def probe(*_):
        barrier.wait(timeout=3)
        return "fake", SimpleNamespace(title="clip")
    monkeypatch.setattr(runtime.router, "probe", probe)
    body = server.TaskIn(url="https://example.test/clip")
    with ThreadPoolExecutor(2) as pool:
        tasks = list(pool.map(lambda _: submit(body), range(2)))
    assert tasks[0]["id"] == tasks[1]["id"]
    assert len(started) == 1


@pytest.mark.parametrize("changed", [
    {"resolution": "1080p"}, {"output_dir": "different-output"},
    {"recognize": True}, {"translate": True, "langs": ["zh"]},
    {"burn": True, "recognize": True, "color": "#112233"},
])
def test_different_output_options_are_separate_tasks(endpoints, changed):
    submit, _, started, _ = endpoints
    first = submit(server.TaskIn(url="https://example.test/clip"))
    second = submit(server.TaskIn(url="https://example.test/clip", **changed))
    assert first["id"] != second["id"]
    assert len(started) == 2


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
def test_terminal_task_does_not_block_explicit_new_submit(endpoints, status):
    submit, _, started, runtime = endpoints
    body = server.TaskIn(url="https://example.test/clip")
    first = submit(body)
    runtime.store.update_task(first["id"], status=status)
    second = submit(body)
    assert first["id"] != second["id"]
    assert len(started) == 2


def test_retry_cannot_duplicate_an_equivalent_active_task(endpoints):
    submit, retry, started, runtime = endpoints
    body = server.TaskIn(url="https://example.test/clip")
    first = submit(body)
    runtime.store.update_task(first["id"], status="cancelled")
    second = submit(body)
    with pytest.raises(HTTPException) as error:
        retry(first["id"])
    assert error.value.status_code == 409
    assert runtime.store.get_task(first["id"])["status"] == "cancelled"
    assert started == [(first["id"],), (second["id"],)]
