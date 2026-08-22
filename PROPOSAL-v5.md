# Agent Pipeline Engine — 最终方案 v5

> 架构师综合修订。综合 BA-01 / DEV-01 / ORC-01 全部审核意见。2026-08-11。
> 
> 审核轨迹：v1(架构师初稿) → v2(ORC批判重写) → v3(BA+DEV+ORC-1/2) → v4(BA-P0跨节点) → v5(综合修订)

---

## 一、问题陈述

当前 CTGC 管道：6 个 Hermes Agent 通过 A2A 通信，KV 状态机能记录状态。但**状态变更不会自动触发下一步**——每次任务流转需要人类手动飞书通知。

2026-08-11 A2A 诊断暴露的系统性问题：

- BA-01：a2a-gate.py while...else 死码 bug，6 天无法接收 A2A 任务
- BA-01：Playwright 安装导致系统僵死，需硬重启
- QA-01：Agent 池被长任务占满，所有 A2A 入站 504 超时
- ORC：系统静默重启，无人知晓

核心问题不是 A2A 协议不稳定。A2A 本身是通的。问题是：(1) Agent 不知道自己挂了 (2) Agent 不知道下一步该谁 (3) 故障没有自动告警。

---

## 二、架构总则（三条，不可协商）

### 2.1 数据层：DB 独占 + HTTP API

**pipeline.db 由 Dispatcher 节点独占直写。所有 Agent 的心跳、证据、状态写入一律走 Dispatcher HTTP API。** Agent 不直接访问 SQLite 文件。

```
Agent (DEV-01) → HTTP → Dispatcher API (:8800) → SQLite (本地独写)
```

### 2.2 通信分层

| 通道 | 协议 | 用途 |
|------|------|------|
| Dispatcher HTTP API | HTTP + Bearer Token | Agent 读写状态、心跳、证据 |
| A2A | JSON-RPC | 人类交互、架构师诊断、轻量唤醒通知（可选） |
| 飞书 | 消息推送 | 仅通知（不接收指令） |
| CLI `ctgc` | HTTP → Dispatcher API | 人类签署/取消/推进 |

**A2A 降级为辅助通道。** 任务内容不通过 A2A 传输，不通过 A2A 等结果。

### 2.3 部署拓扑

```
新增 ECS (1C2G, 公网 IP)
┌─────────────────────────────────────┐
│  Dispatcher (primary)               │
│  :8800 HTTP API (FastAPI)           │
│  pipeline.db (独占直写)              │
│  systemd service (常驻，非 cron)     │
└────────────────┬────────────────────┘
                 │ HTTP API (Bearer Token + IP 白名单)
    ┌────────────┼────────────┬────────────┬────────────┐
    ▼            ▼            ▼            ▼            ▼
  DEV-01       QA-01        DEMO         BA-01        ORC-01
  cron 3min    cron 3min    cron 3min    cron 3min    cron 3min
  GET /tasks   GET /tasks   GET /tasks   GET /tasks   GET /tasks

架构师本机 (jipin)
  ctgc CLI → HTTP → Dispatcher API
```

Dispatcher 放新增最小 ECS（1C2G）。不放架构师本机（内网不可达），不放 ORC（职责混淆）。

---

## 三、管道状态机（15 状态）

```
                     ┌── cancelled (终态)
                     │
received ──→ ears_draft ──→ waiting_human ──→ ready ──→ dev_working ──→ qa_working ──→ demo_working ──→ done
               │                    ↑ 人类签署        │           │               │               │
               │                    │                   │           │               │               │
               │               cancelled           blocked     blocked         blocked         blocked
               │                                   (外部依赖)   (外部依赖)       (外部依赖)       (外部依赖)
               │
          escalate_human (超时降级后的人工处理状态)
```

