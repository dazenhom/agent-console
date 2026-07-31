# Test Fixtures (temp DB)

> 10 nodes

## Key Concepts

- **conftest.py** (7 connections) — `tests/conftest.py`
- **temp_db()** (3 connections) — `tests/conftest.py`
- **worktrees_root()** (2 connections) — `tests/conftest.py`
- **git_repo()** (2 connections) — `tests/conftest.py`
- **non_git_dir()** (2 connections) — `tests/conftest.py`
- **测试公共 fixture。  核心约束：所有涉及 DB 的测试都必须落到临时库，绝不碰生产的 data/console.db。 做法是把 config.DB_P** (1 connections) — `tests/conftest.py`
- **把 SQLite 落到临时文件并初始化，测试结束随 tmp_path 一起清理。** (1 connections) — `tests/conftest.py`
- **把 worktree 落地根指向临时目录，避免污染生产 data/worktrees。** (1 connections) — `tests/conftest.py`
- **建一个带一次初始提交的真实 git 仓库，供隔离成功路径使用。** (1 connections) — `tests/conftest.py`
- **一个普通空目录（没有 git init），用于验证降级路径。** (1 connections) — `tests/conftest.py`

## Relationships

- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (1 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (1 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (1 shared connections)

## Source Files

- `tests/conftest.py`

## Audit Trail

- EXTRACTED: 21 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*