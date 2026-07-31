# Goal Loop Iteration Queries

> 58 nodes

## Key Concepts

- **scheduler.py** (40 connections) — `server/scheduler.py`
- **_tick_goal_planned()** (17 connections) — `server/scheduler.py`
- **_run_goal_verify_planned()** (16 connections) — `server/scheduler.py`
- **get_schedule()** (15 connections) — `server/db.py`
- **_run_goal_verify()** (14 connections) — `server/scheduler.py`
- **update_schedule()** (13 connections) — `server/db.py`
- **_tick_goal()** (13 connections) — `server/scheduler.py`
- **_run_goal_plan()** (11 connections) — `server/scheduler.py`
- **compute_next_run()** (10 connections) — `server/scheduler.py`
- **_finish_goal()** (10 connections) — `server/scheduler.py`
- **_gather_verify_context()** (9 connections) — `server/scheduler.py`
- **_run_loop()** (8 connections) — `server/scheduler.py`
- **_broadcast_goal_progress()** (7 connections) — `server/scheduler.py`
- **work_item_id_for_ref()** (6 connections) — `server/db.py`
- **schedules_update()** (6 connections) — `server/main.py`
- **start()** (6 connections) — `server/scheduler.py`
- **sum_session_cost()** (5 connections) — `server/db.py`
- **latest_task_id()** (5 connections) — `server/db.py`
- **get_latest_job()** (5 connections) — `server/db.py`
- **_session_jsonl_path()** (5 connections) — `server/kanban.py`
- **_extract_recent_text()** (5 connections) — `server/kanban.py`
- **schedules_continue()** (5 connections) — `server/main.py`
- **_team_wrap()** (5 connections) — `server/scheduler.py`
- **_build_goal_prompt()** (5 connections) — `server/scheduler.py`
- **get_goal_iteration_by_no()** (4 connections) — `server/db.py`
- *... and 33 more nodes in this community*

## Relationships

- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (21 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (9 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (7 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (6 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (5 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (5 shared connections)
- [Goal Loop Detail Queries](Goal_Loop_Detail_Queries.md) (4 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (4 shared connections)
- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (4 shared connections)
- [Dispatch Subtask Verdict Verification](Dispatch_Subtask_Verdict_Verification.md) (4 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (3 shared connections)
- [Dispatcher Deterministic Routing](Dispatcher_Deterministic_Routing.md) (2 shared connections)

## Source Files

- `server/db.py`
- `server/kanban.py`
- `server/main.py`
- `server/scheduler.py`

## Audit Trail

- EXTRACTED: 300 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*