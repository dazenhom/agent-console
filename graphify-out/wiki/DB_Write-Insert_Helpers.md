# DB Write/Insert Helpers

> 53 nodes

## Key Concepts

- **db.py** (121 connections) — `server/db.py`
- **_exec()** (43 connections) — `server/db.py`
- **_now()** (31 connections) — `server/db.py`
- **new_id()** (25 connections) — `server/db.py`
- **create_session()** (10 connections) — `server/db.py`
- **create_dispatch_subtask()** (10 connections) — `server/db.py`
- **run_reminder()** (9 connections) — `server/memo_reminder.py`
- **create_todo()** (8 connections) — `server/db.py`
- **start_task()** (7 connections) — `server/db.py`
- **create_schedule()** (7 connections) — `server/db.py`
- **create_triage_todo()** (7 connections) — `server/db.py`
- **create_report()** (7 connections) — `server/db.py`
- **create_goal_iteration()** (7 connections) — `server/db.py`
- **replace_goal_subtasks()** (7 connections) — `server/db.py`
- **update_work_item_status_by_ref()** (7 connections) — `server/db.py`
- **add_message()** (6 connections) — `server/db.py`
- **create_artifact()** (6 connections) — `server/db.py`
- **create_arbitration()** (6 connections) — `server/db.py`
- **create_work_item()** (6 connections) — `server/db.py`
- **reconcile_stale_running()** (5 connections) — `server/db.py`
- **start_job()** (5 connections) — `server/db.py`
- **create_snippet()** (5 connections) — `server/db.py`
- **reconcile_dispatch_subtasks()** (5 connections) — `server/db.py`
- **ensure_secretary_session()** (5 connections) — `server/db.py`
- **create_memo()** (5 connections) — `server/db.py`
- *... and 28 more nodes in this community*

## Relationships

- [DB Query Helpers](DB_Query_Helpers.md) (43 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (21 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (17 shared connections)
- [Dispatch Subtask Verdict Verification](Dispatch_Subtask_Verdict_Verification.md) (16 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (14 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (12 shared connections)
- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (10 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (10 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (7 shared connections)
- [Queue Item CRUD](Queue_Item_CRUD.md) (5 shared connections)
- [Dispatch Fanout Finalization + Tests](Dispatch_Fanout_Finalization_%2B_Tests.md) (4 shared connections)
- [Goal Loop Detail Queries](Goal_Loop_Detail_Queries.md) (3 shared connections)

## Source Files

- `server/db.py`
- `server/main.py`
- `server/memo_reminder.py`
- `tests/test_dispatch_arbitration.py`

## Audit Trail

- EXTRACTED: 444 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*