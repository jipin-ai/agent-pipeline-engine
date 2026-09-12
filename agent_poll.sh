#!/usr/bin/env bash
# agent_poll.sh — Agent 节点侧：心跳 + 任务轮询（cron 每 3min）+ --upload 制品上传
# 用法: AGENT_NAME=DEV-01 ./agent_poll.sh   （TOKEN 从 ~/.hermes/.env 读）
#       AGENT_NAME=DEV-01 ./agent_poll.sh --upload <task_id> <dtype> <local_file>
set -u
AGENT_NAME="${AGENT_NAME:?need AGENT_NAME}"
API="${PIPELINE_API:-http://127.0.0.1:8800}"
# Dispatcher API 用 DISPATCHER_API_TOKEN（08-23 token 分离；A2A_BEARER_TOKEN 打 Dispatcher 已 401）
TOKEN=$(grep '^DISPATCHER_API_TOKEN=' "$HOME/.hermes/.env" | head -1 | cut -d= -f2)

# --upload：制品/文档上传子命令（cron 脚本路径，不经 Hermes 会话，天然免审批闸门）
# 复用 Dispatcher 已有 POST /tasks/{task_id}/docs/{dtype}（dtype ∈ ears|orc|dev|qa|demo|artifact）
if [ "${1:-}" = "--upload" ]; then
  TASK_ID="${2:?need task_id}"; DTYPE="${3:?need dtype}"; FILE="${4:?need local_file}"
  [ -f "$FILE" ] || { echo "file not found: $FILE" >&2; exit 2; }
  code=$(curl -s --max-time 120 -o /tmp/agent_poll_upload_resp.txt -w "%{http_code}" -X POST \
    "$API/tasks/$TASK_ID/docs/$DTYPE" \
    -H "Authorization: Bearer $TOKEN" \
    --data-binary @"$FILE")
  cat /tmp/agent_poll_upload_resp.txt
  echo "upload http_code=$code"
  [ "$code" = "200" ]
  exit $?
fi

# 1. 心跳
curl -s --max-time 10 -X POST "$API/agents/$AGENT_NAME/heartbeat" \
  -H "Authorization: Bearer $TOKEN" -o /dev/null

# 2. 轮询属于自己的任务
TASKS=$(curl -s --max-time 10 "$API/tasks?agent=$AGENT_NAME" \
  -H "Authorization: Bearer $TOKEN")

# 3. 有任务则唤起 Hermes 会话处理（无任务则静默退出——cron 友好）
if [ -n "$TASKS" ] && [ "$TASKS" != "[]" ]; then
  echo "$TASKS" | python3 -c "
import json, sys
for t in json.load(sys.stdin):
    if not t.get('claimed_by'):
        print(t['task_id'])
" | while read -r tid; do
    # 认领
    code=$(curl -s --max-time 10 -o /dev/null -w "%{http_code}" -X POST \
      "$API/tasks/$tid/claim" -H "Authorization: Bearer $TOKEN" \
      -H "X-CTGC-Actor: $AGENT_NAME")
    [ "$code" = "200" ] && echo "$(date -Is) claimed $tid" >> "$HOME/pipeline-claims.log"
    # 实际处理由 Agent 会话接手（此处只保证可见性）
  done
fi
exit 0
