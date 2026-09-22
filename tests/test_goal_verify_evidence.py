"""验收成本治理的回归测试。

P0：worktree 会话的 git_diff 必须能看到 base..HEAD 的已提交改动——agent 干完活会自己
commit，原来无 ref 的 `git diff --stat`（只看未提交改动）实测在三个真实 worktree 会话里
输出全为空，评委只能在零证据下自己跑 97 条 git 命令重建改动清单（验收成本 27.1 万 tok
vs 共享会话 11.5 万）。
"""
import asyncio
import subprocess
from pathlib import Path

from server import scheduler, worktree


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _isolated_worktree_with_commit(git_repo):
    """建一个隔离 worktree，在里面改一个文件并**提交**（模拟 agent 的真实收尾动作）。
    返回 (worktree 目录, base HEAD sha)。"""
    base_sha = _git(git_repo, "rev-parse", "HEAD").strip()
    wd, branch, is_wt, wt_base, notice, wt_sha = worktree.provision_workdir(
        git_repo, "cost-test", isolate=True)
    assert is_wt == 1 and wt_sha == base_sha and branch
    (Path(wd) / "report.md").write_text("done\n")
    _git(wd, "add", ".")
    _git(wd, "commit", "-q", "-m", "写报告")
    return wd, base_sha


def test_git_diff_stat_with_base_sha_sees_committed_changes(git_repo, worktrees_root):
    wd, base_sha = _isolated_worktree_with_commit(git_repo)

    # 现状（无 base sha）：改动已提交，无 ref 的 diff --stat 恒为空 —— 这正是生产失效的根因
    assert asyncio.run(scheduler._git_diff_stat(wd)) == ""

    # 修复后：给出 base..HEAD 的结构化摘要（提交列表 + stat + name-status + 未提交改动）
    out = asyncio.run(scheduler._git_diff_stat(wd, base_sha))
    assert f"[基线 {base_sha[:10]}..HEAD]" in out
    assert "写报告" in out                 # log --oneline 里有本轮提交
    assert "report.md" in out              # stat / name-status 里有改动文件
    assert "[未提交改动]" in out


def test_git_diff_stat_ignores_non_sha_base(git_repo, worktrees_root):
    # 脏数据/历史会话：base 不是 sha 形态 → 退回原行为（只看未提交改动），绝不拼进 git 命令
    wd, _ = _isolated_worktree_with_commit(git_repo)
    assert asyncio.run(scheduler._git_diff_stat(wd, "HEAD~1; rm -rf /")) == ""
    assert asyncio.run(scheduler._git_diff_stat(wd, "")) == ""
