# ClaudeRunner Process Management

> 36 nodes

## Key Concepts

- **ClaudeRunner** (17 connections) — `server/claude_runner.py`
- **LoopDetector** (8 connections) — `server/claude_runner.py`
- **._spawn_session()** (8 connections) — `server/claude_runner.py`
- **._kill_session()** (7 connections) — `server/claude_runner.py`
- **.send_turn()** (6 connections) — `server/claude_runner.py`
- **.run_turn()** (5 connections) — `server/claude_runner.py`
- **._reader()** (5 connections) — `server/claude_runner.py`
- **._is_sleep_bash()** (4 connections) — `server/claude_runner.py`
- **.feed()** (4 connections) — `server/claude_runner.py`
- **._build_cmd()** (4 connections) — `server/claude_runner.py`
- **.cancel()** (4 connections) — `server/claude_runner.py`
- **._get_proc()** (3 connections) — `server/claude_runner.py`
- **.ensure_warm()** (3 connections) — `server/claude_runner.py`
- **.respond_permission()** (3 connections) — `server/claude_runner.py`
- **.forget_session()** (3 connections) — `server/claude_runner.py`
- **.cleanup_idle()** (3 connections) — `server/claude_runner.py`
- **.is_running()** (2 connections) — `server/claude_runner.py`
- **EventCallback** (2 connections)
- **.__init__()** (1 connections) — `server/claude_runner.py`
- **.__init__()** (1 connections) — `server/claude_runner.py`
- **Process** (1 connections)
- **.was_cancelled()** (1 connections) — `server/claude_runner.py`
- **轻量循环检测：喂进原始 stream-json 事件，判断 agent 是否卡在循环里。      两种循环信号：       1. 连续 repeat_thr** (1 connections) — `server/claude_runner.py`
- **判断 tool_use block 是否为「Bash 运行 sleep」。          这类调用（agent 轮询等待后台任务）不应计入循环检测。分段判定** (1 connections) — `server/claude_runner.py`
- **返回 (is_loop: bool, reason: str)。** (1 connections) — `server/claude_runner.py`
- *... and 11 more nodes in this community*

## Relationships

- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (9 shared connections)

## Source Files

- `server/claude_runner.py`

## Audit Trail

- EXTRACTED: 107 (98%)
- INFERRED: 2 (2%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*