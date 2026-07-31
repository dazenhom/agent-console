# AgentProvider Abstract Base

> 32 nodes

## Key Concepts

- **session_hub.py** (18 connections) — `server/session_hub.py`
- **claude_runner.py** (16 connections) — `server/claude_runner.py`
- **AgentProvider** (13 connections) — `server/agent_provider.py`
- **codex_runner.py** (12 connections) — `server/codex_runner.py`
- **logging_util.py** (10 connections) — `server/logging_util.py`
- **_child_env()** (9 connections) — `server/agent_provider.py`
- **_kill_process_group()** (9 connections) — `server/agent_provider.py`
- **agent_provider.py** (8 connections) — `server/agent_provider.py`
- **get_logger()** (6 connections) — `server/logging_util.py`
- **is_sleep_command()** (5 connections) — `server/agent_provider.py`
- **.run_turn()** (3 connections) — `server/agent_provider.py`
- **ABC** (2 connections)
- **.send_turn()** (2 connections) — `server/agent_provider.py`
- **translate_event()** (2 connections) — `server/session_hub.py`
- **_activity_label()** (2 connections) — `server/session_hub.py`
- **Process** (1 connections)
- **EventCallback** (1 connections)
- **.forget_session()** (1 connections) — `server/agent_provider.py`
- **.respond_permission()** (1 connections) — `server/agent_provider.py`
- **.ensure_warm()** (1 connections) — `server/agent_provider.py`
- **.cleanup_idle()** (1 connections) — `server/agent_provider.py`
- **Agent provider 基础契约：on_event 接收 type 为 assistant、携带 tool_result 的 user、result 或** (1 connections) — `server/agent_provider.py`
- **判断一条 shell 命令是否为「等待型」sleep 调用。      这类调用（agent 轮询等待后台任务）不该被计入循环检测 / 超时预算。     规则** (1 connections) — `server/agent_provider.py`
- **构造给 tclaude 子进程的环境：复制当前环境，剔除继承来的编排态变量。** (1 connections) — `server/agent_provider.py`
- **优雅地把 proc 整个进程组发信号。返回 True 表示信号已发出。** (1 connections) — `server/agent_provider.py`
- *... and 7 more nodes in this community*

## Relationships

- [ClaudeRunner Process Management](ClaudeRunner_Process_Management.md) (9 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (7 shared connections)
- [CodexRunner Process Management](CodexRunner_Process_Management.md) (5 shared connections)
- [Logger Protocol Design](Logger_Protocol_Design.md) (5 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (4 shared connections)
- [WebSocket Session Monitoring](WebSocket_Session_Monitoring.md) (3 shared connections)
- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (2 shared connections)
- [Git Worktree Management](Git_Worktree_Management.md) (2 shared connections)
- [Agent Store CRUD](Agent_Store_CRUD.md) (1 shared connections)
- [Context Compactor](Context_Compactor.md) (1 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (1 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (1 shared connections)

## Source Files

- `server/agent_provider.py`
- `server/claude_runner.py`
- `server/codex_runner.py`
- `server/logging_util.py`
- `server/session_hub.py`

## Audit Trail

- EXTRACTED: 129 (96%)
- INFERRED: 5 (4%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*