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
# 放行的工具列表（传给 --allowedTools，空格分隔）：只放行只读/安全工具，免权限提示直接跑；
# 写类工具（Bash / Write / Edit / MultiEdit）不放行，遇到就触发 control_request，
# 经 WS 推前端弹窗让用户逐次确认（见 CLAUDE_PERMISSION_PROMPT）。
# 想精细控制可改成如 "Bash(git *) Read Write"；设为空字符串则不加该参数（回到默认拒绝）。
CLAUDE_ALLOWED_TOOLS = os.environ.get(
    "CLAUDE_ALLOWED_TOOLS",
    "Read Glob Grep WebFetch WebSearch Task TodoWrite NotebookEdit",
)
# 是否开启权限请求弹窗：未放行的工具触发 control_request 时，经 WS 推前端弹窗让用户确认，
# 用户点允许/拒绝后回 control_response 给 CLI。仅在常驻模式（有 stdin 可回写）下生效。
CLAUDE_PERMISSION_PROMPT = os.getenv("CLAUDE_PERMISSION_PROMPT", "true").lower() == "true"
# 单次 Agent 回合的超时（秒），防止卡死。
# 注意：常驻模式已改为"空闲超时 + 循环检测 + 安全上限"的看门狗机制（见下方几个变量），
# 这个值仅老模式（run_turn）仍作硬超时用，保留向后兼容。
CLAUDE_TURN_TIMEOUT = int(os.environ.get("CLAUDE_TURN_TIMEOUT", "1800"))
# 看门狗机制（常驻模式 send_turn）：只要 agent 还在推进（有新事件）就不杀，
# 只有真正卡死（长时间无事件）、在循环、或超过绝对安全上限才终止。
CLAUDE_IDLE_TIMEOUT = int(os.environ.get("CLAUDE_IDLE_TIMEOUT", "600"))   # 600s无事件 → 判定卡死
CLAUDE_TURN_MAX = int(os.environ.get("CLAUDE_TURN_MAX", "7200"))           # 2h绝对上限
CLAUDE_LOOP_REPEAT = int(os.environ.get("CLAUDE_LOOP_REPEAT", "8"))        # 相同工具调用连续N次 → 循环
CLAUDE_LOOP_ERRORS = int(os.environ.get("CLAUDE_LOOP_ERRORS", "10"))       # 连续报错N次 → 循环
CLAUDE_WATCHDOG_INTERVAL = int(os.environ.get("CLAUDE_WATCHDOG_INTERVAL", "15"))  # 看门狗检查间隔
# 是否加 --include-partial-messages：开启后 CLI 逐字推送文本增量，前端做打字机效果。
# 关掉则回到整段输出（向后兼容）。
CLAUDE_STREAM_PARTIAL = os.environ.get("CLAUDE_STREAM_PARTIAL", "true").lower() == "true"

# 模型档位 → tclaude 模型 ID。空字符串 = 不传 model 让 CLI 用默认。
# 合法值见 `tclaude -- --model bogus` 的报错列表：
#   claude-sonnet-4-6 / claude-sonnet-4-6[1m] / claude-opus-4-8[1m] /
#   claude-opus-4-7[1m] / claude-opus-4-6[1m] / claude-haiku-4-5 / claude-hy3-preview
CLAUDE_MODEL_FAST = os.environ.get("CLAUDE_MODEL_FAST", "claude-haiku-4-5")
# 看板进展摘要专用模型：用 hy preview（tclaude 提供的预览档模型），概括质量更好。
CLAUDE_MODEL_KANBAN = os.environ.get("CLAUDE_MODEL_KANBAN", "claude-hy3-preview")
CLAUDE_MODEL_STRONG = os.environ.get("CLAUDE_MODEL_STRONG", "claude-sonnet-4-6")
CLAUDE_MODEL_SUPER = os.environ.get("CLAUDE_MODEL_SUPER", "claude-opus-4-8[1m]")
# 新会话默认档位：fast / strong / super。默认 strong（sonnet）——速度与智能平衡，要最强顶栏切 super。
CLAUDE_DEFAULT_MODE = os.environ.get("CLAUDE_DEFAULT_MODE", "strong")
# 思考深度（low/medium/high/xhigh/max）。medium 平衡速度与质量；要更深手动调。
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
# 常驻进程模式：每会话维持一个长生命周期 tclaude 进程（--input-format stream-json），
# 更接近交互式，上下文常驻进程内。false=老的每回合新进程+resume 模式（回退用）。
CLAUDE_PERSISTENT = os.environ.get("CLAUDE_PERSISTENT", "true").lower() == "true"
# 常驻进程空闲多久无消息就回收（秒），下次消息自动重起 + resume 续上下文。
# 设长一点（1小时）减少冷启动重复发生——冷启动是 tclaude 加载工具/认证的固有开销。
CLAUDE_SESSION_IDLE_SEC = int(os.environ.get("CLAUDE_SESSION_IDLE_SEC", "3600"))

# ---- 会话行摘要（列表里"刚做了什么"一句话）----
# 用一次性 Haiku 概括（独立子进程，不碰会话上下文）；失败回退启发式截断。
SUMMARY_ENABLED = os.environ.get("SUMMARY_ENABLED", "true").lower() == "true"
SUMMARY_TIMEOUT = float(os.environ.get("SUMMARY_TIMEOUT", "60"))

# ---- 会话标题异步刷新 ----
# 多轮对话后持续用 AI 重起标题，越来越准地反映整个对话。TITLE_EARLY_TURNS 前每回合刷，
# 之后每 TITLE_EVERY_N 回合刷一次；用户手动改名（title_auto=0）后不再自动覆盖。
TITLE_REFRESH_ENABLED = os.environ.get("TITLE_REFRESH_ENABLED", "true").lower() == "true"
TITLE_EARLY_TURNS = int(os.environ.get("TITLE_EARLY_TURNS", "3"))
TITLE_EVERY_N = int(os.environ.get("TITLE_EVERY_N", "5"))

# ---- 图片上传（手机拍照/截图发给 Agent）----
# 存到会话 workdir 下的子目录，tclaude 用 Read 工具读图。
UPLOAD_DIR_NAME = os.environ.get("UPLOAD_DIR_NAME", ".console_uploads")
UPLOAD_DIR = BASE_DIR / UPLOAD_DIR_NAME
UPLOAD_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))
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

# ---- 鉴权 ----
# 手机登录用的口令，务必改掉默认值
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "change-me-please")

# ---- 服务 ----
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8800"))

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
