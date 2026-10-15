"""设置读取模型、显卡状态时不能提前载入语音推理运行库。"""
import os
import subprocess
import sys
from pathlib import Path


def test_status_probes_do_not_import_inference_runtime(tmp_path):
    environment = dict(os.environ, LOCALAPPDATA=str(tmp_path), HF_HUB_CACHE=str(tmp_path / 'hf'))
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c',
        "from app.services.transcriber import local_whisper_model, _has_cuda; "
        "import sys; local_whisper_model('small'); _has_cuda(); "
        "assert 'faster_whisper' not in sys.modules; assert 'ctranslate2' not in sys.modules"],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
