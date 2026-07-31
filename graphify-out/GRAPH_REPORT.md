# Graph Report - agent-console  (2026-07-31)

## Corpus Check
- 70 files · ~75,587 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1172 nodes · 2781 edges · 68 communities (55 shown, 13 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 18 edges (avg confidence: 0.64)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `3785f9ed`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Frontend Chat UI
- REST API Routes (Todos/Memos/Queue)
- Goal Loop Iteration Queries
- DB Query Helpers
- DB Write/Insert Helpers
- WebSocket Session Monitoring
- Backlog Import & Goal Prompt Building
- Arbiter Back-to-Back Arbitration
- Frontend Agent/Artifact UI
- ClaudeRunner Process Management
- Console-Dev Agent Pipeline
- Env Config & run.sh
- Dispatch Subtask Verdict Verification
- AgentProvider Abstract Base
- Codex Oneshot Helper & Config
- Frontend Todo/Memo Actions
- Skill Frontmatter & Safe Path Utils
- Frontend Message Stream Rendering
- Frontend Session List Management
- Dispatch Plan Work Item Tracking
- Git Worktree Management
- Agent Store CRUD
- Memory Store CRUD
- Session-Todo Linking
- Session Import
- Frontend Monitor WS Client
- Logger Protocol Design
- LLMs.txt Skill Index Builder
- Frontend Goal Loop Cards
- Frontend Arbitration View
- ASR Audio Decoding
- CodexRunner Process Management
- Dispatch Fanout Finalization + Tests
- Summarizer (Title Gen)
- YAML Frontmatter Utils
- WeCom Notification
- Work Items CRUD Tests
- PWA Manifest
- Goal Loop Detail Queries
- Test Fixtures (temp DB)
- Context Compactor
- Dispatcher Deterministic Routing
- KnotRunner (unwired experimental)
- Frontend Dispatch View
- Task DB Tests
- Healthcheck Script
- Queue Item CRUD
- Fake Subprocess Test Helper
- agent-tunnel.sh Script
- Goal Loops List Query
- watchdog.sh Script
- Message Parent Migration Script
- restart.sh Script
- preview_html
- _goal_protected_uploads
- list_todos
- list_work_items
- search_message_sessions
- refresh_todo_progress
- delete_todo
- asr
- get_audio
- get_config
- llms_full_txt
- llms_txt
- _swanlab_sid_warmup

## God Nodes (most connected - your core abstractions)
1. `_query()` - 71 edges
2. `api()` - 61 edges
3. `toast()` - 57 edges
4. `escapeHtml()` - 55 edges
5. `el()` - 51 edges
6. `_exec()` - 43 edges
7. `_now()` - 31 edges
8. `SessionHub` - 29 edges
9. `get_session()` - 26 edges
10. `new_id()` - 24 edges

## Surprising Connections (you probably didn't know these)
- `Agent Console App 图标（深色底+对话框+对勾折线）` --conceptually_related_to--> `README.md：Agent Console 项目总览`  [INFERRED]
  web/icon.svg → README.md
- `痛点1：目标循环入口埋得深/命名错位` --references--> `#goal-loop-btn 目标循环 Hero 入口按钮`  [AMBIGUOUS]
  docs/GOAL_LOOP_现状与痛点.md → web/index.html
- `test_retry_preserves_need_arbitration()` --calls--> `retry_subtask()`  [EXTRACTED]
  tests/test_dispatch_arbitration.py → server/dispatcher.py
- `swanlab_upload()` --calls--> `_child_env()`  [INFERRED]
  server/main.py → server/agent_provider.py
- `test_b2b_no_workdir_backward_compatible()` --calls--> `verify_back_to_back()`  [EXTRACTED]
  tests/test_dispatch_arbitration.py → server/arbiter.py

## Import Cycles
- 3-file cycle: `server/scheduler.py -> server/secretary.py -> server/triage.py -> server/scheduler.py`

## Hyperedges (group relationships)
- **analyst→developer→reviewer→ops 四角流水线模式** — claude_agents_analyst, claude_agents_developer, claude_agents_reviewer, claude_agents_ops, claude_skills_console_dev_skill, claude_skills_devops_team_skill, claude_skills_quant_dev_skill [EXTRACTED 1.00]
- **目标循环未接入的智能件（dispatcher/arbiter/goal_verifier/scheduler）** — docs_goal_loop_status_pain_points_scheduler, docs_goal_loop_status_pain_points_dispatcher, docs_goal_loop_status_pain_points_arbiter, docs_goal_loop_status_pain_points_goal_verifier [EXTRACTED 1.00]

## Communities (68 total, 13 thin omitted)

### Community 0 - "Frontend Chat UI"
Cohesion: 0.06
Nodes (56): appendToToolGroup(), clearChatHighlights(), closeDrawer(), cssEscape(), ensureTopBtn(), fillRecognized(), firstMatchIdx(), fmtSchedule() (+48 more)

### Community 1 - "REST API Routes (Todos/Memos/Queue)"
Cohesion: 0.07
Nodes (25): FastAPI, _asset_version(), get_sessions(), index(), memory_create(), memory_delete(), memory_get(), memory_list() (+17 more)

### Community 2 - "Goal Loop Iteration Queries"
Cohesion: 0.05
Nodes (67): get_goal_iteration_by_no(), get_goal_loop_detail(), get_latest_job(), get_schedule(), latest_task_id(), list_goal_iterations(), list_goal_subtasks(), 该会话最近一条 tasks 记录的 id，用于把刚发起的迭代回合关联进本轮历史。 (+59 more)

### Community 3 - "DB Query Helpers"
Cohesion: 0.06
Nodes (51): active_goal_prompts(), count_messages(), count_triage_dispatched_today(), count_user_messages(), create_report(), due_schedules(), get_goal_subtask(), get_latest_task() (+43 more)

### Community 4 - "DB Write/Insert Helpers"
Cohesion: 0.10
Nodes (38): add_message(), create_artifact(), create_goal_iteration(), create_memo(), create_schedule(), create_session(), create_snippet(), create_triage_todo() (+30 more)

### Community 5 - "WebSocket Session Monitoring"
Cohesion: 0.07
Nodes (21): NoCacheStatic, 一条 WS 连接的轻封装。send 为发送回调；hidden 表示该客户端是否在后台（锁屏/切走）。, 给监控订阅者推一条会话状态变化，驱动前端列表实时刷新。, 该会话是否有「处于前台」的订阅者。无 → 回合结束发企业微信。, 回合结束后自动刷该会话关联的 in_progress 看板进展，并推 monitor。, 发起一个回合。已在跑则拒绝（同会话内串行）。返回是否成功发起。, 展开 skill 后开始回合。返回 'started' 或 'error:..., 在跑则入队，空闲则直接开跑。返回 'queued'/'started'/'error:... (+13 more)

### Community 6 - "Backlog Import & Goal Prompt Building"
Cohesion: 0.09
Nodes (30): date, secretary_trigger(), build_evening_prompt(), build_morning_prompt(), _format_sessions(), _format_todos(), gather_day_data(), run_report() (+22 more)

### Community 7 - "Arbiter Back-to-Back Arbitration"
Cohesion: 0.06
Nodes (47): _broadcast_arb_stage(), _build_arbitration_prompt(), _parse_verdict(), 背对背双执行 + 综合仲裁。  同一问题背对背交给两位"工程师"独立作答——A 走 Claude（tclaude），B 走 Codex（tcodex）—— 互不, 向 monitor 通道推一条仲裁阶段进展（fire-and-forget，异常吞掉绝不影响仲裁主流程，     写法与 scheduler._broadcas, 一次性调用 tclaude，返回 (job_id, 答案文本)。解析方式与 goal_verifier.verify 一致：     从 stdout 逐行挑出, 把一路评委的原始答复解析成 (done, reason)。解析规则与 goal_verifier.verify 一致：     取首个非空行精确匹配 == "D, 困难 dispatch 子任务的背对背双评委验收：复用 goal_verifier 的验收 prompt，交给两位     评委独立判定——A 走更强的 Cla (+39 more)

### Community 8 - "Frontend Agent/Artifact UI"
Cohesion: 0.14
Nodes (37): agentFormHtml(), attachArtifacts(), confirmDialog(), doJumpToSession(), el(), escapeAttr(), escapeHtml(), fileUrl() (+29 more)

### Community 9 - "ClaudeRunner Process Management"
Cohesion: 0.08
Nodes (18): ClaudeRunner, LoopDetector, EventCallback, Process, 组 tclaude 命令。stream_input=True 时走常驻 stream-json 输入（message 走 stdin）。, 预热常驻进程：进程已存活则返回 'running'，否则 spawn 并返回 'warmed'。, 中断当前回合。常驻模式：杀进程（下回合自动 resume 续上）；老模式：杀进程组。, 跑一个回合。返回 {claude_session_id, returncode, error}。          model: 传给 `tclaude -- (+10 more)

### Community 10 - "Console-Dev Agent Pipeline"
Cohesion: 0.10
Nodes (35): Analyst Agent (需求拆解/根因定位), Developer Agent (调度 tcodex 实现代码), tcodex (Codex CLI wrapper, gpt-5.6-sol), Ops Agent (只读部署验证/不改代码), Reviewer Agent (审查改动/不改代码), console-dev skill (agent-console 自身开发四角流水线), devops-team skill (通用四角流水线), quant-dev skill (quant-recommender 四角流水线) (+27 more)

### Community 11 - "Env Config & run.sh"
Cohesion: 0.06
Nodes (31): AGENT_WORKDIR, ASR2_API_KEY, ASR2_BASE_URLS, ASR2_MODEL, ASR2_TIMEOUT, AUTH_TOKEN, CLAUDE_BIN, CLAUDE_DEFAULT_MODE (+23 more)

### Community 12 - "Dispatch Subtask Verdict Verification"
Cohesion: 0.17
Nodes (21): create_dispatch_subtask(), get_dispatch_subtask(), start_task(), update_dispatch_subtask(), 后台判定单个 dispatch 子任务是否完成，是唯一把 status 从 verifying 推向 done/failed 的地方。     模式完全参考 _, _run_dispatch_verify(), 建一条 verifying 子任务，指向一个极简子会话（无 claude_session_id / 非 worktree，     使 produced/git, _seed_verifying_subtask() (+13 more)

### Community 13 - "AgentProvider Abstract Base"
Cohesion: 0.12
Nodes (16): ABC, AgentProvider, _child_env(), is_sleep_command(), _kill_process_group(), EventCallback, Process, Agent provider 基础契约：on_event 接收 type 为 assistant、携带 tool_result 的 user、result 或 (+8 more)

### Community 14 - "Codex Oneshot Helper & Config"
Cohesion: 0.08
Nodes (31): 一次性调用 tcodex exec，返回 (job_id, 拼接后的答案文本, stderr 前200字, status)。      status ∈ suc, run_codex_oneshot_text(), list_goal_loops(), 目标循环历史列表态：所有 kind=goal 的 schedule，附带已记录的轮次数。, _build_prompt(), _classify(), _last_reason(), 目标循环总览总结：把所有 goal 循环的完成情况汇总，用一次性子进程生成一段中文小结。  与 kanban.summarize_progress / goal (+23 more)

### Community 15 - "Frontend Todo/Memo Actions"
Cohesion: 0.11
Nodes (43): api(), archiveTodo(), bindSessionChips(), clearMemoBadge(), createSession(), deleteSession(), deleteTodo(), enterApp() (+35 more)

### Community 16 - "Skill Frontmatter & Safe Path Utils"
Cohesion: 0.13
Nodes (27): dump_frontmatter(), _fmt_val(), _needs_quote(), parse_frontmatter(), _parse_simple_yaml(), 文件系统工具：路径安全 + 轻量 frontmatter 解析/序列化。  memory_store / agent_store 共用。刻意不引入 PyYAML, 把 (meta, body) 序列化回带 frontmatter 的文本。      保留传入 meta 的全部字段（含 tclaude 写入的 originS, 把 name 安全地拼到 base 下，防止 ../ 越界。      双重防御：① 字符白名单拒绝任何分隔符/点路径；② resolve() 后用     i (+19 more)

### Community 17 - "Frontend Message Stream Rendering"
Cohesion: 0.14
Nodes (27): appendDelta(), appendMessageGrouped(), appendMsgWithPreview(), appendSubagentDelta(), buildMessageNode(), clearStream(), copyText(), extractQuickReplies() (+19 more)

### Community 18 - "Frontend Session List Management"
Cohesion: 0.23
Nodes (16): applySessionSearch(), deriveState(), fillList(), fillListGrouped(), handleMonitorMessage(), patchSessionRow(), refreshDispatchGroupSummary(), renderArchivedSessionList() (+8 more)

### Community 19 - "Dispatch Plan Work Item Tracking"
Cohesion: 0.50
Nodes (4): list_dispatch_subtasks(), get_arbitration(), get_dispatch_plan(), get_work_item()

### Community 20 - "Git Worktree Management"
Cohesion: 0.08
Nodes (38): _build_planner_prompt(), _capabilities_block(), dispatch(), _fire_subtask(), 角色化动态调度（Dispatcher）：把一个大需求拆成若干子任务，按确定性规则路由到 最合适的引擎/模型，并为每个子任务建隔离子会话后台开工。  与 tria, 确定性路由：返回 (engine, model, category)。按 R1>R2>R3>R4 顺序，命中即止。, 后台跑 start_turn 并兜住其执行期异常：不然会被 asyncio 打成     "Task exception was never retrieved, 规划 → 路由 → 建子会话 → 后台开工。返回 plan_id。      单个子任务建立失败记 status=error 跳过，不中断整个循环；子会话回合用 (+30 more)

### Community 21 - "Agent Store CRUD"
Cohesion: 0.17
Nodes (21): _agents_dir(), agents_json(), _build_meta(), create_agent(), delete_agent(), get_agent(), list_agents(), _norm_tools() (+13 more)

### Community 22 - "Memory Store CRUD"
Cohesion: 0.24
Nodes (16): create_memory(), delete_memory(), get_memory(), list_memories(), _memory_dir(), _path(), Path, Memory 管理：读写 tclaude 的 project memory 文件。  tclaude 把记忆按 cwd（workdir）分项目存到   <TCL (+8 more)

### Community 23 - "Session-Todo Linking"
Cohesion: 0.22
Nodes (14): get_session(), list_messages(), update_session(), archive_session(), compact_session(), get_messages(), 真实上下文压缩：把整段对话概括成摘要并重置会话，摘要作前缀注入下一条消息续接。, resume_session() (+6 more)

### Community 24 - "Session Import"
Cohesion: 0.21
Nodes (14): import_do(), import_list(), _first_user_text(), import_sessions(), latest_ai_title(), list_importable(), _project_dir(), Path (+6 more)

### Community 25 - "Frontend Monitor WS Client"
Cohesion: 0.15
Nodes (17): attachmentLines(), clearPendingImages(), closeMonitor(), closeWs(), connectMonitor(), connectWs(), dangerHit(), logout() (+9 more)

### Community 26 - "Logger Protocol Design"
Cohesion: 0.14
Nodes (9): Protocol, configure(), Logger, LoggerProvider, 日志器接口：仅列出本项目实际使用的方法（stdlib Logger 已满足此结构）。, logger 来源抽象：给定名称返回一个 Logger。, 默认实现：直接透传 stdlib logging.getLogger，与改造前行为完全一致。, 初始化装配点：安装全局 LoggerProvider。      传 None 时安装默认的 `StdLoggerProvider`（幂等，与改造前一致）；传入 (+1 more)

### Community 27 - "LLMs.txt Skill Index Builder"
Cohesion: 0.20
Nodes (13): _agent_files(), _build_index(), build_llms_full_txt(), build_llms_txt(), llms.txt / llms-full.txt：给外部 agent 摄取的项目文本索引。  遵循 llms.txt 约定（https://llmstxt.or, [(subagent名, .md 全文)]，按名排序。, 精选文本索引，末尾指向 /llms-full.txt。, 索引 + 每个 skill / subagent 定义全文，拼成单一 markdown。 (+5 more)

### Community 28 - "Frontend Goal Loop Cards"
Cohesion: 0.25
Nodes (15): buildGoalCard(), closeGoalView(), fmtGoalStatus(), genGoalSummary(), goalGroup(), goalPhaseText(), loadGoalDetail(), openGoalContinueForm() (+7 more)

### Community 29 - "Frontend Arbitration View"
Cohesion: 0.18
Nodes (15): closeArbView(), fmtRelTime(), fmtTime(), fmtWorkItemStatus(), openArbDetail(), openArbitrationInput(), openArbView(), openWorkItemDetail() (+7 more)

### Community 30 - "ASR Audio Decoding"
Cohesion: 0.23
Nodes (13): RuntimeError, ASRError, decode_to_wav16k(), _decode_with_pyav(), _next_base(), _post_once(), ASR 客户端：把上传音频解码成 16k 单声道 WAV，再发给 HY ContextASR 服务转写。  协议：OpenAI 多模态 Chat Complet, 对 base64 的 WAV 调 ASR，返回识别文本。失败抛 ASRError。      多端点 round-robin；可重试错误（5xx/网络）退避重试 (+5 more)

### Community 31 - "CodexRunner Process Management"
Cohesion: 0.18
Nodes (6): CodexRunner, EventCallback, Process, 组 tcodex exec 命令。prompt 作为最后一个位置参数。, 中断当前回合：杀整个进程组（下回合自动 resume 续上）。, 跑一个回合。返回 {claude_session_id, returncode, error, cancelled}。          on_permissi

### Community 32 - "Dispatch Fanout Finalization + Tests"
Cohesion: 0.35
Nodes (13): get_work_item(), _maybe_finalize_fanout(), plan 下所有 subtask 都到终态（done/failed/error）后，聚合收尾对应 work_item：     全 done → done，否则, _patch_subs(), server.scheduler._maybe_finalize_fanout 的聚合收尾逻辑。  用 temp_db 走真实的 work_item 写入路径，, 建一条 ref_id=plan_id 的 work_item，返回其 id。, _seed_work_item(), test_finalize_all_done_marks_work_item_done() (+5 more)

### Community 33 - "Summarizer (Title Gen)"
Cohesion: 0.20
Nodes (14): _build_goal_prompt(), _build_payload(), _build_stop_condition(), _content_hash(), _load_existing(), parse_backlog(), 把 quant-recommender 的 mandatory-backlog.md 接入 triage 收件箱。  与 triage.py 一脉相承：解析出的, triage_payload 结构对齐 triage 现状约定，供 _dispatch_existing_todo 消费。 (+6 more)

### Community 34 - "YAML Frontmatter Utils"
Cohesion: 0.29
Nodes (7): Path, 把 relpath（可含多级子目录）安全地解析到 base 之下，防 ../ 越界。      与 safe_join 区别：允许目录分隔符和多级路径（产物预览, safe_path_under(), get_file(), 读取文件。图片允许任意绝对路径（模型可输出工作目录外的图片路径）；文本仍限 workdir 内。, 收 base64 图片存到项目固定目录 UPLOAD_DIR 下，返回绝对路径供注入消息。      body: {"session_id": "...", ", upload_image()

### Community 35 - "WeCom Notification"
Cohesion: 0.10
Nodes (19): 编排层公共 codex 一次性子进程助手：便宜档机械任务（看板摘要/分诊/目标循环总览/ 行摘要/标题/验收）统一走这里调 tcodex，避免每处重复拼命令 +, 集中配置：全部可通过环境变量覆盖，方便部署时调整。, 一次性子进程运行记录：起子进程 + 落库 + 落盘日志。  triage.run_triage / goal_verifier.verify / kanban., _extract_recent_text(), Path, 看板进展总结：读取会话 jsonl，用一次性子进程概括开发进展。  与 summarizer 一脉相承：独立的一次性子进程，绝不碰会话的常驻上下文； 走 cod, workdir → tclaude 项目目录 slug（与 session_import._slug 一致：/ _ . → -）。, 读 jsonl 最后若干条 user/assistant 文本消息，拼成上下文。 (+11 more)

### Community 37 - "PWA Manifest"
Cohesion: 0.18
Nodes (10): background_color, description, display, icons, name, orientation, scope, short_name (+2 more)

### Community 38 - "Goal Loop Detail Queries"
Cohesion: 0.15
Nodes (14): create_todo(), delete_session(), 返回关联了该会话的【未归档】看板任务标题列表（归档任务不锁定会话，避免死锁）。, 覆盖式设置任务关联会话；同步把主会话（列表第一个）写回 todos.session_id。, set_todo_sessions(), todos_linked_to_session(), remove_session(), todos_create() (+6 more)

### Community 39 - "Test Fixtures (temp DB)"
Cohesion: 0.20
Nodes (9): git_repo(), non_git_dir(), 测试公共 fixture。  核心约束：所有涉及 DB 的测试都必须落到临时库，绝不碰生产的 data/console.db。 做法是把 config.DB_P, 把 SQLite 落到临时文件并初始化，测试结束随 tmp_path 一起清理。, 把 worktree 落地根指向临时目录，避免污染生产 data/worktrees。, 建一个带一次初始提交的真实 git 仓库，供隔离成功路径使用。, 一个普通空目录（没有 git init），用于验证降级路径。, temp_db() (+1 more)

### Community 40 - "Context Compactor"
Cohesion: 0.20
Nodes (11): list_queue(), check_token(), login(), queue_list(), 监控通道：推送所有会话的状态/活动变化，驱动前端会话列表实时刷新。, 鉴权：优先 Authorization header，其次 query 参数 token。     用于 <img src> 等浏览器直接发起、无法携带 hea, require_auth(), require_auth_query() (+3 more)

### Community 41 - "Dispatcher Deterministic Routing"
Cohesion: 0.22
Nodes (9): init_db(), list_sessions(), 进程重启后对齐"僵尸 running"状态：上次进程被杀时，DB 里可能残留 status='running'     的会话与 tasks。它们的执行早已不在, 进程重启后把卡在中间态的目标循环复位：producing/verifying 的 fire-and-forget     任务已随进程消失，不会自愈；重启时归位, 进程重启后复位卡在 verifying 的 dispatch 子任务：其后台判定任务（_run_dispatch_verify）     随进程消失，不会自愈，, reconcile_dispatch_subtasks(), reconcile_goal_schedules(), reconcile_stale_running() (+1 more)

### Community 42 - "KnotRunner (unwired experimental)"
Cohesion: 0.22
Nodes (4): KnotRunner, EventCallback, SSE 行: 'data: {...}' / 'data:{...}'。心跳冒号行返回空串。, _strip_sse_prefix()

### Community 43 - "Frontend Dispatch View"
Cohesion: 0.39
Nodes (9): closeDispatchView(), _dispatchFriendlyName(), openDispatchDetail(), openDispatchInput(), openDispatchView(), pollDispatch(), renderDispatchPlan(), showDispatchList() (+1 more)

### Community 45 - "Healthcheck Script"
Cohesion: 0.47
Nodes (5): check_http(), check_process(), main(), 本机是否有 uvicorn server.main:app 进程在跑。, 探活一个已存在的鉴权接口，返回 (是否正常, 说明文字)。

### Community 46 - "Queue Item CRUD"
Cohesion: 0.60
Nodes (5): delete_queue_item(), get_queue_item(), update_queue_item(), queue_delete(), queue_edit()

### Community 47 - "Fake Subprocess Test Helper"
Cohesion: 0.29
Nodes (8): 背对背跑 A/B 两版方案，再综合仲裁。整体 try 包裹，异常置 error。     全程分阶段打 stage 并广播 arbitration_progre, run_arbitration(), create_arbitration(), get_arbitration(), 按来源表主键收尾对应影子记录状态（找不到静默跳过）。     ref_id 全局唯一（schedule/plan/arbitration id），无需再按 or, update_arbitration(), update_work_item_status_by_ref(), post_arbitrate()

### Community 49 - "Goal Loops List Query"
Cohesion: 0.33
Nodes (6): _claimed_worktree_branches(), delete_worktree_orphan(), list_worktree_orphans(), 当前 sessions 表里被占用的 worktree 分支集合（含归档/秘书会话——只要记录在就算认领）。, 列出孤儿 agent/* 分支：仓库里存在但没有任何会话记录认领的分支（会话已删、分支残留）。     只读维护接口，供 ops/管理员手动清理用，无前端 UI, 删除一个孤儿 agent/* 分支（git branch -D）。删除前再次核实未被会话占用，防 race。

### Community 55 - "preview_html"
Cohesion: 0.50
Nodes (4): Request, preview_html(), 把任意绝对路径的 HTML 文件重定向到 /fs/<path>，由静态文件服务提供，     这样页面内的相对路径（fetch('viz/data.json'), swanlab_proxy()

### Community 56 - "_goal_protected_uploads"
Cohesion: 0.50
Nodes (4): _cleanup_uploads(), _goal_protected_uploads(), 活跃目标循环 prompt 里固化引用的上传附件绝对路径集合。目标循环跨多轮复用同一 prompt，     其中的附件路径不能被 TTL 清理删除，否则后续迭, _uploads_cleanup_loop()

### Community 57 - "list_todos"
Cohesion: 0.67
Nodes (3): list_todos(), todos_list(), triage_list()

### Community 58 - "list_work_items"
Cohesion: 0.67
Nodes (3): list_work_items(), 阶段4：统一"任务运行"聚合视图的列表态。按 origin/status 过滤（None/空则不过滤），     纯只读参数化查询，不做任何决策依据用途。, get_work_items()

### Community 59 - "search_message_sessions"
Cohesion: 0.67
Nodes (3): 按内容子串匹配 messages.content，返回去重后的 session_id 列表。, search_message_sessions(), search_sessions()

### Community 60 - "refresh_todo_progress"
Cohesion: 0.67
Nodes (3): 刷新单个 todo 卡片的进展摘要，带 jsonl mtime 缓存。, refresh_todo_progress(), todo_refresh_progress()

## Ambiguous Edges - Review These
- `痛点1：目标循环入口埋得深/命名错位` → `#goal-loop-btn 目标循环 Hero 入口按钮`  [AMBIGUOUS]
  docs/GOAL_LOOP_现状与痛点.md · relation: references

## Knowledge Gaps
- **47 isolated node(s):** `agent-tunnel.sh script`, `restart.sh script`, `run.sh script`, `AUTH_TOKEN`, `CLAUDE_BIN` (+42 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **13 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `痛点1：目标循环入口埋得深/命名错位` and `#goal-loop-btn 目标循环 Hero 入口按钮`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **Why does `ClaudeRunner` connect `ClaudeRunner Process Management` to `AgentProvider Abstract Base`?**
  _High betweenness centrality (0.033) - this node is a cross-community bridge._
- **Why does `SessionHub` connect `WebSocket Session Monitoring` to `WeCom Notification`?**
  _High betweenness centrality (0.030) - this node is a cross-community bridge._
- **Why does `_runner_for()` connect `WebSocket Session Monitoring` to `REST API Routes (Todos/Memos/Queue)`, `WeCom Notification`, `Session-Todo Linking`?**
  _High betweenness centrality (0.017) - this node is a cross-community bridge._
- **What connects `agent-tunnel.sh script`, `restart.sh script`, `run.sh script` to the rest of the system?**
  _47 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Frontend Chat UI` be split into smaller, more focused modules?**
  _Cohesion score 0.057692307692307696 - nodes in this community are weakly interconnected._
- **Should `REST API Routes (Todos/Memos/Queue)` be split into smaller, more focused modules?**
  _Cohesion score 0.07389162561576355 - nodes in this community are weakly interconnected._