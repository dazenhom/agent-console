# Graph Report - agent-console  (2026-08-03)

## Corpus Check
- 74 files · ~78,737 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1263 nodes · 2832 edges · 70 communities (48 shown, 22 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 16 edges (avg confidence: 0.64)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `367c8368`
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
- config.py
- test_notify_auth.py
- Session-Todo Linking
- Session Import
- Logger Protocol Design
- LLMs.txt Skill Index Builder
- Frontend Goal Loop Cards
- Frontend Arbitration View
- ASR Audio Decoding
- CodexRunner Process Management
- Dispatch Fanout Finalization + Tests
- YAML Frontmatter Utils
- WeCom Notification
- Work Items CRUD Tests
- PWA Manifest
- Test Fixtures (temp DB)
- Context Compactor
- Dispatcher Deterministic Routing
- KnotRunner (unwired experimental)
- Frontend Dispatch View
- Task DB Tests
- Healthcheck Script
- Fake Subprocess Test Helper
- agent-tunnel.sh Script
- Goal Loops List Query
- watchdog.sh Script
- Message Parent Migration Script
- restart.sh Script
- start_dual.py Launcher
- preview_html
- refresh_todo_progress
- get_audio
- showMemoBanner
- llms_txt
- _claimed_worktree_branches
- _validate_schedule
- _rewrite_asset_versions
- NoCacheStatic
- asr
- get_audio
- get_config
- get_file
- get_progress
- goals_summary
- llms_full_txt
- schedules_iterations
- schedules_subtasks
- schedules_continue

## God Nodes (most connected - your core abstractions)
1. `api()` - 63 edges
2. `_query()` - 61 edges
3. `toast()` - 57 edges
4. `escapeHtml()` - 55 edges
5. `el()` - 52 edges
6. `_exec()` - 43 edges
7. `SessionHub` - 36 edges
8. `_now()` - 31 edges
9. `switchSession()` - 24 edges
10. `new_id()` - 23 edges

## Surprising Connections (you probably didn't know these)
- `Agent Console App 图标（深色底+对话框+对勾折线）` --conceptually_related_to--> `README.md：Agent Console 项目总览`  [INFERRED]
  web/icon.svg → README.md
- `痛点1：目标循环入口埋得深/命名错位` --references--> `#goal-loop-btn 目标循环 Hero 入口按钮`  [AMBIGUOUS]
  docs/GOAL_LOOP_现状与痛点.md → web/index.html
- `test_retry_preserves_need_arbitration()` --calls--> `retry_subtask()`  [EXTRACTED]
  tests/test_dispatch_arbitration.py → server/dispatcher.py
- `test_b2b_no_workdir_backward_compatible()` --calls--> `verify_back_to_back()`  [EXTRACTED]
  tests/test_dispatch_arbitration.py → server/arbiter.py
- `test_b2b_workdir_into_prompt_and_cwd()` --calls--> `verify_back_to_back()`  [EXTRACTED]
  tests/test_dispatch_arbitration.py → server/arbiter.py

## Import Cycles
- 3-file cycle: `server/scheduler.py -> server/secretary.py -> server/triage.py -> server/scheduler.py`

## Hyperedges (group relationships)
- **analyst→developer→reviewer→ops 四角流水线模式** — claude_agents_analyst, claude_agents_developer, claude_agents_reviewer, claude_agents_ops, claude_skills_console_dev_skill, claude_skills_devops_team_skill, claude_skills_quant_dev_skill [EXTRACTED 1.00]
- **目标循环未接入的智能件（dispatcher/arbiter/goal_verifier/scheduler）** — docs_goal_loop_status_pain_points_scheduler, docs_goal_loop_status_pain_points_dispatcher, docs_goal_loop_status_pain_points_arbiter, docs_goal_loop_status_pain_points_goal_verifier [EXTRACTED 1.00]

## Communities (70 total, 22 thin omitted)

### Community 0 - "Frontend Chat UI"
Cohesion: 0.07
Nodes (51): appendToToolGroup(), applySessionSearch(), clearChatHighlights(), clearSwanlabSessionCookie(), clearSwanlabTimer(), closeMonitor(), closeSwanlab(), closeWs() (+43 more)

### Community 1 - "REST API Routes (Todos/Memos/Queue)"
Cohesion: 0.03
Nodes (3): get_arbitration(), get_work_item(), FastAPI 应用：REST（会话/历史/任务）+ WebSocket（流式对话）。

### Community 2 - "Goal Loop Iteration Queries"
Cohesion: 0.08
Nodes (50): get_goal_iteration_by_no(), get_latest_job(), get_schedule(), get_session(), latest_task_id(), 该会话最近一条 tasks 记录的 id，用于把刚发起的迭代回合关联进本轮历史。, 该 schedule 最近一条指定 kind 的一次性子进程记录，用于把验收调用关联进本轮历史。, 按来源表主键反查对应影子记录 id，供四处发起点把 work_item_id 回填进各自子表。     ref_id 全局唯一；找不到（如历史数据、边缘情况）返 (+42 more)

### Community 3 - "DB Query Helpers"
Cohesion: 0.06
Nodes (57): active_goal_prompts(), count_messages(), count_triage_dispatched_today(), count_user_messages(), delete_queue_item(), delete_snippet(), due_schedules(), get_goal_iteration() (+49 more)

### Community 4 - "DB Write/Insert Helpers"
Cohesion: 0.11
Nodes (39): add_message(), create_arbitration(), create_artifact(), create_goal_iteration(), create_memo(), create_report(), create_schedule(), create_session() (+31 more)

### Community 5 - "WebSocket Session Monitoring"
Cohesion: 0.15
Nodes (15): 背对背跑 A/B 两版方案，再综合仲裁。整体 try 包裹，异常置 error。     全程分阶段打 stage 并广播 arbitration_progre, run_arbitration(), create_work_item_safe(), delete_session(), get_arbitration(), create_work_item 的容错包装：work_items 纯观测，写失败绝不影响主流程——     异常只记日志并返回空串。四处发起点统一走这里，免得, 覆盖式设置任务关联会话；同步把主会话（列表第一个）写回 todos.session_id。, set_todo_sessions() (+7 more)

### Community 6 - "Backlog Import & Goal Prompt Building"
Cohesion: 0.20
Nodes (14): _build_goal_prompt(), _build_payload(), _build_stop_condition(), _content_hash(), _load_existing(), parse_backlog(), 把 quant-recommender 的 mandatory-backlog.md 接入 triage 收件箱。  与 triage.py 一脉相承：解析出的, triage_payload 结构对齐 triage 现状约定，供 _dispatch_existing_todo 消费。 (+6 more)

### Community 7 - "Arbiter Back-to-Back Arbitration"
Cohesion: 0.09
Nodes (31): _broadcast_arb_stage(), _build_arbitration_prompt(), 背对背双执行 + 综合仲裁。  同一问题背对背交给两位"工程师"独立作答——A 走 Claude（tclaude），B 走 Codex（tcodex）—— 互不, 向 monitor 通道推一条仲裁阶段进展（fire-and-forget，异常吞掉绝不影响仲裁主流程，     写法与 scheduler._broadcas, 一次性调用 tclaude，返回 (job_id, 答案文本)。解析方式与 goal_verifier.verify 一致：     从 stdout 逐行挑出, 一次性调用 tcodex exec，返回 (job_id, 答案文本)。      命令拼法与 codex_runner._build_cmd 一致（无 res, _run_claude_oneshot(), _run_codex_oneshot() (+23 more)

### Community 8 - "Frontend Agent/Artifact UI"
Cohesion: 0.10
Nodes (53): agentFormHtml(), api(), attachArtifacts(), closeDispatchView(), confirmDialog(), cssEscape(), _dispatchFriendlyName(), el() (+45 more)

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
Cohesion: 0.16
Nodes (23): create_dispatch_subtask(), get_dispatch_subtask(), start_task(), update_dispatch_subtask(), 扫描所有还有未终结子任务的 dispatch plan，逐个推进子任务状态。每 tick 只推进一步。     转换先写库再动作（先落 verifying 再起, 后台判定单个 dispatch 子任务是否完成，是唯一把 status 从 verifying 推向 done/failed 的地方。     模式完全参考 _, _run_dispatch_verify(), _tick_fanout() (+15 more)

### Community 13 - "AgentProvider Abstract Base"
Cohesion: 0.18
Nodes (6): CodexRunner, EventCallback, Process, 组 tcodex exec 命令。prompt 作为最后一个位置参数。, 中断当前回合：杀整个进程组（下回合自动 resume 续上）。, 跑一个回合。返回 {claude_session_id, returncode, error, cancelled}。          on_permissi

### Community 14 - "Codex Oneshot Helper & Config"
Cohesion: 0.17
Nodes (15): 编排层公共 codex 一次性子进程助手：便宜档机械任务（看板摘要/分诊/目标循环总览/ 行摘要/标题/验收）统一走这里调 tcodex，避免每处重复拼命令 +, 一次性调用 tcodex exec，返回 (job_id, 拼接后的答案文本, stderr 前200字, status)。      status ∈ suc, run_codex_oneshot_text(), _build_prompt(), gen_title(), _gen_title_haiku(), _haiku(), _heuristic() (+7 more)

### Community 15 - "Frontend Todo/Memo Actions"
Cohesion: 0.20
Nodes (19): archiveTodo(), bindSessionChips(), deleteTodo(), initDrop(), initImage(), refreshKanbanAfterTodoMutation(), refreshKanbanAll(), renderKanban() (+11 more)

### Community 16 - "Skill Frontmatter & Safe Path Utils"
Cohesion: 0.06
Nodes (64): _agents_dir(), agents_json(), _build_meta(), create_agent(), delete_agent(), get_agent(), list_agents(), _norm_tools() (+56 more)

### Community 17 - "Frontend Message Stream Rendering"
Cohesion: 0.08
Nodes (44): appendDelta(), appendMessageGrouped(), appendMsgWithPreview(), appendSubagentDelta(), attachmentLines(), buildMessageNode(), clearPendingImages(), clearStream() (+36 more)

### Community 18 - "Frontend Session List Management"
Cohesion: 0.10
Nodes (38): createSession(), deleteSession(), deriveState(), doJumpToSession(), enterApp(), fillList(), fillListGrouped(), handleMonitorMessage() (+30 more)

### Community 19 - "Dispatch Plan Work Item Tracking"
Cohesion: 0.15
Nodes (22): JSONResponse, Response, _b64url_decode(), _b64url_encode(), _clear_swanlab_session_cookie(), create_swanlab_session(), _make_swanlab_session_token(), _normalize_swanlab_public_base() (+14 more)

### Community 20 - "Git Worktree Management"
Cohesion: 0.14
Nodes (9): Protocol, configure(), Logger, LoggerProvider, 日志器接口：仅列出本项目实际使用的方法（stdlib Logger 已满足此结构）。, logger 来源抽象：给定名称返回一个 Logger。, 默认实现：直接透传 stdlib logging.getLogger，与改造前行为完全一致。, 初始化装配点：安装全局 LoggerProvider。      传 None 时安装默认的 `StdLoggerProvider`（幂等，与改造前一致）；传入 (+1 more)

### Community 21 - "config.py"
Cohesion: 0.18
Nodes (9): 集中配置：全部可通过环境变量覆盖，方便部署时调整。, 备忘录提醒。  按每条备忘的 remind_mode 判断今天是否该提醒（daily / weekly / monthly / once / deadline, _should_remind(), _activity_label(), SessionHub：连接无关的会话中枢。  把「跑一个回合」从 WebSocket 连接里彻底解耦出来，是「多 Agent 调度台」的地基：   - 回合执行, 从一条翻译后的消息提炼「当前在干嘛」一行活动，给会话列表实时显示。, 把 Claude stream-json 事件翻译成前端消息（可能 0~N 条）。, translate_event() (+1 more)

### Community 22 - "test_notify_auth.py"
Cohesion: 0.33
Nodes (9): _post_notify(), test_correct_local_secret_bypasses_bearer(), test_empty_local_secret_header_never_bypasses(), test_env_secret_takes_priority_without_touching_files(), test_no_local_secret_with_valid_bearer_is_200(), test_non_ascii_local_secret_header_is_401_not_500(), test_secret_generation_failure_falls_back_to_bearer(), test_wrong_local_secret_with_valid_bearer_is_200() (+1 more)

### Community 24 - "Session Import"
Cohesion: 0.23
Nodes (13): list_sessions(), _first_user_text(), import_sessions(), latest_ai_title(), list_importable(), _project_dir(), Path, 接续电脑终端交互模式聊过的会话。  用户在服务器终端跑交互式 tclaude 的会话存在   <TCLAUDE_HOME>/projects/<slug>/<c (+5 more)

### Community 26 - "Logger Protocol Design"
Cohesion: 0.15
Nodes (14): Request, check_token(), login(), post_notify(), preview_html(), 把任意绝对路径的 HTML 文件重定向到 /fs/<path>，由静态文件服务提供，     这样页面内的相对路径（fetch('viz/data.json'), 监控通道：推送所有会话的状态/活动变化，驱动前端会话列表实时刷新。, 供 Agent（tclaude/tcodex 里跑的 Skill/Agent）主动推一条通知：携带正确的     X-Local-Secret（本机共享密钥，见 (+6 more)

### Community 27 - "LLMs.txt Skill Index Builder"
Cohesion: 0.20
Nodes (13): _agent_files(), _build_index(), build_llms_full_txt(), build_llms_txt(), llms.txt / llms-full.txt：给外部 agent 摄取的项目文本索引。  遵循 llms.txt 约定（https://llmstxt.or, [(subagent名, .md 全文)]，按名排序。, 精选文本索引，末尾指向 /llms-full.txt。, 索引 + 每个 skill / subagent 定义全文，拼成单一 markdown。 (+5 more)

### Community 28 - "Frontend Goal Loop Cards"
Cohesion: 0.16
Nodes (22): buildGoalCard(), closeGoalView(), fmtGoalStatus(), fmtRelTime(), fmtTime(), fmtWorkItemStatus(), genGoalSummary(), goalGroup() (+14 more)

### Community 29 - "Frontend Arbitration View"
Cohesion: 0.36
Nodes (8): closeArbView(), openArbDetail(), openArbitrationInput(), openArbView(), pollArbitration(), renderArbitration(), showArbList(), stopArbPoll()

### Community 30 - "ASR Audio Decoding"
Cohesion: 0.23
Nodes (13): RuntimeError, ASRError, decode_to_wav16k(), _decode_with_pyav(), _next_base(), _post_once(), ASR 客户端：把上传音频解码成 16k 单声道 WAV，再发给 HY ContextASR 服务转写。  协议：OpenAI 多模态 Chat Complet, 对 base64 的 WAV 调 ASR，返回识别文本。失败抛 ASRError。      多端点 round-robin；可重试错误（5xx/网络）退避重试 (+5 more)

### Community 31 - "CodexRunner Process Management"
Cohesion: 0.12
Nodes (16): ABC, AgentProvider, _child_env(), is_sleep_command(), _kill_process_group(), EventCallback, Process, Agent provider 基础契约：on_event 接收 type 为 assistant、携带 tool_result 的 user、result 或 (+8 more)

### Community 32 - "Dispatch Fanout Finalization + Tests"
Cohesion: 0.35
Nodes (13): get_work_item(), _maybe_finalize_fanout(), plan 下所有 subtask 都到终态（done/failed/error）后，聚合收尾对应 work_item：     全 done → done，否则, _patch_subs(), server.scheduler._maybe_finalize_fanout 的聚合收尾逻辑。  用 temp_db 走真实的 work_item 写入路径，, 建一条 ref_id=plan_id 的 work_item，返回其 id。, _seed_work_item(), test_finalize_all_done_marks_work_item_done() (+5 more)

### Community 35 - "WeCom Notification"
Cohesion: 0.24
Nodes (11): _extract_recent_text(), Path, 看板进展总结：读取会话 jsonl，用一次性子进程概括开发进展。  与 summarizer 一脉相承：独立的一次性子进程，绝不碰会话的常驻上下文； 走 cod, workdir → tclaude 项目目录 slug（与 session_import._slug 一致：/ _ . → -）。, 读 jsonl 最后若干条 user/assistant 文本消息，拼成上下文。, 用一次性子进程概括进展。返回干净摘要；任何异常兜底成友好提示。, 刷新单个 todo 卡片的进展摘要，带 jsonl mtime 缓存。, refresh_todo_progress() (+3 more)

### Community 37 - "PWA Manifest"
Cohesion: 0.18
Nodes (10): background_color, description, display, icons, name, orientation, scope, short_name (+2 more)

### Community 39 - "Test Fixtures (temp DB)"
Cohesion: 0.18
Nodes (10): init_db(), git_repo(), non_git_dir(), 测试公共 fixture。  核心约束：所有涉及 DB 的测试都必须落到临时库，绝不碰生产的 data/console.db。 做法是把 config.DB_P, 把 SQLite 落到临时文件并初始化，测试结束随 tmp_path 一起清理。, 把 worktree 落地根指向临时目录，避免污染生产 data/worktrees。, 建一个带一次初始提交的真实 git 仓库，供隔离成功路径使用。, 一个普通空目录（没有 git init），用于验证降级路径。 (+2 more)

### Community 40 - "Context Compactor"
Cohesion: 0.06
Nodes (50): _parse_verdict(), 把一路评委的原始答复解析成 (done, reason)。解析规则与 goal_verifier.verify 一致：     取首个非空行精确匹配 == "D, 困难 dispatch 子任务的背对背双评委验收：复用 goal_verifier 的验收 prompt，交给两位     评委独立判定——A 走更强的 Cla, verify_back_to_back(), _build_planner_prompt(), _capabilities_block(), dispatch(), _fire_subtask() (+42 more)

### Community 41 - "Dispatcher Deterministic Routing"
Cohesion: 0.20
Nodes (10): FastAPI, _cleanup_uploads(), get_swanlab_status(), _goal_protected_uploads(), lifespan(), _login_swanlab(), 活跃目标循环 prompt 里固化引用的上传附件绝对路径集合。目标循环跨多轮复用同一 prompt，     其中的附件路径不能被 TTL 清理删除，否则后续迭, 启动时预热 SwanLab sid，避免第一批并发请求各自登录。 (+2 more)

### Community 42 - "KnotRunner (unwired experimental)"
Cohesion: 0.20
Nodes (5): KnotRunner, EventCallback, 封装 Knot HTTPS API（AG-UI 协议）作为底层 Agent。  通过 POST `{KNOT_API_BASE}/apigw/api/v1/ag, SSE 行: 'data: {...}' / 'data:{...}'。心跳冒号行返回空串。, _strip_sse_prefix()

### Community 43 - "Frontend Dispatch View"
Cohesion: 0.25
Nodes (9): isNearBottom(), loadHistory(), openPeek(), peekReply(), renderHistorySnapshot(), renderLoadEarlierBtn(), sameHistorySnapshot(), skeleton() (+1 more)

### Community 45 - "Healthcheck Script"
Cohesion: 0.47
Nodes (5): check_http(), check_process(), main(), 本机是否有 uvicorn server.main:app 进程在跑。, 探活一个已存在的鉴权接口，返回 (是否正常, 说明文字)。

### Community 49 - "Goal Loops List Query"
Cohesion: 0.05
Nodes (34): 一条 WS 连接的轻封装。send 为发送回调；hidden 表示该客户端是否在后台（锁屏/切走）。, 给监控订阅者推一条会话状态变化，驱动前端列表实时刷新。, 该会话是否有「处于前台」的订阅者。无 → 回合结束发企业微信。, 统一的回合完成提醒出口：广播前端事件，并视条件发送企业微信。          elapsed: 本回合实际耗时（秒），由调用方在清掉 self._turn_s, 给常驻 Claude 进程绑定回合外续跑事件回调，避免后台 Agent 汇报被吞。, 构造给 runner 回合看门狗使用的进度回调。, 按会话 engine 字段选 runner，未知或缺省 engine 默认落 ClaudeRunner。, 回合结束后自动刷该会话关联的 in_progress 看板进展，并推 monitor。 (+26 more)

### Community 53 - "start_dual.py Launcher"
Cohesion: 0.24
Nodes (18): get_local_notify_secret(), 返回 /api/notify 本机免鉴权用的共享密钥；生成/读取失败返回空串（调用方据此     一律回落到标准 Bearer token 鉴权，不能因为密钥拿, atomic_write_private_at(), load_or_create_local_notify_secret(), locked_secret_dir(), open_lock_file(), open_secret_dir(), Path (+10 more)

### Community 55 - "preview_html"
Cohesion: 0.25
Nodes (10): _build_prompt(), _classify(), _last_reason(), 目标循环总览总结：把所有 goal 循环的完成情况汇总，用一次性子进程生成一段中文小结。  与 kanban.summarize_progress / goal, 按 enabled×goal_status 归类：进行中 / 已完成 / 未完成（耗尽）/ 未完成（暂停）。, 会话标题优先，prompt 兜底；压平换行截 50 字。, 末轮验收反馈：过滤 verdict 非空的轮（去掉进程重启残留的纯 producing 僵尸行），     取 iter_no 最大那条的 feedback，截, 汇总所有目标循环并生成小结。返回 (ok, summary)。任何超时/异常都返回 (False, 原因)。 (+2 more)

### Community 60 - "refresh_todo_progress"
Cohesion: 0.10
Nodes (29): date, build_evening_prompt(), build_morning_prompt(), _format_sessions(), _format_todos(), gather_day_data(), run_report(), _build_triage_prompt() (+21 more)

### Community 65 - "showMemoBanner"
Cohesion: 0.33
Nodes (7): clearMemoBadge(), closeDrawer(), openManage(), openMemoQuickPanel(), refreshMemoBadge(), setMemoBadge(), showMemoBanner()

### Community 67 - "_claimed_worktree_branches"
Cohesion: 0.33
Nodes (6): _claimed_worktree_branches(), delete_worktree_orphan(), list_worktree_orphans(), 当前 sessions 表里被占用的 worktree 分支集合（含归档/秘书会话——只要记录在就算认领）。, 列出孤儿 agent/* 分支：仓库里存在但没有任何会话记录认领的分支（会话已删、分支残留）。     只读维护接口，供 ops/管理员手动清理用，无前端 UI, 删除一个孤儿 agent/* 分支（git branch -D）。删除前再次核实未被会话占用，防 race。

### Community 68 - "_validate_schedule"
Cohesion: 0.50
Nodes (4): 校验并归一化定时任务参数。返回 {kind, interval_min, at_hhmm[, stop_condition, max_iterations]}。, schedules_create(), schedules_update(), _validate_schedule()

### Community 70 - "_rewrite_asset_versions"
Cohesion: 0.67
Nodes (3): _asset_version(), index(), _rewrite_asset_versions()

## Ambiguous Edges - Review These
- `痛点1：目标循环入口埋得深/命名错位` → `#goal-loop-btn 目标循环 Hero 入口按钮`  [AMBIGUOUS]
  docs/GOAL_LOOP_现状与痛点.md · relation: references

## Knowledge Gaps
- **47 isolated node(s):** `agent-tunnel.sh script`, `restart.sh script`, `run.sh script`, `AUTH_TOKEN`, `CLAUDE_BIN` (+42 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **22 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `痛点1：目标循环入口埋得深/命名错位` and `#goal-loop-btn 目标循环 Hero 入口按钮`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **Why does `SessionHub` connect `Goal Loops List Query` to `config.py`?**
  _High betweenness centrality (0.036) - this node is a cross-community bridge._
- **Why does `ClaudeRunner` connect `ClaudeRunner Process Management` to `CodexRunner Process Management`?**
  _High betweenness centrality (0.025) - this node is a cross-community bridge._
- **Why does `Subscriber` connect `Goal Loops List Query` to `config.py`?**
  _High betweenness centrality (0.020) - this node is a cross-community bridge._
- **What connects `agent-tunnel.sh script`, `restart.sh script`, `run.sh script` to the rest of the system?**
  _47 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Frontend Chat UI` be split into smaller, more focused modules?**
  _Cohesion score 0.06715063520871144 - nodes in this community are weakly interconnected._
- **Should `REST API Routes (Todos/Memos/Queue)` be split into smaller, more focused modules?**
  _Cohesion score 0.02531645569620253 - nodes in this community are weakly interconnected._