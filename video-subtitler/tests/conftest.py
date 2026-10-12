"""Tests must never recover or modify the running application's task database."""
import pytest


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    from app.core import paths
    from app.api import server
    from app.services import ytdlp_updater

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(paths, "is_portable", lambda: False)
    monkeypatch.setattr(paths, "default_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(server, "_ensure_aria2c", lambda *_: None)
    monkeypatch.setattr(ytdlp_updater, "activate_vendored", lambda: None)
    monkeypatch.setattr(ytdlp_updater, "auto_update", lambda *_: None)
