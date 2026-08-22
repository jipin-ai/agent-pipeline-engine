
# ADR-001: Agent Pipeline Engine 架构定位 — 确定性控制面 + 非确定性智能

> **状态**：草案（Draft）— 待决策者确定目标与方案
> **日期**：2026-08-15
> **决策者**：易宁
> **关联**：PROPOSAL-v5.md（五轮方案迭代，未提交）、A2A-拓扑与通信规范-v1.0

---

## 背景

Agent Pipeline Engine（agent-pipeline-engine）是 CTGC 多 Agent 协同管道的开源产物，演进经历了两个关键节点：

1. **A2A 同步通道的失败**：A2A 同步 request/response 模型与 LLM 非确定性处理模式不匹配，多节点管道无法可靠协同。结论：A2A 降级为通知通道，任务执行改为 Agent PULL + 确定性 Dispatcher。

2. **架构讨论收敛**（2026-08-15）：从"Multi-Agent 是否有前途"出发，四轮讨论收敛到核心框架——**确定性控制面（Deterministic Control Plane）+ 非确定性智能（Non-deterministic Intelligence）+ 可验证证据 / 持久记忆（Verifiable Evidence / Persistent Memory）**。

代码核实发现真实状态与预期不符：仓库停在 v0.1（A2A 同步轮询 + 单张 status 覆盖写确认表 + 关键词自报解析），Dispatcher HTTP API、追加式事件日志、证据门禁均未实现。学习环 spec 已成型，但其前提"事件日志"尚不存在。

## 核心判断（已收敛，作为本 ADR 的决策依据）

1. **Multi-Agent 的价值判据是独立信息增益，而非 Agent 数量**：第二个 Agent 的边际价值取决于条件信息量 `I(Y;X₂|X₁)`，不是 `I(Y;X₂)`。三个同源、同模型、同上下文的 Agent 不是三倍智能，是重复采样同一种错误。

2. **验证比"错误造成的损失"便宜**（而非比"生成"便宜）：验证很贵但错误更贵时（如金融风控），昂贵验证仍然值得。`Expected Value = Expected Error Reduction × Error Cost − Coordination Cost`。

3. **先问能不能确定性验证**：pytest / symbolic solver / rule engine 等确定性 verifier 优先；只有确定性验证不可行时，才考虑增加独立推理节点。

4. **独立性单位是 Context Boundary，不是 Agent 数量**：单 Agent 上限 = 单上下文上限。多 Agent 的价值在于上下文隔离（Context Reset），而非"多个模型一起想"。

5. **"独立"降级为"去相关"（Error Decorrelation）**：换模型只是弱代理，前沿模型在重叠数据上训练、收敛到相似错误。目标是"verifier 的错误不与 generator 完美相关"，可操作手段是 verifier 不继承 generator 的 epistemic trajectory（验证 artifact，不复述 reasoning），且 verifier 必须有独立证据通道。

6. **State 是中心相关错误源**：所有 Agent 路由到共享 State，State 里的错误会完美相关地污染所有读者。State 的可审计性（provenance）不是卫生问题，是中心点的去相关机制。

7. **Completed ≠ Agent 自报**：`Completed = f(State, Evidence, Policy)`，是状态机根据证据推导出的状态，不是 Agent 的输出。

8. **学习环三表 + 双轨**：episodes → failure_patterns → policy_proposals；统计轨（高频低损，要求统计显著）+ 灾难轨（低频高损，不要求统计显著但强制人类审批）。错误越严重，统计要求越低，但验证与人工审批要求越高。

9. **构建顺序：控制面 → 观察 → 学习**，不是反过来。

## 架构约束（已确定）

| 约束 | 说明 |
|------|------|
| A2A 只做通知通道 | 不传任务内容、不等回复，任务执行走 PULL |
| Agent PULL 而非 PUSH | Agent 醒来自查任务队列 |
| 机器时钟记账 | Agent 自报时间不可信，时间由系统时钟产生 |
| 追加式事件日志 | append-only，替换 status 覆盖写；状态转移历史不可丢失 |
| 证据门禁 | completed 由证据推导，不接受 Agent 自报 |
| 三权分离 | Execution deterministic / Proposal non-deterministic / Promotion deterministic |
| Agent 不可改 Policy | 任何 Agent 不得直接写入 active policy，Proposal 与 Policy 物理隔离 |
| 证据通道独立 | Generator 与 Verifier 必须拥有不同证据通道 |

## 目标

- **成功标准**：一个任务从创建到验收能可靠走完全流程，且每一步都有带机器时钟、带 provenance 的不可伪造事件记录。
- **做什么 / 给谁用**：待决策者确定。

## 方案（待决策者确定）

> 占位：构建方案（Phase 0-3 具体化、实施路径、是否走内部管道）。由决策者确定后填入。

## 后果（预期）

> 待目标与方案确定后补充。
