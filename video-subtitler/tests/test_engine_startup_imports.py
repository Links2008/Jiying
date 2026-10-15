"""服务导入阶段不提前载入翻译HTTP客户端或推理库。"""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize('initialize', [False, True])
def test_api_startup_does_not_load_optional_clients_or_inference(tmp_path, initialize):
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path), PYTHONUTF8="1")
    code = "import sys,json; import app.api.server as server\n"
    if initialize:
        code += ("from app.services import ytdlp_updater\n"
                 "server._ensure_aria2c=lambda config: None\n"
                 "ytdlp_updater.auto_update=lambda config: None\n"
                 "server.create_app()\n")
    code += "print(json.dumps([n for n in ('requests','qrcode','PIL','faster_whisper','ctranslate2','numpy','torch') if n in sys.modules]))"
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                            capture_output=True, text=True, encoding="utf-8", env=env,
                            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == []
