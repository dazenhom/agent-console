#!/usr/bin/env python3
"""启动 Agent Console，同时监听两个端口：
  - 8800 HTTPS（自签证书）：语音输入等需要安全上下文的功能用这个
  - 80   HTTP：本地/日常快速访问，无证书警告（但 HTTP 下浏览器禁用麦克风）
两个进程共享同一个 SQLite 数据库，会话/历史互通。

用法：python3 start_dual.py
  （脚本 Popen 出两个 detached uvicorn 后立即退出，进程独立存活）
"""
import fcntl
import os
import secrets
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path


ROOT = Path("/apdcephfs_gy2/share_302533218/zhihangxu/agent-console")
CERT_DIR = ROOT / "data" / "certs"
SECRET_DIR = ROOT / "data" / ".secrets"
SWANLAB_API_KEY_FILE = SECRET_DIR / "swanlab_api_key"
SWANLAB_SESSION_SECRET_FILE = SECRET_DIR / "swanlab_session_secret"
SWANLAB_SECRET_LOCK_FILE = SECRET_DIR / ".lock"


def _validate_owned_mode(
    metadata: os.stat_result,
    label: str,
    expected_type: str,
    expected_mode: int,
) -> None:
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(f"{label} owner 必须是当前运行用户")
    if expected_type == "directory":
        valid_type = stat.S_ISDIR(metadata.st_mode)
    else:
        valid_type = stat.S_ISREG(metadata.st_mode)
    if not valid_type:
        raise RuntimeError(f"{label} 必须是普通{expected_type}")
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if actual_mode != expected_mode:
        raise RuntimeError(f"{label} 权限必须精确为 {expected_mode:04o}")


def _open_secret_dir(secret_dir: Path) -> int:
    """安全打开 secret 目录，返回调用方负责关闭的目录 FD。"""
    try:
        initial_metadata = secret_dir.lstat()
    except FileNotFoundError:
        initial_metadata = None
    if initial_metadata is not None:
        _validate_owned_mode(
            initial_metadata, "SwanLab secret 目录", "directory", 0o700
        )

    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        parent_fd = os.open(secret_dir.parent, parent_flags)
    except OSError as exc:
        raise RuntimeError("SwanLab secret 父目录无法安全打开") from exc
    try:
        try:
            metadata = os.stat(secret_dir.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir(secret_dir.name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            metadata = os.stat(secret_dir.name, dir_fd=parent_fd, follow_symlinks=False)
        _validate_owned_mode(metadata, "SwanLab secret 目录", "directory", 0o700)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            directory_fd = os.open(secret_dir.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise RuntimeError("SwanLab secret 目录无法安全打开") from exc
        try:
            opened_metadata = os.fstat(directory_fd)
            _validate_owned_mode(
                opened_metadata, "SwanLab secret 目录", "directory", 0o700
            )
            if (
                metadata.st_dev != opened_metadata.st_dev
                or metadata.st_ino != opened_metadata.st_ino
            ):
                raise RuntimeError("SwanLab secret 目录在打开期间被替换")
            return directory_fd
        except Exception:
            os.close(directory_fd)
            raise
    finally:
        os.close(parent_fd)


def _open_lock_file(directory_fd: int) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        lock_fd = os.open(".lock", flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise RuntimeError("SwanLab secret 锁文件无法安全打开") from exc
    try:
        _validate_owned_mode(
            os.fstat(lock_fd), "SwanLab secret 锁文件", "file", 0o600
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd
    except Exception:
        os.close(lock_fd)
        raise


def _read_private_value_at(directory_fd: int, name: str, label: str) -> str:
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise RuntimeError(f"{label} 私有文件无法安全打开") from exc
    try:
        _validate_owned_mode(os.fstat(fd), f"{label} 私有文件", "file", 0o600)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = handle.read().strip()
    finally:
        if fd >= 0:
            os.close(fd)
    if not value:
        raise RuntimeError(f"{label} 私有文件为空")
    return value


def _atomic_write_private_at(
    directory_fd: int,
    name: str,
    value: str,
) -> None:
    temp_name = f".{name}.{secrets.token_hex(16)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(fd, 0o600)
        _validate_owned_mode(
            os.fstat(fd), f"{name} 临时私有文件", "file", 0o600
        )
        payload = (value + "\n").encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(
            temp_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


@contextmanager
def _locked_secret_dir(secret_dir: Path):
    directory_fd = _open_secret_dir(secret_dir)
    lock_fd = -1
    try:
        lock_fd = _open_lock_file(directory_fd)
        yield directory_fd
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


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


def build_base_env(
    environ: dict[str, str] | None = None,
    api_key_path: Path = SWANLAB_API_KEY_FILE,
    session_secret_path: Path = SWANLAB_SESSION_SECRET_FILE,
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
            "CLAUDE_BIN": "/root/.nvm/versions/node/v22.23.1/bin/tclaude",
            "ASR2_BASE_URLS": "http://29.228.42.53:8012",
            "ASR2_MODEL": "HYAudio",
            "CLAUDE_MODEL_FAST": "claude-haiku-4-5",
            "CLAUDE_MODEL_STRONG": "claude-sonnet-5",
            "CLAUDE_MODEL_SUPER": "claude-opus-5[1m]",
            "CLAUDE_DEFAULT_MODE": "claude-sonnet-5",
            "CLAUDE_EFFORT": "medium",
            "CLAUDE_PERSISTENT": "true",
            "CLAUDE_SESSION_IDLE_SEC": "3600",
            "AGENT_WORKDIR": "/apdcephfs_gy2/share_302533218/zhihangxu",
            "WECOM_ENABLED": "true",
            "WECOM_WEBHOOK_KEY": source_env.get(
                "WECOM_WEBHOOK_KEY", "be0c3312-4e95-4648-bdb2-2360ab6dbf97"
            ),
            "WECOM_PROXY": "http://star-proxy.oa.com:3128",
            "NVM_BIN": "/root/.nvm/versions/node/v22.23.1/bin",
            "PATH": (
                "/root/.nvm/versions/node/v22.23.1/bin:/opt/venv/bin:"
                + source_env.get("PATH", "")
            ),
            # 两个 Uvicorn 子进程从同一份本地值注入，跨重启保持稳定。
            "SWANLAB_API_KEY": load_swanlab_api_key(source_env, api_key_path),
            "SWANLAB_SESSION_SECRET": load_or_create_swanlab_session_secret(
                source_env, session_secret_path
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
        "/opt/venv/bin/uvicorn",
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
    base_env = build_base_env()
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
