"""main.py — Dispatcher FastAPI 应用 + dispatch_loop（v5 第五/六/八章）"""
import json, os, sqlite3, time, urllib.request
from datetime import datetime, timezone

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse, FileResponse

import gate_judge
import models

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.environ.get("PIPELINE_DOCS", os.path.join(BASE, "docs"))
CONFIG_PATH = os.path.join(BASE, "config.json")

def _load_config():
    if os.path.exists(CONFIG_PATH):
        return json.load(open(CONFIG_PATH))
    return {}

_CFG = _load_config()
API_TOKEN = os.environ.get("PIPELINE_TOKEN") or _CFG.get("api_token", "")
A2A_TOKEN = os.environ.get("A2A_BEARER_TOKEN") or _CFG.get("a2a_token", "")
IP_WHITELIST = set(_CFG.get("ip_whitelist", ["127.0.0.1"]))
AGENT_URLS = _CFG.get("agent_urls", {})       # {"DEV-01": "http://<agent-host>:19904/", ...}
FEISHU_WEBHOOK = _CFG.get("feishu_webhook", "")
FEISHU_CFG = _CFG.get("feishu", {})          # {chat_id, app_id, app_secret, at_mapping}
_HEARTBEAT_TIMEOUT_S = _CFG.get("heartbeat_timeout_s", 360)
HEARTBEAT_TIMEOUT_S = _HEARTBEAT_TIMEOUT_S
ALERT_COOLDOWN_S = _CFG.get("alert_cooldown_s", 900)
DISPATCH_LEASE_S = _CFG.get("dispatch_lease_s", 300)

app = FastAPI(title="CTGC Pipeline Dispatcher", version="v5")


# ---------- 认证：Bearer + IP 白名单 ----------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    ip = request.client.host if request.client else ""
    if IP_WHITELIST and ip not in IP_WHITELIST:
        return JSONResponse({"error": f"ip {ip} not whitelisted"}, 403)
    auth = request.headers.get("Authorization", "")
    if not API_TOKEN or auth != f"Bearer {API_TOKEN}":
        return JSONResponse({"error": "unauthorized"}, 401)
    return await call_next(request)


def _actor(request: Request) -> str:
    return request.headers.get("X-CTGC-Actor", "api")


# ---------- 端点 ----------
@app.get("/health")
def health():
    return {"status": "ok", "uptime": int(time.time() - _START)}


@app.get("/tasks")
def list_tasks(agent: str = "", status: str = ""):
    conn = models.connect()
    sql, args = "SELECT * FROM pipeline_status WHERE 1=1", []
    if agent:
        # agent 认领属于自己工作站的业务状态
        my = [s for s, a in gate_judge.STATUS_AGENT.items() if a == agent]
        sql += " AND status IN (%s)" % ",".join("?" * len(my))
        args += my
    if status:
        ss = status.split(",")
        sql += " AND status IN (%s)" % ",".join("?" * len(ss))
        args += ss
    sql += " ORDER BY created_at"
    rows = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    return rows


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    conn = models.connect()
    t = models.get_task(conn, task_id)
    conn.close()
    if not t:
        return JSONResponse({"error": "not found"}, 404)
    return dict(t)


@app.post("/tasks")
def create_task(request: Request, body: dict):
    tid = body.get("task_id")
    proj = body.get("project", "default")
    if not tid:
        return JSONResponse({"error": "task_id required"}, 400)
    conn = models.connect()
    if models.get_task(conn, tid):
        conn.close()
        return JSONResponse({"error": "exists"}, 409)
    conn.execute("INSERT INTO pipeline_status (task_id, project, priority) VALUES (?,?,?)",
                 (tid, proj, int(body.get("priority", 1))))
    models.audit(conn, tid, "create", None, "received", _actor(request))
    conn.commit()
    conn.close()
    return {"task_id": tid, "status": "received"}


@app.post("/tasks/{task_id}/claim")
def claim_task(task_id: str, request: Request):
    actor = _actor(request)
    conn = models.connect()
    t = models.get_task(conn, task_id)
    if not t:
        conn.close()
        return JSONResponse({"error": "not found"}, 404)
    if t["claimed_by"] and t["claimed_by"] != actor:
        conn.close()
        return JSONResponse({"error": "already claimed", "claimed_by": t["claimed_by"]}, 409)
    conn.execute(
        "UPDATE pipeline_status SET claimed_by=?, claimed_at=datetime('now'),"
        " dispatch_ack=1, updated_at=datetime('now') WHERE task_id=?", (actor, task_id))
    models.audit(conn, task_id, "claim", t["status"], t["status"], actor)
    conn.commit()
    conn.close()
    return {"task_id": task_id, "claimed_by": actor}


