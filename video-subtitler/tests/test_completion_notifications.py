"""完成提醒由引擎触发，界面关闭后仍可发送；测试不启动真实通知进程。"""
import json
from pathlib import Path
from types import SimpleNamespace

from app.services import completion_notifications as notifications


def test_frozen_engine_notifies_without_a_desktop_connection(monkeypatch, tmp_path):
    engine = tmp_path / 'program/engine'
    engine.mkdir(parents=True)
    helper = engine.parent / 'Jiying.Native.exe'
    helper.write_bytes(b'isolated helper fixture')
    monkeypatch.setattr(notifications.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(notifications, 'app_root', lambda: engine)
    calls = []
    monkeypatch.setattr(notifications.subprocess, 'Popen', lambda *a, **k: calls.append((a, k)))
    notifications.notify_completed('task-1', '视频 "标题" & <字符>')
    assert calls[0][0][0] == [str(helper), '--completion-notify', 'task-1', '视频 "标题" & <字符>']
    assert not calls[0][1].get('shell')


def test_disabled_notifications_do_not_launch_helper(monkeypatch, tmp_path):
    folder = tmp_path / 'Jiying'
    folder.mkdir()
    (folder / 'native-ui.json').write_text(json.dumps({'notify': False}), encoding='utf-8')
    monkeypatch.setattr(notifications.sys, 'frozen', True, raising=False)
    engine = tmp_path / 'program/engine'
    engine.mkdir(parents=True)
    (engine.parent / 'Jiying.Native.exe').write_bytes(b'fixture')
    monkeypatch.setattr(notifications, 'app_root', lambda: engine)
    monkeypatch.setattr(notifications.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(AssertionError('不应启动')))
    notifications.notify_completed('task-1', '测试标题')


def test_notification_error_does_not_fail_completed_task(monkeypatch, tmp_path):
    engine = tmp_path / 'program/engine'
    engine.mkdir(parents=True)
    (engine.parent / 'Jiying.Native.exe').write_bytes(b'fixture')
    monkeypatch.setattr(notifications.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(notifications, 'app_root', lambda: engine)
    monkeypatch.setattr(notifications.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(OSError('模拟通知启动失败')))
    notifications.notify_completed('task-1', '测试标题')
