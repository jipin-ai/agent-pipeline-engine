<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License">
  <img src="https://img.shields.io/badge/deps-2-brightgreen?style=flat-square" alt="2 Dependencies">
  <img src="https://img.shields.io/badge/production-proven-orange?style=flat-square" alt="Production Proven">
</p>

<br>

# Agent Pipeline Engine

**多 Agent 协同的可信方向：SQLite 状态机 + 证据门禁 + 审计链 + 人类任意点介入。**

**Trustworthy multi-agent collaboration: agents never self-report "done" — every handoff is verified against evidence.**

<br>

## 为什么存在

让 AI Agent 干活的人都知道这个瞬间：它说"完成了，测试都过了"，你打开一看——没影的事。

单 Agent 场景这只是一次复测的浪费。多 Agent 管道里，假完成会复利：下游每一站都在虚构之上认真施工。我们运营一条五 Agent 生产管道（需求→拆解→编码→测试→部署），这条管道抓到过一次真事：部署 Agent 把自己的身份名片地址当"部署 URL"交上来，健康检查自报"通过"。

门禁把它当场打回了。这就是这个引擎的核心：**`Completed = f(State, Evidence, Policy)`，不等于 Agent 自报。**

## 架构

```
        Boss / Architect
        CLI: approve / cancel / rollback at ANY state
                      |
                      v
+--------------------------------------------------+
|  Dispatcher (FastAPI, single ECS)                |
|                                                  |
|  dispatch_loop (systemd timer, every 30s)        |
|      |                                           |
|  gate_judge — evidence gate, pure code, no LLM   |
|      |                                           |
|  pipeline.db (SQLite WAL, single writer)         |
|  docs/ (centralized artifact docs)               |
+--------------------------------------------------+
       ^         ^                         |
       | HTTP    | PATCH evidence          | alerts
       | PULL    | commit / report / SHA  v
+-------+--------+-------+-------+---------+     +--------+
| BA    | ORC    | DEV   | QA    | DEMO    | --> | Feishu |
+-------+--------+-------+-------+---------+     +--------+
   EARS -> split -> code -> test -> deploy
```

**Agent PULL, not PUSH.** 每个 Agent 每 3 分钟轮询 Dispatcher，认领任务，在自己的会话里执行，把证据 PATCH 回来。轮询看起来笨，但推送会丢、轮询不会——这是踩坑踩出来的设计。

## 证据门禁（五站）

| 站 | 放行条件 | 校验方式 |
|---|---|---|
| BA → ready | EARS 文档 + 人类签署 | 非空 + CLI 签署记录 |
| ORC → dev | 拆解文档 + DEV/QA 双确认 | 非空 + 双确认位 |
| DEV → qa | git commit + pytest 报告 + 制品 SHA256 | 三证缺一不可 |
| QA → demo | 测试报告 + 覆盖率 ≥60 + 阻塞问题 =0 | 阈值判定 |
| DEMO → done | 部署 URL | **无条件活体探针**（不信自报的 health 字段） |

## 超时降级链

```
retry (30s → 60s → 120s 指数退避)
  → skip（仅低优先级任务）
  → rollback（上一站，最多一次，防乒乓）
  → escalate_human（告警 + 冻结等人类）
```

告警冷却 15 分钟落库（跨进程有效），Agent 心跳 6 分钟判离线即告警。

## 快速上手

```bash
pip install fastapi uvicorn   # 仅有的两个依赖
python init_db.py             # 建库（WAL，幂等）
uvicorn main:app --port 8800  # 起服务
python main.py                # 手动跑一轮 dispatch_loop（生产用 systemd timer 30s）

# CLI
export CTGC_API_TOKEN=...
./ctgc create my-task my-project
./ctgc approve my-task
./ctgc agents                 # 五节点在线状态
```

## 设计判断（和 LangGraph 们的差异）

| | LangGraph / CrewAI | 本引擎 |
|---|---|---|
| 验证 | Agent 自报即过 | 证据门禁，独立校验 |
| 执行 | 单进程函数调用 | 跨 ECS 分布式（HTTP PULL） |
| 人类介入 | 图里预埋 interrupt | **任意状态** CLI 介入 |
| 审计 | 无 | 全操作 append-only 审计链 |

不是造轮子：是我们把对话式协同（A2A 同步请求-回复）那条路走穿过一次，确认它当不了任务编排主干道，收敛到这条路上来的。

## 实战连载

每一次故障、每一次假完成、每一条用事故换来的规矩，都写在《54集》：**https://jipin-ai.github.io/**

## License

MIT
