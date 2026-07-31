# Goal Loop Detail Queries

> 10 nodes

## Key Concepts

- **list_goal_iterations()** (6 connections) — `server/db.py`
- **get_goal_loop_detail()** (6 connections) — `server/db.py`
- **list_goal_subtasks()** (5 connections) — `server/db.py`
- **schedules_iterations()** (4 connections) — `server/main.py`
- **schedules_subtasks()** (4 connections) — `server/main.py`
- **goals_detail()** (2 connections) — `server/main.py`
- **某个目标循环的完整轮次历史（详情态用），按轮次升序，附带该轮回合的耗时/花费/状态。** (1 connections) — `server/db.py`
- **目标循环详情态：schedule 本体 + 完整轮次历史 + (planned 模式) 子任务清单。** (1 connections) — `server/db.py`
- **某个目标循环的每轮迭代历史（含验收判定/反馈/产出摘要），只读。** (1 connections) — `server/main.py`
- **planned 目标循环拆解出的子任务清单及各自执行/验收状态，只读。** (1 connections) — `server/main.py`

## Relationships

- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (4 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (3 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (3 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (2 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (1 shared connections)

## Source Files

- `server/db.py`
- `server/main.py`

## Audit Trail

- EXTRACTED: 31 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*