| 状态 | 含义 | 执行者 | 门禁 | 通过后 |
|------|------|--------|------|--------|
| `received` | 任务已创建 | — | 自动 | → ears_draft |
| `ears_draft` | BA 写 EARS | BA-01 | EARS 文档路径非空 | → waiting_human |
| `waiting_human` | 等人类签署 | 用户 | CLI `ctgc approve` | → ready |
| `ready` | ORC 拆解 + 五问 | ORC-01 | 拆解文档 + dev_confirmed=1 AND qa_confirmed=1 | → dev_working |
| `dev_working` | DEV 编码 | DEV-01 | git_commit + pytest 报告路径 + SHA256 | → qa_working |
| `qa_working` | QA 盲测 | QA-01 | 测试报告 + 覆盖率≥60% + blocking_issues=0 | → demo_working |
| `demo_working` | DEMO 部署 | DEMO-01 | deploy_url + health_check HTTP 200 | → done |
| `done` | 完成 | — | — | 终态 |
| `blocked` | 外部依赖阻塞 | — | 人工解除 | → 回到原状态 |
| `cancelled` | 任务取消 | — | — | 终态 |
| `escalate_human` | 升级人类处理 | 架构师/用户 | 人工修复 | → resume |
| `timeout` | 超时 | — | 自动 → 降级链 | → 降级 |

**sub_status 字段**跟踪任务内进度：`orc_decomposing` → `orc_done` → `fiveq_waiting` → `fiveq_done` 等。

**retry 不单独成状态。** retry 是 retry_count 计数器 + next_retry_at 字段。状态留在原站，超时后按降级链处理，retry 时 sub_status 标为 `retrying`。

**waiting_human 和 escalate_human 豁免超时判定。**

---

## 四、证据门禁（不依赖 Agent 自报）

| 门禁 | 证据 | 写入者 | Dispatcher 验证 |
|------|------|--------|----------------|
| BA 完成 | EARS 文档路径 + 人类签署 | BA(路径) + 人类(签署) | 路径非空 + signed_by 非空 |
| ORC 完成 | 拆解文档路径 + dev/qa_confirmed | ORC | 路径非空 + 双确认=1 |
| DEV 完成 | git_commit + pytest 报告落盘路径 + SHA256 | DEV | 三项均非空 |
| QA 完成 | 测试报告路径 + coverage_pct + blocking_issues=0 | QA | 路径非空 + 覆盖率≥60 + blocking=0 |
| DEMO 完成 | deploy_url + health_check_ok | DEMO | URL 非空 + health check HTTP 200 |

**Phase 3 必做：注故障验证。** 故意写入假 SHA256/假报告 → 抓不住就补 Dispatcher 抽检复算环节。

---

## 五、SQLite DDL

```sql
CREATE TABLE pipeline_status (
    task_id           TEXT PRIMARY KEY,
    project           TEXT NOT NULL,
    status            TEXT DEFAULT 'received',
    sub_status        TEXT,
    
    -- BA 证据
    ears_doc_path     TEXT,
    ears_signed_by    TEXT,
    ears_signed_at    TEXT,
    
    -- ORC 证据
    orc_doc_path      TEXT,
    dev_confirmed     INTEGER DEFAULT 0,
    qa_confirmed      INTEGER DEFAULT 0,
    
    -- DEV 证据
    git_commit        TEXT,
    pytest_report_path TEXT,
    artifact_sha256   TEXT,
    
    -- QA 证据
    test_report_path  TEXT,
    coverage_pct      REAL,
    blocking_issues   INTEGER DEFAULT 0,
    
    -- DEMO 证据
    deploy_url        TEXT,
    health_check_ok   INTEGER DEFAULT 0,
    
    -- 时间戳（全部 SQLite datetime('now')）
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    status_changed_at TEXT DEFAULT (datetime('now')),
    
    -- 派发控制（lease，防竞态）
    dispatch_sent_at  TEXT,
    dispatch_agent    TEXT,
    dispatch_ack      INTEGER DEFAULT 0,
    
    -- 心跳（Agent 通过 HTTP API 写入）
    agent_heartbeat_at TEXT,
    task_heartbeat_at  TEXT,
    
    -- 降级/重试
    retry_count       INTEGER DEFAULT 0,
    max_retries       INTEGER DEFAULT 3,
    next_retry_at     TEXT,
    priority          INTEGER DEFAULT 1,
    hold_timeout_m    INTEGER DEFAULT 120
);

CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL,
    action        TEXT NOT NULL,
    old_status    TEXT,
    new_status    TEXT,
    triggered_by  TEXT,
    details       TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE dispatcher_lease (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    holder      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE agents (
    name              TEXT PRIMARY KEY,
    url               TEXT NOT NULL,
    last_heartbeat_at TEXT,
    is_down           INTEGER DEFAULT 0,
    alerted_at        TEXT
);
```

