"""本机私有密钥文件的安全读写与加锁工具。"""
import fcntl
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path


def validate_owned_mode(
    metadata: os.stat_result,
    label: str,
    expected_type: str,
    expected_mode: int | None,
) -> None:
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(f"{label} owner 必须是当前运行用户")
    if expected_type == "directory":
        valid_type = stat.S_ISDIR(metadata.st_mode)
    else:
        valid_type = stat.S_ISREG(metadata.st_mode)
    if not valid_type:
        raise RuntimeError(f"{label} 必须是普通{expected_type}")
    if expected_mode is not None:
        actual_mode = stat.S_IMODE(metadata.st_mode)
        if actual_mode != expected_mode:
            raise RuntimeError(f"{label} 权限必须精确为 {expected_mode:04o}")


def open_secret_dir(
    secret_dir: Path,
    *,
    expected_mode: int | None = 0o700,
    label: str = "SwanLab secret",
) -> int:
    """安全打开 secret 目录，返回调用方负责关闭的目录 FD。"""
    secret_dir = Path(secret_dir)
    try:
        initial_metadata = secret_dir.lstat()
    except FileNotFoundError:
        initial_metadata = None
    if initial_metadata is not None:
        validate_owned_mode(
            initial_metadata, f"{label} 目录", "directory", expected_mode
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
        raise RuntimeError(f"{label} 父目录无法安全打开") from exc
    try:
        try:
            metadata = os.stat(secret_dir.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir(secret_dir.name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            metadata = os.stat(secret_dir.name, dir_fd=parent_fd, follow_symlinks=False)
        validate_owned_mode(metadata, f"{label} 目录", "directory", expected_mode)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            directory_fd = os.open(secret_dir.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise RuntimeError(f"{label} 目录无法安全打开") from exc
        try:
            opened_metadata = os.fstat(directory_fd)
            validate_owned_mode(
                opened_metadata, f"{label} 目录", "directory", expected_mode
            )
            if (
                metadata.st_dev != opened_metadata.st_dev
                or metadata.st_ino != opened_metadata.st_ino
            ):
                raise RuntimeError(f"{label} 目录在打开期间被替换")
            return directory_fd
        except Exception:
            os.close(directory_fd)
            raise
    finally:
        os.close(parent_fd)


def open_lock_file(directory_fd: int, label: str = "SwanLab secret") -> int:
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
        raise RuntimeError(f"{label} 锁文件无法安全打开") from exc
    try:
        validate_owned_mode(
            os.fstat(lock_fd), f"{label} 锁文件", "file", 0o600
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd
    except Exception:
        os.close(lock_fd)
        raise


def read_private_value_at(directory_fd: int, name: str, label: str) -> str:
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
        validate_owned_mode(os.fstat(fd), f"{label} 私有文件", "file", 0o600)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = handle.read().strip()
    finally:
        if fd >= 0:
            os.close(fd)
    if not value:
        raise RuntimeError(f"{label} 私有文件为空")
    return value


def atomic_write_private_at(directory_fd: int, name: str, value: str) -> None:
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
        validate_owned_mode(
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
def locked_secret_dir(
    secret_dir: Path,
    *,
    expected_mode: int | None = 0o700,
    label: str = "SwanLab secret",
):
    directory_fd = open_secret_dir(
        secret_dir, expected_mode=expected_mode, label=label
    )
    lock_fd = -1
    try:
        lock_fd = open_lock_file(directory_fd)
        yield directory_fd
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def load_or_create_local_notify_secret(environ: dict, path) -> str:
    from_env = (environ.get("LOCAL_NOTIFY_SECRET") or "").strip()
    if from_env:
        return from_env

    path = Path(path)
    with locked_secret_dir(
        path.parent, expected_mode=None, label="notify secret"
    ) as directory_fd:
        try:
            return read_private_value_at(
                directory_fd, path.name, "LOCAL_NOTIFY_SECRET"
            )
        except RuntimeError as exc:
            try:
                os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if "私有文件为空" not in str(exc):
                    raise exc
            value = secrets.token_hex(32)
            atomic_write_private_at(directory_fd, path.name, value)
            return value
