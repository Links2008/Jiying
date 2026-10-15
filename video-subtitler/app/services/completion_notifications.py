"""完成事件启动短时原生通知助手，不依赖桌面窗口或前端轮询。"""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from ..core.paths import app_root

log = logging.getLogger(__name__)


def notify_completed(task_id: str, title: str) -> None:
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return
    try:
        settings = Path(os.environ.get('LOCALAPPDATA', '')) / 'Jiying' / 'native-ui.json'
        if settings.is_file() and json.loads(settings.read_text(encoding='utf-8')).get('notify', True) is False:
            return
        executable = app_root().parent / 'Jiying.Native.exe'
        if not executable.is_file():
            return
        subprocess.Popen([str(executable), '--completion-notify', task_id, str(title)[:512]],
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as error:
        log.warning('系统完成通知未发送，任务与已下载文件不受影响：%s', error)
