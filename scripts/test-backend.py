"""从 video-subtitler 运行指定 SQLite 实际加载环境的后端回归。"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


def main(library: str, args: list[str]) -> int:
    if library != "-":
        from ctypes import WinDLL

        sqlite_dll = WinDLL(str(Path(library).resolve(strict=True)))
    import sqlite3

    import pytest

    sys.path.insert(0, str(Path.cwd()))
    from app.models.db import Store, _wal_supported

    with TemporaryDirectory(prefix="jiying-sqlite-") as temp:
        path = Path(temp) / "test.db"
        store = Store(path)
        try:
            version = store._conn.execute("SELECT sqlite_version()").fetchone()[0]
            journal_mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            store.close()
        expected_mode = "wal" if _wal_supported(path) else "delete"
        print(f"SQLite {version}, journal_mode={journal_mode}", flush=True)
        if library != "-" and (version != "3.54.1" or journal_mode != "wal"):
            raise RuntimeError("回归未加载发行版 SQLite 或未启用 WAL")
        if journal_mode != expected_mode:
            raise RuntimeError(f"测试磁盘日志模式不匹配，预期 {expected_mode}")
    return pytest.main(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
