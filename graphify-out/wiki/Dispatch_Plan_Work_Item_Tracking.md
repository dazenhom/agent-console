# Dispatch Plan Work Item Tracking

> 23 nodes

## Key Concepts

- **dispatch()** (10 connections) — `server/dispatcher.py`
- **retry_subtask()** (9 connections) — `server/dispatcher.py`
- **_tick_fanout()** (9 connections) — `server/scheduler.py`
- **list_dispatch_subtasks()** (7 connections) — `server/db.py`
- **update_dispatch_subtask()** (7 connections) — `server/db.py`
- **create_work_item_safe()** (7 connections) — `server/db.py`
- **_fire_subtask()** (6 connections) — `server/dispatcher.py`
- **schedules_create()** (6 connections) — `server/main.py`
- **get_latest_task()** (4 connections) — `server/db.py`
- **list_active_dispatch_plans()** (4 connections) — `server/db.py`
- **post_dispatch()** (4 connections) — `server/main.py`
- **post_dispatch_subtask_retry()** (4 connections) — `server/main.py`
- **_validate_schedule()** (4 connections) — `server/main.py`
- **get_dispatch_plan()** (3 connections) — `server/main.py`
- **该会话最近一条 tasks 记录（含 status），用于判断回合是否真正结束。     fire-and-forget 的 start_turn 从写 sta** (1 connections) — `server/db.py`
- **还有未终结子任务（status in dispatched/verifying）的 plan_id 列表，     供调度 tick 发现待推进的 dispat** (1 connections) — `server/db.py`
- **create_work_item 的容错包装：work_items 纯观测，写失败绝不影响主流程——     异常只记日志并返回空串。四处发起点统一走这里，免得** (1 connections) — `server/db.py`
- **后台跑 start_turn 并兜住其执行期异常：不然会被 asyncio 打成     "Task exception was never retrieved** (1 connections) — `server/dispatcher.py`
- **规划 → 路由 → 建子会话 → 后台开工。返回 plan_id。      单个子任务建立失败记 status=error 跳过，不中断整个循环；子会话回合用** (1 connections) — `server/dispatcher.py`
- **重派一个已判定失败（failed/error）的 dispatch 子任务：新建一个隔离/共享子会话跑同样的     指令，把子任务状态重置回 dispatch** (1 connections) — `server/dispatcher.py`
- **重派一个失败的 dispatch 子任务：新起子会话跑同样内容，状态重置回 dispatched，     交回 _tick_fanout 自动接管判定。仅对终** (1 connections) — `server/main.py`
- **校验并归一化定时任务参数。返回 {kind, interval_min, at_hhmm[, stop_condition, max_iterations]}。** (1 connections) — `server/main.py`
- **扫描所有还有未终结子任务的 dispatch plan，逐个推进子任务状态。每 tick 只推进一步。     转换先写库再动作（先落 verifying 再起** (1 connections) — `server/scheduler.py`

## Relationships

- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (14 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (6 shared connections)
- [Dispatch Subtask Verdict Verification](Dispatch_Subtask_Verdict_Verification.md) (5 shared connections)
- [Dispatcher Deterministic Routing](Dispatcher_Deterministic_Routing.md) (5 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (4 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (3 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (3 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (2 shared connections)
- [Dispatch Fanout Finalization + Tests](Dispatch_Fanout_Finalization_%2B_Tests.md) (1 shared connections)
- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (1 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (1 shared connections)

## Source Files

- `server/db.py`
- `server/dispatcher.py`
- `server/main.py`
- `server/scheduler.py`

## Audit Trail

- EXTRACTED: 93 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*