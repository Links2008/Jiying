"""在隔离数据目录启动冻结引擎，验证真实 API 版本、鉴权和退出。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests


def unprotect(encoded: str) -> str:
    # 复用产品中 Windows 用户级 DPAPI 的实现。
    source = Path(__file__).resolve().parents[1] / "video-subtitler"
    sys.path.insert(0, str(source))
    from app.core.windows_data import unprotect as read_credential

    return read_credential(encoded)


def main(engine: Path, state: Path):
    version = (engine.parent / "_internal/app/VERSION").read_text(encoding="utf-8").strip()
    env = os.environ | {"LOCALAPPDATA": str(state), "APPDATA": str(state), "TEMP": str(state), "TMP": str(state),
                        "HF_HOME": str(state / "hf"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    data = state / "VideoSubtitler"
    data.mkdir()
    (data / "config.yaml").write_text("update:\n  yt_dlp_auto: false\n", encoding="utf-8")
    name = hashlib.sha256(os.path.normcase(str(engine.parent)).encode("utf-8")).hexdigest() + ".json"
    descriptor = data / "runtime" / name
    process = subprocess.Popen([str(engine)], env=env, cwd=engine.parent, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with requests.Session() as http:
            http.trust_env = False
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"冻结引擎提前退出 {process.returncode}")
                try:
                    record = json.loads(descriptor.read_text(encoding="utf-8"))
                    assert record["pid"] == process.pid
                    api = f"http://127.0.0.1:{record['port']}"
                    headers = {"Authorization": "Bearer " + unprotect(record["token_dpapi"])}
                    if http.get(api + "/api/v1/session/health", headers=headers, timeout=1).status_code == 204:
                        break
                except (FileNotFoundError, requests.RequestException):
                    pass
                time.sleep(.2)
            else:
                raise TimeoutError("冻结引擎未就绪")

            assert http.get(api + "/api/v1/tasks", timeout=3).status_code == 401
            doc = http.get(api + "/openapi.json", headers=headers, timeout=4)
            doc.raise_for_status()
            assert doc.json()["info"]["version"] == version
            assert http.post(api + "/api/v1/session/exit", headers=headers, timeout=4).status_code == 202
            assert process.wait(timeout=10) == 0
        print(f"冻结引擎版本、会话及退出通过，版本 {version}")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=6)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
