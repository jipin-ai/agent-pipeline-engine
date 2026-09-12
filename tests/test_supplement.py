"""test_supplement.py — pipeline-rework-upload-fix M-1/M-2 验收单测（AC-1/2/4 + 迁移幂等）。

独立临时库（PIPELINE_DB 指向 tmp_path），不碰生产 /opt/pipeline/pipeline.db。
"""
import os, sys, sqlite3, importlib

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "pipeline.db")
    monkeypatch.setenv("PIPELINE_DB", db)
    monkeypatch.setenv("PIPELINE_DOCS", str(tmp_path / "docs"))
    monkeypatch.setenv("PIPELINE_TOKEN", "test-token")
    # 清模块缓存，保证每个用例拿到指向临时库的新模块
    for m in ("models", "gate_judge", "main"):
        sys.modules.pop(m, None)
    import models
    importlib.reload(models)
    models.init_db()
    import main
    importlib.reload(main)
    main.IP_WHITELIST = {"testclient", "127.0.0.1"}  # TestClient 的 client.host 是 "testclient"
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    return models, main, client, db


def auth(actor="ORC-01"):
    return {"Authorization": "Bearer test-token", "X-CTGC-Actor": actor}


def _mk_task(models, db, tid, status):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO pipeline_status (task_id, project, status) VALUES (?,?,?)",
                 (tid, "ape", status))
    conn.commit(); conn.close()


# ---------- AC-1：done 任务可派生补充子任务 ----------
def test_supplement_done_task(env):
    models, main, client, db = env
    _mk_task(models, db, "t-done", "done")
    r = client.post("/tasks/t-done/supplement", headers=auth(),
                    json={"start_status": "dev_working"})
    assert r.status_code == 200
    d = r.json()
    assert d["parent_task_id"] == "t-done"
    assert d["status"] == "dev_working"
    sub = d["task_id"]
    # 子任务落库 + parent_task_id 正确
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM pipeline_status WHERE task_id=?", (sub,)).fetchone()
    assert row["parent_task_id"] == "t-done" and row["status"] == "dev_working"
    # 审计链
    a = conn.execute("SELECT * FROM audit_log WHERE task_id=? AND action='supplement'", (sub,)).fetchone()
    assert a is not None and "t-done" in a["details"]
    conn.close()


def test_supplement_custom_id_and_cancelled_parent(env):
    models, main, client, db = env
    _mk_task(models, db, "t-cancel", "cancelled")
    r = client.post("/tasks/t-cancel/supplement", headers=auth("architect"),
                    json={"task_id": "t-cancel-sup1", "start_status": "demo_working"})
    assert r.status_code == 200
    assert r.json()["task_id"] == "t-cancel-sup1"


# ---------- AC-4：权限/状态/起点边界 ----------
def test_supplement_forbidden_actor(env):
    models, main, client, db = env
    _mk_task(models, db, "t-done", "done")
    r = client.post("/tasks/t-done/supplement", headers=auth("DEV-01"), json={})
    assert r.status_code == 403


def test_supplement_non_terminal_parent(env):
    models, main, client, db = env
    _mk_task(models, db, "t-live", "dev_working")
    r = client.post("/tasks/t-live/supplement", headers=auth(), json={})
    assert r.status_code == 409


def test_supplement_bad_start_status(env):
    models, main, client, db = env
    _mk_task(models, db, "t-done", "done")
    for bad in ("received", "ears_draft", "waiting_human", "nonsense"):
        r = client.post("/tasks/t-done/supplement", headers=auth(),
                        json={"start_status": bad})
        assert r.status_code == 400, bad


def test_supplement_not_found(env):
    models, main, client, db = env
    r = client.post("/tasks/ghost/supplement", headers=auth(), json={})
    assert r.status_code == 404


def test_supplement_duplicate_sub_id(env):
    models, main, client, db = env
    _mk_task(models, db, "t-done", "done")
    body = {"task_id": "sub-1", "start_status": "ready"}
    assert client.post("/tasks/t-done/supplement", headers=auth(), json=body).status_code == 200
    assert client.post("/tasks/t-done/supplement", headers=auth(), json=body).status_code == 409


# ---------- AC-2：子任务可被 agent 轮询到（STATUS_AGENT 映射直通） ----------
def test_sub_task_visible_to_agent_poll(env):
    models, main, client, db = env
    _mk_task(models, db, "t-done", "done")
    client.post("/tasks/t-done/supplement", headers=auth(),
                json={"task_id": "sub-dev", "start_status": "dev_working"})
    r = client.get("/tasks?agent=DEV-01", headers=auth())
    ids = [t["task_id"] for t in r.json()]
    assert "sub-dev" in ids
    # parent_task_id 随 list_tasks 返回（改动 3）
    sub = [t for t in r.json() if t["task_id"] == "sub-dev"][0]
    assert sub["parent_task_id"] == "t-done"


# ---------- AC-3：子任务走完整门禁推进（dispatch_loop 复用） ----------
def test_sub_task_gate_progression(env):
    models, main, client, db = env
    _mk_task(models, db, "t-done", "done")
    client.post("/tasks/t-done/supplement", headers=auth(),
                json={"task_id": "sub-flow", "start_status": "dev_working"})
    # 证据未齐 → 门禁 BLOCKED，不推进
    assert main.dispatch_loop()["role"] == "primary"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM pipeline_status WHERE task_id='sub-flow'").fetchone()[0] == "dev_working"
    # 补齐 dev_working 三件套 → 推进 qa_working
    conn.execute("UPDATE pipeline_status SET git_commit='abc', pytest_report_path='r.xml',"
                 " artifact_sha256='deadbeef' WHERE task_id='sub-flow'")
    conn.commit(); conn.close()
    main.dispatch_loop()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM pipeline_status WHERE task_id='sub-flow'").fetchone()[0] == "qa_working"
    conn.close()


# ---------- 迁移幂等：旧库（无 parent_task_id）init_db 后补列，重复执行不炸 ----------
def test_migration_idempotent(tmp_path, monkeypatch):
    db = str(tmp_path / "old.db")
    monkeypatch.setenv("PIPELINE_DB", db)
    sys.modules.pop("models", None)
    import models, importlib
    importlib.reload(models)
    # 造一个"旧 schema"库：手工建无 parent_task_id 的表
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE pipeline_status (task_id TEXT PRIMARY KEY, project TEXT NOT NULL,"
                 " status TEXT DEFAULT 'received')")
    conn.commit(); conn.close()
    models.init_db()   # 第一次：补列
    models.init_db()   # 第二次：幂等，不抛错
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_status)")}
    assert "parent_task_id" in cols
    conn.close()
