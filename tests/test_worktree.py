"""server.worktree.provision_workdir 的三条分支：
不隔离 / 隔离成功 / 隔离降级（非 git 仓库）。"""
import subprocess

from server import worktree


def test_provision_not_isolated_returns_shared_base(non_git_dir):
    # isolate=False：原样返回共享 base，不尝试建 worktree。
    workdir, branch, is_wt, wt_base, notice, wt_sha = worktree.provision_workdir(
        non_git_dir, "sess-hint", isolate=False
    )
    assert workdir == non_git_dir
    assert branch == ""
    assert is_wt == 0
    assert wt_base == ""
    assert notice == ""
    assert wt_sha == ""


def test_provision_isolated_git_repo_creates_worktree(git_repo, worktrees_root):
    # isolate=True 且 base 是 git 仓库：返回隔离目录 + 分支元信息，无降级提示；
    # 并带上创建点的 base HEAD sha（验收侧靠它算 base..HEAD 改动）。
    head = subprocess.run(["git", "-C", git_repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    workdir, branch, is_wt, wt_base, notice, wt_sha = worktree.provision_workdir(
        git_repo, "my-session", isolate=True
    )
    assert workdir != git_repo
    assert workdir.startswith(str(worktrees_root))
    assert branch.startswith("agent/my-session-")
    assert is_wt == 1
    assert wt_base == git_repo
    assert notice == ""
    assert wt_sha == head


def test_provision_isolated_non_git_falls_back_with_notice(non_git_dir, worktrees_root):
    # isolate=True 但 base 非 git 仓库：降级为共享 base，并给出提示文案。
    workdir, branch, is_wt, wt_base, notice, wt_sha = worktree.provision_workdir(
        non_git_dir, "sess", isolate=True
    )
    assert workdir == non_git_dir
    assert branch == ""
    assert is_wt == 0
    assert wt_base == ""
    assert notice  # 非空提示
    assert "共享工作区" in notice
    assert wt_sha == ""
