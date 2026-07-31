# Session-Todo Linking

> 21 nodes

## Key Concepts

- **get_session()** (27 connections) — `server/db.py`
- **update_session()** (12 connections) — `server/db.py`
- **refresh_todo_progress()** (10 connections) — `server/kanban.py`
- **list_messages()** (6 connections) — `server/db.py`
- **todos_linked_to_session()** (4 connections) — `server/db.py`
- **set_session_engine()** (4 connections) — `server/main.py`
- **set_session_workdir()** (4 connections) — `server/main.py`
- **remove_session()** (4 connections) — `server/main.py`
- **get_file()** (4 connections) — `server/main.py`
- **set_session_mode()** (3 connections) — `server/main.py`
- **set_session_effort()** (3 connections) — `server/main.py`
- **set_session_title()** (3 connections) — `server/main.py`
- **archive_session()** (3 connections) — `server/main.py`
- **unarchive_session()** (3 connections) — `server/main.py`
- **compact_session()** (3 connections) — `server/main.py`
- **get_messages()** (3 connections) — `server/main.py`
- **todo_refresh_progress()** (2 connections) — `server/main.py`
- **返回关联了该会话的【未归档】看板任务标题列表（归档任务不锁定会话，避免死锁）。** (1 connections) — `server/db.py`
- **刷新单个 todo 卡片的进展摘要，带 jsonl mtime 缓存。** (1 connections) — `server/kanban.py`
- **真实上下文压缩：把整段对话概括成摘要并重置会话，摘要作前缀注入下一条消息续接。** (1 connections) — `server/main.py`
- **读取文件。图片允许任意绝对路径（模型可输出工作目录外的图片路径）；文本仍限 workdir 内。** (1 connections) — `server/main.py`

## Relationships

- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (15 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (10 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (6 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (4 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (3 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (3 shared connections)
- [WebSocket Session Monitoring](WebSocket_Session_Monitoring.md) (2 shared connections)
- [Dispatch Subtask Verdict Verification](Dispatch_Subtask_Verdict_Verification.md) (1 shared connections)
- [Session Import](Session_Import.md) (1 shared connections)
- [Context Compactor](Context_Compactor.md) (1 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (1 shared connections)
- [YAML Frontmatter Utils](YAML_Frontmatter_Utils.md) (1 shared connections)

## Source Files

- `server/db.py`
- `server/kanban.py`
- `server/main.py`

## Audit Trail

- EXTRACTED: 102 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*