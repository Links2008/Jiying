"""核对 Python wheel 的版本来源以及已包含的运行时资源。"""
from email.parser import BytesParser
from pathlib import Path
from sys import argv
from zipfile import ZipFile

from packaging.version import Version


repo = Path(__file__).resolve().parents[1]
version = (repo / "video-subtitler/app/VERSION").read_text(encoding="utf-8").strip()
wheel, = Path(argv[1]).glob("videosubtitler-*.whl")
with ZipFile(wheel) as archive:
    metadata, = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    headers = BytesParser().parsebytes(archive.read(metadata))
    assert Version(headers["Version"]) == Version(version)
    assert archive.read("app/VERSION").decode("utf-8").strip() == version
    modules = {
        "app/api/server.py", "app/api/runtime.py", "app/api/task_routes.py",
        "app/pipeline/speech.py", "app/pipeline/rendering.py", "app/services/whisper_models.py",
    }
    assert modules.issubset(archive.namelist())
print(f"Python wheel 与运行时版本一致，版本 {version}")