@app.post("/tasks/{task_id}/heartbeat")
def task_heartbeat(task_id: str):
    conn = models.connect()
    conn.execute("UPDATE pipeline_status SET task_heartbeat_at=datetime('now') WHERE task_id=?",
                 (task_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/agents/{name}/heartbeat")
def agent_heartbeat(name: str):
    conn = models.connect()
    conn.execute(
        "INSERT INTO agents (name, url, last_heartbeat_at, is_down) VALUES (?,?,datetime('now'),0)"
        " ON CONFLICT(name) DO UPDATE SET last_heartbeat_at=datetime('now'), is_down=0",
        (name, AGENT_URLS.get(name, "")))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.patch("/tasks/{task_id}")
def patch_task(task_id: str, request: Request, body: dict):
    ALLOWED = {"sub_status", "ears_doc_path", "orc_doc_path", "dev_confirmed",
               "qa_confirmed", "git_commit", "pytest_report_path", "artifact_sha256",
               "test_report_path", "coverage_pct", "blocking_issues", "deploy_url",
               "health_check_ok", "priority", "hold_timeout_m", "max_retries"}
    sets = {k: v for k, v in body.items() if k in ALLOWED}
    if not sets:
        return JSONResponse({"error": "no allowed fields"}, 400)
    conn = models.connect()
    if not models.get_task(conn, task_id):
        conn.close()
        return JSONResponse({"error": "not found"}, 404)
    sql = "UPDATE pipeline_status SET " + ", ".join(f"{k}=?" for k in sets) + \
          ", updated_at=datetime('now') WHERE task_id=?"
    conn.execute(sql, list(sets.values()) + [task_id])
    models.audit(conn, task_id, "patch", None, None, _actor(request), json.dumps(sets)[:500])
    conn.commit()
    conn.close()
    return {"ok": True, "updated": sorted(sets)}


@app.post("/tasks/{task_id}/approve")
def approve_task(task_id: str, request: Request, body: dict = None):
    by = (body or {}).get("signed_by") or _actor(request)
    conn = models.connect()
    t = models.get_task(conn, task_id)
    if not t:
        conn.close()
        return JSONResponse({"error": "not found"}, 404)
    if t["status"] != "waiting_human":
        conn.close()
        return JSONResponse({"error": f"status={t['status']}，仅 waiting_human 可签署"}, 409)
    conn.execute(
        "UPDATE pipeline_status SET ears_signed_by=?, ears_signed_at=datetime('now') WHERE task_id=?",
        (by, task_id))
    models.touch_status(conn, task_id, "ready", by, "人类签署")
    conn.close()
    return {"ok": True, "status": "ready", "signed_by": by}


@app.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    conn = models.connect()
    t = models.get_task(conn, task_id)
    if not t:
        conn.close()
        return JSONResponse({"error": "not found"}, 404)
    models.touch_status(conn, task_id, "cancelled", _actor(request), "人工取消")
    conn.close()
    return {"ok": True, "status": "cancelled"}


@app.post("/tasks/{task_id}/advance")
def advance_task(task_id: str, request: Request, body: dict):
    target = body.get("status")
    valid = set(gate_judge.STATUS_NEXT) | gate_judge.TERMINAL_STATES | \
        {"blocked", "escalate_human", "waiting_human"}
    if target not in valid:
        return JSONResponse({"error": f"非法状态 {target}"}, 400)
    conn = models.connect()
    if not models.get_task(conn, task_id):
        conn.close()
        return JSONResponse({"error": "not found"}, 404)
    models.touch_status(conn, task_id, target, _actor(request), "强制推进")
    conn.close()
    return {"ok": True, "status": target}


@app.get("/agents")
def list_agents():
    conn = models.connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY name")]
    conn.close()
    return rows


@app.get("/audit")
def get_audit(task_id: str = ""):
    conn = models.connect()
    if task_id:
        rows = conn.execute("SELECT * FROM audit_log WHERE task_id=? ORDER BY id", (task_id,))
    else:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100")
    out = [dict(r) for r in rows]
    conn.close()
    return out


# ---------- 文档存储（v5 终审补丁：集中存 Dispatcher） ----------
DOC_TYPES = {"ears", "orc", "dev", "qa", "demo", "artifact"}

def _doc_path(task_id: str, dtype: str) -> str:
    safe = "".join(c for c in task_id if c.isalnum() or c in "-_")
    return os.path.join(DOCS_DIR, dtype, f"{safe}.md")


@app.post("/tasks/{task_id}/docs/{dtype}")
async def upload_doc(task_id: str, dtype: str, request: Request):
    if dtype not in DOC_TYPES:
        return JSONResponse({"error": "bad doc type"}, 400)
    body = await request.body()
    if not body:
        return JSONResponse({"error": "empty"}, 400)
    path = _doc_path(task_id, dtype)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)
    return {"ok": True, "path": path, "bytes": len(body)}


@app.get("/tasks/{task_id}/docs/{dtype}")
def download_doc(task_id: str, dtype: str):
    if dtype not in DOC_TYPES:
        return JSONResponse({"error": "bad doc type"}, 400)
    path = _doc_path(task_id, dtype)
    if not os.path.exists(path):
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(path)


# ---------- dispatch_loop（systemd timer 每 30s 一次性调用） ----------
def _now():
    return datetime.now(timezone.utc)

def _parse(ts):
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _try_acquire_lease(conn, holder="primary"):
    conn.execute(
        "INSERT INTO dispatcher_lease (id, holder, acquired_at, expires_at)"
        " VALUES (1, ?, datetime('now'), datetime('now', '+35 seconds'))"
        " ON CONFLICT(id) DO UPDATE SET holder=excluded.holder,"
        " acquired_at=excluded.acquired_at, expires_at=excluded.expires_at"
        " WHERE expires_at < datetime('now')", (holder,))
    conn.commit()
    row = conn.execute("SELECT holder FROM dispatcher_lease WHERE id=1").fetchone()
    return row and row["holder"] == holder


def _a2a_wakeup(agent_name, task_id):
    """可选唤醒通知。stdlib urllib 直发，10s 超时，丢失靠轮询兜底。"""
    url = AGENT_URLS.get(agent_name)
    if not url or not A2A_TOKEN:
        return
    payload = json.dumps({
        "jsonrpc": "2.0", "method": "message/send", "id": 1,
        "params": {"message": {"role": "user", "parts": [
            {"type": "text", "text": f"[Pipeline] 新任务待处理: {task_id}，请轮询 Dispatcher 认领。"}]}}
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {A2A_TOKEN}"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


_feishu_token_cache = {"token": "", "exp": 0}

def _feishu_send(text):
    """经飞书应用 API 发消息到监控群。无配置则静默跳过。"""
    if not FEISHU_CFG.get("app_id"):
        return False
    try:
        now = time.time()
        if now > _feishu_token_cache["exp"] - 60:
            data = json.dumps({"app_id": FEISHU_CFG["app_id"],
                               "app_secret": FEISHU_CFG["app_secret"]}).encode()
            with urllib.request.urlopen(urllib.request.Request(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    data=data, headers={"Content-Type": "application/json"}), timeout=10) as r:
                d = json.loads(r.read().decode())
            _feishu_token_cache["token"] = d["tenant_access_token"]
            _feishu_token_cache["exp"] = now + int(d.get("expire", 7200))
        body = json.dumps({
            "receive_id": FEISHU_CFG["chat_id"], "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False)}).encode()
        with urllib.request.urlopen(urllib.request.Request(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                data=body, headers={"Content-Type": "application/json",
                                    "Authorization": f"Bearer {_feishu_token_cache['token']}"}),
                timeout=10) as r:
            return json.loads(r.read().decode()).get("code") == 0
    except Exception:
        return False


def _notify_human(conn, message, task_id="-", route="cli"):
    """告警冷却 15min（落库持久，跨 oneshot 进程有效）。route 决定 @谁。"""
    row = conn.execute(
        "SELECT created_at FROM audit_log WHERE action='alert' AND details=?"
        " ORDER BY id DESC LIMIT 1", (message[:200],)).fetchone()
    if row and (_now() - _parse(row["created_at"])).total_seconds() < ALERT_COOLDOWN_S:
        return
    models.audit(conn, task_id, "alert", None, None, "dispatcher", message[:200])
    conn.commit()
    # 路由：离线→对应 Agent；escalate→owner+cli；waiting_human→owner；其余→cli
    at_map = FEISHU_CFG.get("at_mapping", {})
    who = {"owner": at_map.get("owner", ""), "cli": at_map.get("cli", "")}
    if route in who and who[route]:
        message = f"@{who[route]} {message}"
    if FEISHU_WEBHOOK:
        try:
            data = json.dumps({"msg_type": "text", "content": {"text": message}}).encode()
            urllib.request.urlopen(urllib.request.Request(
                FEISHU_WEBHOOK, data=data,
                headers={"Content-Type": "application/json"}), timeout=10)
        except Exception:
            pass
    _feishu_send(message)


def _handle_timeout(conn, task):
    """四级降级链。"""
    tid = task["task_id"]
    if task["retry_count"] < task["max_retries"]:
        n = task["retry_count"] + 1
        backoff = 30 * (2 ** (n - 1))
        conn.execute(
            "UPDATE pipeline_status SET retry_count=?, sub_status='retrying',"
            " next_retry_at=datetime('now', ?), updated_at=datetime('now') WHERE task_id=?",
            (n, f"+{backoff} seconds", tid))
        models.audit(conn, tid, "retry", task["status"], task["status"], "dispatcher",
                     f"L1 retry {n}/{task['max_retries']}, backoff {backoff}s")
    elif task["priority"] == 2:
        models.touch_status(conn, tid, "cancelled", "dispatcher", "L2 低优任务超时跳过")
        models.audit(conn, tid, "skip", None, None, "dispatcher", "L2 skip non-critical")
    elif (task["status"] in gate_judge.STATUS_NEXT
          and task["status"] != "received"
          and task["sub_status"] != "rolled_back"):
        # L3 只允许回滚一次；回滚后再超时直接 L4（演练实钓：无限回滚-推进乒乓 bug）
        prev = _prev_status(task["status"])
        models.touch_status(conn, tid, prev, "dispatcher", "L3 回滚上一站")
        conn.execute("UPDATE pipeline_status SET sub_status='rolled_back' WHERE task_id=?", (tid,))
        conn.commit()
    else:
        models.touch_status(conn, tid, "escalate_human", "dispatcher", "L4 升级人类")
        _notify_human(conn, f"[Pipeline] 任务 {tid} 超时升级，状态={task['status']}", tid)
    conn.commit()


def _prev_status(status):
    chain = list(gate_judge.STATUS_NEXT.keys())
    return chain[chain.index(status) - 1] if status in chain and chain.index(status) > 0 else "received"


def _already_dispatched(task):
    if not task["dispatch_sent_at"]:
        return False
    if task["dispatch_ack"]:
        return True
    age = (_now() - _parse(task["dispatch_sent_at"])).total_seconds()
    return age <= DISPATCH_LEASE_S


def dispatch_loop():
    conn = models.connect()
    if not _try_acquire_lease(conn):
        conn.close()
        return {"role": "standby"}

    advanced = []
    rows = conn.execute(
        "SELECT * FROM pipeline_status WHERE status NOT IN ('done','cancelled')").fetchall()
    for t in rows:
        # 重试退避期内的任务跳过
        if t["next_retry_at"] and _parse(t["next_retry_at"]) > _now():
            continue
        verdict, evidence = gate_judge.judge(t)
        if verdict == gate_judge.PASS:
            nxt = gate_judge.STATUS_NEXT.get(t["status"])
            if not nxt:
                continue
            if not _already_dispatched(t):
                conn.execute(
                    "UPDATE pipeline_status SET dispatch_sent_at=datetime('now'),"
                    " dispatch_agent=?, dispatch_ack=0 WHERE task_id=?",
                    (gate_judge.STATUS_AGENT.get(nxt, ""), t["task_id"]))
                _a2a_wakeup(gate_judge.STATUS_AGENT.get(nxt, ""), t["task_id"])
            models.touch_status(conn, t["task_id"], nxt, "dispatcher", evidence)
            advanced.append(t["task_id"])
            if nxt == "waiting_human":
                _notify_human(conn, f"[Pipeline] 任务 {t['task_id']} 进入待签署，请 ctgc approve {t['task_id']}",
                              t["task_id"], route="owner")
        elif verdict == gate_judge.BLOCKED:
            if t["status"] in gate_judge.HUMAN_STATES:
                continue
            age = (_now() - _parse(t["status_changed_at"])).total_seconds()
            if age > (t["hold_timeout_m"] or 120) * 60:
                _handle_timeout(conn, t)

    # Agent 心跳检查（冷却在 _notify_human 内）
    for a in conn.execute("SELECT * FROM agents"):
        hb = _parse(a["last_heartbeat_at"])
        if hb and (_now() - hb).total_seconds() > HEARTBEAT_TIMEOUT_S and not a["is_down"]:
            conn.execute("UPDATE agents SET is_down=1 WHERE name=?", (a["name"],))
            _notify_human(conn, f"[Pipeline] Agent {a['name']} 无心跳超 {HEARTBEAT_TIMEOUT_S}s")
    conn.execute("UPDATE dispatcher_lease SET expires_at=datetime('now','+35 seconds') WHERE id=1")
    conn.commit()
    conn.close()
    return {"role": "primary", "advanced": advanced}


_START = time.time()

if __name__ == "__main__":
    models.init_db()
    print(json.dumps(dispatch_loop(), ensure_ascii=False))
