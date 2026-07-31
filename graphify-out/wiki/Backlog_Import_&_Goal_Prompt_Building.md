# Backlog Import & Goal Prompt Building

> 44 nodes

## Key Concepts

- **triage.py** (21 connections) — `server/triage.py`
- **run_report()** (14 connections) — `server/secretary.py`
- **backlog_import.py** (13 connections) — `server/backlog_import.py`
- **secretary.py** (13 connections) — `server/secretary.py`
- **scan_backlog()** (10 connections) — `server/backlog_import.py`
- **dispatch_triage()** (9 connections) — `server/triage.py`
- **_build_and_start()** (9 connections) — `server/triage.py`
- **_auto_dispatch_one()** (9 connections) — `server/triage.py`
- **_dispatch_existing_todo()** (7 connections) — `server/triage.py`
- **run_triage()** (6 connections) — `server/triage.py`
- **_build_payload()** (5 connections) — `server/backlog_import.py`
- **_dup_triage_title()** (5 connections) — `server/triage.py`
- **_load_existing()** (4 connections) — `server/backlog_import.py`
- **gather_day_data()** (4 connections) — `server/secretary.py`
- **build_evening_prompt()** (4 connections) — `server/secretary.py`
- **build_morning_prompt()** (4 connections) — `server/secretary.py`
- **parse_backlog()** (3 connections) — `server/backlog_import.py`
- **_run_async()** (3 connections) — `server/backlog_import.py`
- **triage_dispatch()** (3 connections) — `server/main.py`
- **_format_sessions()** (3 connections) — `server/secretary.py`
- **_format_todos()** (3 connections) — `server/secretary.py`
- **_today_start_ts()** (3 connections) — `server/triage.py`
- **_build_triage_prompt()** (3 connections) — `server/triage.py`
- **_to_inbox()** (3 connections) — `server/triage.py`
- **_content_hash()** (2 connections) — `server/backlog_import.py`
- *... and 19 more nodes in this community*

## Relationships

- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (12 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (10 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (8 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (6 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (5 shared connections)
- [WeCom Notification](WeCom_Notification.md) (2 shared connections)
- [Git Worktree Management](Git_Worktree_Management.md) (2 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (1 shared connections)
- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (1 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (1 shared connections)

## Source Files

- `server/backlog_import.py`
- `server/main.py`
- `server/secretary.py`
- `server/triage.py`

## Audit Trail

- EXTRACTED: 182 (98%)
- INFERRED: 4 (2%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*