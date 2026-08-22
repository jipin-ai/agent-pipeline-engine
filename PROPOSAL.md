# Agent Pipeline Engine v1 — 方案提案

> 供 BA-01 / ORC-01 / DEV-01 讨论审核。架构师起草，2026-08-11。

---

## 一、问题陈述

**当前 CTGC 管道现状：**

- 6 个 Hermes Agent 节点通过 A2A 通信
- KV 状态机能记录每个任务的状态
- 但**状态变更不会自动触发下一步**
- 每次任务流转需要人类手动在飞书通知下一个角色

**今天上午的 A2A 诊断暴露了三个系统性问题：**

| 时间 | 节点 | 故障 | 影响 |
|------|------|------|------|
| 08:00 | BA-01 | a2a-gate.py while...else 死码 bug | 6天无法接收 A2A 任务 |
| 08:10 | BA-01 | Playwright 安装导致系统僵死 | 需阿里云控制台硬重启 |
| 12:00 | QA-01 | Agent 池被长任务占满 | 所有 A2A 入站 504 超时 |
| 12:10 | ORC | 系统静默重启 | 无人知晓 |

**核心问题不是 A2A 协议不稳定。** A2A 本身是通的（修复后全网 6 节点 HTTP 200）。问题是：

1. **Agent 不知道自己挂了。** BA-01 的 Gate 带着死码跑了 6 天。
2. **Agent 不知道下一步该谁。** ORC 完成了不会自动通知 DEV。
3. **故障没有人自动告警。** 每次都要架构师手动 SSH 进去发现。

---

## 二、方案概述

在现有 KV 状态机上面加一层**确定性反应器（Dispatcher）**。

```
                     ┌──────────────────┐
                     │  Dispatcher      │  ← 新增：确定性 Python 进程
                     │  (cron 30s)      │
                     └──────┬───────────┘
                            │ 读状态 + 判定
                     ┌──────▼───────────┐
                     │  SQLite 状态机    │  ← 已有：KV 的升级版
                     │  pipeline.db     │
                     └──────┬───────────┘
                            │ A2A call 派发任务
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
   ┌─────────┐        ┌─────────┐        ┌─────────┐
   │ BA-01   │        │ DEV-01  │        │ QA-01   │
   │ EARS    │   →    │ 编码    │   →    │ 盲测    │
   └─────────┘        └─────────┘        └─────────┘
```

**核心变化：**

| | 现在 | 方案 v1 |
|---|---|---|
| 状态跟踪 | KV 记录 | SQLite（更结构化） |
| 任务派发 | 人类手动 | Dispatcher 自动 |
| 门禁判定 | 人类判断 | gate_judge.py 自动 |
| 超时告警 | 无 | 超时自动通知架构师 |
| Agent 故障 | 不知道 | poll 超时 → 告警 |
| 人类介入 | 全程参与 | 关键节点暂停（BA签署、DEMO部署） |

---

## 三、状态机设计

### 3.1 管道五站

```
received ──→ ears_draft ──→ ready ──→ dev_done ──→ qa_passed ──→ demo_deployed
   │            │             │           │            │              │
   BA创建      BA产出EARS    DEV+QA     DEV完成      QA通过        DEMO部署
               等待签署      五问通过    派发QA       派发DEMO      等待验收
```

### 3.2 每个状态的触发和门禁

| 当前状态 | 谁执行 | 做什么 | 门禁条件 | 通过后 |
|----------|--------|--------|----------|--------|
| `received` | — | 任务已创建 | 自动 | → `ears_draft` |
| `ears_draft` | BA-01 | 产出 EARS 需求文档 | **人类签署**（interrupt） | → `ready` |
| `ready` | ORC-01 | 拆解任务 + DEV/QA 五问 | dev_confirmed=1 AND qa_confirmed=1 | → `dev_done` |
| `dev_done` | DEV-01 | 编码 + CGO=0 + SHA256 | DEV 自报 `worker_done` | → `qa_passed` |
| `qa_passed` | QA-01 | 盲测 + 边界数据 | QA 自报 `review_passed` | → `demo_deployed` |
| `demo_deployed` | DEMO-01 | 部署 + 四证自证 | DEMO 自报 `deployed` | → 等待甲方验收 |

### 3.3 SQLite 表结构（在已有基础上扩展）

```sql
CREATE TABLE pipeline_status (
    task_id        TEXT PRIMARY KEY,
    project        TEXT NOT NULL,
    status         TEXT DEFAULT 'received',
    
    -- Agent 确认字段
    ba_done        INTEGER DEFAULT 0,    -- BA EARS已完成
    dev_confirmed  INTEGER DEFAULT 0,    -- DEV 五问确认
    qa_confirmed   INTEGER DEFAULT 0,    -- QA 五问确认
    dev_done       INTEGER DEFAULT 0,    -- DEV 编码完成
    qa_done        INTEGER DEFAULT 0,    -- QA 测试完成
    demo_done      INTEGER DEFAULT 0,    -- DEMO 部署完成
    
    -- 产出物
    ears_doc       TEXT,                 -- EARS 文档路径
    artifact_path  TEXT,                 -- 制品路径
    sha256         TEXT,                 -- 制品 SHA256
    
    -- 时间戳（自动）
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now')),
    status_changed_at TEXT DEFAULT (datetime('now'))
);
```

