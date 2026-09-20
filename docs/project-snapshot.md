# 项目现状规模速查（会过时，精确值以现查为准）

- **后端模块**：数十个（2026-09-10 核对为 36 个 .py），除 session_hub.py/claude_runner.py/codex_runner.py/db.py/main.py 外，还有委派编排相关（dispatcher.py、arbiter.py、triage.py、scheduler.py、goal_verifier.py、goal_summary.py、verifier.py）、隔离与子进程相关（worktree.py、job_store.py、codex_oneshot.py、agent_provider.py——provider 统一契约基类）、其余（summarizer.py、skill_store.py、knot_runner.py、kanban.py、secretary.py、asr_client.py、fs_util.py、memory_store.py、memo_reminder.py、agent_store.py、session_import.py、wecom_notify.py、backlog_import.py、compactor.py、llms_doc.py、logging_util.py、healthcheck.py、config.py）。此清单可能不全，新增模块以现查为准。
- **REST 路由**：上百个（2026-09-10 核对为 100+ 装饰器），功能域：会话管理、消息队列、任务看板、Memory 记忆库、Subagent 管理（`/api/agents*`）、委派编排（`/api/dispatch`）、仲裁（`/api/arbitrate`）、job 运行记录（`/api/jobs`）、快捷指令、会话导入、定时任务、待办清单/智能看板、备忘录、日报/秘书、文件预览、上传、语音识别（/api/asr）
- **WebSocket**：`/ws`（会话流式）+ `/ws/monitor`（全局监控通道）
- **数据库**：17 张表（sessions、messages、tasks、snippets、schedules、todos、todo_sessions、reports、queue_items、memos、artifacts、job_runs、arbitrations、dispatch_subtasks、goal_iterations、goal_subtasks、work_items）
- **前端**：5 个顶部 Tab（Overview 调度台、Sessions 会话、New 新建/接续、Review、Experimental）；app.js 行数以 `wc -l web/app.js` 现查为准（2026-09-10 约 6825 行，持续增长中）
