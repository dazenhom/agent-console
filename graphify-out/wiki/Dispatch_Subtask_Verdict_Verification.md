# Dispatch Subtask Verdict Verification

> 33 nodes

## Key Concepts

- **test_dispatch_arbitration.py** (21 connections) — `tests/test_dispatch_arbitration.py`
- **get_dispatch_subtask()** (13 connections) — `server/db.py`
- **_run_dispatch_verify()** (12 connections) — `server/scheduler.py`
- **verify_back_to_back()** (10 connections) — `server/arbiter.py`
- **test_scheduler_fanout.py** (10 connections) — `tests/test_scheduler_fanout.py`
- **_seed_subtask()** (8 connections) — `tests/test_scheduler_fanout.py`
- **_seed_verifying_subtask()** (7 connections) — `tests/test_dispatch_arbitration.py`
- **_run_tick()** (7 connections) — `tests/test_scheduler_fanout.py`
- **_stub_judges()** (6 connections) — `tests/test_dispatch_arbitration.py`
- **_run_b2b()** (6 connections) — `tests/test_dispatch_arbitration.py`
- **test_tick_advances_when_task_terminal_and_idle()** (6 connections) — `tests/test_scheduler_fanout.py`
- **test_tick_skips_when_child_still_running()** (5 connections) — `tests/test_scheduler_fanout.py`
- **test_tick_skips_when_latest_task_running()** (5 connections) — `tests/test_scheduler_fanout.py`
- **_parse_verdict()** (4 connections) — `server/arbiter.py`
- **test_verify_routes_to_arbiter_when_need_arbitration()** (4 connections) — `tests/test_dispatch_arbitration.py`
- **test_verify_routes_to_judge_when_not_need_arbitration()** (4 connections) — `tests/test_dispatch_arbitration.py`
- **test_tick_skips_when_no_task_yet()** (4 connections) — `tests/test_scheduler_fanout.py`
- **test_b2b_both_done()** (3 connections) — `tests/test_dispatch_arbitration.py`
- **test_b2b_one_continue_fails()** (3 connections) — `tests/test_dispatch_arbitration.py`
- **test_b2b_one_exception_treated_as_continue()** (3 connections) — `tests/test_dispatch_arbitration.py`
- **test_b2b_empty_output_treated_as_continue()** (3 connections) — `tests/test_dispatch_arbitration.py`
- **test_parse_verdict_rules()** (2 connections) — `tests/test_dispatch_arbitration.py`
- **test_b2b_workdir_into_prompt_and_cwd()** (2 connections) — `tests/test_dispatch_arbitration.py`
- **test_b2b_no_workdir_backward_compatible()** (2 connections) — `tests/test_dispatch_arbitration.py`
- **把一路评委的原始答复解析成 (done, reason)。解析规则与 goal_verifier.verify 一致：     取首个非空行精确匹配 == "D** (1 connections) — `server/arbiter.py`
- *... and 8 more nodes in this community*

## Relationships

- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (16 shared connections)
- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (8 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (5 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (4 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (1 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (1 shared connections)
- [Dispatch Fanout Finalization + Tests](Dispatch_Fanout_Finalization_%2B_Tests.md) (1 shared connections)
- [Dispatcher Deterministic Routing](Dispatcher_Deterministic_Routing.md) (1 shared connections)
- [Git Worktree Management](Git_Worktree_Management.md) (1 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (1 shared connections)

## Source Files

- `server/arbiter.py`
- `server/db.py`
- `server/scheduler.py`
- `tests/test_dispatch_arbitration.py`
- `tests/test_scheduler_fanout.py`

## Audit Trail

- EXTRACTED: 159 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*