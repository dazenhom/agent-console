# Goal Loop 现状说明与两大痛点根因

> 生成日期：2026-07-16 ｜ 只做现状梳理与根因分析，不含代码改动。
> 事实以当前代码为准（README/IMPLEMENTATION 已严重滞后，勿参考）。

---

## 一、Goal Loop 是什么、怎么触发

Goal Loop（目标循环）是「设一次目标，系统自迭代直到达成」的能力。它**不是独立功能模块**，而是寄生在「定时任务（schedules）」体系里的第三种 `kind`：

| kind | 含义 |
|------|------|
| `interval` | 每 N 分钟跑一次 |
| `daily` | 每天 HH:MM 跑一次 |
| **`goal`** | **目标循环：自迭代到达成/触顶** |

### 触发链路（前端 → 接口 → 库 → 调度器）

1. **前端入口**（藏得很深，见痛点 1）：
   `Experimental` Tab → 「管理中心」宫格 → `⏰ 定时任务` 按钮（`web/index.html:211` `#open-schedules-btn`）→ 列表页点 `+ 新建` → 表单里把「触发方式」下拉从默认「每隔一段时间」改成「**目标循环（自迭代直到达成）**」（`web/app.js:2473`）。
   - 目标循环表单额外露出两个字段：**完成标准 `stop_condition`**（自然语言）和 **最大迭代轮数 `max_iterations`**（1–100，默认 10），见 `web/app.js:2482-2487`、`syncKind()` 2493-2499。
   - 提交走 `POST /api/schedules`（`web/app.js:2521`）。

2. **接口层**（`server/main.py`）：
   - `POST /api/schedules`（`main.py:632`）→ `_validate_schedule`（`main.py:587`）校验 `kind in (interval/daily/goal)`；goal 分支要求 `stop_condition` 非空、`max_iterations` 用 `is None` 判空（`main.py:594-601`，规避 `0 or 默认` falsy 陷阱）。
   - goal 创建时 `next_run = compute_next_run("goal", …)`、`goal_status="running"`（`main.py:641-645`）。
   - `PUT /api/schedules/{sid}`（`main.py:650`）：重新启用 goal 会**干净重启循环**——复位 `goal_status=running`、`iter_count=0`、清 `last_feedback`（`main.py:678-684`）。
   - 其余：`GET /api/schedules`、`DELETE /api/schedules/{sid}`。

3. **调度器状态机**（`server/scheduler.py`，后台 asyncio 循环每 30s tick）：
   - `_run_loop`（scheduler.py:50）扫 `db.due_schedules(now)`；`kind=="goal"` 的走 `_tick_goal`（scheduler.py:63-65）。
   - **跨 tick 状态机**（scheduler.py:101 `_tick_goal`），每个 tick 只推进一步、先落库再动作：
     ```
     running   ─[会话空闲 & 未触顶]→ start_turn(迭代prompt), iter_count+1, → producing
     producing ─[会话跑完]→ 先落库 verifying, ensure_future 后台 verify
     verifying ─[后台任务完成]→ DONE→done / CONTINUE→running(存反馈) 或 exhausted
     done/exhausted → 终态, enabled=0
     ```
   - **迭代 prompt** 由 `_build_goal_prompt`（scheduler.py:87）拼：目标 + 完成标准 +（有则）上轮验收反馈 + 轮次提示。
   - **验收（checker）**：`_run_goal_verify`（scheduler.py:158）后台读本轮产出片段，调 `goal_verifier.verify`。验收器（`server/goal_verifier.py`）是**一次性子进程**，用 `CLAUDE_MODEL_KANBAN`（默认 `claude-hy3`，与生产模型分离，maker/checker 独立），首行严格 `== "DONE"` 才算完成，任何不确定一律 CONTINUE。
   - **护栏**：`max_iterations` 上限 + `GOAL_MAX_COST_USD`（默认 5）成本熔断（scheduler.py:135-146）；终态推企微 + 广播 monitor（`_finish_goal` scheduler.py:193）。
   - 关键约束：`hub.start_turn` 是 fire-and-forget，验收必须后台 `ensure_future`，**绝不在 tick 主体 await**（否则阻塞整个调度循环）。

### 状态持久化（SQLite `data/console.db`）

Goal 状态**全部落在 `schedules` 表**，无独立表。相关列（`server/db.py:85-97` 建表 + `db.py:250-254` 迁移补列）：

| 列 | 用途 |
|----|------|
| `kind` | `'goal'` |
| `prompt` | 目标正文 |
| `stop_condition` | 完成标准（自然语言） |
| `max_iterations` | 迭代上限（默认 10） |
| `iter_count` | 已迭代轮数 |
| `goal_status` | `running/producing/verifying/done/exhausted` |
| `last_feedback` | 上轮验收反馈（喂给下轮 prompt） |
| `enabled` / `next_run` / `last_run` / `created_at` | 调度通用列 |

- `db.due_schedules`（db.py:605）：`enabled=1 AND next_run<=now` 出队。
- `db.reconcile_goal_schedules`（db.py:619）：**重启对账**——把卡在 `producing/verifying` 的 goal 复位 `running`，在 lifespan 启动无条件跑（`main.py:36`），防重启后死锁。
- `db.has_active_goal`（db.py:627）：判某会话是否有活跃 goal。
- 一次性子进程（验收/规划/仲裁）另记 `job_runs` 表 + `data/job_logs/<jid>.log`（`server/job_store.py`）。

