"""SQLite 存储：task / task_stage / artifact / download_job / cache_entry 五张表。

MVP 用标准库 sqlite3（PyInstaller 友好、零额外依赖）；
接口按 DAO 风格收敛，后续换 SQLAlchemy/PostgreSQL 只改这一层。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    url_key       TEXT NOT NULL,
    title         TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending/running/completed/failed/interrupted
    resolution    TEXT DEFAULT 'best',
    target_langs  TEXT DEFAULT '',
    subtitle_mode TEXT DEFAULT 'softmux',            -- none | softmux | burn
    error         TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    finished_at   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_stage (
    task_id     TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',     -- pending/running/done/failed
    error       TEXT DEFAULT '',
    started_at  TEXT DEFAULT '',
    finished_at TEXT DEFAULT '',
    PRIMARY KEY (task_id, stage)
);
CREATE TABLE IF NOT EXISTS artifact (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,        -- video/audio/subtitle/output
    path         TEXT NOT NULL,
    resolution   TEXT DEFAULT '',
    lang         TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_job (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    kind           TEXT DEFAULT 'merged',   -- video/audio/merged（直连 aria2c 时拆分）
    protocol       TEXT DEFAULT 'direct',   -- direct | fragments
    backend        TEXT DEFAULT 'native',   -- native | aria2c
    url            TEXT DEFAULT '',
    status         TEXT DEFAULT 'pending',
    downloaded     INTEGER DEFAULT 0,
    total          INTEGER DEFAULT 0,
    speed_bps      REAL DEFAULT 0,
    backend_job_id TEXT DEFAULT ''           -- aria2 gid
);
CREATE TABLE IF NOT EXISTS cache_entry (
    cache_key  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,              -- probe | transcript | audio
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_task ON artifact(task_id);
CREATE INDEX IF NOT EXISTS idx_job_task ON download_job(task_id);
"""

_RELATIONS = ("task_stage", "artifact", "download_job")

_LOCATIONS = (
    "CREATE TABLE IF NOT EXISTS artifact_location (artifact_id INTEGER PRIMARY KEY "
    "REFERENCES artifact(id) ON DELETE CASCADE, path_key TEXT, file_key TEXT, mutable INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_artifact_path ON artifact(path, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_location_path ON artifact_location(path_key)",
    "CREATE INDEX IF NOT EXISTS idx_location_file ON artifact_location(file_key) WHERE file_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_location_mutable ON artifact_location(mutable) WHERE mutable=1",
    "CREATE TRIGGER IF NOT EXISTS artifact_location_changed AFTER UPDATE OF path ON artifact "
    "WHEN old.path<>new.path BEGIN DELETE FROM artifact_location WHERE artifact_id=new.id; END",
)


def _wal_supported(path: Path) -> bool:
    version = sqlite3.sqlite_version_info
    # SQLite 3.51.3 及官方回移植修复了多个 WAL 连接并发 checkpoint 时的已知问题。
    patched = version >= (3, 51, 3) or ((3, 50, 7) <= version < (3, 51)) or ((3, 44, 6) <= version < (3, 45))
    if not patched or str(path).startswith(("\\\\", "//")):
        return False
    if os.name == "nt":
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 3
    return True


def _path_identity(path: str) -> tuple[str, str | None, bool]:
    lexical = Path(os.path.abspath(path))
    try:
        canonical = lexical.resolve()
        info = canonical.stat()
        fingerprint = f"{info.st_dev:x}:{info.st_ino:x}" if info.st_ino else None
        return os.path.normcase(str(canonical)), fingerprint, canonical != lexical
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(str(lexical)), None, True

