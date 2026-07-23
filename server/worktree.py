"""Git worktree 会话隔离：为会话创建独立分支+独立目录，使并行会话互不干扰。
不是 git 仓库时降级为直接返回 base（不报错）。绝不自动 merge 回主干——合并走人工。"""
import re
import secrets
import subprocess
from pathlib import Path

from . import config
from .logging_util import get_logger

log = get_logger(__name__)


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


def provision_workdir(base: str, hint: str, isolate: bool) -> tuple[str, str, int, str, str]:
    """统一隔离工作区编排：把"调 create → 判是否真隔离 → 定 worktree 元信息 → 拼降级提示"
    这段散落在多处的逻辑收成一个入口。返回 (workdir, branch, is_worktree, worktree_base, notice)：
    - isolate=False：不隔离，直接返回共享 base；
    - isolate=True 且建 worktree 成功：返回隔离目录 + 分支元信息；
    - isolate=True 但 base 非 git 仓库 / 创建失败：降级为共享 base，并给出提示文案。"""
    if not isolate:
        return base, "", 0, "", ""
    path, branch = create(base, hint)
    if branch:
        return path, branch, 1, base, ""
    return base, "", 0, "", "该目录不是 Git 仓库（或创建 worktree 失败），已使用共享工作区，未隔离。"


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


# 分支名白名单：只放行 create() 生成的 agent/<slug>-<hash> 形态，
# 严防传给 git 命令的分支名夹带注入/穿越字符。
_BRANCH_RE = re.compile(r"^agent/[A-Za-z0-9._-]+$")


def is_agent_branch(branch: str) -> bool:
    return bool(_BRANCH_RE.match(branch or ""))


def list_agent_branches(base) -> list[dict]:
    """列出仓库内所有 agent/* 分支及最后提交信息。非 git 仓库或失败返回空列表。
    每条：{branch, last_commit_at(%cI), last_commit_hash(%h)}。"""
    if not is_git_repo(base):
        return []
    try:
        r = _run(["git", "-C", base, "branch", "--list", "agent/*",
                  "--format=%(refname:short)"])
    except Exception as e:
        log.warning("list agent branches 异常：%s", e)
        return []
    if r.returncode != 0:
        log.warning("list agent branches 失败：%s", r.stderr.strip())
        return []
    out = []
    for line in r.stdout.splitlines():
        br = line.strip()
        if not br:
            continue
        commit_at, commit_hash = "", ""
        try:
            # %cI（严格 ISO 提交时间）与 %h（简写 hash）用 NUL 分隔，避免时间串里出现分隔符歧义
            info = _run(["git", "-C", base, "log", "-1", "--format=%cI%x00%h", br])
            if info.returncode == 0 and info.stdout.strip():
                commit_at, _, commit_hash = info.stdout.strip().partition("\x00")
        except Exception as e:
            log.warning("读取分支 %s 提交信息异常：%s", br, e)
        out.append({"branch": br, "last_commit_at": commit_at, "last_commit_hash": commit_hash})
    return out


def delete_branch(base, branch: str) -> tuple[bool, str]:
    """强制删除一个 agent/* 分支（git branch -D）。分支名必须过白名单再传给 git。
    返回 (ok, err)：成功 (True, "")，失败 (False, 原因)。"""
    if not is_agent_branch(branch):
        return False, "分支名不合法（仅允许 agent/<name> 形态）"
    if not is_git_repo(base):
        return False, "目标不是 Git 仓库"
    try:
        r = _run(["git", "-C", base, "branch", "-D", branch])
    except Exception as e:
        log.warning("delete branch %s 异常：%s", branch, e)
        return False, str(e)
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, ""
