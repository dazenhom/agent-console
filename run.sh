#!/usr/bin/env bash
# Agent Console 启动脚本（tclaude 后端，stream-json 非交互管道）
set -euo pipefail
cd "$(dirname "$0")"

# ====== 必改配置 ======
# 手机登录口令（前端登录时用）。务必改掉默认值。
export AUTH_TOKEN="${AUTH_TOKEN:-123}"

# ====== tclaude（腾讯内部 claude-code wrapper）======
# CLI 路径。tclaude 装在 nvm 下，subprocess 需要同目录的 node，所以要把它所在的
# bin 目录注入 PATH。这里**自动探测**真实路径，避免写死某个 node 版本（曾因写死
# v20.20.2 而那下面没有 tclaude，导致“找不到 Claude CLI”）。
_detect_tclaude() {
  # 1) 显式指定优先
  if [ -n "${CLAUDE_BIN:-}" ] && [ -x "${CLAUDE_BIN:-}" ]; then echo "$CLAUDE_BIN"; return; fi
  # 2) 候选位置 + PATH 中已有的
  local c
  for c in \
      /root/.nvm/versions/node/v22.23.1/bin/tclaude \
      /root/nodejs/bin/tclaude \
      "$(command -v tclaude 2>/dev/null || true)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then echo "$c"; return; fi
  done
  return 1
}
_TCLAUDE="$(_detect_tclaude || true)"
if [ -z "$_TCLAUDE" ]; then
  echo "  [致命] 找不到可执行的 tclaude，请先安装或用 CLAUDE_BIN 指定路径" >&2
  exit 1
fi
export CLAUDE_BIN="$_TCLAUDE"
# tclaude 的 bin 目录（含配套 node）必须在 PATH 前部，否则子进程找不到 node
export NVM_BIN="${NVM_BIN:-$(dirname "$CLAUDE_BIN")}"
export PATH="$NVM_BIN:$PATH"
# tclaude 在 root 下会拒绝 --dangerously-skip-permissions；默认权限模式 root 已能跑工具，保持 false
export CLAUDE_SKIP_PERMISSIONS="${CLAUDE_SKIP_PERMISSIONS:-false}"
# 单次回合超时（老模式 run_turn 硬超时用；常驻模式已改看门狗，见下方）
export CLAUDE_TURN_TIMEOUT="${CLAUDE_TURN_TIMEOUT:-1800}"
# 看门狗：空闲多久判卡死、回合绝对安全上限（常驻模式 send_turn 用）
export CLAUDE_IDLE_TIMEOUT="${CLAUDE_IDLE_TIMEOUT:-600}"
export CLAUDE_TURN_MAX="${CLAUDE_TURN_MAX:-7200}"
# 循环检测：重复操作阈值、连续报错阈值、看门狗轮询间隔
export CLAUDE_LOOP_REPEAT="${CLAUDE_LOOP_REPEAT:-8}"
export CLAUDE_LOOP_ERRORS="${CLAUDE_LOOP_ERRORS:-10}"
export CLAUDE_WATCHDOG_INTERVAL="${CLAUDE_WATCHDOG_INTERVAL:-15}"

# ====== ASR 语音识别（HY ContextASR）======
# 录音 → /api/asr → 转 16k WAV → POST {base}/v1/chat/completions。多端点逗号分隔。
export ASR2_BASE_URLS="${ASR2_BASE_URLS:-http://29.228.42.53:8012}"
export ASR2_MODEL="${ASR2_MODEL:-HYAudio}"
export ASR2_API_KEY="${ASR2_API_KEY:-}"
export ASR2_TIMEOUT="${ASR2_TIMEOUT:-30}"

# 模型档位（tclaude --model 现已生效；合法值见 config.py 注释）
export CLAUDE_MODEL_FAST="${CLAUDE_MODEL_FAST:-claude-haiku-4-5}"
export CLAUDE_MODEL_STRONG="${CLAUDE_MODEL_STRONG:-claude-sonnet-4-6}"
export CLAUDE_MODEL_SUPER="${CLAUDE_MODEL_SUPER:-claude-opus-4-8[1m]}"
export CLAUDE_DEFAULT_MODE="${CLAUDE_DEFAULT_MODE:-claude-deepseek-v4.1-flash[1m]}"
# 兼容旧档位：若仍设为旧值则覆盖为新默认
case "$CLAUDE_DEFAULT_MODE" in fast|strong|super) export CLAUDE_DEFAULT_MODE="claude-deepseek-v4.1-flash[1m]" ;; esac

# Agent 默认工作目录
export AGENT_WORKDIR="${AGENT_WORKDIR:-$(cd .. && pwd)}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-80}"

# ====== HTTPS（语音输入等需要安全上下文的浏览器 API 必需）======
# ENABLE_HTTPS=true 时自动生成自签证书（首次/缺失才生成），把所有本机 IP 写进 SAN，
# 手机无论连哪个内网 IP 都能用。自签证书首次访问需在浏览器点"仍然访问/继续前往"。
export ENABLE_HTTPS="${ENABLE_HTTPS:-true}"
_SSL_ARGS=()
if [ "$ENABLE_HTTPS" = "true" ]; then
  CERT_DIR="${CERT_DIR:-$(pwd)/data/certs}"
  mkdir -p "$CERT_DIR"
  CERT="$CERT_DIR/server.crt"; KEY="$CERT_DIR/server.key"
  if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    echo "  [HTTPS] 生成自签证书 → $CERT_DIR"
    # 收集所有本机 IPv4，拼成 SAN 列表（含 localhost / 127.0.0.1）
    # 两条途径都试（ip / hostname -I），不同环境可用性不一，合并去重
    _ips=$( { ip -4 addr show 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}'; hostname -I 2>/dev/null | tr ' ' '\n'; } | grep -E '^[0-9.]+$' | sort -u)
    _san="DNS:localhost,IP:127.0.0.1"
    for ip in $_ips; do [ "$ip" != "127.0.0.1" ] && _san="$_san,IP:$ip"; done
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout "$KEY" -out "$CERT" \
      -subj "/CN=agent-console" \
      -addext "subjectAltName=$_san" >/dev/null 2>&1 \
      && echo "  [HTTPS] SAN = $_san" \
      || echo "  [HTTPS][警告] 证书生成失败，将回退 HTTP" >&2
  fi
  if [ -f "$CERT" ] && [ -f "$KEY" ]; then
    _SSL_ARGS=(--ssl-keyfile "$KEY" --ssl-certfile "$CERT")
    _SCHEME="https"
  fi
fi
_SCHEME="${_SCHEME:-http}"

echo "Agent Console 启动中（tclaude stream-json 后端）..."
echo "  CLAUDE_BIN     = $CLAUDE_BIN"
echo "  AGENT_WORKDIR  = $AGENT_WORKDIR"
echo "  PATH (front)   = $NVM_BIN"
echo "  访问地址        = ${_SCHEME}://<本机IP>:$PORT"

if [ ! -x "$CLAUDE_BIN" ]; then
  echo "  [警告] CLAUDE_BIN 不存在或不可执行：$CLAUDE_BIN" >&2
fi

# 单进程模式默认启用调度器（多进程部署时由外部显式关掉）；用户可 RUN_SCHEDULER=0 禁用
export RUN_SCHEDULER="${RUN_SCHEDULER:-1}"
# 单进程模式默认在启动时对账目标调度（重启后把卡在 verifying 态的 goal 推出死锁）
export RECONCILE_ON_START="${RECONCILE_ON_START:-1}"

exec uvicorn server.main:app --host "$HOST" --port "$PORT" "${_SSL_ARGS[@]}"
