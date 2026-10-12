"""SQLite 存储：task / task_stage / artifact / download_job / cache_entry 五张表。

MVP 用标准库 sqlite3（PyInstaller 友好、零额外依赖）；
接口按 DAO 风格收敛，后续换 SQLAlchemy/PostgreSQL 只改这一层。
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    task_id     TEXT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',     -- pending/running/done/failed
    error       TEXT DEFAULT '',
    started_at  TEXT DEFAULT '',
    finished_at TEXT DEFAULT '',
    PRIMARY KEY (task_id, stage)
);
CREATE TABLE IF NOT EXISTS artifact (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,        -- video/audio/subtitle/output
    path         TEXT NOT NULL,
    resolution   TEXT DEFAULT '',
    lang         TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS download_job (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
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
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            for table, cols in _MIGRATIONS.items():
                existing = {r["name"] for r in
                            self._conn.execute(f"PRAGMA table_info({table})")}
                for col, ddl in cols.items():
                    if col not in existing:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    # ---- task ----
    def create_task(self, url: str, resolution: str, langs: list[str],
                    subtitle_mode: str, title: str = "", output_dir: str = "",
                    font_name: str = "", font_size: int = 0,
                    position: str = "bottom", source_lang: str = "",
                    outline: int = 1, bilingual: bool = False,
                    translate: bool = True, color: str = "#FFFFFF",
                    vertical_offset: int = 0,
                    horizontal_offset: int = 0) -> dict:
        tid = uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO task (id, url, url_key, title, status, resolution, target_langs,"
            " subtitle_mode, output_dir, font_name, font_size, position, source_lang,"
            " outline, bilingual, translate, color, vertical_offset, horizontal_offset, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, url, url, title, "pending", resolution, ",".join(langs), subtitle_mode,
             str(output_dir or ""), str(font_name or ""), int(font_size or 0),
             str(position or "bottom"), str(source_lang or ""), int(outline or 0),
             int(bool(bilingual)), int(bool(translate)), color,
             int(vertical_offset), int(horizontal_offset), _now()),
        )
        return self.get_task(tid)

    def recover_stale_running(self) -> int:
        """引擎启动恢复：上次进程遗留的 running/pending 是僵尸任务
        （工作线程已随旧进程消失），标记为 interrupted 并允许重试。"""
        rows = self._exec(
            "SELECT id FROM task WHERE status IN ('running','pending','created')"
        ).fetchall()
        for r in rows:
            self.update_task(r["id"], status="interrupted",
                             error="引擎重启时任务被中断，可重试")
        return len(rows)

    def get_task(self, task_id: str) -> dict | None:
        row = self._exec("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def find_active_equivalent(self, task: dict) -> dict | None:
        """提交/重试复用相同输出设置的活跃任务；终态不阻止用户重新提交。"""
        options = ("resolution", "target_langs", "subtitle_mode", "output_dir",
                   "font_name", "font_size", "position", "source_lang", "outline",
                   "bilingual", "translate", "color", "vertical_offset", "horizontal_offset")
        rows = self._exec(
            "SELECT * FROM task WHERE url=? AND id<>?"
            " AND status IN ('pending','running','created') ORDER BY created_at",
            (task["url"], task.get("id", "")),
        ).fetchall()
        return next((dict(row) for row in rows
                     if all(row[key] == task[key] for key in options)), None)

    def claim_retry(self, task_id: str) -> bool:
        """一次条件更新领取重试，多个请求不能同时启动同一任务。"""
        return self._exec(
            "UPDATE task SET status='pending', error='', finished_at='' WHERE id=?"
            " AND (status IN ('failed','cancelled','interrupted')"
            " OR (status='completed' AND error<>''))", (task_id,),
        ).rowcount == 1

    def claim_terminal_task(self, task_id: str) -> bool:
        """占用已结束任务，串行化重渲染、删除与重试。"""
        return self._exec(
            "UPDATE task SET status='running', finished_at='' WHERE id=?"
            " AND status IN ('completed','failed','cancelled','interrupted')", (task_id,),
        ).rowcount == 1

    def list_tasks(self) -> list[dict]:
        rows = self._exec("SELECT * FROM task ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_task(self, task_id: str) -> None:
        """删除任务记录（含阶段/产物/下载作业登记；不删磁盘文件）"""
        self._exec("DELETE FROM artifact WHERE task_id=?", (task_id,))
        self._exec("DELETE FROM task_stage WHERE task_id=?", (task_id,))
        self._exec("DELETE FROM download_job WHERE task_id=?", (task_id,))
        self._exec("DELETE FROM task WHERE id=?", (task_id,))

    def update_task(self, task_id: str, **fields) -> None:
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
            return
        vals.append(task_id)
        self._exec(f"UPDATE task SET {', '.join(cols)} WHERE id=?", tuple(vals))

    # ---- task_stage（checkpoint 核心）----
    def upsert_stage(self, task_id: str, stage: str, status: str, error: str = "") -> None:
        ts = _now()
        finished = ts if status in ("done", "failed") else ""
        self._exec(
            "INSERT INTO task_stage (task_id, stage, status, error, started_at, finished_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(task_id, stage) DO UPDATE SET status=excluded.status,"
            " error=excluded.error, finished_at=excluded.finished_at",
            (task_id, stage, status, error, ts, finished),
        )

    def stage_status(self, task_id: str, stage: str) -> str | None:
        row = self._exec(
            "SELECT status FROM task_stage WHERE task_id=? AND stage=?",
            (task_id, stage),
        ).fetchone()
        return row["status"] if row else None

    def list_stages(self, task_id: str) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM task_stage WHERE task_id=? ORDER BY started_at", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- artifact ----
    def add_artifact(self, task_id: str, kind: str, path: str,
                     resolution: str = "", lang: str = "") -> None:
        self._exec(
            "INSERT INTO artifact (task_id, kind, path, resolution, lang, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (task_id, kind, str(path), resolution, lang, _now()),
        )

    def move_artifact(self, task_id: str, old_path: str, new_path: str) -> None:
        self._exec("UPDATE artifact SET path=? WHERE task_id=? AND path=?",
                   (str(new_path), task_id, str(old_path)))

    def get_artifact(self, artifact_id: int) -> dict | None:
        row = self._exec("SELECT * FROM artifact WHERE id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None

    def artifact_exists(self, task_id: str, path: str) -> bool:
        """重跑时避免登记重复产物。"""
        row = self._exec(
            "SELECT 1 FROM artifact WHERE task_id=? AND path=?", (task_id, str(path))
        ).fetchone()
        return row is not None

    def artifact_owned_by_other(self, task_id: str, path: str) -> bool:
        row = self._exec(
            "SELECT 1 FROM artifact WHERE task_id<>? AND path=? LIMIT 1",
            (task_id, str(path)),
        ).fetchone()
        return row is not None

    def list_artifacts(self, task_id: str) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM artifact WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
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
        row = self._exec(
            "SELECT path, expires_at FROM cache_entry WHERE cache_key=?", (key,)
        ).fetchone()
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
