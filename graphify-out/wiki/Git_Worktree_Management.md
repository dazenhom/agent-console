# Git Worktree Management

> 23 nodes

## Key Concepts

- **worktree.py** (20 connections) — `server/worktree.py`
- **_run()** (7 connections) — `server/worktree.py`
- **provision_workdir()** (7 connections) — `server/worktree.py`
- **is_git_repo()** (6 connections) — `server/worktree.py`
- **create()** (6 connections) — `server/worktree.py`
- **prune()** (5 connections) — `server/worktree.py`
- **delete_branch()** (5 connections) — `server/worktree.py`
- **test_worktree.py** (5 connections) — `tests/test_worktree.py`
- **list_agent_branches()** (4 connections) — `server/worktree.py`
- **remove()** (3 connections) — `server/worktree.py`
- **is_agent_branch()** (3 connections) — `server/worktree.py`
- **_slug()** (2 connections) — `server/worktree.py`
- **test_provision_not_isolated_returns_shared_base()** (2 connections) — `tests/test_worktree.py`
- **test_provision_isolated_git_repo_creates_worktree()** (2 connections) — `tests/test_worktree.py`
- **test_provision_isolated_non_git_falls_back_with_notice()** (2 connections) — `tests/test_worktree.py`
- **Git worktree 会话隔离：为会话创建独立分支+独立目录，使并行会话互不干扰。 不是 git 仓库时降级为直接返回 base（不报错）。绝不自动 mer** (1 connections) — `server/worktree.py`
- **为会话创建独立 worktree + 分支。成功返回 (path, branch)；     base 不是 git 仓库或创建失败 → 降级返回 (base,** (1 connections) — `server/worktree.py`
- **统一隔离工作区编排：把"调 create → 判是否真隔离 → 定 worktree 元信息 → 拼降级提示"     这段散落在多处的逻辑收成一个入口。返回** (1 connections) — `server/worktree.py`
- **删除会话时清理 worktree 目录（不删分支）。失败只打日志，不 raise。** (1 connections) — `server/worktree.py`
- **清理失效的 worktree 记录（目录已被删但 git 元数据残留）。** (1 connections) — `server/worktree.py`
- **列出仓库内所有 agent/* 分支及最后提交信息。非 git 仓库或失败返回空列表。     每条：{branch, last_commit_at(%cI),** (1 connections) — `server/worktree.py`
- **强制删除一个 agent/* 分支（git branch -D）。分支名必须过白名单再传给 git。     返回 (ok, err)：成功 (True, ""** (1 connections) — `server/worktree.py`
- **server.worktree.provision_workdir 的三条分支： 不隔离 / 隔离成功 / 隔离降级（非 git 仓库）。** (1 connections) — `tests/test_worktree.py`

## Relationships

- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (3 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (2 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (2 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (2 shared connections)
- [Dispatcher Deterministic Routing](Dispatcher_Deterministic_Routing.md) (1 shared connections)
- [Dispatch Subtask Verdict Verification](Dispatch_Subtask_Verdict_Verification.md) (1 shared connections)

## Source Files

- `server/worktree.py`
- `tests/test_worktree.py`

## Audit Trail

- EXTRACTED: 87 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*