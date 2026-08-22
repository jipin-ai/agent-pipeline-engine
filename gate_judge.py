"""gate_judge.py — 证据门禁判定（v5 第四章）。不依赖 Agent 自报。"""
import urllib.request

PASS, BLOCKED, WAITING = "PASS", "BLOCKED", "WAITING"

# 人类状态：不由 dispatcher 推进，也豁免超时
HUMAN_STATES = {"waiting_human", "escalate_human"}
TERMINAL_STATES = {"done", "cancelled"}

STATUS_NEXT = {
    "received": "ears_draft",
    "ears_draft": "waiting_human",
    "ready": "dev_working",
    "dev_working": "qa_working",
    "qa_working": "demo_working",
    "demo_working": "done",
}

# 每个工作站的负责 Agent（A2A 唤醒用）
STATUS_AGENT = {
    "ears_draft": "BA-01",
    "ready": "ORC-01",
    "dev_working": "DEV-01",
    "qa_working": "QA-01",
    "demo_working": "DEMO-01",
}


def judge(task):
    """返回 (verdict, evidence)。task 为 sqlite3.Row。"""
    s = task["status"]

    if s in TERMINAL_STATES:
        return WAITING, "终态"
    if s in HUMAN_STATES:
        return WAITING, "等待人类"
    if s == "blocked":
        return WAITING, "外部依赖阻塞"

    if s == "received":
        return PASS, "自动进入 BA"

    if s == "ears_draft":
        if task["ears_doc_path"]:
            return PASS, "EARS 文档已提交"
        return BLOCKED, "无 EARS 文档"

    if s == "ready":
        if not task["orc_doc_path"]:
            return BLOCKED, "无拆解文档"
        if not (task["dev_confirmed"] and task["qa_confirmed"]):
            return BLOCKED, "五问双确认未齐"
        return PASS, "拆解 + 双确认完成"

    if s == "dev_working":
        if not task["git_commit"]:
            return BLOCKED, "无 git commit"
        if not task["pytest_report_path"]:
            return BLOCKED, "无 pytest 报告"
        if not task["artifact_sha256"]:
            return BLOCKED, "无制品 SHA256"
        return PASS, "DEV 证据齐"

    if s == "qa_working":
        if not task["test_report_path"]:
            return BLOCKED, "无测试报告"
        if task["coverage_pct"] is None or task["coverage_pct"] < 60:
            return BLOCKED, "覆盖率不足 60%"
        if task["blocking_issues"]:
            return BLOCKED, "有 blocking issue"
        return PASS, "QA 证据齐"

    if s == "demo_working":
        if not task["deploy_url"]:
            return BLOCKED, "无部署地址"
        # 无条件活检：自报 health_check_ok 不作为依据（假完成实证：DEMO 自报 1 + 假 URL 混过了门禁）
        if not _live_health(task["deploy_url"]):
            return BLOCKED, "健康检查活检未过"
        return PASS, "DEMO 证据齐（活检通过）"

    return BLOCKED, f"未知状态 {s}"


def _live_health(deploy_url):
    try:
        url = deploy_url.rstrip("/") + "/health"
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False
