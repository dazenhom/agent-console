#!/usr/bin/env python3
"""启动 Agent Console，同时监听两个端口：
  - 8800 HTTPS（自签证书）：语音输入等需要安全上下文的功能用这个
  - 80   HTTP：本地/日常快速访问，无证书警告（但 HTTP 下浏览器禁用麦克风）
两个进程共享同一个 SQLite 数据库，会话/历史互通。

用法：python3 start_dual.py
  （脚本 Popen 出两个 detached uvicorn 后立即退出，进程独立存活）
"""
import os
import subprocess

ROOT = "/apdcephfs_gy2/share_302533218/zhihangxu/agent-console"
CERT_DIR = ROOT + "/data/certs"

# 剔除继承来的编排态环境变量（否则 tclaude 子进程会连父会话代理导致 403）
BASE_ENV = {k: v for k, v in os.environ.items()
            if k not in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
                         'ANTHROPIC_CUSTOM_HEADERS', 'CLAUDE_CODE_CHILD_SESSION',
                         'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CONFIG_DIR', 'CLAUDECODE')}
BASE_ENV.update({
    'AUTH_TOKEN': '123',
    'CLAUDE_BIN': '/root/.nvm/versions/node/v22.23.1/bin/tclaude',
    'ASR2_BASE_URLS': 'http://29.228.42.53:8012', 'ASR2_MODEL': 'HYAudio',
    'CLAUDE_MODEL_FAST': 'claude-haiku-4-5', 'CLAUDE_MODEL_STRONG': 'claude-sonnet-5',
    'CLAUDE_MODEL_SUPER': 'claude-opus-4-8[1m]', 'CLAUDE_DEFAULT_MODE': 'claude-sonnet-5',
    'CLAUDE_EFFORT': 'medium',
    'CLAUDE_PERSISTENT': 'true',  # 常驻进程模式（更接近交互式）
    'CLAUDE_SESSION_IDLE_SEC': '3600',
    'AGENT_WORKDIR': '/apdcephfs_gy2/share_302533218/zhihangxu',
    # 企业微信完成通知：内网直连出不去公网，必须经公司代理 star-proxy 才能到 qyapi。
    # WECOM_WEBHOOK_KEY 填群机器人 webhook url 里的 key 后，把 WECOM_ENABLED 设 true 即生效。
    'WECOM_ENABLED': 'true',
    'WECOM_WEBHOOK_KEY': os.environ.get('WECOM_WEBHOOK_KEY', 'be0c3312-4e95-4648-bdb2-2360ab6dbf97'),
    'WECOM_PROXY': 'http://star-proxy.oa.com:3128',
    'NVM_BIN': '/root/.nvm/versions/node/v22.23.1/bin',
    'PATH': '/root/.nvm/versions/node/v22.23.1/bin:/opt/venv/bin:' + os.environ.get('PATH', ''),
})


def launch(port, https, logfile, extra_env=None):
    cmd = ['/opt/venv/bin/uvicorn', 'server.main:app', '--host', '0.0.0.0', '--port', str(port)]
    # WS 保活：每 20s 发 ping、60s 无 pong 才判死。经 Tailscale Funnel + SSH 隧道多层代理时，
    # 主动 ping 让中间设备认为连接活跃，减少 WS 被空闲掐断（前端"连接断开"刷屏的主因）。
    cmd += ['--ws-ping-interval', '20', '--ws-ping-timeout', '60', '--timeout-keep-alive', '65']
    if https:
        cmd += ['--ssl-keyfile', CERT_DIR + '/server.key', '--ssl-certfile', CERT_DIR + '/server.crt']
    env = BASE_ENV if not extra_env else {**BASE_ENV, **extra_env}
    p = subprocess.Popen(
        cmd, env=env, cwd=ROOT,
        stdout=open(ROOT + '/' + logfile, 'w'), stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proto = 'https' if https else 'http'
    print(f'  [{proto}] port {port} -> PID {p.pid}', flush=True)


print('启动 Agent Console 双端口：')
launch(8800, True, 'console.log')       # HTTPS 语音
# 只让 80 端口进程在启动时对齐僵尸 running 状态，双进程不重复做。
launch(80, False, 'console_http.log', {'RECONCILE_ON_START': '1', 'RUN_SCHEDULER': '1'})   # HTTP 日常
print('完成。HTTPS: https://<IP>:8800  |  HTTP: http://<IP>:80')
