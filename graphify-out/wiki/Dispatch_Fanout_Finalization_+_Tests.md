# Dispatch Fanout Finalization + Tests

> 14 nodes

## Key Concepts

- **_maybe_finalize_fanout()** (11 connections) — `server/scheduler.py`
- **test_scheduler_finalize.py** (11 connections) — `tests/test_scheduler_finalize.py`
- **_seed_work_item()** (8 connections) — `tests/test_scheduler_finalize.py`
- **get_work_item()** (7 connections) — `server/db.py`
- **_patch_subs()** (6 connections) — `tests/test_scheduler_finalize.py`
- **test_finalize_all_done_marks_work_item_done()** (5 connections) — `tests/test_scheduler_finalize.py`
- **test_finalize_with_failure_marks_exhausted()** (5 connections) — `tests/test_scheduler_finalize.py`
- **test_finalize_with_error_also_exhausted()** (5 connections) — `tests/test_scheduler_finalize.py`
- **test_finalize_skips_when_nonterminal_present()** (5 connections) — `tests/test_scheduler_finalize.py`
- **test_finalize_skips_when_no_subtasks()** (5 connections) — `tests/test_scheduler_finalize.py`
- **test_finalize_empty_plan_id_is_noop()** (2 connections) — `tests/test_scheduler_finalize.py`
- **plan 下所有 subtask 都到终态（done/failed/error）后，聚合收尾对应 work_item：     全 done → done，否则** (1 connections) — `server/scheduler.py`
- **server.scheduler._maybe_finalize_fanout 的聚合收尾逻辑。  用 temp_db 走真实的 work_item 写入路径，** (1 connections) — `tests/test_scheduler_finalize.py`
- **建一条 ref_id=plan_id 的 work_item，返回其 id。** (1 connections) — `tests/test_scheduler_finalize.py`

## Relationships

- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (4 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (2 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (1 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (1 shared connections)
- [Dispatch Subtask Verdict Verification](Dispatch_Subtask_Verdict_Verification.md) (1 shared connections)

## Source Files

- `server/db.py`
- `server/scheduler.py`
- `tests/test_scheduler_finalize.py`

## Audit Trail

- EXTRACTED: 73 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*