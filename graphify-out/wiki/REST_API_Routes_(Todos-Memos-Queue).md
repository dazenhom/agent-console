# REST API Routes (Todos/Memos/Queue)

> 61 nodes

## Key Concepts

- **main.py** (144 connections) — `server/main.py`
- **lifespan()** (12 connections) — `server/main.py`
- **check_token()** (6 connections) — `server/main.py`
- **ws_endpoint()** (6 connections) — `server/main.py`
- **list_todos()** (5 connections) — `server/db.py`
- **_claimed_worktree_branches()** (5 connections) — `server/main.py`
- **ws_monitor()** (5 connections) — `server/main.py`
- **list_queue()** (4 connections) — `server/db.py`
- **list_memos()** (4 connections) — `server/db.py`
- **delete_worktree_orphan()** (4 connections) — `server/main.py`
- **get_work_item()** (4 connections) — `server/main.py`
- **_goal_protected_uploads()** (4 connections) — `server/main.py`
- **_uploads_cleanup_loop()** (4 connections) — `server/main.py`
- **upload_image()** (4 connections) — `server/main.py`
- **init_db()** (3 connections) — `server/db.py`
- **require_auth_query()** (3 connections) — `server/main.py`
- **list_worktree_orphans()** (3 connections) — `server/main.py`
- **queue_list()** (3 connections) — `server/main.py`
- **memos_create()** (3 connections) — `server/main.py`
- **preview_html()** (3 connections) — `server/main.py`
- **_swanlab_sid_warmup()** (3 connections) — `server/main.py`
- **_rewrite_asset_versions()** (3 connections) — `server/main.py`
- **delete_todo()** (2 connections) — `server/db.py`
- **FastAPI** (2 connections)
- **require_auth()** (2 connections) — `server/main.py`
- *... and 36 more nodes in this community*

## Relationships

- [DB Query Helpers](DB_Query_Helpers.md) (27 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (17 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (15 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (7 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (6 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (6 shared connections)
- [Agent Store CRUD](Agent_Store_CRUD.md) (6 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (6 shared connections)
- [Memory Store CRUD](Memory_Store_CRUD.md) (6 shared connections)
- [WebSocket Session Monitoring](WebSocket_Session_Monitoring.md) (6 shared connections)
- [Session Import](Session_Import.md) (5 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (5 shared connections)

## Source Files

- `server/db.py`
- `server/main.py`

## Audit Trail

- EXTRACTED: 292 (98%)
- INFERRED: 5 (2%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*