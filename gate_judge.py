#!/usr/bin/env python3
"""
Agent Pipeline Engine — 门禁判定
读 SQLite 状态机，三分支输出：PASS / BLOCKED / NOT_FOUND
"""
import sqlite3
import sys
import os
from pathlib import Path

DB_PATH = os.environ.get("PIPELINE_DB", str(Path.home() / ".pipeline" / "pipeline.db"))

# 状态流转规则
STATUS_GATES = {
    "received": {
        "next": "ears_draft",
        "description": "等待 ORC 拆解"
    },
    "ears_draft": {
        "condition": "dev_confirmed == 1 AND qa_confirmed == 1",
        "on_pass": "ready",
        "on_blocked": "hold",
        "description": "等待 DEV+QA 五问确认"
    },
    "ready": {
        "next": "dev_done",
        "description": "门禁绿灯，派发 DEV"
    },
    "dev_done": {
        "next": "qa_passed",
        "description": "DEV 完成，派发 QA"
    },
    "qa_passed": {
        "next": "demo_deployed",
        "description": "QA 通过，派发 DEMO"
    },
    "demo_deployed": {
        "next": None,
        "description": "等待甲方验收"
    }
}


class GateJudge:
    def __init__(self, db_path: str = DB_PATH):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def judge(self, task_id: str) -> dict:
        """判定管道状态。返回 {verdict, current_status, details}"""
        row = self.conn.execute(
            "SELECT * FROM pipeline_status WHERE task_id = ?", (task_id,)
        ).fetchone()

        if not row:
            return {
                "verdict": "NOT_FOUND",
                "task_id": task_id,
                "details": "任务不存在"
            }

        row = dict(row)
        current = row["status"]

        if current not in STATUS_GATES:
            return {
                "verdict": "PASS",
                "task_id": task_id,
                "current_status": current,
                "details": f"未知状态 '{current}'，手动判定"
            }

        gate = STATUS_GATES[current]

        if "condition" in gate:
            try:
                passed = eval(
                    gate["condition"],
                    {},
                    {
                        "dev_confirmed": row["dev_confirmed"],
                        "qa_confirmed": row["qa_confirmed"]
                    }
                )
            except Exception as e:
                return {
                    "verdict": "ERROR",
                    "task_id": task_id,
                    "current_status": current,
                    "details": f"条件求值失败: {e}"
                }

            if passed:
                return {
                    "verdict": "PASS",
                    "task_id": task_id,
                    "current_status": current,
                    "next_status": gate["on_pass"],
                    "details": f"{gate['description']} → {gate['on_pass']}"
                }
            else:
                return {
                    "verdict": "BLOCKED",
                    "task_id": task_id,
                    "current_status": current,
                    "details": f"{gate['description']} — 门禁未通过",
                    "status_snapshot": {
                        "dev_confirmed": row["dev_confirmed"],
                        "qa_confirmed": row["qa_confirmed"]
                    }
                }

        return {
            "verdict": "PASS",
            "task_id": task_id,
            "current_status": current,
            "next_status": gate.get("next"),
            "details": gate["description"]
        }

    def all_tasks(self) -> list:
        """列出所有任务状态"""
        rows = self.conn.execute(
            "SELECT task_id, project, status, dev_confirmed, qa_confirmed, updated_at FROM pipeline_status ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    judge = GateJudge()

    if len(sys.argv) > 1:
        task_id = sys.argv[1]
        result = judge.judge(task_id)
    else:
        tasks = judge.all_tasks()
        if not tasks:
            print("(no tasks)")
            sys.exit(0)
        for t in tasks:
            result = judge.judge(t["task_id"])
            icon = {"PASS": "✅", "BLOCKED": "🔴", "NOT_FOUND": "❓", "ERROR": "💥"}.get(result["verdict"], "⚪")
            print(f"{icon} {result['task_id']} [{result['current_status']}] {result['details']}")
        sys.exit(0)

    icon = {"PASS": "✅", "BLOCKED": "🔴", "NOT_FOUND": "❓", "ERROR": "💥"}.get(result["verdict"], "⚪")
    print(f"{icon} {result['verdict']}: {result['details']}")
    if "status_snapshot" in result:
        print(f"   dev_confirmed={result['status_snapshot']['dev_confirmed']} qa_confirmed={result['status_snapshot']['qa_confirmed']}")
