"""Git worktree 会话隔离：为会话创建独立分支+独立目录，使并行会话互不干扰。
不是 git 仓库时降级为直接返回 base（不报错）。绝不自动 merge 回主干——合并走人工。"""
import logging
import re
import secrets
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


def _run(args, cwd=None, timeout=60):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def is_git_repo(base: str) -> bool:
    if not base:
        return False
    try:
        r = _run(["git", "-C", base, "rev-parse", "--is-inside-work-tree"])
    except Exception:
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def _slug(sid_hint: str) -> str:
    # 保留 [a-zA-Z0-9-]，其余转 -，截断 ~24 字符，去首尾 -，空则回退 "s"
    s = re.sub(r"[^a-zA-Z0-9-]", "-", sid_hint or "")
    s = s[:24].strip("-")
    return s or "s"


def create(base, sid_hint) -> tuple[str, str]:
    """为会话创建独立 worktree + 分支。成功返回 (path, branch)；
    base 不是 git 仓库或创建失败 → 降级返回 (base, "")。"""
    if not is_git_repo(base):
        return base, ""
    name = f"{_slug(sid_hint)}-{secrets.token_hex(3)}"
    branch = f"agent/{name}"
    root = Path(config.WORKTREES_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    try:
        r = _run(["git", "-C", base, "worktree", "add", "-b", branch, str(path), "HEAD"])
    except Exception as e:
        log.warning("worktree create 异常，降级为共享工作区：%s", e)
        return base, ""
    if r.returncode != 0:
        log.warning("worktree create 失败，降级为共享工作区：%s", r.stderr.strip())
        return base, ""
    return str(path), branch


def remove(path, base) -> None:
    """删除会话时清理 worktree 目录（不删分支）。失败只打日志，不 raise。"""
    if not path or not base:
        return
    try:
        # 用 base（原 repo）作 -C 上下文，git 才能定位并注销该 worktree
        r = _run(["git", "-C", base, "worktree", "remove", "--force", path])
        if r.returncode != 0:
            log.warning("worktree remove 失败：%s", r.stderr.strip())
    except Exception as e:
        log.warning("worktree remove 异常：%s", e)


def prune(base) -> None:
    """清理失效的 worktree 记录（目录已被删但 git 元数据残留）。"""
    if not base or not is_git_repo(base):
        return
    try:
        _run(["git", "-C", base, "worktree", "prune"])
    except Exception as e:
        log.warning("worktree prune 异常：%s", e)