---

## 六、HTTP API 规范

### 6.1 端点

```
# Agent 侧
GET    /health                              → {"status":"ok","uptime":3600}
GET    /tasks?agent={name}&status={s1,s2}   → [{task_id,status,...}]
GET    /tasks/{task_id}                     → 单个任务全文
POST   /tasks/{task_id}/claim               → 认领 (200 / 409)
POST   /tasks/{task_id}/heartbeat           → 任务心跳
POST   /agents/{name}/heartbeat             → Agent 节点心跳
PATCH  /tasks/{task_id}                     → 更新状态+证据

# 人类侧
POST   /tasks/{task_id}/approve             → 签署
POST   /tasks/{task_id}/cancel              → 取消
POST   /tasks/{task_id}/advance             → 强制推进(架构师)

# 管理侧
GET    /agents                              → Agent 在线状态
GET    /audit?task_id={id}                  → 审计日志
```

### 6.2 认证

- 所有请求带 `Authorization: Bearer <token>`
- Dispatcher 验证 token + 来源 IP 白名单（云端内网段）
- CLI 用环境变量 `CTGC_API_TOKEN`

---

## 七、Agent 完整流程

```
1. cron 每 3min:
   curl -X POST http://dispatcher:8800/agents/DEV-01/heartbeat \
     -H "Authorization: Bearer ***"

2. cron 每 3min (或收到 A2A 唤醒通知后):
   curl http://dispatcher:8800/tasks?agent=DEV-01&status=dev_working
   → [{"task_id":"task-007","status":"dev_working",...}]

3. 认领:
   curl -X POST http://dispatcher:8800/tasks/task-007/claim \
     -H "Authorization: Bearer ***"
   → 200 OK

4. 读全文:
   curl http://dispatcher:8800/tasks/task-007
   → {ears_doc_path, orc_doc_path, acceptance_criteria, ...}

5. 在 Hermes 会话中编码...

6. 完成，写证据:
   curl -X PATCH http://dispatcher:8800/tasks/task-007 \
     -H "Authorization: Bearer ***" \
     -d '{"sub_status":"dev_done","git_commit":"abc123",
          "pytest_report_path":"/path/to/report","artifact_sha256":"def456"}'

7. Dispatcher 下轮扫描发现 dev_done → 更新 status='qa_working'
   QA 下次轮询时发现新任务
```

---

## 八、Dispatcher 实现

### 8.1 主循环

```python
def dispatch_loop():
    conn = sqlite3.connect(DB_PATH)
    if not try_acquire_lease(conn, "primary"):
        conn.close()
        return  # standby, skip

    for task in get_active_tasks(conn):
        verdict, evidence = gate_judge.judge(task)
        
        if verdict == "PASS":
            next_status = STATUS_NEXT[task.status]
            if next_status:
                advance_status(conn, task.id, next_status)
                # 可选：发 A2A 唤醒通知给下一站 Agent
                a2a_wakeup(STATUS_AGENT[next_status], task.id)
                audit_log(conn, task.id, "advance", task.status, next_status)
        
        elif verdict == "BLOCKED":
            if task.status not in HUMAN_STATES:
                if time_since(task.status_changed_at) > task.hold_timeout_m * 60:
                    handle_timeout(conn, task)  # 降级链

    # 心跳检查
    check_heartbeats(conn)
    
    conn.execute("UPDATE dispatcher_lease SET expires_at=datetime('now','+35 seconds')")
    conn.close()
```

