# Agent Pipeline Engine

**30 行 YAML 定义多 Agent 协作管道。SQLite 状态机 + A2A 通信。不需要消息队列，不需要工作流引擎。**

---

## 这是什么

一个配置驱动的多 Agent 编排引擎。你定义 Agent 角色、管道阶段、门禁条件，引擎自动管理状态流转、Agent 唤醒、人类介入。

为 Hermes Agent 的 A2A 协议设计，但状态机和门禁层与通信协议解耦。

## 30 秒看懂

```yaml
# pipeline.yaml
stages:
  - id: review
    trigger: task_created
    action: a2a_orchestrate [reviewer_a, reviewer_b]
    gate:
      condition: reviewer_a_confirmed == 1 AND reviewer_b_confirmed == 1
      on_pass: dispatch_worker
    human_interrupt: [owner]
    polling:
      interval: 5m
      timeout: 120s
```

一个任务创建 → 两个审查者同时收到五问 → 引擎每 5 分钟轮询状态 → 双方确认后自动放行下一阶段 → 任何时刻人类可介入。

## 为什么不用 n8n / LangGraph / Dify

- **n8n** 是确定性工作流引擎。LLM Agent 是非确定性节点——超时原因不是网络，可能是模型幻觉。n8n 不理解这个区别。
- **LangGraph** 给你图编排能力，但你需要自己实现状态持久化、超时重试、人类介入、审计日志。
- **Dify** 是单 Agent 低代码平台。没有多 Agent 编排。

这个项目只做一件事：**管理"谁做完了、下一步谁做、谁卡住了"这个状态机。** 其他交给 Hermes。

## 核心文件

| 文件 | 作用 |
|------|------|
| `init_db.py` | 初始化 SQLite WAL 数据库，幂等 |
| `gate_judge.py` | 读状态机，判定 PASS / BLOCKED / NOT_FOUND |
| `poll_agents.py` | 定时轮询 Agent 状态，更新 SQLite |
| `pipeline.example.yaml` | 30 行示例配置 |

## 依赖

- Python 3.11+
- SQLite 3（标准库自带）
- Hermes Agent（A2A 通信层）
