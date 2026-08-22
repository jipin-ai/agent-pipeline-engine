#!/usr/bin/env bash
# agent_poll.sh — Agent 节点侧：心跳 + 任务轮询（cron 每 3min）
# 用法: AGENT_NAME=DEV-01 ./agent_poll.sh   （TOKEN 从 ~/.hermes/.env 读）
set -u
AGENT_NAME="${AGENT_NAME:?need AGENT_NAME}"
API="http://123.56.25.232:8800"
TOKEN=$(grep '^A2A_BEARER_TOKEN=' "$HOME/.hermes/.env" | head -1 | cut -d= -f2)

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
