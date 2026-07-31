# Codex Oneshot Helper & Config

> 31 nodes

## Key Concepts

- **config.py** (31 connections) — `server/config.py`
- **__init__.py** (29 connections) — `server/__init__.py`
- **kanban.py** (13 connections) — `server/kanban.py`
- **run_codex_oneshot_text()** (12 connections) — `server/codex_oneshot.py`
- **goal_summary.py** (12 connections) — `server/goal_summary.py`
- **llms_doc.py** (12 connections) — `server/llms_doc.py`
- **codex_oneshot.py** (10 connections) — `server/codex_oneshot.py`
- **memo_reminder.py** (9 connections) — `server/memo_reminder.py`
- **summarize_goals()** (7 connections) — `server/goal_summary.py`
- **_build_prompt()** (5 connections) — `server/goal_summary.py`
- **summarize_progress()** (5 connections) — `server/kanban.py`
- **knot_runner.py** (5 connections) — `server/knot_runner.py`
- **_title()** (4 connections) — `server/goal_summary.py`
- **_last_reason()** (4 connections) — `server/goal_summary.py`
- **_classify()** (3 connections) — `server/goal_summary.py`
- **goals_summary()** (3 connections) — `server/main.py`
- **_should_remind()** (2 connections) — `server/memo_reminder.py`
- **编排层公共 codex 一次性子进程助手：便宜档机械任务（看板摘要/分诊/目标循环总览/ 行摘要/标题/验收）统一走这里调 tcodex，避免每处重复拼命令 +** (1 connections) — `server/codex_oneshot.py`
- **一次性调用 tcodex exec，返回 (job_id, 拼接后的答案文本, stderr 前200字, status)。      status ∈ suc** (1 connections) — `server/codex_oneshot.py`
- **集中配置：全部可通过环境变量覆盖，方便部署时调整。** (1 connections) — `server/config.py`
- **目标循环总览总结：把所有 goal 循环的完成情况汇总，用一次性子进程生成一段中文小结。  与 kanban.summarize_progress / goal** (1 connections) — `server/goal_summary.py`
- **按 enabled×goal_status 归类：进行中 / 已完成 / 未完成（耗尽）/ 未完成（暂停）。** (1 connections) — `server/goal_summary.py`
- **会话标题优先，prompt 兜底；压平换行截 50 字。** (1 connections) — `server/goal_summary.py`
- **末轮验收反馈：过滤 verdict 非空的轮（去掉进程重启残留的纯 producing 僵尸行），     取 iter_no 最大那条的 feedback，截** (1 connections) — `server/goal_summary.py`
- **汇总所有目标循环并生成小结。返回 (ok, summary)。任何超时/异常都返回 (False, 原因)。** (1 connections) — `server/goal_summary.py`
- *... and 6 more nodes in this community*

## Relationships

- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (13 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (8 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (7 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (7 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (7 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (6 shared connections)
- [Summarizer (Title Gen)](Summarizer_%28Title_Gen%29.md) (6 shared connections)
- [LLMs.txt Skill Index Builder](LLMs.txt_Skill_Index_Builder.md) (6 shared connections)
- [Agent Store CRUD](Agent_Store_CRUD.md) (3 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (3 shared connections)
- [WeCom Notification](WeCom_Notification.md) (3 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (3 shared connections)

## Source Files

- `server/__init__.py`
- `server/codex_oneshot.py`
- `server/config.py`
- `server/goal_summary.py`
- `server/kanban.py`
- `server/knot_runner.py`
- `server/llms_doc.py`
- `server/main.py`
- `server/memo_reminder.py`

## Audit Trail

- EXTRACTED: 180 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*