---

## 二、痛点根因

### 痛点 1：接口不好找（入口埋得深、命名不清）

**根因 A — 物理层级太深（4 层点击）**：
创建一个目标循环要 `Experimental Tab → 管理中心宫格 → ⏰ 定时任务 → + 新建 → 下拉切到「目标循环」`。目标循环是「定时任务」表单里下拉框的**第三个、非默认选项**（`web/app.js:2473`），进表单默认停在「每隔一段时间」，用户不主动展开下拉根本看不到它。

**根因 B — 命名与信息架构错位**：
- 目标循环在语义上是「**派一个会自我推进的目标**」，却被归到「**定时任务/⏰**」里——用户心智里「定时任务」= 闹钟/周期提醒，不会去那里找「自迭代目标」。
- Experimental Tab 图标 `⌬`、命名「实验性/管理中心」，暗示「不稳定、少用」，进一步降低发现率。
- 对比：`背对背仲裁`、`智能分派` 都有**独立顶层入口**（Experimental 宫格 + 会话详情底部 `act-btn`，`index.html:217-218,285-286`），唯独目标循环**没有任何快捷入口**，只能从下拉里翻。

**根因 C — 无上下文触发点**：
在会话详情页（用户下达指令的地方）没有「把这个会话转成目标循环」的按钮；必须离开会话、去 Experimental、新建表单、再从下拉选会话。创建路径与实际工作流断裂。

> 一句话根因：**目标循环没有与其重要性匹配的一级入口，被降级成「定时任务」表单里的一个隐藏下拉项**。

### 痛点 2：智能化不够（loop 只是简单重复，没结合子任务/团队协作/console-dev 流水线）

**根因 A — 单会话原地重复**：
`_tick_goal` 每轮只是对**同一个 session_id** 调 `hub.start_turn(sid, _build_goal_prompt(sch))`（scheduler.py:147-149）。迭代 prompt 只是「目标 + 完成标准 + 上轮反馈 + 第 N 轮」的文本拼接，本质是**在一个会话里反复重发略有变化的指令**，没有任何任务分解或角色分工。

**根因 B — 已有的「智能件」没接进 loop**：
项目里其实已经具备实现智能化的三块能力，但它们是**彼此独立的功能，全都没被目标循环调用**：
- **`dispatcher.py`（🧭 智能分派 / `POST /api/dispatch`）**：能用最强 Claude 把大需求**拆成 ≤8 个子任务**，按确定性规则路由到不同引擎/模型（codex 复核 / opus 深度 / sonnet 开发），并各建隔离子会话开工。—— 这正是「结合子任务」的现成能力，但 loop 从不调用它。
- **`arbiter.py`（⚖ 背对背仲裁 / `POST /api/arbitrate`）**：能让 Claude + Codex 背对背独立作答再综合仲裁。—— 这正是「团队协作/交叉验证」的现成能力，loop 也不用。
- **验收器 `goal_verifier`**：目前只做「DONE/CONTINUE」判定 + 一句文字反馈，**不会把不达标拆成下一步子任务派单**，反馈只是喂回同一会话的 prompt。

**根因 C — 与 console-dev 流水线脱节**：
Loop 内没有「analyst→developer→reviewer→ops」四角流水线，也没有 worktree 隔离多路并行（H1 的 `isolate` 只在建单个会话时生效，loop 迭代不会 fan-out 到多个隔离 worktree）。maker/checker 分离仅停留在「一个生产会话 + 一个验收子进程」，没有把「规划 / 执行 / 复核 / 集成」拆成协作角色。

**根因 D — 反馈闭环浅**：
验收 `reason` 截断到 200 字（goal_verifier.py:75），只作为纯文本追加到下轮 prompt。没有结构化的「失败项 → 具体子任务」映射，模型只能靠自己重读一段模糊反馈，迭代效率低、容易原地打转。

> 一句话根因：**loop 的「一轮」= 单会话重发 prompt + 一次 DONE/CONTINUE 判定；项目已有的分派（dispatcher）、协作仲裁（arbiter）、流水线/worktree 隔离等智能件全部游离在 loop 之外，未被编排进目标循环的每一轮。**

---

## 三、关键文件索引（供后续改造定位）

| 关注点 | 文件:行 |
|--------|---------|
| goal 状态机 / 迭代 prompt / 验收调度 | `server/scheduler.py:87-212` |
| 验收器（checker，独立小模型） | `server/goal_verifier.py` |
| schedules 建表 + goal 列迁移 | `server/db.py:85-97, 250-254` |
| goal 相关 db 方法（due/reconcile/has_active） | `server/db.py:605-631` |
| 接口校验/创建/更新/删除 | `server/main.py:587-693` |
| 重启对账挂载 | `server/main.py:36` |
| 前端入口（定时任务按钮） | `web/index.html:211` |
| 前端表单（下拉第三项 = 目标循环） | `web/app.js:2450-2526` |
| goal 列表渲染/文案 | `web/app.js:2389-2448` |
| **已有但未接入 loop 的智能件** | `server/dispatcher.py`（分派）、`server/arbiter.py`（仲裁） |
| 一次性子进程执行外壳 | `server/job_store.py` |
| goal 相关配置默认值 | `server/config.py:111-116` |
