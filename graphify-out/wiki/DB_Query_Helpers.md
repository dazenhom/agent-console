# DB Query Helpers

> 56 nodes

## Key Concepts

- **_query()** (71 connections) — `server/db.py`
- **update_todo()** (11 connections) — `server/db.py`
- **set_todo_sessions()** (8 connections) — `server/db.py`
- **search_message_sessions()** (4 connections) — `server/db.py`
- **update_snippet()** (4 connections) — `server/db.py`
- **delete_snippet()** (4 connections) — `server/db.py`
- **active_goal_prompts()** (4 connections) — `server/db.py`
- **count_triage_dispatched_today()** (4 connections) — `server/db.py`
- **list_dispatch_plans()** (4 connections) — `server/db.py`
- **next_pending_goal_subtask()** (4 connections) — `server/db.py`
- **list_work_items()** (4 connections) — `server/db.py`
- **todos_update()** (4 connections) — `server/main.py`
- **list_tasks()** (3 connections) — `server/db.py`
- **list_snippets()** (3 connections) — `server/db.py`
- **list_schedules()** (3 connections) — `server/db.py`
- **due_schedules()** (3 connections) — `server/db.py`
- **has_active_goal()** (3 connections) — `server/db.py`
- **list_reports()** (3 connections) — `server/db.py`
- **get_report()** (3 connections) — `server/db.py`
- **get_secretary_session()** (3 connections) — `server/db.py`
- **list_artifacts()** (3 connections) — `server/db.py`
- **list_arbitrations()** (3 connections) — `server/db.py`
- **get_goal_subtask()** (3 connections) — `server/db.py`
- **todos_create()** (3 connections) — `server/main.py`
- **todos_set_sessions()** (3 connections) — `server/main.py`
- *... and 31 more nodes in this community*

## Relationships

- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (43 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (27 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (10 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (9 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (4 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (3 shared connections)
- [Goal Loop Detail Queries](Goal_Loop_Detail_Queries.md) (2 shared connections)
- [Dispatch Subtask Verdict Verification](Dispatch_Subtask_Verdict_Verification.md) (1 shared connections)
- [Queue Item CRUD](Queue_Item_CRUD.md) (1 shared connections)
- [Dispatch Fanout Finalization + Tests](Dispatch_Fanout_Finalization_%2B_Tests.md) (1 shared connections)
- [Goal Loops List Query](Goal_Loops_List_Query.md) (1 shared connections)
- [Session Import](Session_Import.md) (1 shared connections)

## Source Files

- `server/db.py`
- `server/main.py`

## Audit Trail

- EXTRACTED: 223 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*