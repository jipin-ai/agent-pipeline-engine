"""models.py — SQLite 数据层（pipeline_status / audit_log / dispatcher_lease / agents）"""
import os, sqlite3

DB_PATH = os.environ.get("PIPELINE_DB", "/opt/pipeline/pipeline.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_status (
    task_id           TEXT PRIMARY KEY,
    project           TEXT NOT NULL,
    status            TEXT DEFAULT 'received',
    sub_status        TEXT,
    ears_doc_path     TEXT,
    ears_signed_by    TEXT,
    ears_signed_at    TEXT,
    orc_doc_path      TEXT,
    dev_confirmed     INTEGER DEFAULT 0,
    qa_confirmed     INTEGER DEFAULT 0,
    git_commit        TEXT,
    pytest_report_path TEXT,
    artifact_sha256   TEXT,
    test_report_path  TEXT,
    coverage_pct      REAL,
    blocking_issues   INTEGER DEFAULT 0,
    deploy_url        TEXT,
    health_check_ok   INTEGER DEFAULT 0,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    status_changed_at TEXT DEFAULT (datetime('now')),
    dispatch_sent_at  TEXT,
    dispatch_agent    TEXT,
    dispatch_ack      INTEGER DEFAULT 0,
    claimed_by        TEXT,
    claimed_at        TEXT,
    agent_heartbeat_at TEXT,
    task_heartbeat_at TEXT,
    retry_count       INTEGER DEFAULT 0,
    max_retries       INTEGER DEFAULT 3,
    next_retry_at     TEXT,
    priority          INTEGER DEFAULT 1,
    hold_timeout_m    INTEGER DEFAULT 120
);
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL,
    action        TEXT NOT NULL,
    old_status    TEXT,
    new_status    TEXT,
    triggered_by  TEXT,
    details       TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS dispatcher_lease (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    holder      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    name              TEXT PRIMARY KEY,
    url               TEXT NOT NULL,
    last_heartbeat_at TEXT,
    is_down           INTEGER DEFAULT 0,
    alerted_at        TEXT
);
"""

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    return mode

def audit(conn, task_id, action, old, new, by, details=""):
    conn.execute(
        "INSERT INTO audit_log (task_id, action, old_status, new_status, triggered_by, details)"
        " VALUES (?,?,?,?,?,?)",
        (task_id, action, old, new, by, details))

def get_task(conn, task_id):
    return conn.execute("SELECT * FROM pipeline_status WHERE task_id=?", (task_id,)).fetchone()

def touch_status(conn, task_id, new_status, by, details=""):
    old = get_task(conn, task_id)
    old_s = old["status"] if old else None
    # 换站即清上一站的认领（否则下一站的 Agent 永远看不到任务——E2E 实测坑）
    conn.execute(
        "UPDATE pipeline_status SET status=?, status_changed_at=datetime('now'),"
        " updated_at=datetime('now'), claimed_by=NULL, claimed_at=NULL,"
        " dispatch_sent_at=NULL, dispatch_ack=0 WHERE task_id=?",
        (new_status, task_id))
    audit(conn, task_id, "status_change", old_s, new_status, by, details)
    conn.commit()
