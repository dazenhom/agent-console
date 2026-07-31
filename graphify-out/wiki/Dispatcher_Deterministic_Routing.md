# Dispatcher Deterministic Routing

> 9 nodes

## Key Concepts

- **dispatcher.py** (19 connections) — `server/dispatcher.py`
- **run_planner()** (7 connections) — `server/dispatcher.py`
- **_capabilities_block()** (5 connections) — `server/dispatcher.py`
- **_build_planner_prompt()** (3 connections) — `server/dispatcher.py`
- **_route()** (3 connections) — `server/dispatcher.py`
- **角色化动态调度（Dispatcher）：把一个大需求拆成若干子任务，按确定性规则路由到 最合适的引擎/模型，并为每个子任务建隔离子会话后台开工。  与 tria** (1 connections) — `server/dispatcher.py`
- **列出已有 skills（编排流水线）与 subagents，供 planner 拆任务时复用。     都为空则返回空串。每段最多 20 条，descripti** (1 connections) — `server/dispatcher.py`
- **一次性子进程跑规划，返回校验后的子任务列表。失败/超时/解析不到 → []。** (1 connections) — `server/dispatcher.py`
- **确定性路由：返回 (engine, model, category)。按 R1>R2>R3>R4 顺序，命中即止。** (1 connections) — `server/dispatcher.py`

## Relationships

- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (5 shared connections)
- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (4 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (2 shared connections)
- [Agent Store CRUD](Agent_Store_CRUD.md) (2 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (2 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (2 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (1 shared connections)
- [Git Worktree Management](Git_Worktree_Management.md) (1 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (1 shared connections)
- [Dispatch Subtask Verdict Verification](Dispatch_Subtask_Verdict_Verification.md) (1 shared connections)

## Source Files

- `server/dispatcher.py`

## Audit Trail

- EXTRACTED: 41 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*