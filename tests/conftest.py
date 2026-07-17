"""测试公共 fixture。

核心约束：所有涉及 DB 的测试都必须落到临时库，绝不碰生产的 data/console.db。
做法是把 config.DB_PATH 指向 tmp 目录后再调 db.init_db()——init_db 在调用时读
config.DB_PATH 并重建模块级 _conn，因此每个测试拿到的是一条全新的、隔离的连接。
"""
import subprocess

import pytest

from server import config, db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """把 SQLite 落到临时文件并初始化，测试结束随 tmp_path 一起清理。"""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "console.db"))
    db.init_db()
    yield db


@pytest.fixture
def worktrees_root(tmp_path, monkeypatch):
    """把 worktree 落地根指向临时目录，避免污染生产 data/worktrees。"""
    root = tmp_path / "worktrees"
    monkeypatch.setattr(config, "WORKTREES_ROOT", str(root))
    return root


@pytest.fixture
def git_repo(tmp_path):
    """建一个带一次初始提交的真实 git 仓库，供隔离成功路径使用。"""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        )

    _git("init", "-q")
    _git("config", "user.email", "t@t.dev")
    _git("config", "user.name", "test")
    (repo / "README.md").write_text("hello\n")
    _git("add", ".")
    _git("commit", "-q", "-m", "init")
    return str(repo)


@pytest.fixture
def non_git_dir(tmp_path):
    """一个普通空目录（没有 git init），用于验证降级路径。"""
    d = tmp_path / "plain"
    d.mkdir()
    return str(d)
