#!/bin/bash
# ============================================================
#  Agent Console 公网隧道一键启动（Mac 上运行）
#  架构: 手机(任意网络) → Tailscale Funnel(公网HTTPS,真证书)
#         → Mac:8888 → SSH隧道 → 内网服务器:80
#  用法: ~/agent-tunnel.sh   (Ctrl+C 一键全停)
# ============================================================

# ---------- 换机器只改这 4 个 ----------
SSH_HOST="root@29.191.211.218"
SSH_PORT="36000"
SSH_PASS="quBalkL4ig0DsuG,"
LOCAL_PORT="8888"          # Mac 本地端口(一般不用改)
REMOTE_PORT="80"           # 服务器上 Agent Console 端口(一般不用改)
# ---------------------------------------

PUBLIC_URL="https://zhihangxu.taila0ce66.ts.net/"

# 兼容 GUI 版和 brew CLI 版的 tailscale 命令
TS_BIN="tailscale"
[ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ] && \
  TS_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"

# ---------- 1/3 Tailscale daemon ----------
echo "▶ 1/3 检查 Tailscale…"
if ! $TS_BIN status &>/dev/null; then
  echo "   启动 tailscaled (需要 sudo)…"
  sudo tailscaled >/tmp/tailscaled.log 2>&1 &
  sleep 3
  sudo $TS_BIN up
fi
echo "   ✓ Tailscale 就绪"

# ---------- 2/3 SSH 隧道(自动重连) ----------
echo "▶ 2/3 建立 SSH 隧道 (Mac:$LOCAL_PORT → 服务器:$REMOTE_PORT)…"
( while true; do
    # 关键修复:每次(重)连前先清掉占用本地端口的旧隧道,否则
    # ExitOnForwardFailure=yes 会因 8888 还没释放而立刻退出 → 疯狂重连刷屏
    lsof -ti tcp:$LOCAL_PORT | xargs kill -9 2>/dev/null
    echo "[ssh] 连接中…"
    sshpass -p "$SSH_PASS" ssh -g -p "$SSH_PORT" -N \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      -o StrictHostKeyChecking=no -o ExitOnForwardFailure=yes \
      -L "$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" "$SSH_HOST"
    echo "[ssh] 断开，3 秒后重连…"
    sleep 3
  done ) >/tmp/agent-ssh.log 2>&1 &
SSH_LOOP_PID=$!
sleep 2
echo "   ✓ SSH 隧道已起 (日志: tail -f /tmp/agent-ssh.log)"

# ---------- 3/3 Funnel 公网出口 ----------
echo "▶ 3/3 开启 Tailscale Funnel → 公网 HTTPS"
# funnel 域名【必须】等于本机在 Tailscale 里的真实设备名(DNSName),不能自定义。
# 设备一旦改名(如 zhihangs-macbook-pro → mac),旧域名的 funnel 配置会残留且失效
# (证书签不出来 → 公网 TLS 握手被掐 → 000)。所以这里先用 `serve reset` 彻底清掉
# 所有残留配置,再用证书认可的正确域名重新开,保证幂等、不会叠加坏配置。
$TS_BIN serve reset >/dev/null 2>&1 || true
if $TS_BIN funnel --bg --https=443 "$LOCAL_PORT" 2>/tmp/ts-funnel.log; then
  echo "   ✓ Funnel 已开启"
else
  echo "   ✗ Funnel 启动失败,详情看 /tmp/ts-funnel.log:"
  cat /tmp/ts-funnel.log
fi

echo ""
echo "   🌐  手机访问(任意网络): $PUBLIC_URL"
echo "   自检中…"
sleep 2
# 自检①本地隧道(②③④段),②公网 funnel(①段)。两个都 200 才算全通。
local_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:$LOCAL_PORT/" 2>/dev/null)
echo "   本地隧道 http://127.0.0.1:$LOCAL_PORT/ → HTTP $local_code  $([ "$local_code" = "200" ] && echo '✓ 通' || echo '✗ 不通,看 /tmp/agent-ssh.log')"
pub_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$PUBLIC_URL" 2>/dev/null)
echo "   公网 Funnel $PUBLIC_URL → HTTP $pub_code  $([ "$pub_code" = "200" ] && echo '✓ 通(手机可访问)' || echo '✗ 不通,看 /tmp/ts-funnel.log')"
echo ""
echo "   Ctrl+C 退出 (会一并关闭 SSH 隧道与 Funnel)"
echo ""

# Ctrl+C 时连带关掉 SSH 保活循环、隧道、以及 funnel 公网出口
cleanup() {
  echo; echo "正在关闭…"
  kill "$SSH_LOOP_PID" 2>/dev/null
  lsof -ti tcp:$LOCAL_PORT | xargs kill -9 2>/dev/null
  # serve reset 一次清掉所有 serve/funnel 配置,比挨个 off 可靠(off 在域名残留时会失败)
  $TS_BIN serve reset >/dev/null 2>&1 || true
  echo "已停止 SSH 隧道与 Funnel。"
  exit 0
}
trap cleanup INT TERM

# 保持前台,让 trap 生效(原脚本结尾会直接退出,trap 没机会触发)
echo "(隧道与 Funnel 运行中,此窗口请保持打开)"
wait "$SSH_LOOP_PID"
