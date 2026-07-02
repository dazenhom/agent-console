#!/usr/bin/env bash
# ============================================================
#  Agent Console 一键重启（双端口：80 HTTP + 8800 HTTPS）
#  用法：bash restart.sh          正常重启（有 running 会话会提示确认）
#        bash restart.sh -f       强制重启（跳过 running 检查）
#  说明：走 start_dual.py，不要用 run.sh（run.sh 是单端口会打乱端口布局）。
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

TOKEN="${AUTH_TOKEN:-123}"
FORCE=0
[ "${1:-}" = "-f" ] && FORCE=1

echo "▶ 1/4 检查是否有正在运行的会话…"
# 用本机 80 端口查（起不来/查不到就当作 0，不阻塞重启）
RUNNING=$(curl -s --max-time 5 "http://127.0.0.1:80/api/sessions" -H "Authorization: Bearer $TOKEN" 2>/dev/null \
  | python3 -c "import sys,json; print(len([s for s in json.load(sys.stdin) if s.get('status')=='running']))" 2>/dev/null || echo "0")
echo "   running 会话数：$RUNNING"
if [ "$RUNNING" != "0" ] && [ "$FORCE" != "1" ]; then
  echo "   ⚠️  有 $RUNNING 个会话正在运行，重启会中断它们。"
  read -r -p "   确认重启？(y/N) " ans
  case "$ans" in
    y|Y) ;;
    *) echo "   已取消。"; exit 0 ;;
  esac
fi

echo "▶ 2/4 校验代码语法…"
if command -v node >/dev/null 2>&1; then
  node -c web/app.js && echo "   ✓ app.js 语法 OK" || { echo "   ✗ app.js 语法错误，已中止重启"; exit 1; }
fi
/opt/venv/bin/python3 -c "import ast; ast.parse(open('server/main.py').read())" \
  && echo "   ✓ main.py 语法 OK" || { echo "   ✗ main.py 语法错误，已中止重启"; exit 1; }

echo "▶ 3/4 停止旧进程并拉起新进程…"
pkill -f "uvicorn server.main:app" 2>/dev/null
sleep 2
LEFT=$(ps aux | grep "uvicorn server.main" | grep -v grep | wc -l | tr -d ' ')
echo "   旧进程剩余：$LEFT"
/opt/venv/bin/python3 start_dual.py || { echo "   ✗ start_dual.py 启动失败，已中止"; exit 1; }

echo "▶ 4/4 验证双端口…"
sleep 5
# 用根路径 / 验证（无需鉴权），更可靠地反映"服务是否起来"，不受 AUTH_TOKEN 影响
H80=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "http://127.0.0.1:80/" 2>/dev/null)
H8800=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 8 "https://127.0.0.1:8800/" 2>/dev/null)
echo "   80  (HTTP)  → $H80  $([ "$H80" = "200" ] && echo '✓' || echo '✗')"
echo "   8800(HTTPS) → $H8800  $([ "$H8800" = "200" ] && echo '✓' || echo '✗')"
if [ "$H80" = "200" ] && [ "$H8800" = "200" ]; then
  echo "✅ 重启完成，双端口正常。刷新页面即可。"
  # Ensure watchdog is running after restart
  WATCHDOG_PID_FILE="/tmp/agent-console-watchdog.pid"
  if [ -f "$WATCHDOG_PID_FILE" ] && kill -0 "$(cat "$WATCHDOG_PID_FILE")" 2>/dev/null; then
      echo "[restart] watchdog already running (pid $(cat $WATCHDOG_PID_FILE))"
  else
      echo "[restart] starting watchdog..."
      nohup bash "$ROOT/watchdog.sh" >/dev/null 2>&1 &
  fi
else
  echo "⚠️  有端口未就绪，看日志：tail -20 console.log / console_http.log"
  exit 1
fi
