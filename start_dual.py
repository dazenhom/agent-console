#!/usr/bin/env python3
"""启动 Agent Console，同时监听两个端口：
  - 8800 HTTPS（自签证书）：语音输入等需要安全上下文的功能用这个
  - 80   HTTP：本地/日常快速访问，无证书警告（但 HTTP 下浏览器禁用麦克风）
两个进程共享同一个 SQLite 数据库，会话/历史互通。

用法：python3 start_dual.py
  （脚本 Popen 出两个 detached uvicorn 后立即退出，进程独立存活）
"""
import os
import secrets
import subprocess
from pathlib import Path

from server.secret_store import (
    atomic_write_private_at,
    load_or_create_local_notify_secret,
    locked_secret_dir,
    open_lock_file,
    open_secret_dir,
    read_private_value_at,
    validate_owned_mode,
)

ROOT = Path("/apdcephfs_gy2/share_302533218/zhihangxu/agent-console")
CERT_DIR = ROOT / "data" / "certs"
SECRET_DIR = ROOT / "data" / ".secrets"
SWANLAB_API_KEY_FILE = SECRET_DIR / "swanlab_api_key"
SWANLAB_SESSION_SECRET_FILE = SECRET_DIR / "swanlab_session_secret"
SWANLAB_SECRET_LOCK_FILE = SECRET_DIR / ".lock"
LOCAL_NOTIFY_SECRET_FILE = ROOT / "data" / "notify_secret"


_validate_owned_mode = validate_owned_mode
_open_secret_dir = open_secret_dir
_open_lock_file = open_lock_file
_read_private_value_at = read_private_value_at
_atomic_write_private_at = atomic_write_private_at
_locked_secret_dir = locked_secret_dir


def load_swanlab_api_key(
    environ: dict[str, str] | None = None,
    path: Path = SWANLAB_API_KEY_FILE,
) -> str:
    env = os.environ if environ is None else environ
    from_env = (env.get("SWANLAB_API_KEY") or "").strip()
    if from_env:
        return from_env
    with _locked_secret_dir(path.parent) as directory_fd:
        try:
            return _read_private_value_at(directory_fd, path.name, "SWANLAB_API_KEY")
        except RuntimeError as exc:
            if "无法安全打开" in str(exc):
                try:
                    os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    raise RuntimeError(
                        "SWANLAB_API_KEY 未配置：请设置环境变量或创建本机私有文件"
                    ) from exc
            raise


def load_or_create_swanlab_session_secret(
    environ: dict[str, str] | None = None,
    path: Path = SWANLAB_SESSION_SECRET_FILE,
) -> str:
    env = os.environ if environ is None else environ
    from_env = (env.get("SWANLAB_SESSION_SECRET") or "").strip()
    if from_env:
        if len(from_env) < 32:
            raise RuntimeError("SWANLAB_SESSION_SECRET 长度不足，至少需要 32 个字符")
        return from_env

    with _locked_secret_dir(path.parent) as directory_fd:
        try:
            value = _read_private_value_at(
                directory_fd, path.name, "SWANLAB_SESSION_SECRET"
            )
        except RuntimeError as exc:
            try:
                os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                value = secrets.token_urlsafe(48)
                _atomic_write_private_at(directory_fd, path.name, value)
            else:
                raise exc
        if not value:
            value = secrets.token_urlsafe(48)
            _atomic_write_private_at(directory_fd, path.name, value)
        if len(value) < 32:
            raise RuntimeError("SWANLAB_SESSION_SECRET 私有文件内容过短")
        return value


_NO_PROXY_DEFAULT = (
    "127.0.0.1,localhost,::1,"
    "29.191.211.213,29.228.42.53,"
    ".oa.com,.woa.com,.svc.cluster.local,.local"
)


