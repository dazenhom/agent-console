#!/usr/bin/env bash
# ============================================================
#  Agent Console 看门狗（watchdog）
#  周期探测双端口（80 HTTP + 8800 HTTPS），连续失败则调 restart.sh -f 拉起。
#  说明：自己不 pkill，全部通过 restart.sh 完成重启；单实例运行。
#  用法：nohup bash watchdog.sh >/dev/null 2>&1 &
# ============================================================
# 注意：不加 set -euo pipefail —— 主循环里单条 curl/命令失败不能让脚本退出。

# ---- 可配置变量 ----
INTERVAL=30          # 每轮探测间隔（秒）
FAIL_THRESHOLD=2     # 连续失败多少次才触发重启
COOLDOWN=180         # 两次重启之间的最小冷却（秒）

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$ROOT/watchdog.log"
PID_FILE="/tmp/agent-console-watchdog.pid"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# ---- 单实例锁 ----
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
  echo "watchdog already running (pid $(cat "$PID_FILE"))"
  exit 0
fi
echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

log "watchdog started (interval=${INTERVAL}s, threshold=${FAIL_THRESHOLD}, cooldown=${COOLDOWN}s)"

fail_count=0
last_restart=0
ok_streak=0

while true; do
  H80=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "http://127.0.0.1:80/" 2>/dev/null)
  H8800=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 8 "https://127.0.0.1:8800/" 2>/dev/null)

  if [ "$H80" = "200" ] && [ "$H8800" = "200" ]; then
    fail_count=0
    ok_streak=$((ok_streak + 1))
    # 心跳：每 10 轮 ok 才输出一次，避免刷屏
    if [ $((ok_streak % 10)) -eq 1 ]; then
      log "ok (80=$H80 8800=$H8800)"
    fi
  else
    ok_streak=0
    fail_count=$((fail_count + 1))
    [ "$H80" != "200" ] && log "warn: port 80 down (code=$H80), fail_count=$fail_count"
    [ "$H8800" != "200" ] && log "warn: port 8800 down (code=$H8800), fail_count=$fail_count"

    if [ "$fail_count" -ge "$FAIL_THRESHOLD" ]; then
      now=$(date +%s)
      elapsed=$((now - last_restart))
      if [ "$elapsed" -ge "$COOLDOWN" ]; then
        log "detected down, triggering restart"
        bash "$ROOT/restart.sh" -f >> "$LOG_FILE" 2>&1
        last_restart=$(date +%s)
        fail_count=0
      else
        log "in cooldown ($((COOLDOWN - elapsed))s left), skip restart"
      fi
    fi
  fi

  sleep "$INTERVAL"
done
