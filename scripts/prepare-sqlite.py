"""获取 Windows x64 构建使用的官方 SQLite DLL，核对发布 SHA3-256。"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import sys
from urllib.request import urlopen
from zipfile import ZipFile

URL = "https://www.sqlite.org/2026/sqlite-dll-win-x64-3540100.zip"
SHA3 = "c2d09c8babb9399277055c057c97a53f1ffd7c5f11d0c1f2118ffdfa39f2daa4"


def prepare(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "sqlite-3540100.zip"
    if not archive.is_file():
        with urlopen(URL, timeout=30) as response:
            payload = response.read(4 * 1024 * 1024)
        if hashlib.sha3_256(payload).hexdigest() != SHA3:
            raise RuntimeError("SQLite 发布包校验失败")
        archive.write_bytes(payload)
    payload = archive.read_bytes()
    if hashlib.sha3_256(payload).hexdigest() != SHA3:
        raise RuntimeError("本地 SQLite 发布包校验失败")
    dll = folder / "sqlite3.dll"
    with ZipFile(io.BytesIO(payload)) as bundle:
        dll.write_bytes(bundle.read("sqlite3.dll"))
    return dll


if __name__ == "__main__":
    print(prepare(Path(sys.argv[1]).resolve()))
