# Agent Console 实现文档

> 记录当前版本（MVP）的完整设计与实现，便于后续维护和扩展。
> 项目路径：`/apdcephfs/gy1/apdcephfs_gy2/share_302533218/zhihangxu/agent-console/`

---

## 1. 项目定位

一个跑在服务器上的轻量 Web 控制台：通过 VPN，用**手机浏览器**指挥服务器上的
**Claude Code CLI**（`claude-internal`）Agent 干活（运维、跑脚本、查状态等），
并优雅地记录 / 回看整个工作流。

核心思路：**不自研 Agent**，而是包一层 Claude Code CLI 的 headless 模式（`-p --output-format stream-json`），
本服务只负责「会话编排 + 流式转发 + 持久化 + 移动端展示」。Agent 能力天然来自 CLI 自带工具（Bash / 文件等），
未来扩展无需改动本服务核心。

---

## 2. 整体架构

```
┌─────────────┐   HTTPS/WSS    ┌──────────────────────────────────┐
│   手机浏览器  │ ◄───────────► │           服务器 (VPN 内)           │
│ (移动端 SPA) │                │                                  │
└─────────────┘                │  ┌────────────────────────────┐  │
                               │  │  FastAPI 后端                │  │
                               │  │  - REST  (会话/历史/任务)     │  │
                               │  │  - WebSocket (实时流式对话)   │  │
                               │  │  - Token 鉴权                │  │
                               │  └─────────────┬──────────────┘  │
                               │                │                 │
                               │  ┌─────────────▼──────────────┐  │
                               │  │  ClaudeRunner               │  │
                               │  │  调用 claude-internal 子进程  │  │
                               │  │  解析 stream-json 事件        │  │
                               │  └─────────────┬──────────────┘  │
                               │                │                 │
                               │  ┌─────────────▼──────────────┐  │
                               │  │  SQLite (sessions/messages  │  │
                               │  │         /tasks)             │  │
                               │  └────────────────────────────┘  │
                               └──────────────────────────────────┘
```

---

## 3. 目录结构

```
agent-console/
├── server/
│   ├── __init__.py
│   ├── main.py           # FastAPI：REST + WebSocket 编排
│   ├── claude_runner.py  # 封装 Claude Code CLI（stream-json 解析）
│   ├── db.py             # SQLite 持久化
│   └── config.py         # 配置（全部走环境变量）
├── web/
│   ├── index.html        # 移动端单页
│   ├── style.css         # 移动优先、深色、规范排版
│   └── app.js            # 流式渲染 + 断线重连 + 会话/任务管理
├── data/
│   └── console.db        # SQLite 数据文件（运行后自动生成）
├── requirements.txt
├── run.sh                # 启动脚本
├── README.md
└── IMPLEMENTATION.md     # 本文档
```

---

## 4. 技术选型

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | FastAPI + Uvicorn | 异步、原生 WebSocket、Python 生态 |
| 实时通信 | WebSocket | Agent 输出逐事件推送 |
| 前端 | 原生 HTML/CSS/JS 单页 | 移动优先，无需构建、无需装 App |
| 存储 | SQLite | 零运维、单文件 |
| Agent | Claude Code CLI (`claude-internal`) headless | 自带 Bash/文件工具 |
| 访问 | VPN 内网 IP 直连 | 无需公网穿透 |

依赖（`requirements.txt`）：
```
fastapi>=0.110
uvicorn[standard]>=0.27
```

---

## 5. 配置项（`server/config.py`，全部可用环境变量覆盖）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CLAUDE_BIN` | `/root/.nvm/versions/node/v20.20.2/bin/claude-internal` | Claude Code CLI 路径 |
| `CLAUDE_SKIP_PERMISSIONS` | `true` | 是否加 `--dangerously-skip-permissions`（运维需 true 才能执行命令） |
| `CLAUDE_TURN_TIMEOUT` | `1800` | 单回合超时（秒），防卡死 |
| `AGENT_WORKDIR` | 项目上级目录 | Agent 默认工作目录 |
| `AUTH_TOKEN` | `change-me-please` | 手机登录口令（**务必修改**） |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8800` | 监听端口 |
| `DB_PATH` | `server/../data/console.db` | SQLite 路径 |
| `CODEBUDDY_API_KEY` | （可选） | CLI 认证；若已通过工蜂 OAuth 登录则无需 |

### 认证说明
`claude-internal` 支持两种认证：
1. **工蜂 OAuth**（交互式登录，凭证落盘长期有效）—— 当前采用，`-p` 模式直接复用。
2. **`CODEBUDDY_API_KEY`**（API Key 模式，仅支持 `-p/--print`）—— 备选，无人值守场景用。

子进程调用 CLI 时会**继承服务进程的环境变量**，所以只要服务进程能认证，Agent 就能认证。

---

## 6. 核心实现

### 6.1 ClaudeRunner（`server/claude_runner.py`）

每个回合执行的命令：
```bash
tclaude -- -p "<用户消息>" --output-format stream-json --verbose \
    [--model <档位模型>] [--resume <上次的claude会话id>]
