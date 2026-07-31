# Agent Store CRUD

> 22 nodes

## Key Concepts

- **agent_store.py** (22 connections) — `server/agent_store.py`
- **_path()** (8 connections) — `server/agent_store.py`
- **_read()** (7 connections) — `server/agent_store.py`
- **list_agents()** (7 connections) — `server/agent_store.py`
- **update_agent()** (6 connections) — `server/agent_store.py`
- **_agents_dir()** (5 connections) — `server/agent_store.py`
- **get_agent()** (5 connections) — `server/agent_store.py`
- **create_agent()** (5 connections) — `server/agent_store.py`
- **agents_json()** (5 connections) — `server/agent_store.py`
- **_norm_tools()** (4 connections) — `server/agent_store.py`
- **_tools_list()** (4 connections) — `server/agent_store.py`
- **_build_meta()** (3 connections) — `server/agent_store.py`
- **delete_agent()** (3 connections) — `server/agent_store.py`
- **agents_list()** (2 connections) — `server/main.py`
- **agents_create()** (2 connections) — `server/main.py`
- **agents_get()** (2 connections) — `server/main.py`
- **agents_update()** (2 connections) — `server/main.py`
- **agents_delete()** (2 connections) — `server/main.py`
- **Path** (1 connections)
- **Subagent 管理：读写 claude-code 标准的 .claude/agents/<name>.md。  每个 subagent 是一个带 front** (1 connections) — `server/agent_store.py`
- **tools 接受逗号串或数组，归一成 claude-code 的逗号分隔单行字符串。** (1 connections) — `server/agent_store.py`
- **把所有 subagent 拼成 tclaude `--agents <json>` 需要的 JSON 字符串。      headless `-p` 模式下，t** (1 connections) — `server/agent_store.py`

## Relationships

- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (6 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (4 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (3 shared connections)
- [YAML Frontmatter Utils](YAML_Frontmatter_Utils.md) (3 shared connections)
- [Dispatcher Deterministic Routing](Dispatcher_Deterministic_Routing.md) (2 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (1 shared connections)
- [LLMs.txt Skill Index Builder](LLMs.txt_Skill_Index_Builder.md) (1 shared connections)

## Source Files

- `server/agent_store.py`
- `server/main.py`

## Audit Trail

- EXTRACTED: 98 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*