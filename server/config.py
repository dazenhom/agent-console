"""集中配置：全部可通过环境变量覆盖，方便部署时调整。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ---- Claude Code CLI (tclaude wrapper) ----
# tclaude 是腾讯内部对 claude-code 的 wrapper；默认走 PATH 解析（run.sh 会注入 nvm bin）。
# 部署时可用环境变量 CLAUDE_BIN 指向真实路径。
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "tclaude")
# 是否加 --dangerously-skip-permissions。注意：tclaude/claude-code 在 **root 下会硬拒绝**
# 这个参数（"cannot be used with root/sudo privileges"），加了会导致每个回合启动失败。
# 所以 root 部署务必保持 false，靠下面的 CLAUDE_ALLOWED_TOOLS 放行工具来免权限提示。
CLAUDE_SKIP_PERMISSIONS = os.environ.get("CLAUDE_SKIP_PERMISSIONS", "false").lower() == "true"
# 放行的工具列表（传给 --allowedTools，空格分隔）。这是 root 下绕开权限提示的正路：
# headless -p 默认模式会静默拒绝工具调用（permission_denials），导致 Agent 干不了实事；
# 显式 allowedTools 不受 root 限制，放行后零拒绝。默认放行全套常用工具。
# 想精细控制可改成如 "Bash(git *) Read Write"；设为空字符串则不加该参数（回到默认拒绝）。
CLAUDE_ALLOWED_TOOLS = os.environ.get(
    "CLAUDE_ALLOWED_TOOLS",
    "Bash Read Write Edit Glob Grep WebFetch WebSearch Task TodoWrite NotebookEdit",
)
# 禁用工具列表（空格分隔）。AskUserQuestion 仅在非常驻模式（headless -p，无 stdin）下
# 被禁用以防卡死；常驻模式（stream-json，有 stdin）会自动放行它并走前端选项卡片交互。
# 见 claude_runner._build_cmd。设为空字符串则任何模式都不加 --disallowedTools 参数。
CLAUDE_DISALLOWED_TOOLS = os.environ.get("CLAUDE_DISALLOWED_TOOLS", "AskUserQuestion")
# 是否开启权限请求弹窗：未放行的工具触发 control_request 时，经 WS 推前端弹窗让用户确认，
# 用户点允许/拒绝后回 control_response 给 CLI。仅在常驻模式（有 stdin 可回写）下生效。
CLAUDE_PERMISSION_PROMPT = os.getenv("CLAUDE_PERMISSION_PROMPT", "true").lower() == "true"
# 单次 Agent 回合的超时（秒），防止卡死。
# 注意：常驻模式已改为"空闲超时 + 循环检测 + 安全上限"的看门狗机制（见下方几个变量），
# 这个值仅老模式（run_turn）仍作硬超时用，保留向后兼容。
CLAUDE_TURN_TIMEOUT = int(os.environ.get("CLAUDE_TURN_TIMEOUT", "1800"))
# 看门狗机制（常驻模式 send_turn）：只要 agent 还在推进（有新事件）就不杀，
# 只有真正卡死（长时间无事件）、在循环、或超过绝对安全上限才终止。
# 关键：单个 Bash 工具调用（如大体量数据处理/训练/构建）执行期间 CLI 不会吐任何中间事件，
# 直到该调用返回才有下一条 JSONL——这段"命令自己在跑"的时间会被计入 idle。300~600s 对真实
# 数据管线常常不够，之前多次把"命令还在正常跑"误判成"卡死"提前掐断；调大到 1800s（30min）
# 给耐心；真正卡死另有循环检测（CLAUDE_LOOP_REPEAT/ERRORS）兜底，不靠 idle 单独判断。
CLAUDE_IDLE_TIMEOUT = int(os.environ.get("CLAUDE_IDLE_TIMEOUT", "1800"))   # 1800s无事件 → 判定卡死
CLAUDE_TURN_MAX = int(os.environ.get("CLAUDE_TURN_MAX", "14400"))          # 4h绝对上限（同样为大任务放宽）
CLAUDE_LOOP_REPEAT = int(os.environ.get("CLAUDE_LOOP_REPEAT", "8"))        # 相同工具调用连续N次 → 循环
CLAUDE_LOOP_ERRORS = int(os.environ.get("CLAUDE_LOOP_ERRORS", "10"))       # 连续报错N次 → 循环
CLAUDE_WATCHDOG_INTERVAL = int(os.environ.get("CLAUDE_WATCHDOG_INTERVAL", "15"))  # 看门狗检查间隔
# 是否加 --include-partial-messages：开启后 CLI 逐字推送文本增量，前端做打字机效果。
# 关掉则回到整段输出（向后兼容）。
CLAUDE_STREAM_PARTIAL = os.environ.get("CLAUDE_STREAM_PARTIAL", "true").lower() == "true"

# 模型档位 → tclaude 模型 ID。空字符串 = 不传 model 让 CLI 用默认。
# 合法值见 `tclaude -- --model bogus` 的报错列表：
#   claude-sonnet-4-6 / claude-sonnet-4-6[1m] / claude-opus-5[1m] /
#   claude-opus-4-8[1m] / claude-opus-4-7[1m] / claude-opus-4-6[1m] / claude-haiku-4-5 / claude-hy3
CLAUDE_MODEL_FAST = os.environ.get("CLAUDE_MODEL_FAST", "claude-haiku-4-5")
# 看板进展摘要专用模型：用 hy3（tclaude 提供的档位模型），概括质量更好。
# 注：已不再被 kanban/triage/goal_summary/summarizer 的便宜档一次性任务使用（见 CHEAP_MODEL）；
# 仅 session_hub 的旧档位兼容映射（_LEGACY_MAP "fast"）还引用 CLAUDE_MODEL_FAST。
CLAUDE_MODEL_KANBAN = os.environ.get("CLAUDE_MODEL_KANBAN", "claude-hy3")
CLAUDE_MODEL_STRONG = os.environ.get("CLAUDE_MODEL_STRONG", "claude-sonnet-5")
CLAUDE_MODEL_SUPER = os.environ.get("CLAUDE_MODEL_SUPER", "claude-opus-5[1m]")
# 前端可选的完整模型列表（mode 直接存模型 ID）。claude-sonnet-5 放首位作为默认（GLM 5.2 曾
# 出过问题，撤下默认位，仍保留在列表里可手动选）。
CLAUDE_MODELS = [
    "claude-sonnet-5", "claude-sonnet-5[1m]",
    "claude-sonnet-4-6", "claude-sonnet-4-6[1m]",
    "claude-opus-5", "claude-opus-5[1m]",
    "claude-opus-4-8", "claude-opus-4-8[1m]",
    "claude-opus-4-7", "claude-opus-4-7[1m]",
    "claude-opus-4-6", "claude-opus-4-6[1m]",
    "claude-haiku-4-5", "claude-hy3", "opusplan",
    "claude-glm-5.2", "claude-glm-5.2[1m]",
    "claude-deepseek-v4-pro", "claude-deepseek-v4-pro[1m]",
    "claude-deepseek-v4-flash", "claude-deepseek-v4-flash[1m]",
]
# 新会话默认模型：Sonnet 5。（GLM 5.2 曾用作默认，出问题后改回；旧档位 fast/strong/super
# 仍兼容，见 session_hub 的 _LEGACY_MAP。）
CLAUDE_DEFAULT_MODE = os.environ.get("CLAUDE_DEFAULT_MODE", "claude-sonnet-5")
# 编排层"便宜模型"一次性机械任务（看板摘要/分诊/目标循环总览/行摘要/标题）统一用的模型：
# 走 codex 引擎（tcodex），与生产会话（claude 引擎）分离。原来是 claude 侧 hy3/haiku，
# 换成 gpt-5.6-luna（sol/terra/luna 三档里最轻量的一档，见 CODEX_MODELS）。
CHEAP_MODEL = os.environ.get("CHEAP_MODEL", "gpt-5.6-luna")
# 思考深度（low/medium/high/xhigh/max）。medium 平衡速度与质量；要更深手动调。
# CLAUDE_EFFORT 是全局默认/兜底；每会话可在 sessions.effort 单独设置（见 db/main）。
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
# 前端可选的完整 effort 列表（会话 effort 直接存这些值）。
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# 新会话默认 effort：不显式指定则沿用全局 CLAUDE_EFFORT。
CLAUDE_DEFAULT_EFFORT = os.environ.get("CLAUDE_DEFAULT_EFFORT", CLAUDE_EFFORT)
# 一次性子进程（摘要/标题/看板/验收/分诊等机械型任务）用的 effort：默认 low，省 token、更快。
CLAUDE_ONESHOT_EFFORT = os.environ.get("CLAUDE_ONESHOT_EFFORT", "low")
# 常驻进程模式：每会话维持一个长生命周期 tclaude 进程（--input-format stream-json），
# 更接近交互式，上下文常驻进程内。false=老的每回合新进程+resume 模式（回退用）。
CLAUDE_PERSISTENT = os.environ.get("CLAUDE_PERSISTENT", "true").lower() == "true"
# 常驻进程空闲多久无消息就回收（秒），下次消息自动重起 + resume 续上下文。
# 设长一点（1小时）减少冷启动重复发生——冷启动是 tclaude 加载工具/认证的固有开销。
CLAUDE_SESSION_IDLE_SEC = int(os.environ.get("CLAUDE_SESSION_IDLE_SEC", "3600"))

# ---- Codex CLI (tcodex wrapper) ----
# tcodex 是对 codex CLI 的 wrapper；跑无状态的 `tcodex -- exec [resume <id>] --json ...`，
# 每回合一个子进程，从 stdout 逐行读 JSONL 事件。默认走 workspace-write 沙箱，
# CODEX_BYPASS=true 时改用 --dangerously-bypass-approvals-and-sandbox 免审批（root/内网常用）。
CODEX_BIN = os.environ.get("CODEX_BIN", "/root/.nvm/versions/node/v22.23.1/bin/tcodex")
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "workspace-write")
CODEX_BYPASS = os.environ.get("CODEX_BYPASS", "true").lower() == "true"
CODEX_SKIP_GIT_CHECK = os.environ.get("CODEX_SKIP_GIT_CHECK", "true").lower() == "true"
# codex 引擎的回合超时：原本是整回合硬超时（含内部所有工具调用耗时）。现在改为
# **非等待的有效工作时间上限**——识别出的等待型调用（如 sleep 轮询等 GPU 训练完成）
# 的时长会被从倒计时里扣除（见 codex_runner.run_turn 的看门狗），避免主动轮询长任务
# 的正常场景被误杀。值不变，默认仍 7200s。
CODEX_TURN_TIMEOUT = int(os.environ.get("CODEX_TURN_TIMEOUT", "7200"))
# 绝对墙钟硬上限（秒）：不管有没有在等待，回合总耗时撞到这个值就必杀，兜底防豁免逻辑失控。
CODEX_TURN_MAX_WALL = int(os.environ.get("CODEX_TURN_MAX_WALL", "14400"))
# 单次等待型调用最多豁免的秒数：防止一次 `sleep 3600` 直接吃掉大半有效工作预算。
CODEX_WAIT_WAIVE_PER_CALL_MAX = int(os.environ.get("CODEX_WAIT_WAIVE_PER_CALL_MAX", "1800"))
# 全回合累计豁免上限（秒）：所有等待型调用一起最多能延长这么多。
CODEX_WAIT_WAIVE_TOTAL_MAX = int(os.environ.get("CODEX_WAIT_WAIVE_TOTAL_MAX", "7200"))
# 看门狗监视循环的检查间隔（秒）：定期核对有效工作时间 / 墙钟是否超限。
CODEX_WATCHDOG_INTERVAL = int(os.environ.get("CODEX_WATCHDOG_INTERVAL", "5"))
# 超时/取消终止 codex 进程时，SIGTERM 后等多久再兜底 SIGKILL（秒）。原来硬编码 5s 太短，
# codex 收到 SIGTERM 后要落稳 rollout 文件（下一回合 resume 就靠它），等太短容易没写完就被
# SIGKILL，导致后续 exec resume 失败/长时间零事件"假死"。放宽到 12s 给它收尾。
CODEX_TIMEOUT_GRACE_SECONDS = int(os.environ.get("CODEX_TIMEOUT_GRACE_SECONDS", "12"))
# 续跑（exec resume）心跳提示阈值（秒）：resume 回合里 thread.started 之后若超过该秒数仍未收到
# 任何 item 事件，就推一条状态提示，避免前端一直只显示"思考中"、被用户误当卡死点停止。
CODEX_RESUME_HEARTBEAT_SECONDS = int(os.environ.get("CODEX_RESUME_HEARTBEAT_SECONDS", "20"))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")
# 前端可选的 codex 模型列表（codex 会话的 mode 直接存模型 ID）。gpt-5.6-sol 放首位作为默认。
CODEX_MODELS = [
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
    "gpt-5.3-codex", "gpt-5.1-codex", "gpt-5.1-codex-mini", "glm-5.2-ioa", "hy3-ioa",
]
CODEX_DEFAULT_MODEL = os.environ.get("CODEX_DEFAULT_MODEL", "gpt-5.6-sol")
# Dispatch 基础执行(dev/兜底)路由的默认引擎与模型；deep/评判仍走 Opus，不受此影响
DISPATCH_EXEC_ENGINE = os.environ.get("DISPATCH_EXEC_ENGINE", "codex")
DISPATCH_EXEC_MODEL = os.environ.get("DISPATCH_EXEC_MODEL", CODEX_DEFAULT_MODEL)
# codex 推理深度：该值目前不会被传给 codex CLI 命令行。已确认命令行显式传
# `-c model_reasoning_effort=<level>` 会触发 codex CLI (v0.144.6) 的 prewarm-mismatch bug——
# 即使数值与 `~/.tcodex/config.toml` 里的默认值相同，显式传参也会导致本轮 turn 无法复用
# 启动时预热好的 websocket 连接，卡 ~15 秒后现开新连接，而新连接在当前腾讯内网网关环境下
# 必现失败超时。四处调用点（goal_verifier.py / arbiter.py / codex_oneshot.py /
# codex_runner.py）已移除该拼接；实际生效的 effort 档位由 `~/.tcodex/config.toml` 里的
# `model_reasoning_effort` 默认值决定。若未来要复用这个环境变量，必须先确认 codex CLI
# 有不触发该 bug 的传参方式，不要简单恢复 `-c` 拼接。
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "high")
# 会话底层 Agent 引擎：claude（tclaude）或 codex（tcodex）。新会话默认 claude。
VALID_ENGINES = {"claude", "codex"}
DEFAULT_ENGINE = os.environ.get("DEFAULT_ENGINE", "claude")

# ---- 会话行摘要（列表里"刚做了什么"一句话）----
# 用一次性 Haiku 概括（独立子进程，不碰会话上下文）；失败回退启发式截断。
SUMMARY_ENABLED = os.environ.get("SUMMARY_ENABLED", "true").lower() == "true"
SUMMARY_TIMEOUT = float(os.environ.get("SUMMARY_TIMEOUT", "60"))

# ---- 目标循环（kind=goal）----
# 给一个自然语言目标 + 完成标准，让会话自迭代直到 verifier（checker）判定达成
# 或触顶（迭代数/成本）。GOAL_POLL_SEC 是状态机 tick 间隔；verifier 走 codex 引擎
# （GOAL_VERIFY_MODEL），与生产会话（claude）引擎分离，保证 maker/checker 独立。
# GOAL_MAX_COST_USD<=0 表示不做成本熔断。
GOAL_POLL_SEC = int(os.environ.get("GOAL_POLL_SEC", "30"))
GOAL_MAX_ITERATIONS = int(os.environ.get("GOAL_MAX_ITERATIONS", "10"))
GOAL_MAX_COST_USD = float(os.environ.get("GOAL_MAX_COST_USD", "20"))
# 验收员没有 verify_command 客观信号时得自己去 workdir 里 Glob/Read 核实产出，真实项目
# （尤其大型数据管线）经常在 300s 内探索不完，900s 兜住这类场景，避免"验收超时=系统性未完成出口"。
GOAL_VERIFY_TIMEOUT = float(os.environ.get("GOAL_VERIFY_TIMEOUT", "900"))
# 验收员用的 codex 模型：换掉原来 claude 侧的便宜档 hy3，用更强的 gpt-5.6-terra 减少
# 复杂真实项目验收里的漏判/超时。
GOAL_VERIFY_MODEL = os.environ.get("GOAL_VERIFY_MODEL", "gpt-5.6-terra")
# 新版 planned 目标循环：先把目标拆成有序子任务，每轮只推进一个子任务并单独验收。
# 单个子任务验收未过时最多重试多少轮，超过则标记 skipped 跳过、继续下一个（避免卡死在某个子任务）。
GOAL_SUBTASK_MAX_ATTEMPTS = int(os.environ.get("GOAL_SUBTASK_MAX_ATTEMPTS", "3"))
# 可执行验收命令（verify_command）的子进程超时（秒）：真实数据管线的 verify_command 常常是
# "跑全量测试 + 扫产出目录核对数据集"，300s 经常不够、把"命令还在正常跑"误判成验收超时。
# 调大到 1800s（30min）给耐心；命令本身该多久跑完仍由 verify_command 的内容决定，这里只是
# 不再抢先掐断。
GOAL_CMD_TIMEOUT = float(os.environ.get("GOAL_CMD_TIMEOUT", "1800"))

# ---- Triage 自动分流（H3）----
# 秘书生成晚报后，用便宜模型（CHEAP_MODEL）对当日会话/任务做一次性分诊：
# 判断哪些是值得跟进的事项，逐项给出置信度与建议动作。默认保守——TRIAGE_AUTO_DISPATCH=false
# 时全部进"待分诊收件箱"等人工确认，绝不自动派单。只有开了自动派单、且置信度够高、
# 范围小、有明确完成标准，且未超每日上限时，才建隔离会话+目标循环自动开工。
TRIAGE_ENABLED = os.environ.get("TRIAGE_ENABLED", "true").lower() == "true"
TRIAGE_AUTO_DISPATCH = os.environ.get("TRIAGE_AUTO_DISPATCH", "false").lower() == "true"
TRIAGE_AUTO_CONFIDENCE = float(os.environ.get("TRIAGE_AUTO_CONFIDENCE", "0.85"))
TRIAGE_MAX_AUTO_PER_DAY = int(os.environ.get("TRIAGE_MAX_AUTO_PER_DAY", "2"))
TRIAGE_MAX_ITEMS = int(os.environ.get("TRIAGE_MAX_ITEMS", "8"))
TRIAGE_TIMEOUT = float(os.environ.get("TRIAGE_TIMEOUT", "90"))
# 自动派单给的目标循环轮数上限：3 轮对"大型数据管线"这类事项经常不够（每轮验收不过就
# 耗尽退出，B1 就是这么被提前判定失败的），调大到 6 轮给更多耐心；仍远低于手工创建目标循环
# 的默认上限（GOAL_MAX_ITERATIONS=10），维持"自动派单更保守"的整体设计。
TRIAGE_GOAL_MAX_ITERATIONS = int(os.environ.get("TRIAGE_GOAL_MAX_ITERATIONS", "6"))

# ---- 背对背双执行 + 综合仲裁 ----
# 同一问题背对背交给 Claude（工程师A）+ Codex（工程师B）各出一版方案，再用更强的
# Claude 模型综合仲裁。三次都是一次性子进程（run_logged_oneshot），互不干扰会话上下文。
# 方案生成/仲裁可能较长，超时给足；ARBITER_MODEL 是仲裁用的强模型。
ARBITRATION_TIMEOUT = float(os.environ.get("ARBITRATION_TIMEOUT", "600"))
ARBITER_MODEL = os.environ.get("ARBITER_MODEL", "claude-opus-5[1m]")

# ---- 会话标题异步刷新 ----
# 多轮对话后持续用 AI 重起标题，越来越准地反映整个对话。TITLE_EARLY_TURNS 前每回合刷，
# 之后每 TITLE_EVERY_N 回合刷一次；用户手动改名（title_auto=0）后不再自动覆盖。
TITLE_REFRESH_ENABLED = os.environ.get("TITLE_REFRESH_ENABLED", "true").lower() == "true"
TITLE_EARLY_TURNS = int(os.environ.get("TITLE_EARLY_TURNS", "3"))
TITLE_EVERY_N = int(os.environ.get("TITLE_EVERY_N", "5"))
# 首条 user 消息超过该字数则视为 skill 固定前言（编排指令），起标题时跳过前言只取附加需求。
# 正常手输极少这么长；skill 前言（SKILL.md）动辄上千字。
TITLE_SKIP_PREFIX_CHARS = int(os.environ.get("TITLE_SKIP_PREFIX_CHARS", "400"))

# ---- 图片上传（手机拍照/截图发给 Agent）----
# 存到会话 workdir 下的子目录，tclaude 用 Read 工具读图。
UPLOAD_DIR_NAME = os.environ.get("UPLOAD_DIR_NAME", ".console_uploads")
UPLOAD_DIR = BASE_DIR / UPLOAD_DIR_NAME
UPLOAD_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", str(100 * 1024 * 1024)))
UPLOAD_TTL_DAYS = int(os.environ.get("UPLOAD_TTL_DAYS", "7"))
UPLOAD_CLEAN_INTERVAL_HOURS = int(os.environ.get("UPLOAD_CLEAN_INTERVAL_HOURS", "6"))

# ---- Knot HTTPS API ----
# 文档：https://iwiki.woa.com/p/4016921090
# 必填：KNOT_TOKEN（在 https://knot.woa.com/settings/token 申请）
#       KNOT_AGENT_ID（智能体详情页 URL 中可见，如 https://knot.woa.com/agent/{ID}/...）
# 团队 token 场景需要额外配 KNOT_USER（企微英文名），个人 token 不用配
KNOT_API_BASE = os.environ.get("KNOT_API_BASE", "https://knot.woa.com")
KNOT_TOKEN = os.environ.get("KNOT_TOKEN", "")
KNOT_AGENT_ID = os.environ.get("KNOT_AGENT_ID", "")
KNOT_USER = os.environ.get("KNOT_USER", "")
# AG-UI 流式读超时（秒），长任务/复杂工具可能需要更大值
KNOT_READ_TIMEOUT = float(os.environ.get("KNOT_READ_TIMEOUT", "1800"))

# ---- ASR 语音识别（HY ContextASR，OpenAI 多模态 Chat Completions）----
# 录音 → 后端 /api/asr → 转 16k WAV → POST {base}/v1/chat/completions → 取识别文本。
# ASR2_BASE_URLS 逗号分隔多个端点（round-robin）；ASR2_MODEL 是 served-model-name。
ASR2_BASE_URLS = os.environ.get("ASR2_BASE_URLS", "")
ASR2_MODEL = os.environ.get("ASR2_MODEL", "HYAudio")
ASR2_API_KEY = os.environ.get("ASR2_API_KEY", "")
ASR2_TIMEOUT = float(os.environ.get("ASR2_TIMEOUT", "30"))

# ---- 企业微信「消息推送」完成通知 ----
# 回合完成后发一条 markdown 摘要卡片到群/单聊。服务器在内网直连出不去公网，
# 必须经公司代理 star-proxy 才能到 qyapi.weixin.qq.com（实测可达）。
# WECOM_WEBHOOK_KEY：群机器人 webhook url 里的 key（务必保密，别提交到代码库）。
# WECOM_PROXY：发企业微信用的 HTTP 代理（只挂在这一个请求上，不污染 tclaude 子进程）。
# WECOM_CHATID：可选，指定会话/单聊 id，不填默认发到该机器人所在的群。
WECOM_ENABLED = os.environ.get("WECOM_ENABLED", "false").lower() == "true"
WECOM_WEBHOOK_KEY = os.environ.get("WECOM_WEBHOOK_KEY", "")
WECOM_PROXY = os.environ.get("WECOM_PROXY", "http://star-proxy.oa.com:3128")
WECOM_CHATID = os.environ.get("WECOM_CHATID", "")
WECOM_TIMEOUT = float(os.environ.get("WECOM_TIMEOUT", "15"))

# Agent 默认工作目录（它在哪个目录里干活）
DEFAULT_WORKDIR = os.environ.get("AGENT_WORKDIR", str(BASE_DIR.parent))
# agent-console 仓库自身根目录。会话 workdir 传 "@self" 时解析到这里，
# 让团队开发 agent-console 时能拿到基于本仓库的 worktree 隔离。
SELF_REPO_DIR = os.environ.get("SELF_REPO_DIR", str(BASE_DIR))

# ---- Git Worktree 会话隔离 ----
# 隔离会话的独立 worktree 目录都放这个根下，分支前缀 agent/。
WORKTREES_ROOT = os.environ.get("WORKTREES_ROOT", str(BASE_DIR / "data" / "worktrees"))

# ---- 鉴权 ----
# 手机登录用的口令，务必改掉默认值
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "change-me-please")

# ---- SwanLab 代理 ----
# start_dual.py 会从进程环境或 data/.secrets/ 私有文件注入。这里不提供任何
# 可由仓库内容推导的 fallback；缺失时 SwanLab 接口 fail closed。
SWANLAB_API_KEY: str = os.getenv("SWANLAB_API_KEY", "").strip()
# iframe 无法携带 Authorization header，因此先由已鉴权 API 签发短期代理 Cookie。
# 必须是独立随机密钥，不能从 AUTH_TOKEN 或其他仓库固定值推导。
SWANLAB_SESSION_SECRET: str = os.getenv("SWANLAB_SESSION_SECRET", "").strip()
SWANLAB_SESSION_TTL: int = int(os.getenv("SWANLAB_SESSION_TTL", "3600"))

# ---- 服务 ----
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8800"))

# ---- 音频代理白名单 ----
# GET /api/audio 只允许读取这些 root 下的音频文件（安全边界）。
# 环境变量 AUDIO_ROOTS 用冒号分隔多个 root 覆盖默认值。
AUDIO_ROOTS = [
    r for r in os.environ.get("AUDIO_ROOTS", "/apdcephfs_gy2:/apdcephfs_gy8").split(":") if r
]

# ---- 存储 ----
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "console.db"))

WEB_DIR = BASE_DIR / "web"

# ---- Memory / Subagent 管理 ----
# tclaude 的家目录（memory 按 <TCLAUDE_HOME>/projects/<slug>/memory/ 存）
TCLAUDE_HOME = os.environ.get("TCLAUDE_HOME", "/root/.tclaude")
# subagent 定义目录（claude-code 标准 .claude/agents/*.md）。默认放本项目根下。
AGENTS_DIR = os.environ.get("AGENTS_DIR", str(BASE_DIR / ".claude" / "agents"))

# skill 定义目录（claude-code 标准 .claude/skills/<name>/SKILL.md）。网页对话里输入
# /<skill名> 时从这里读正文展开成 prompt（headless 模式不认原生斜杠命令）。
SKILLS_DIR = os.environ.get("SKILLS_DIR", str(BASE_DIR / ".claude" / "skills"))

# ---- 快捷指令模板（只读，前端 chip） ----
# 点一下把模板填进输入框，提升常用运维操作效率。
SNIPPETS = [
    {"id": "explain", "label": "解释代码", "text": "解释这段代码的作用和关键逻辑："},
    {"id": "status", "label": "查训练进度", "text": "帮我看一下当前训练任务的进度和最新 loss。"},
    {"id": "ckpt", "label": "列 checkpoint", "text": "列出最近的 checkpoint 文件（按时间倒序，含大小）。"},
    {"id": "gpu", "label": "查 GPU", "text": "运行 nvidia-smi，总结当前 GPU 占用情况。"},
    {"id": "disk", "label": "查磁盘", "text": "查看磁盘使用情况，找出占空间最大的几个目录。"},
    {"id": "summary", "label": "总结会话", "text": "总结一下我们这次会话做了哪些事、结论是什么。"},
]

# ---- 秘书 Agent ----
# 定时生成工作日报/早报，并经企业微信推送。时间用本地时区（服务器时间）HH:MM。
SECRETARY_ENABLED = os.environ.get("SECRETARY_ENABLED", "true").lower() == "true"
SECRETARY_EVENING_TIME = os.environ.get("SECRETARY_EVENING_TIME", "21:00")
SECRETARY_MORNING_TIME = os.environ.get("SECRETARY_MORNING_TIME", "09:00")
SECRETARY_MODEL = os.environ.get("SECRETARY_MODEL", "strong")

# ---- 备忘录每日提醒 ----
# 每天定点把开启提醒的备忘汇总推送一次（同一天只推一次）。时间用本地时区 HH:MM。
MEMO_REMIND_ENABLED = os.environ.get("MEMO_REMIND_ENABLED", "true").lower() == "true"
MEMO_REMIND_TIME = os.environ.get("MEMO_REMIND_TIME", "09:30")
