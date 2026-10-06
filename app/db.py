"""SQLite 连接与初始化。

表结构：
- meta：单行，记录当前已发布修订号（0 表示尚未发布）。
- pages：按修订号保存每个页面的完整快照。读接口永远只读取
  ``revision = 当前修订号`` 的那一批行，因此发布是原子的，
  重启进程后数据仍在（文件型 SQLite）。
- redirects：旧地址 -> 当前真实页面路径的 308 跳转登记。跳转是
  **当前状态**而非页面修订：登记/撤销不推进修订号，但与页面快照
  一样持久化在库中，重启后仍然有效。``old_path`` 唯一（旧地址沿用
  页面路径格式，且不与真实页面/其他跳转冲突）；``target_path``
  必须始终指向真实页面（搬迁时在同一事务内同步改写，不形成跳转链）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DOCS_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "docs.db"))

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_revision INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO meta (id, current_revision) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS pages (
    revision INTEGER NOT NULL,
    path     TEXT    NOT NULL,
    content  TEXT    NOT NULL,
    PRIMARY KEY (revision, path)
);

CREATE TABLE IF NOT EXISTS redirects (
    old_path    TEXT PRIMARY KEY,
    target_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_redirects_target ON redirects (target_path);
"""


def connect() -> sqlite3.Connection:
    """打开一个配置好的数据库连接（每次请求一个连接）。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """创建表并写入初始修订号 0。幂等，可重复执行。"""
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
