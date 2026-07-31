# CodexRunner Process Management

> 14 nodes

## Key Concepts

- **CodexRunner** (10 connections) — `server/codex_runner.py`
- **.run_turn()** (7 connections) — `server/codex_runner.py`
- **.cancel()** (4 connections) — `server/codex_runner.py`
- **._build_cmd()** (3 connections) — `server/codex_runner.py`
- **._get_proc()** (3 connections) — `server/codex_runner.py`
- **.is_running()** (2 connections) — `server/codex_runner.py`
- **.__init__()** (1 connections) — `server/codex_runner.py`
- **Process** (1 connections)
- **.was_cancelled()** (1 connections) — `server/codex_runner.py`
- **EventCallback** (1 connections)
- **.ensure_warm()** (1 connections) — `server/codex_runner.py`
- **组 tcodex exec 命令。prompt 作为最后一个位置参数。** (1 connections) — `server/codex_runner.py`
- **中断当前回合：杀整个进程组（下回合自动 resume 续上）。** (1 connections) — `server/codex_runner.py`
- **跑一个回合。返回 {claude_session_id, returncode, error, cancelled}。          on_permissi** (1 connections) — `server/codex_runner.py`

## Relationships

- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (5 shared connections)

## Source Files

- `server/codex_runner.py`

## Audit Trail

- EXTRACTED: 36 (97%)
- INFERRED: 1 (3%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*