### 8.2 Lease Fencing（防主备裂脑）

```python
def try_acquire_lease(conn, my_name):
    conn.execute("""
        INSERT INTO dispatcher_lease (id, holder, acquired_at, expires_at)
        VALUES (1, ?, datetime('now'), datetime('now', '+35 seconds'))
        ON CONFLICT(id) DO UPDATE
        SET holder=excluded.holder, acquired_at=excluded.acquired_at,
            expires_at=excluded.expires_at
        WHERE expires_at < datetime('now')
    """, (my_name,))
    conn.commit()
    row = conn.execute("SELECT holder FROM dispatcher_lease WHERE id=1").fetchone()
    return row[0] == my_name
```

### 8.3 派发防重（Lease）

```python
def already_dispatched(task):
    if not task.dispatch_sent_at:
        return False
    if task.dispatch_ack == 1:
        return True
    age = (datetime.utcnow() - parse_time(task.dispatch_sent_at)).seconds
    return age <= 300  # 5min 内不重发
```

### 8.4 A2A 通知（stdlib urllib，不用 hermes chat）

```python
import urllib.request, json

def a2a_wakeup(agent_name, task_id):
    url = AGENT_URLS[agent_name]
    payload = json.dumps({
        "jsonrpc": "2.0", "method": "message/send",
        "params": {"message": {"role": "user", "parts": [
            {"type": "text", "text": f"新任务: {task_id}"}
        ]}}, "id": 1
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {A2A_TOKEN}"
    })
    try:
        urllib.request.urlopen(req, timeout=10)
    except:
        pass  # 可选通知，丢失靠 Agent 轮询兜底
```

### 8.5 告警冷却（防轰炸）

```python
alerted_cache = {}

def notify_human(message):
    key = message[:40]
    now = datetime.utcnow()
    if key in alerted_cache and (now - alerted_cache[key]).seconds < 900:
        return  # 15min 内不重复
    alerted_cache[key] = now
    feishu_send(message)
```

### 8.6 证据门禁判定

```python
def judge_dev_done(task):
    if not task.git_commit:
        return BLOCKED, "无 git commit"
    if not task.pytest_report_path:
        return BLOCKED, "无 pytest 报告"
    if not task.artifact_sha256:
        return BLOCKED, "无制品 SHA256"
    return PASS, "DEV 已完成"

def judge_qa_done(task):
    if not task.test_report_path:
        return BLOCKED, "无测试报告"
    if task.coverage_pct and task.coverage_pct < 60:
        return BLOCKED, f"覆盖率 {task.coverage_pct}% < 60%"
    if task.blocking_issues > 0:
        return BLOCKED, f"有 {task.blocking_issues} 个 blocking issue"
    return PASS, "QA 已通过"

def judge_demo_done(task):
    if not task.deploy_url:
        return BLOCKED, "无部署地址"
    if not task.health_check_ok:
        try:
            r = requests.get(f"{task.deploy_url}/health", timeout=5)
            if r.status_code != 200:
                return BLOCKED, f"健康检查失败: HTTP {r.status_code}"
        except:
            return BLOCKED, "健康检查不可达"
    return PASS, "DEMO 已部署"
```

---

## 九、超时降级链（四级）

```
超时触发
  ↓
Level 1: RETRY（retry_count < max_retries）
  指数退避：30s → 60s → 120s
  ↓ 耗尽
Level 2: SKIP（仅 priority=P2 低优任务可跳过）
  ↓ P0/P1 不可跳过
Level 3: ROLLBACK
  状态退回上一站，通知上一站 Agent
  ↓ 回滚失败
Level 4: ESCALATE_HUMAN
  飞书通知架构师/用户
```

---

## 十、CLI

