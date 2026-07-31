# Arbiter Back-to-Back Arbitration

> 39 nodes

## Key Concepts

- **arbiter.py** (19 connections) — `server/arbiter.py`
- **run_logged_oneshot()** (19 connections) — `server/job_store.py`
- **goal_verifier.py** (12 connections) — `server/goal_verifier.py`
- **job_store.py** (12 connections) — `server/job_store.py`
- **judge()** (12 connections) — `server/verifier.py`
- **set_job_output()** (10 connections) — `server/db.py`
- **run_arbitration()** (9 connections) — `server/arbiter.py`
- **verifier.py** (9 connections) — `server/verifier.py`
- **test_job_store_cwd.py** (9 connections) — `tests/test_job_store_cwd.py`
- **test_verifier.py** (8 connections) — `tests/test_verifier.py`
- **_run_claude_oneshot()** (6 connections) — `server/arbiter.py`
- **verify()** (6 connections) — `server/goal_verifier.py`
- **_run_codex_oneshot()** (5 connections) — `server/arbiter.py`
- **_build_prompt()** (5 connections) — `server/goal_verifier.py`
- **_broadcast_arb_stage()** (3 connections) — `server/arbiter.py`
- **_log_dir()** (3 connections) — `server/job_store.py`
- **_patch_subprocess()** (3 connections) — `tests/test_job_store_cwd.py`
- **test_run_logged_oneshot_passes_cwd()** (3 connections) — `tests/test_job_store_cwd.py`
- **test_run_logged_oneshot_default_cwd_none()** (3 connections) — `tests/test_job_store_cwd.py`
- **_build_arbitration_prompt()** (2 connections) — `server/arbiter.py`
- **test_build_prompt_includes_workdir()** (2 connections) — `tests/test_job_store_cwd.py`
- **test_build_prompt_without_workdir_backward_compatible()** (2 connections) — `tests/test_job_store_cwd.py`
- **test_judge_nl_routes_to_goal_verifier()** (2 connections) — `tests/test_verifier.py`
- **test_judge_candidates_routes_to_arbiter()** (2 connections) — `tests/test_verifier.py`
- **test_judge_unknown_strategy_raises()** (2 connections) — `tests/test_verifier.py`
- *... and 14 more nodes in this community*

## Relationships

- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (13 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (10 shared connections)
- [Dispatch Subtask Verdict Verification](Dispatch_Subtask_Verdict_Verification.md) (8 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (4 shared connections)
- [Context Compactor](Context_Compactor.md) (4 shared connections)
- [Dispatcher Deterministic Routing](Dispatcher_Deterministic_Routing.md) (4 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (2 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (1 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (1 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (1 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (1 shared connections)
- [Fake Subprocess Test Helper](Fake_Subprocess_Test_Helper.md) (1 shared connections)

## Source Files

- `server/arbiter.py`
- `server/db.py`
- `server/goal_verifier.py`
- `server/job_store.py`
- `server/verifier.py`
- `tests/test_job_store_cwd.py`
- `tests/test_verifier.py`

## Audit Trail

- EXTRACTED: 181 (99%)
- INFERRED: 1 (1%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*