**时间戳铁律：** 所有时间来自 SQLite `datetime('now')`，不来自 Agent 自报。看板所有时间指标从这张表算。

---

## 四、Dispatcher 设计（核心新增）

### 4.1 做什么

每 30 秒运行一次（cron），执行三个步骤：

```
1. 扫描 pipeline_status 表
2. 对每个 status，调用 gate_judge 判定
3. 如果 PASS → 调 a2a_call 派发给下一站 Agent，更新 status
   如果 BLOCKED → 检查超时，超时则告警
   如果 Agent 无响应 → 标记故障，告警架构师
```

### 4.2 伪代码

```python
def dispatch_loop():
    for task in get_active_tasks():
        verdict = gate_judge.judge(task.task_id)
        
        if verdict == "PASS":
            next_agent = STATUS_AGENT_MAP[verdict.next_status]
            result = a2a_call(next_agent, task_to_prompt(task))
            if result.ok:
                advance_status(task.task_id, verdict.next_status)
            else:
                log_error(task, "A2A call failed")
                
        elif verdict == "BLOCKED":
            if time_since(task.updated_at) > task.timeout:
                notify_architect(task, "超时未完成")
                
        elif verdict == "ERROR":
            notify_architect(task, verdict.details)
```

### 4.3 不做什么

- **不跑 LLM。** Dispatcher 是纯 Python 确定性代码。
- **不判断产出质量。** 只判定"Agent 报了完成没有"。
- **不替代人类决策。** BA 签署、DEMO 部署前暂停等人类确认。

---

## 五、人类介入点（Interrupts）

| 介入点 | 触发条件 | 人类做什么 | 如何继续 |
|--------|----------|-----------|---------|
| BA 产出 EARS 后 | status = `ears_draft` | 审核 EARS，飞书回复"批准"或"修改" | Dispatcher 收到批准 → advance |
| DEV 编码完成 | status = `dev_done` | 可选：架构师检查制品 | 自动流转到 QA |
| QA 测试完成 | status = `qa_passed` | 可选 | 自动流转到 DEMO |
| DEMO 部署前 | status = `qa_passed` | 甲方/架构师确认可以部署 | 飞书回复"部署" |
| 任何阶段超时 | hold_timeout 到期 | 架构师介入调查 | 手动修复后 resume |

**人类介入 = 在状态字段加 `_approved` 标记，Dispatcher 检查到标记后自动继续。**

---

## 六、故障处理

| 故障类型 | 检测方式 | 处理 |
|----------|---------|------|
| Agent 无响应 | A2A call 超时 120s | 重试 3 次 → 标记 `agent_down` → 通知架构师 |
| Gateway 端口不通 | TCP 连接超时 | 同上 |
| 任务卡住 | status 不变超过 hold_timeout | 通知架构师 |
| Dispatcher 挂了 | cron watchdog | 架构师收到 cron 离线告警 |

---

## 七、与现有系统的关系

| 组件 | 方案 v1 后 |
|------|-----------|
| KV 状态机 | 保留——作为看板数据源，Dispatcher 同时写 KV 和 SQLite |
| A2A 协议 | 保留——Dispatcher 通过 A2A 调 Agent |
| Hermes Agent | 不变——BA/ORC/DEV/QA/DEMO 各自照常运行 |
| cron | 新增 2 个：`dispatch_loop`（30s）+ `health_check`（5min） |

---

## 八、实施计划

### Phase 0：方案审核（本阶段）

- [ ] BA-01 审阅 EARS 产出流程和签署点
- [ ] ORC-01 审阅拆解流程和五问触发机制
- [ ] DEV-01 审阅编码→自报→流转流程
- [ ] QA-01 审阅盲测触发和通过条件

### Phase 1：Dispatcher 核心

- [ ] 扩展 SQLite schema（新增字段）
- [ ] 实现 `dispatch_loop.py`（状态扫描 + A2A 派发）
- [ ] 部署到 ORC 节点（管道中枢）
- [ ] 配置 cron 30s 定时

### Phase 2：人类介入

- [ ] BA 签署机制（飞书消息 → Dispatcher 识别）
- [ ] 超时告警（飞书通知架构师）
- [ ] Agent 健康检查

### Phase 3：全管道试跑

- [ ] 一个真实任务从 BA 到 DEMO 全自动流转
- [ ] 故意注入故障（Agent 下线）验证告警
- [ ] 测量端到端延迟

---

## 九、待讨论问题

1. **Dispatcher 放在哪个节点？** 建议 ORC（管道中枢），或架构师节点。
2. **BA 签署是什么形式？** 飞书消息？CLI 命令？看板按钮？
3. **如果一个 Agent 同时处理多个任务怎么办？** 队列还是并发？
4. **需要保留 KV 状态机还是完全迁到 SQLite？**

---

> 架构师。方案供团队审核，不改代码，只讨论。