# 轻量迁移：新增列（老库自动补齐，避免重建数据）
_MIGRATIONS = {
    "task": {
        "output_dir": "TEXT DEFAULT ''",   # 自定义输出目录（空 = 数据目录/library/<任务>）
        "font_name": "TEXT DEFAULT ''",    # 硬字幕字体（空 = ffmpeg 默认）
        "font_size": "INTEGER DEFAULT 0",  # 硬字幕字号（0 = 配置默认值）
        "position": "TEXT DEFAULT ''",    # 字幕位置 bottom/middle/top
        "source_lang": "TEXT DEFAULT ''",  # 源语言（空 = 自动检测）
        "outline": "INTEGER DEFAULT 1",    # 硬字幕黑色描边粗细（0-8）
        "bilingual": "INTEGER DEFAULT 0",  # 双语上下堆叠（上=源语言 下=译文）
        "person_notes": "INTEGER DEFAULT 0",  # 旧任务默认不添加人名注释
        "audio_subtitles": "INTEGER DEFAULT 1",
        "translate": "INTEGER DEFAULT 1",  # 旧任务沿用原有自动翻译行为
        "color": "TEXT DEFAULT '#FFFFFF'",
        "vertical_offset": "INTEGER DEFAULT 0",
        "horizontal_offset": "INTEGER DEFAULT 0",
        "enhance": "INTEGER DEFAULT 0",  # 保留历史任务字段，不再创建超分任务
        "enhance_scale": "INTEGER DEFAULT 2",
    }
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    """线程安全的薄封装：CLI 单线程、API 多线程共用。"""

    def __init__(self, path: str | Path):
        self._path = Path(path).resolve() if str(path) != ":memory:" else None
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._closed = False
        self._reader_state = threading.Condition()
        self._reader_local = threading.local()
        self._readers: dict[threading.Thread, sqlite3.Connection] = {}
        self._active_readers = 0
        self._reader_sweep_at = 0.
        try:
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self.wal_enabled = bool(self._path and _wal_supported(self._path) and
                                    self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal")
            self._conn.executescript(_SCHEMA)
            with self._conn:
                self._conn.execute("BEGIN IMMEDIATE")
                self._migrate_relations()
                for table, cols in _MIGRATIONS.items():
                    existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                    for col, ddl in cols.items():
                        if col not in existing:
                            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                for statement in _LOCATIONS:
                    self._conn.execute(statement)
                # 旧版回退期间可能手工清除关系表行，回到新版时补齐索引。
                self._conn.execute("DELETE FROM artifact_location WHERE artifact_id NOT IN (SELECT id FROM artifact)")
                old_paths = self._conn.execute(
                    "SELECT a.id,a.path FROM artifact a LEFT JOIN artifact_location l ON l.artifact_id=a.id "
                    "WHERE l.artifact_id IS NULL").fetchall()
                for row in old_paths:
                    self._put_location(row["id"], _path_identity(row["path"]))
        except BaseException:
            self._conn.close()
            raise

    def _migrate_relations(self) -> None:
        for table in _RELATIONS:
            if self._conn.execute(f"PRAGMA foreign_key_list({table})").fetchone():
                continue
            source = f"_old_{table}"
            self._conn.execute(f"ALTER TABLE {table} RENAME TO {source}")
            declaration = next(sql for sql in _SCHEMA.split(";")
                               if sql.strip().startswith(f"CREATE TABLE IF NOT EXISTS {table} "))
            self._conn.execute(declaration)
            detached = self._conn.execute(
                f"SELECT * FROM {source} WHERE task_id NOT IN (SELECT id FROM task)").fetchall()
            if detached:
                self._conn.execute("CREATE TABLE IF NOT EXISTS legacy_orphan "
                                   "(source_table TEXT NOT NULL, previous_record TEXT NOT NULL)")
                self._conn.executemany("INSERT INTO legacy_orphan VALUES (?,?)",
                    [(table, json.dumps(dict(row), ensure_ascii=False)) for row in detached])
            columns = ",".join(row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})"))
            self._conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {source} "
                               "WHERE task_id IN (SELECT id FROM task)")
            if table == "artifact":
                previous = self._conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (source,)).fetchone()
                if previous:
                    self._conn.execute("INSERT OR IGNORE INTO sqlite_sequence(name,seq) "
                                       "SELECT 'artifact',? WHERE NOT EXISTS "
                                       "(SELECT 1 FROM sqlite_sequence WHERE name='artifact')", (previous[0],))
                    self._conn.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='artifact'", (previous[0],))
            self._conn.execute(f"DROP TABLE {source}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_task ON artifact(task_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_job_task ON download_job(task_id)")

    def _require_open(self) -> None:
        if self._closed:
            raise sqlite3.ProgrammingError("任务存储已关闭")

    @contextmanager
    def _main_connection(self):
        with self._lock:
            self._require_open()
            yield self._conn

    @contextmanager
    def _reader(self):
        if not self.wal_enabled:
            with self._main_connection() as reader:
                yield reader
            return
        with self._reader_state:
            self._require_open()
            reader = getattr(self._reader_local, "connection", None)
            now = monotonic()
            if reader is None or now >= self._reader_sweep_at:
                for thread in tuple(self._readers):
                    if not thread.is_alive():
                        self._readers.pop(thread).close()
                # 在正常读请求中低频回收，空闲时无需运行维护线程。
                self._reader_sweep_at = now + 5
            if reader is None:
                # 正常读只由所属线程执行。关库方等所有快照释放后可跨线程关闭。
                reader = sqlite3.connect(self._path.as_uri() + "?mode=ro", uri=True, timeout=5,
                                         check_same_thread=False)
                reader.row_factory = sqlite3.Row
                self._reader_local.connection = reader
                self._readers[threading.current_thread()] = reader
            self._active_readers += 1
        try:
            reader.execute("BEGIN")
            try:
                yield reader
            finally:
                reader.rollback()
        finally:
            with self._reader_state:
                self._active_readers -= 1
                if not self._active_readers:
                    self._reader_state.notify_all()

    def close(self):
        with self._lock:
            if self._closed:
                return
            with self._reader_state:
                self._closed = True
                self._reader_state.wait_for(lambda: not self._active_readers)
                for reader in self._readers.values():
                    reader.close()
                self._readers.clear()
                self._reader_local = threading.local()
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._main_connection() as conn, conn:
            return conn.execute(sql, params)

    def _fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        # 同一连接的游标也需要保护，不能在解锁后再 fetch。
        with self._reader() as reader:
            return reader.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._reader() as reader:
            return reader.execute(sql, params).fetchall()

    # ---- task ----
    def create_task(self, url: str, resolution: str, langs: list[str],
                    subtitle_mode: str, title: str = "", output_dir: str = "",
                    font_name: str = "", font_size: int = 0,
                    position: str = "bottom", source_lang: str = "",
                    outline: int = 1, bilingual: bool = False,
                    translate: bool = True, color: str = "#FFFFFF",
                    vertical_offset: int = 0,
                    horizontal_offset: int = 0, person_notes: bool = False, audio_subtitles: bool = True) -> dict:
        tid = uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO task (id, url, url_key, title, status, resolution, target_langs,"
            " subtitle_mode, output_dir, font_name, font_size, position, source_lang,"
            " outline, bilingual, translate, color, vertical_offset, horizontal_offset, person_notes, audio_subtitles, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, url, url, title, "pending", resolution, ",".join(langs), subtitle_mode,
             str(output_dir or ""), str(font_name or ""), int(font_size or 0),
             str(position or "bottom"), str(source_lang or ""), int(outline or 0),
             int(bool(bilingual)), int(bool(translate)), color,
             int(vertical_offset), int(horizontal_offset), int(bool(person_notes)), int(bool(audio_subtitles)), _now()),
        )
        return self.get_task(tid)

    def recover_stale_running(self) -> int:
        """引擎启动恢复：上次进程遗留的 running/pending 是僵尸任务
        （工作线程已随旧进程消失），标记为 interrupted 并允许重试。"""
        ts, error = _now(), "引擎重启时任务被中断，可重试"
        # 三类记录一起提交，避免任务已进入终态而阶段或下载作业仍显示运行中。
        with self._main_connection() as conn, conn:
            active = "SELECT id FROM task WHERE status IN ('running','pending','created')"
            self._conn.execute(
                "UPDATE task_stage SET status='interrupted', error=?, finished_at=?"
                f" WHERE status='running' AND task_id IN ({active})", (error, ts),
            )
            self._conn.execute(
                "UPDATE download_job SET status='interrupted'"
                f" WHERE status IN ('pending','running','created') AND task_id IN ({active})",
            )
            return self._conn.execute(
                "UPDATE task SET status='interrupted', error=?, finished_at=?"
                " WHERE status IN ('running','pending','created')", (error, ts),
            ).rowcount

    def get_task(self, task_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM task WHERE id=?", (task_id,))
        return dict(row) if row else None

    def find_active_equivalent(self, task: dict) -> dict | None:
        """提交/重试复用相同输出设置的活跃任务；终态不阻止用户重新提交。"""
        options = ("resolution", "target_langs", "subtitle_mode", "output_dir",
                   "font_name", "font_size", "position", "source_lang", "outline",
                   "bilingual", "translate", "color", "vertical_offset", "horizontal_offset", "person_notes",
                   "audio_subtitles")
        rows = self._fetchall(
            "SELECT * FROM task WHERE url=? AND id<>?"
            " AND status IN ('pending','running','created') ORDER BY created_at",
            (task["url"], task.get("id", "")),
        )
        return next((dict(row) for row in rows
                     if all(row[key] == task.get(key, int(key == "audio_subtitles")) for key in options)), None)

    def claim_retry(self, task_id: str) -> bool:
        """一次条件更新领取重试，多个请求不能同时启动同一任务。"""
        return self.claim_retry_snapshot(task_id) is not None

    def claim_retry_snapshot(self, task_id: str) -> dict | None:
        """原子领取重试并保留领取时的终态，启动失败可恢复该次结果。"""
        return self._claim_snapshot(task_id, retry=True)

    def claim_terminal_task(self, task_id: str) -> bool:
        """占用已结束任务，串行化重渲染、删除与重试。"""
        return self.claim_terminal_task_snapshot(task_id) is not None

    def claim_terminal_task_snapshot(self, task_id: str) -> dict | None:
        """原子占用并返回当前终态，防止文件操作恢复过期快照。"""
        return self._claim_snapshot(task_id, retry=False)

    def _claim_snapshot(self, task_id: str, *, retry: bool) -> dict | None:
        with self._main_connection() as conn, conn:
            row = self._conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            allowed = (task["status"] in ("failed", "cancelled", "interrupted")
                       or task["status"] == "completed" and (not retry or bool(task["error"])))
            if not allowed:
                return None
            cursor = self._conn.execute(
                "UPDATE task SET status=?, error=?, finished_at='' WHERE id=?"
                " AND status=? AND error IS ? AND finished_at IS ?",
                ("pending" if retry else "running", "" if retry else task["error"], task_id,
                 task["status"], task["error"], task["finished_at"]),
            )
            return task if cursor.rowcount == 1 else None

    def request_cancel_if_active(self, task_id: str, request_cancel) -> dict | None:
        """检查当前状态并请求取消，防止任务已结束后旧快照重新置位。"""
        with self._main_connection():
            row = self._conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            if task["status"] in ("running", "pending"):
                request_cancel(task_id)
            return task

    def list_tasks(self) -> list[dict]:
        rows = self._fetchall("SELECT * FROM task ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def task_snapshot(self, ids: tuple[str, ...] = (), *, include_artifacts: bool = False) -> list[dict]:
        """从同一份读取事务批量查询任务及关联行。"""
        where = f" WHERE t.id IN ({','.join('?' for _ in ids)})" if ids else ""
        with self._reader() as reader:
            tasks = reader.execute(
                f"SELECT t.* FROM task t{where} ORDER BY t.created_at DESC, t.id", ids
            ).fetchall()
            stages = reader.execute(
                f"SELECT s.* FROM task_stage s JOIN task t ON t.id=s.task_id{where}"
                " ORDER BY s.started_at, s.stage", ids
            ).fetchall()
            artifacts = reader.execute(
                f"SELECT a.* FROM artifact a JOIN task t ON t.id=a.task_id{where} ORDER BY a.id", ids
            ).fetchall() if include_artifacts else ()
        items = {row["id"]: dict(row, stages=[]) for row in tasks}
        for row in stages:
            items[row["task_id"]]["stages"].append(dict(row))
        if include_artifacts:
            for item in items.values():
                item["artifacts"] = []
            for row in artifacts:
                items[row["task_id"]]["artifacts"].append(dict(row))
        return list(items.values())

    def delete_task(self, task_id: str) -> None:
        """删除任务记录（含阶段/产物/下载作业登记；不删磁盘文件）"""
        with self._main_connection() as conn, conn:
            self._conn.execute("DELETE FROM task WHERE id=?", (task_id,))

    def update_task(self, task_id: str, **fields) -> None:
        statement = self._task_update_statement(task_id, fields)
        if statement is not None:
            self._exec(*statement)

    def finish_task(self, task_id: str, **fields) -> None:
        """终态写入和取消清理共用数据库锁，重试只能在两者完成后领取。"""
        from ..core import cancel as task_cancel
        with self._lock:
            try:
                self._require_open()
                with self._conn:
                    statement = self._task_update_statement(task_id, fields)
                    if statement is not None:
                        self._conn.execute(*statement)
            finally:
                # 数据库写入失败也必须回收已结束工作线程的取消状态。
                task_cancel.clear(task_id)

    @staticmethod
    def _task_update_statement(task_id: str, fields: dict) -> tuple[str, tuple] | None:
        allowed = {"status", "title", "error", "finished_at"}
        # 状态进入终态时自动补 finished_at
        if fields.get("status") in ("completed", "failed", "cancelled", "interrupted"):
            fields.setdefault("finished_at", _now())
        cols, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                cols.append(f"{k}=?")
                vals.append(v if not isinstance(v, dict) else str(v))
        if not cols:
            return None
        vals.append(task_id)
        return f"UPDATE task SET {', '.join(cols)} WHERE id=?", tuple(vals)

    # ---- task_stage（checkpoint 核心）----
    def upsert_stage(self, task_id: str, stage: str, status: str, error: str = "") -> None:
        ts = _now()
        finished = ts if status in ("done", "failed", "cancelled", "interrupted") else ""
        self._exec(
            "INSERT INTO task_stage (task_id, stage, status, error, started_at, finished_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(task_id, stage) DO UPDATE SET status=excluded.status,"
            " error=excluded.error, finished_at=excluded.finished_at,"
            " started_at=CASE WHEN excluded.status='running' AND task_stage.status<>'running'"
            " THEN excluded.started_at ELSE task_stage.started_at END",
            (task_id, stage, status, error, ts, finished),
        )

    def stage_status(self, task_id: str, stage: str) -> str | None:
        row = self._fetchone(
            "SELECT status FROM task_stage WHERE task_id=? AND stage=?",
            (task_id, stage),
        )
        return row["status"] if row else None

    def list_stages(self, task_id: str) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM task_stage WHERE task_id=? ORDER BY started_at", (task_id,)
        )
        return [dict(r) for r in rows]

    # ---- artifact ----
    def _put_location(self, artifact_id, identity):
        self._conn.execute("INSERT OR REPLACE INTO artifact_location VALUES (?,?,?,?)", (artifact_id, *identity))

    def add_artifact(self, task_id: str, kind: str, path: str,
                     resolution: str = "", lang: str = "") -> None:
        identity = _path_identity(path)
        with self._main_connection() as conn, conn:
            cursor = self._conn.execute(
                "INSERT INTO artifact (task_id, kind, path, resolution, lang, created_at) VALUES (?,?,?,?,?,?)",
                (task_id, kind, str(path), resolution, lang, _now()))
            self._put_location(cursor.lastrowid, identity)

    def move_artifact(self, task_id: str, old_path: str, new_path: str) -> None:
        identity = _path_identity(new_path)
        with self._main_connection() as conn, conn:
            rows = self._conn.execute("UPDATE artifact SET path=? WHERE task_id=? AND path=? RETURNING id",
                                      (str(new_path), task_id, str(old_path))).fetchall()
            for row in rows:
                self._put_location(row["id"], identity)

    def get_artifact(self, artifact_id: int) -> dict | None:
        row = self._fetchone("SELECT * FROM artifact WHERE id=?", (artifact_id,))
        return dict(row) if row else None

    def artifact_exists(self, task_id: str, path: str) -> bool:
        """重跑时避免登记重复产物。"""
        row = self._fetchone(
            "SELECT 1 FROM artifact WHERE task_id=? AND path=?", (task_id, str(path))
        )
        return row is not None

    def artifact_owned_by_other(self, task_id: str, path: str) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM artifact WHERE task_id<>? AND path=? LIMIT 1",
            (task_id, str(path)),
        )
        if row is not None:
            return True
        # 常见路径只查索引。之前缺失或通过别名登记的少量路径可被重新指向，
        # 因此必要时重验这组文件，磁盘读取发生在数据库事务之外。
        resolved, file_id, _ = _path_identity(path)
        candidates = self._fetchall(
            "SELECT a.path FROM artifact a JOIN artifact_location l ON l.artifact_id=a.id "
            "WHERE a.task_id<>? AND l.path_key=? "
            "UNION SELECT a.path FROM artifact a JOIN artifact_location l ON l.artifact_id=a.id "
            "WHERE a.task_id<>? AND l.file_key IS NOT NULL AND l.file_key=? "
            "UNION SELECT a.path FROM artifact a JOIN artifact_location l ON l.artifact_id=a.id "
            "WHERE a.task_id<>? AND l.mutable=1",
            (task_id, resolved, task_id, file_id, task_id))
        for row in candidates:
            actual, actual_id, _ = _path_identity(row["path"])
            if actual == resolved or file_id is not None and actual_id == file_id:
                return True
        return False

    def artifact_paths_owned_by_other(self, task_id: str) -> list[str]:
        """一次获取其他任务登记的去重路径，供工作目录清理统一保护。"""
        rows = self._fetchall("SELECT DISTINCT path FROM artifact WHERE task_id<>?", (task_id,))
        return [row["path"] for row in rows]

    def list_artifacts(self, task_id: str) -> list[dict]:
        rows = self._fetchall(
            "SELECT * FROM artifact WHERE task_id=? ORDER BY id", (task_id,)
        )
        return [dict(r) for r in rows]

    def delete_artifacts(self, task_id: str, ids: list[int]) -> None:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            self._exec(f"DELETE FROM artifact WHERE task_id=? AND id IN ({placeholders})",
                       (task_id, *ids))

    # ---- download_job ----
    def add_job(self, job_id: str, task_id: str, **fields) -> None:
        cols = ["id", "task_id"] + list(fields.keys())
        vals = [job_id, task_id] + list(fields.values())
        ph = ",".join("?" * len(cols))
        self._exec(f"INSERT INTO download_job ({', '.join(cols)}) VALUES ({ph})", tuple(vals))

    def update_job(self, job_id: str, **fields) -> None:
        allowed = {"status", "downloaded", "total", "speed_bps", "backend", "backend_job_id"}
        cols, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                cols.append(f"{k}=?")
                vals.append(v)
        if not cols:
            return
        vals.append(job_id)
        self._exec(f"UPDATE download_job SET {', '.join(cols)} WHERE id=?", tuple(vals))

    # ---- cache_entry ----
    def cache_get(self, key: str) -> str | None:
        """未过期的缓存返回 path，否则 None。"""
        row = self._fetchone(
            "SELECT path, expires_at FROM cache_entry WHERE cache_key=?", (key,)
        )
        if not row:
            return None
        if row["expires_at"] < _now():
            return None
        return row["path"]

    def cache_put(self, key: str, kind: str, path: str, ttl_hours: float = 24) -> None:
        exp = (datetime.now(timezone.utc).astimezone()
               + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
        self._exec(
            "INSERT INTO cache_entry (cache_key, kind, path, created_at, expires_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(cache_key) DO UPDATE SET path=excluded.path,"
            " created_at=excluded.created_at, expires_at=excluded.expires_at",
            (key, kind, str(path), _now(), exp),
        )

    def cache_purge_kind(self, kind: str) -> None:
        """按类型清空缓存（如登录态变更后丢弃全部 probe 结果）。"""
        self._exec("DELETE FROM cache_entry WHERE kind=?", (kind,))