```bash
# 装在架构师本机 + 用户常用机器
# 认证：环境变量 CTGC_API_TOKEN

ctgc approve task-007      # 签署 EARS
ctgc cancel task-005        # 取消任务
ctgc advance task-003 dev_working  # 强制推进(架构师)
ctgc status task-007        # 查看任务状态
ctgc agents                 # 查看 Agent 在线状态
```

实现：轻量 Python 脚本，HTTP POST 到 Dispatcher API。

---

## 十一、实施计划

**Phase 0：方案签字（当前）**

**Phase 1：基石（1-2 天）**
- [ ] 新增 ECS（1C2G），部署 Dispatcher
- [ ] Dispatcher HTTP API（FastAPI，端口 8800）
- [ ] SQLite schema 部署
- [ ] systemd service（常驻）+ systemd timer（dispatch_loop 30s）
- [ ] Agent 心跳 cron（各节点每 3min curl）
- [ ] ctgc CLI

**Phase 2：门禁（1 天）**
- [ ] gate_judge 逐项实现
- [ ] Agent 轮询 PULL 逻辑（各节点 cron 3min）
- [ ] 证据写入 PATCH API

**Phase 3：试跑（1 天）**
- [ ] 真实任务 BA→ORC→DEV→QA→DEMO 全自动
- [ ] 故障注入：Agent 下线、假 SHA256、假报告
- [ ] 验证：告警冷却、降级链、人类签署恢复

---

## 十二、三方审核结论

| 角色 | 主要关切 | v5 修正 |
|------|---------|---------|
| **BA-01** | SQLite 跨节点不可达 | HTTP API 解决 |
| **BA-01** | 门禁自报完成 | 证据门禁（commit/CI/SHA256/报告） |
| **BA-01** | 超时只告警无降级 | 四级降级链 |
| **BA-01** | 人类介入依赖飞书 | CLI ctgc，飞书仅通知 |
| **DEV-01** | 门禁自报倒退 | 证据字段 + 独立复测 + 故障注入抽检 |
| **DEV-01** | 派发竞态 | lease 三字段 + 过期重发 |
| **DEV-01** | cron 30s 不存在 | systemd timer |
| **DEV-01** | 人类介入无写入路径 | Dispatcher HTTP API + CLI |
| **ORC-01** | A2A 做任务执行 | A2A 降级为通知通道 |
| **ORC-01** | PUSH 模型 | Agent PULL (HTTP 轮询) |
| **ORC-01** | A2A 自报不可靠 | 心跳 + 证据 + Dispatcher 判定 |
| **ORC-01** | 状态粒度 | 15 状态 + sub_status |
| **ORC-01** | hermes chat --yolo | stdlib urllib 直发 |
| **ORC-01** | standby 裂脑 | DB lease fencing |
| **ORC-01** | 告警风暴 | 15min 冷却 |
| **ORC-01** | waiting_human 误杀 | 豁免超时 |
| **ORC-01** | API 认证缺失 | Bearer Token + IP 白名单 |

---

## 十三、v1→v5 变化总结

| 维度 | v1 | v5 |
|------|----|----|
| 任务派发 | Dispatcher → A2A → Agent（PUSH） | Agent → HTTP 轮询 → PULL |
| A2A 角色 | 执行通道 | 辅助通知 + 人类交互 |
| 门禁 | "Agent 自报完成" | 证据字段 + Dispatcher 判定 + 抽检 |
| 数据层 | SQLite 隐式共享 | Dispatcher HTTP API + 独占直写 |
| 状态数 | 6 | 15 + sub_status |
| 降级 | "通知架构师" | 四级自动降级链 |
| 人类介入 | 飞书 | CLI + HTTP API |
| 防竞态 | 无 | lease + dispatch_sent_at + DB fencing |
| 部署 | ORC 上 | 独立 ECS（1C2G） |
| 通知 | hermes chat --yolo | stdlib urllib |

---

架构师。方案已就绪，三方确认后进 Phase 1。
