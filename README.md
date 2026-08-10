<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License">
  <img src="https://img.shields.io/badge/status-alpha-orange?style=flat-square" alt="Alpha">
  <img src="https://img.shields.io/badge/deps-zero-brightgreen?style=flat-square" alt="Zero Dependencies">
</p>

<br>

# Agent Pipeline Engine

**30 行 YAML 定义多 Agent 协作管道。SQLite 状态机 + A2A 通信。不需要消息队列。不需要工作流引擎。**

<br>

```
+---------------+
|   received    |
+---------------+
       |
       v
+---------------+
|  ears_draft   |
+---------------+
       |
       | [G1] DEV+QA dual-confirm
       v
+---------------+
|     ready     |
+---------------+
       |
       v
+---------------+
|   dev_done    |
+---------------+
       |
       v
+---------------+
|  qa_passed    |
+---------------+
       |
       | [G2] customer acceptance
       v
+---------------+
| demo_deployed |
+---------------+
```
<br>

## 这是什么

一个状态机引擎，管理多 Agent 协作管道。回答三个问题：

- **谁做完了？** — 轮询 Agent 确认状态，更新 SQLite
- **下一步谁做？** — 读门禁条件，判定 PASS / BLOCKED
- **谁卡住了？** — 超时自动升级，通知人类介入

它不执行任务。它不调用 Agent。它只是管理"管道现在处于什么状态、下一步该谁上场"这个状态机。任务执行交给 [Hermes Agent](https://github.com/nousresearch/hermes-agent) 的 A2A 协议。

<br>

## 为什么不用 n8n / LangGraph / Dify

| | n8n | LangGraph | Dify | **这个项目** |
|---|:--:|:--:|:--:|:--:|
| LLM 不确定性处理 | 不理解 | 需手写重试 | 无 | 内置超时+升级 |
| 状态持久化 | 内置 | 需自己实现 | 内置 | SQLite WAL |
| 人类介入 | 有 | 需自己实现 | 无 | 任意阶段可介入 |
| 多 Agent 编排 | 无 | 需手写图 | 无 | 核心功能 |
| 部署依赖 | PostgreSQL | 任意 | PostgreSQL | **无外部依赖** |
| 代码量 | 平台 | 平台 | 平台 | **451 行** |

<br>

## 30 秒看懂

```yaml
# pipeline.yaml
stages:
  - id: five_questions
    trigger: task_created
    action: a2a_orchestrate [reviewer_a, reviewer_b]
    gate:
      condition: reviewer_a_confirmed == 1 AND reviewer_b_confirmed == 1
      on_pass: dispatch_worker
      on_blocked: hold
      hold_timeout: 2h
      on_escalate: notify_architect
    human_interrupt: [owner]
```

一个任务创建 → 两个审查者同时收到查询 → 引擎每 5 分钟轮询确认状态 → 双方确认后自动放行 → 阻塞超过 2 小时自动通知架构师 → 任何时刻人类可介入。

<br>

## 快速开始

```bash
# 1. 初始化数据库
python init_db.py

# 2. 插入一个任务
sqlite3 ~/.pipeline/pipeline.db "INSERT INTO pipeline_status (task_id, project, status) VALUES ('task-001', 'demo', 'received')"

# 3. 判定管道状态
python gate_judge.py task-001
# Output: PASS: 等待 ORC 拆解

# 4. 设置定时轮询 Agent 状态（cron）
*/5 * * * * cd /path/to/agent-pipeline-engine && python poll_agents.py >> /var/log/pipeline.log 2>&1
```

<br>

## 核心文件

| 文件 | 行数 | 作用 |
|------|:--:|------|
| `init_db.py` | 47 | 初始化 SQLite WAL 数据库，幂等 |
| `gate_judge.py` | 149 | 读状态机，三分支输出：PASS / BLOCKED / NOT_FOUND |
| `poll_agents.py` | 144 | 定时轮询 Agent 确认状态，更新 SQLite |
| `pipeline.example.yaml` | 58 | 30 行管道配置示例 |

<br>

## 状态机

```
received --> ears_draft --> ready --> dev_done --> qa_passed --> demo_deployed
       ^G1                                ^G2
G1 = DEV+QA dual-confirm gate   G2 = customer acceptance gate
```
每个状态转换由 `gate_judge.py` 判定，`poll_agents.py` 更新 Agent 确认状态。

<br>

## 生态系统

```
+-------------------+    +-------------------+    +-------------------+
|   orchestration   |    |      runtime      |    |      memory       |
| pipeline-engine   | -> |   Hermes Agent    | -> |   Agent-Memory    |
+-------------------+    +-------------------+    +-------------------+
```
三个项目互补，不是竞争关系。这个项目只做编排——把 Hermes Agent 的多 Agent 通信管起来。

<br>

## 设计原则

- **零外部依赖。** Python 3.11 标准库 + SQLite3。不装 PostgreSQL，不装 Redis。
- **配置驱动。** 管道规则在 YAML 里，不硬编码在代码里。
- **人类不离开循环。** 任何阶段都可以 `human_interrupt`，升级超时可以 `notify_architect`。
- **只读状态，不执行任务。** 管道引擎只管"轮到谁了"，不管 Agent 具体干什么。
- **Cron 友好。** `poll_agents.py` 设计为定时任务调用，不是常驻进程。

<br>

## 路线图

- [x] SQLite 状态机 + 门禁判定
- [x] A2A Agent 状态轮询
- [x] 超时升级 + 人类介入
- [ ] pipeline.yaml 热加载（当前需重启 cron）
- [ ] Webhook 触发器替代纯 cron 轮询
- [ ] 管道可视化面板（观察窗）
- [ ] 并行阶段支持（多 Agent 同时执行不同任务）

<br>

## 贡献

Alpha 阶段。欢迎 Issue 和 PR。

项目由 [CTGC](https://github.com/ctgc) 维护 —— 一个多 Agent 协作管道研究团队。

<br>

## 许可证

MIT
