import pytest

from app.core import cancel
from app.core.errors import AppError, Codes
from app.services import renderer


def test_cancel_during_process_registration_reaps_child(monkeypatch):
    class Process:
        killed = False
        waited = False

        def poll(self):
            return -1 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            self.waited = True
            return -1

    process = Process()
    monkeypatch.setattr(renderer.subprocess, 'Popen', lambda *a, **kw: process)
    original_register = cancel.register

    def racing_register(task_id, child):
        cancel.cancel(task_id)  # Cancel lands after Popen, before registration.
        original_register(task_id, child)

    monkeypatch.setattr(cancel, 'register', racing_register)
    try:
        with pytest.raises(AppError) as error:
            renderer._run(['ffmpeg'], task_id='cancel-registration-race')
        assert error.value.code == Codes.TASK_CANCELLED
        assert process.killed and process.waited
    finally:
        cancel.clear('cancel-registration-race')
