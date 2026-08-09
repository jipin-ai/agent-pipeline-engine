#!/usr/bin/env python3
"""
Agent Pipeline Engine — SQLite WAL 状态机初始化
幂等：重复执行不丢数据
"""
import sqlite3
import os
from pathlib import Path

DB_PATH = os.environ.get("PIPELINE_DB", str(Path.home() / ".pipeline" / "pipeline.db"))


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Initialize SQLite database with WAL mode. Idempotent."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pipeline_status (
            task_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            dev_confirmed INTEGER DEFAULT 0,
            qa_confirmed INTEGER DEFAULT 0,
            dev_notes TEXT,
            qa_notes TEXT,
            intent_lock TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_status ON pipeline_status(status);
        CREATE INDEX IF NOT EXISTS idx_project ON pipeline_status(project);
    """)

    conn.commit()
    return conn


if __name__ == "__main__":
    conn = init_db()
    print(f"Database initialized: {DB_PATH}")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"Tables: {tables}")
    conn.close()
