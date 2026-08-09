#!/usr/bin/env python3
"""
Agent Pipeline Engine — A2A 同步轮询
每 N 分钟轮询 Agent 状态，更新 SQLite 状态机
"""
import sqlite3
import subprocess
import json
import time
import os
import sys
from pathlib import Path
from datetime import datetime

DB_PATH = os.environ.get("PIPELINE_DB", str(Path.home() / ".pipeline" / "pipeline.db"))

# 轮询目标配置
# 可通过环境变量 POLL_TARGETS 覆盖，格式：agent_name:field_name,agent_name:field_name
DEFAULT_POLL_TARGETS = [
    ("CTGC-DEV-01", "dev_confirmed", "dev_notes"),
    ("CTGC-QA-01", "qa_confirmed", "qa_notes"),
]

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))  # 秒
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "120"))     # 秒


def a2a_call(agent: str, prompt: str, timeout: int = POLL_TIMEOUT) -> str | None:
    """调用 hermes a2a_call，返回响应文本或超时返回 None"""
    try:
        result = subprocess.run(
            ["hermes", "chat", "-q", f"a2a_call {agent} \"{prompt}\"", "--yolo"],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        print("hermes CLI not found. Is Hermes Agent installed?", file=sys.stderr)
        return None


def parse_response(response: str) -> tuple[bool, str]:
    """从 Agent 响应中提取确认状态。
    返回 (confirmed: bool, notes: str)
    """
    if not response:
        return False, "no response (timeout)"

    response_lower = response.lower()

    # 积极信号
    positive_signals = ["确认", "通过", "同意", "confirmed", "approved", "yes", "pass", "ok"]
    # 消极信号
    negative_signals = ["拒绝", "驳回", "不同意", "rejected", "denied", "no", "需要修改"]

    has_positive = any(s in response_lower for s in positive_signals)
    has_negative = any(s in response_lower for s in negative_signals)

    if has_positive and not has_negative:
        return True, response[:500]
    elif has_negative:
        return False, response[:500]

    return False, f"unclear: {response[:200]}"


def poll_task(task_id: str, conn: sqlite3.Connection) -> dict:
    """轮询单个任务，更新数据库"""
    conn.row_factory = sqlite3.Row

    # 只轮询 ears_draft 状态（等待五问确认）
    row = conn.execute(
        "SELECT * FROM pipeline_status WHERE task_id = ? AND status = 'ears_draft'",
        (task_id,)
    ).fetchone()

    if not row:
        return {"task_id": task_id, "skipped": True, "reason": "no ears_draft tasks"}

    row = dict(row)
    results = {}

    for agent, field, notes_field in DEFAULT_POLL_TARGETS:
        if row[field] == 1:
            results[agent] = "already confirmed"
            continue

        prompt = f"任务 {task_id} 的五问审查状态？请简要回复：确认/需要修改/具体意见。"
        response = a2a_call(agent, prompt)

        confirmed, notes = parse_response(response)
        if confirmed:
            conn.execute(
                f"UPDATE pipeline_status SET {field} = 1, {notes_field} = ?, updated_at = datetime('now') WHERE task_id = ?",
                (notes, task_id)
            )
            results[agent] = "confirmed"
        else:
            results[agent] = f"pending: {notes[:100]}"

    conn.commit()
    return {"task_id": task_id, "polled": True, "results": results}


def poll_all_pending(conn: sqlite3.Connection) -> list:
    """轮询所有 pending 状态的任务"""
    rows = conn.execute(
        "SELECT task_id FROM pipeline_status WHERE status = 'ears_draft'"
    ).fetchall()

    results = []
    for (task_id,) in rows:
        result = poll_task(task_id, conn)
        results.append(result)

    return results


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    task_id = sys.argv[1] if len(sys.argv) > 1 else None

    if task_id:
        result = poll_task(task_id, conn)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        results = poll_all_pending(conn)
        ts = datetime.now().strftime("%H:%M:%S")
        if not results:
            print(f"[{ts}] (no ears_draft tasks to poll)")
        else:
            print(f"[{ts}] polled {len(results)} tasks:")
            for r in results:
                status = "✅" if all(v == "confirmed" or v == "already confirmed" for v in r.get("results", {}).values()) else "⏳"
                print(f"  {status} {r['task_id']} {r.get('results', {})}")

    conn.close()