def build_base_env(
    environ: dict[str, str] | None = None,
    api_key_path: Path = SWANLAB_API_KEY_FILE,
    session_secret_path: Path = SWANLAB_SESSION_SECRET_FILE,
    notify_secret_path: Path = LOCAL_NOTIFY_SECRET_FILE,
) -> dict[str, str]:
    source_env = os.environ if environ is None else environ
    # 剔除继承来的编排态环境变量（否则 tclaude 子进程会连父会话代理导致 403）
    base_env = {
        k: v
        for k, v in source_env.items()
        if k
        not in (
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_CUSTOM_HEADERS",
            "CLAUDE_CODE_CHILD_SESSION",
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CONFIG_DIR",
            "CLAUDECODE",
        )
    }
    base_env.update(
        {
            "AUTH_TOKEN": "123",
            "CLAUDE_BIN": "/root/.local/bin/tclaude",
            "ASR2_BASE_URLS": "http://29.228.42.53:8012",
            "ASR2_MODEL": "HYAudio",
            "CLAUDE_MODEL_FAST": "claude-haiku-4-5",
            "CLAUDE_MODEL_STRONG": "claude-sonnet-5",
            "CLAUDE_MODEL_SUPER": "claude-opus-5[1m]",
            "CLAUDE_DEFAULT_MODE": "claude-sonnet-5",
            "CLAUDE_EFFORT": "medium",
            "CLAUDE_PERSISTENT": source_env.get("CLAUDE_PERSISTENT", "true"),
            "CLAUDE_SESSION_IDLE_SEC": "3600",
            "AGENT_WORKDIR": "/apdcephfs_gy2/share_302533218/zhihangxu",
            "WECOM_ENABLED": "true",
            "WECOM_WEBHOOK_KEY": source_env.get(
                "WECOM_WEBHOOK_KEY", "be0c3312-4e95-4648-bdb2-2360ab6dbf97"
            ),
            "WECOM_PROXY": "http://star-proxy.oa.com:3128",
            # ---- 公司代理（2026-09-11 加）----
            # 本机直连外网不通；tcodex 子进程要访问 copilot.tencent.com /
            # tencent.sso.codebuddy.cn，不带代理会让本地网关全部 502
            # ("Failed to reach upstream gateway") 且 token 刷新失败。
            # 这里显式注入，保证不依赖启动时 shell 是否 source 过 .bashrc。
            # no_proxy 必须豁免本机与内网，否则 vllm(8012-8082)、
            # ASR2_BASE_URLS、本机 80/8800 自查都会被绕进代理。
            "http_proxy": source_env.get(
                "http_proxy", "http://star-proxy.oa.com:3128"
            ),
            "https_proxy": source_env.get(
                "https_proxy", "http://star-proxy.oa.com:3128"
            ),
            "HTTP_PROXY": source_env.get(
                "HTTP_PROXY", "http://star-proxy.oa.com:3128"
            ),
            "HTTPS_PROXY": source_env.get(
                "HTTPS_PROXY", "http://star-proxy.oa.com:3128"
            ),
            "no_proxy": source_env.get("no_proxy", _NO_PROXY_DEFAULT),
            "NO_PROXY": source_env.get("NO_PROXY", _NO_PROXY_DEFAULT),
            "NVM_BIN": "/root/.nvm/versions/node/v22.23.2/bin",
            "PATH": (
                "/root/.nvm/versions/node/v22.23.2/bin:/opt/venv/bin:"
                + source_env.get("PATH", "")
            ),
            # 两个 Uvicorn 子进程从同一份本地值注入，跨重启保持稳定。
            "SWANLAB_API_KEY": load_swanlab_api_key(source_env, api_key_path),
            "SWANLAB_SESSION_SECRET": load_or_create_swanlab_session_secret(
                source_env, session_secret_path
            ),
            "LOCAL_NOTIFY_SECRET": load_or_create_local_notify_secret(
                source_env, notify_secret_path
            ),
        }
    )
    return base_env


def launch(
    port: int,
    https: bool,
    logfile: str,
    base_env: dict[str, str],
    extra_env: dict[str, str] | None = None,
) -> None:
    cmd = [
        "uvicorn",
        "server.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
    ]
    cmd += [
        "--ws-ping-interval",
        "20",
        "--ws-ping-timeout",
        "60",
        "--timeout-keep-alive",
        "65",
    ]
    if https:
        cmd += [
            "--ssl-keyfile",
            str(CERT_DIR / "server.key"),
            "--ssl-certfile",
            str(CERT_DIR / "server.crt"),
        ]
    env = base_env if not extra_env else {**base_env, **extra_env}
    output = open(ROOT / logfile, "w")
    try:
        process = subprocess.Popen(
            cmd,
            env=env,
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        output.close()
    protocol = "https" if https else "http"
    print(f"  [{protocol}] port {port} -> PID {process.pid}", flush=True)


def main() -> None:
    base_env = build_base_env(notify_secret_path=LOCAL_NOTIFY_SECRET_FILE)
    if not (os.environ.get("SWANLAB_API_KEY") or "").strip():
        print(
            "警告：SwanLab API key 来自本机迁移文件；"
            "上游轮换仍是部署前阻断项，需要运维授权后执行。",
            flush=True,
        )
    print("启动 Agent Console 双端口：")
    launch(8800, True, "console.log", base_env)
    launch(
        80,
        False,
        "console_http.log",
        base_env,
        {"RECONCILE_ON_START": "1", "RUN_SCHEDULER": "1"},
    )
    print("完成。HTTPS: https://<IP>:8800  |  HTTP: http://<IP>:80")


if __name__ == "__main__":
    main()