```

> `tclaude` 是腾讯内部对 claude-code 的 wrapper，必须用 `--` 把参数原样透传给底层；
> root 下默认权限模式即可执行工具，不加 `--dangerously-skip-permissions`（该 flag 在 root 下被拒绝）。

关键点：
- 用 `asyncio.create_subprocess_exec` 起子进程，`limit=16MB` 放大缓冲（命令输出可能很大）。
- 逐行 `readline` 解析 stream-json，每行是一个 JSON 事件，回调 `on_event` 给上层。
- 从事件里捕获 `session_id`，作为下回合 `--resume` 的依据（维持多轮上下文）。
- `asyncio.wait_for` 做超时保护；超时 / 异常时 `terminate` 子进程。
- 找不到 CLI（`FileNotFoundError`）时返回友好错误。
- 维护 `session_id -> proc` 映射，支持 `cancel()` 中途停止、`is_running()` 防并发。

### 6.2 stream-json 事件类型

| `type` | 含义 | 翻译成前端的 role |
|---|---|---|
| `system` / `init` | 初始化（含 session_id、工具列表、模型） | `system`（前端不展示） |
| `assistant` | 助手消息，`content` 是 block 列表 | `text`→`assistant`；`tool_use`→`tool_use` |
| `user` | 工具结果回填（tool_result） | `tool_result` |
| `result` | 本回合结束（耗时/花费/轮数） | `result` |

### 6.3 后端编排（`server/main.py`）

REST 接口（均需 `Authorization: Bearer <token>`）：
- `POST /api/login` —— 校验口令
- `GET  /api/sessions` —— 会话列表
- `POST /api/sessions` —— 新建会话
- `DELETE /api/sessions/{sid}` —— 删除会话
- `GET  /api/sessions/{sid}/messages` —— 会话历史
- `GET  /api/tasks` —— 最近任务（看板）

WebSocket `/ws?token=&session_id=`：
- 客户端发 `{"type":"user_message","content":"..."}` 或 `{"type":"cancel"}`
- 服务端推：
  - `{"type":"status","status":"running|idle",...}`
  - `{"type":"message","role":"assistant|tool_use|tool_result|result|error","content":{...}}`
  - `{"type":"error","message":"..."}`

处理流程：
1. 收到用户消息 → 落库 + 起 task + 推 `status:running`
2. 调 `runner.run_turn`，每个事件 `_translate_event` → 落库 + WebSocket 推送
3. 回合结束 → 更新 `claude_session_id`、finish task、推 `status:idle`

### 6.4 数据模型（`server/db.py`，SQLite）

```
sessions  (id, title, claude_session_id, workdir, status, created_at, updated_at)
messages  (id, session_id, role, content[JSON], created_at)
tasks     (id, session_id, summary, status, duration_ms, cost_usd, num_turns, started_at, ended_at)
```
- 用线程锁 + `check_same_thread=False` 保证并发安全（操作都很快）。
- `messages.content` 存 JSON 字符串，role 区分消息种类。

### 6.5 前端（`web/`）

- **登录页**：口令存 localStorage，刷新自动免登。
- **侧边抽屉**：会话列表（切换/新建/删除）+ 最近任务看板（状态色条/耗时/花费）。
- **聊天区**：
  - 用户/助手气泡；工具调用与结果用 `<details>` 折叠展示（summary 显示命令摘要，展开看完整内容）。
  - 回合汇总行（耗时/花费/轮数）。
  - "正在输入"动画。
- **WebSocket**：断线**自动 2s 重连**（手机网络不稳）；连接状态用顶栏圆点指示（绿/红）。
- **输入区**：自适应高度；移动端回车换行、点按钮发送；运行中显示"停止"按钮可中断。
- 移动端适配：`viewport-fit=cover`、`safe-area-inset`、`100dvh`、深色主题。

---

## 7. 启动与访问

### 启动命令
```bash
cd /apdcephfs/gy1/apdcephfs_gy2/share_302533218/zhihangxu/agent-console
pip install -r requirements.txt          # 仅首次
export AUTH_TOKEN=你的登录口令
bash run.sh
```
后台常驻：
```bash
nohup bash run.sh > console.log 2>&1 &
tail -f console.log
```

### 手机访问
```bash
hostname -I        # 取 VPN 网段内网 IP（10.x / 172.x / 192.168.x）
```
手机连 VPN → 浏览器 `http://<内网IP>:8800` → 输入口令登录。

### 本机自测
```bash
curl -s http://127.0.0.1:8800/ | head -3   # 返回 <!DOCTYPE html> 即服务正常
```

---

## 8. 安全说明

- 默认开启 `--dangerously-skip-permissions`，Agent 才能真正执行 Bash / 文件操作。
  服务挂在 **VPN 内网 + Token 鉴权** 之后，风险可控。
- 务必修改默认 `AUTH_TOKEN`，不要把端口直接暴露公网。
- 如需更严格的危险操作二次确认，可接 Claude Code 的 `--permission-prompt-tool`（MCP 权限网关）——已预留扩展位。

---

## 9. 已知状态 & 后续可扩展

当前为可用 MVP，已验证 `claude-internal -p` 在 OAuth 凭证下正常工作。

待扩展（已预留思路）：
- **快捷指令按钮**：手机一键触发高频运维操作（查训练进度 / 列 checkpoint / 跑 avg_hf 等）。
- **更顺滑流式**：开启 CLI 的 `--include-partial-messages` 做逐字打字机效果（需前端累积 delta）。
- **长任务完成推送**：微信 / Server酱 / 邮件提醒。
- **文件 / 产物预览**：日志片段、loss 曲线图。
- **语音输入**：浏览器语音转文字。
- **危险操作二次确认**：权限网关。
- **Agent 团队协作**：`.claude/agents/` 目录已定义 analyst / developer / reviewer / ops 四个专职 Agent，
  形成"需求分析 → 实现 → 审查 → 部署验证"完整闭环。在对话中用 `#analyst`、`#ops` 等前缀呼出对应 Agent。
